# RDS Copilot Bridge Commands Design

## Goal

Implement a complete long-connection bot integration layer for DingTalk and Feishu/Lark that shares one RDS Copilot core, one command surface, one session store, and one active-conversation runtime.

## Architecture

`rds-copilot-bot-gateway/core/rds_copilot.py` is the RDSAI OpenAPI layer. It knows how to call `ChatMessages`, `ChatMessagesTaskStop`, `GetConversations`, `ListCustomAgent`, and `ListSkill`, but it does not know about DingTalk or Feishu.

`rds-copilot-bot-gateway/core/bot_core.py` is the shared bot business layer. It owns command parsing, session state, active streaming state, formatting, stop and snapshot behavior, and RDSAI command handlers.

`rds-copilot-bot-gateway/main.py` and `rds-copilot-bot-gateway/bridges/feishu.py` are bridge layers. They receive platform messages, call `bot_core`, and send platform replies. DingTalk additionally supports AI Card mode; Feishu returns a not-supported response for `/card`.

## Command Boundary

Only these exact short-command families are intercepted:

- `/help`
- `/btw`
- `/stop`
- `/card on`
- `/card off`
- `/card status`
- `/session on`
- `/session off`
- `/session status`
- `/session ls`
- `/session <short-id-or-full-id>`
- `/new`
- `/agent ls`
- `/agent <agent-name>`
- `/agent default`
- `/language`
- `/language <zh-CN|zh-TW|en-US|ja-JP>`
- `/tz`
- `/tz <IANA timezone>`
- `/skills`
- `/skills <page>`

Anything else is sent to RDS Copilot unchanged. Examples such as `/sql-review xxx`, `/$skillname xxx`, `/unknown xxx`, and malformed command-looking text are normal user queries.

## RDS OpenAPI Methods

`RdsCopilot` exposes:

- `chat(query, conversation_id="", custom_agent_id="", language="zh-CN", timezone="Asia/Shanghai")`
- `stop_task(task_id)`
- `list_conversations(last_id="", limit=10, pinned="", sort_by="CreatedAt")`
- `list_custom_agents(page_number=1, page_size=20)`
- `list_skills(page_number=1, page_size=20, language="zh-CN")`

`chat()` uses `EventMode=separate`, passes `CustomAgentId` only when a custom agent is selected, and passes `language` and `timezone` through `ChatMessages` `Inputs`.

## Persistent State

`CopilotConversationStore` stores only durable per-user settings, keyed by `platform + chat/conversation id + sender id`:

```json
{
  "copilot_conversation_id": "...",
  "session_enabled": true,
  "card_enabled": true,
  "custom_agent_id": "...",
  "custom_agent_name": "...",
  "language": "zh-CN",
  "timezone": "Asia/Shanghai",
  "updated_at": 1234567890
}
```

Recent conversation and agent lists are not persisted. They live in an in-memory cache used only for short ID or name resolution in the current process.

The store protects all read-modify-write operations with a `threading.RLock`. Writes use temp-file plus `os.replace` so a partial write does not corrupt `copilot_conversations.json`.

## Session Commands

`/session on` enables context retention. `/session off` disables context retention and clears the current RDSAI `ConversationId`.

`/session status` returns the current context mode and the active `ConversationId` when context is on:

```text
Session: on
ConversationId: 60b335ca-124d-4ee1-864b-de554987xxxx
```

When off:

```text
Session: off
ConversationId: none
```

`/session ls` calls `GetConversations` and shows the latest 10 conversations with short IDs. It caches the returned list in memory.

`/session <id>` accepts a full ID or an 8-character short ID from the latest in-memory `/session ls` cache. It is treated as a command only when the token matches a short/full conversation ID shape; otherwise the text is sent to RDS Copilot. If a short ID is not found, the response asks the user to run `/session ls` or provide a full ID.

`/new` clears only `copilot_conversation_id`. It preserves session, card, and selected custom agent settings.

## Agent Commands

`/agent ls` calls `ListCustomAgent` and shows agent name, short ID, tool state, and tool count. It caches the returned list in memory.

`/agent <agent-name>` resolves the exact agent name from the latest in-memory `/agent ls` cache and persists `custom_agent_id` and `custom_agent_name`.

`/agent default` clears the selected custom agent.

Normal chat requests pass both `ConversationId` and `CustomAgentId` to `ChatMessages` when available.

## Language And Timezone Commands

`/language` returns the supported values: `zh-CN`, `zh-TW`, `en-US`, and `ja-JP`.

`/language <value>` normalizes simple casing and `_` separator differences, validates the selected language against `zh-CN`, `zh-TW`, `en-US`, and `ja-JP`, then persists it in `copilot_conversations.json`.

`/tz` returns the current timezone.

`/tz <IANA timezone>` validates the timezone with the local IANA database and persists it. Invalid values return local fuzzy-match suggestions when possible, such as suggesting `Asia/Shanghai` for `asia/shanghai`.

Normal chat requests pass both values to `ChatMessages`:

```json
{
  "inputs": {
    "language": "zh-CN",
    "timezone": "Asia/Shanghai"
  }
}
```

## Skills Command

`/skills` calls `ListSkill(PageNumber=1, PageSize=20, Language=<current language>)`.

`/skills 2` calls page 2. Language is controlled only by `/language`; `/skills en-US` is not a local command and is sent to RDS Copilot unchanged.

The output ends with:

```text
输入 /$skillname 使用技能，例如：/sql-review
```

Skill invocation is not parsed locally. `/$skillname ...` is sent to RDS Copilot unchanged.

## Card Command

DingTalk supports `/card on`, `/card off`, and `/card status`. Card mode defaults to on. When card mode is on, DingTalk streams into AI Card. When off, DingTalk sends plain text.

Feishu does not support card mode. Its `/card ...` response says the current platform does not support card replies.

## Active Conversation Runtime

`ActiveConversationRegistry` tracks one in-flight task per `platform + chat id + sender id`:

- `platform`
- `chat_id`
- `sender_id`
- `task_id`
- `message_buffer`
- `event_count`
- `phase`
- `started_at`
- `cancel_requested`
- `done`

The registry and each state use locks. Streaming updates, `/btw`, and `/stop` can safely run concurrently.

`/btw` returns the current `message_buffer`. If no visible message has arrived, it reports that no displayable content has been received yet.

`/stop` marks the task cancelled and calls `ChatMessagesTaskStop` when a `task_id` is known. If the task ID arrives later, the streaming loop stops it then.

If a normal user query arrives while the same platform/chat/sender already has an active run, the bridge does not start another RDSAI stream. It replies that a task is running and suggests `/btw` or `/stop`.

Long-running notifications are sent every `RDS_BOT_STILL_WORKING_INTERVAL_SECONDS`, default 180 seconds, using exactly:

```text
Still working... (3 min elapsed — events 10, receiving stream response)
```

## Help Command

`/help` returns a short command reference and does not enter RDS Copilot.

## Testing

Tests cover:

- exact command parsing and unknown slash passthrough
- lock-safe persistent store updates
- `/session status`, `/session ls`, `/session <id>`
- `/agent ls`, `/agent <name>`, `/agent default`
- `/language`, `/tz`, persistence, validation, and ChatMessages input propagation
- `/skills` formatting and final usage hint
- `/card` behavior on DingTalk and Feishu
- `/btw` snapshot behavior
- `/stop` and `ChatMessagesTaskStop`
- busy same-session protection
- long-running notification text
- `CustomAgentId`, language, and timezone propagation into `ChatMessages`
