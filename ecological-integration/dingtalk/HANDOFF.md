# DingTalk RDS AI Bot Handoff

## Current Branch

- Branch: `feature/dingtalk-json-session-fallback`
- Scope: DingTalk ecological integration sample under `ecological-integration/dingtalk`.
- Goal: make DingTalk conversation development easier by adding persistent Copilot sessions, optional card replies, visible error fallbacks, DingTalk reactions, and safer SSE parsing.

## What Changed

### Persistent Copilot Sessions

`JsonCopilotConversationStore` persists RDS Copilot `ConversationId` in a local JSON file.

- Key scope: DingTalk conversation ID + sender ID.
- Default: session retention is enabled, matching the previous in-memory behavior.
- Store path:
  - `RDS_COPILOT_CONVERSATION_STORE_FILE`, if set.
  - Otherwise `copilot_conversations.json` in the process working directory.

Supported commands:

| Command | Behavior |
| --- | --- |
| `/session on` | Enable ConversationId retention for the current DingTalk conversation + sender. |
| `/session off` | Disable retention and clear the saved ConversationId. |
| `/new` | Clear only the saved ConversationId and start a fresh Copilot conversation on the next normal message. |

Command matching is exact after trimming whitespace, so normal questions containing these strings are not intercepted.

### Card Mode Toggle

The bot now supports switching reply mode per DingTalk conversation + sender.

| Command | Behavior |
| --- | --- |
| `/card on` | Use the existing AI Card reply path. This is the default. |
| `/card off` | Do not create an AI Card. Consume the Copilot stream and send the final answer as a normal Markdown message through DingTalk `sessionWebhook`. |

`card_enabled` is persisted in the same JSON item as the session state. Turning card mode on or off does not change the saved Copilot `ConversationId`.

When card mode is off:

- `handle_reply_plain_message()` calls Copilot normally.
- It sends the final visible content through `send_dingtalk_session_webhook()`.
- If `sessionWebhook` is missing or fails, it falls back to `reply_text()`.
- If Copilot errors or returns no final text, the user still receives an English fallback message.

### DingTalk Reactions

The bot adds a lightweight reaction state around each normal Copilot reply:

- Start: add `🤔Thinking`.
- Finish: recall `🤔Thinking`, then add `🥳Done`.

The implementation uses `alibabacloud-dingtalk` robot APIs:

- `robot_reply_emotion_with_options_async`
- `robot_recall_emotion_with_options_async`

There is no direct “switch reaction” API in the SDK, so switching is implemented as recall + add. Event-level reaction switching was intentionally not added here because it can cause visible chat jitter.

Reaction failures only log warnings and never block the main reply path.

### SSE Robustness

`rds_copilot.py` still requests `EventMode=separate`, but a malformed single SSE event no longer aborts the whole stream.

Behavior now:

- Log each raw SSE event preview as `rds_sse_raw_event`.
- If `json.loads(response.event.data)` fails for one event:
  - Log `rds_sse_malformed_event`.
  - Include raw length, head, and tail previews.
  - Continue consuming later SSE events.
- If later `message` events arrive, the user can still receive the final response.
- Non-JSON parser errors are still surfaced to the caller and handled by the card/plain fallback path.

The Copilot endpoint is configurable:

```bash
export RDS_COPILOT_ENDPOINT="rdsai.aliyuncs.com"      # production, default
export RDS_COPILOT_ENDPOINT="rdsai-pre.aliyuncs.com"  # pre-production test
```

## Files To Know

| File | Notes |
| --- | --- |
| `main.py` | DingTalk message routing, command handling, card/plain reply paths, session JSON store, reaction helper, fallback handling. |
| `rds_copilot.py` | RDS AI Copilot OpenAPI client, `EventMode=separate`, SSE parsing and malformed-event tolerance. |
| `tests/test_session_and_fallback.py` | Regression tests for session persistence, card mode, plain reply fallback, reactions, and malformed SSE events. |
| `README.md` | User-facing setup and command documentation. |
| `requirements.txt` | Adds `alibabacloud-dingtalk>=2.0.0` for reaction APIs. |

## Runtime Setup

Install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Required environment variables:

```bash
export ACCESS_KEY_ID="your_aliyun_access_key_id"
export ACCESS_SECRET="your_aliyun_access_key_secret"
export DINGTALK_APP_CLIENT_ID="your_dingtalk_client_id"
export DINGTALK_APP_CLIENT_SECRET="your_dingtalk_client_secret"
```

Optional environment variables:

```bash
export RDS_COPILOT_CONVERSATION_STORE_FILE="/path/to/copilot_conversations.json"
export RDS_COPILOT_ENDPOINT="rdsai-pre.aliyuncs.com"
export DINGTALK_ROBOT_CODE="your_robot_code"
```

Start:

```bash
python main.py
```

## Verification

Run the current regression suite from `ecological-integration/dingtalk`:

```bash
venv/bin/python -m unittest discover tests
venv/bin/python -m py_compile main.py rds_copilot.py tests/test_session_and_fallback.py
git diff --check
```

The tests intentionally exercise exception paths, so stack traces for mocked `ConnectionError` cases can appear in the output while the suite still exits successfully.

## Suggested Next Development Areas

1. Improve stream delivery in plain mode.
   Current plain mode sends the final content once. If product experience needs incremental plain-message updates, design around DingTalk ordinary-message update limits first.

2. Align tracing through the full call chain.
   `rds_copilot.chat()` accepts `trace_id`; passing a DingTalk message ID from `call_with_stream()` would make production log correlation cleaner.

3. Consider moving `call_with_stream()` to `asyncio.Queue`.
   The current target project still follows the original `Queue` + executor pattern. The debug project used an `asyncio.Queue` to avoid blocking worker threads during high concurrency.

4. Confirm globalized copy.
   User-visible fallback and command replies added in this change are English. README remains Chinese because this integration directory already uses Chinese documentation.

5. Validate DingTalk reaction permissions in the target tenant.
   If reactions do not appear, check `DINGTALK_ROBOT_CODE`, app permissions, and whether `dingtalk_client.get_access_token()` returns a valid token in this runtime.
