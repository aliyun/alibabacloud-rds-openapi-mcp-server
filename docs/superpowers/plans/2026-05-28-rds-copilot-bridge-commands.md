# RDS Copilot Bridge Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete shared command, session, active-run, and RDSAI API capability for DingTalk and Feishu long-connection bridges.

**Architecture:** `rds_copilot.py` remains the OpenAPI client, `bot_core.py` owns shared bot behavior, and platform bridge files only adapt messages and delivery. The implementation uses TDD with focused unit tests in `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`.

**Tech Stack:** Python 3.12, unittest, asyncio, loguru, Alibaba Cloud Tea OpenAPI SDK, DingTalk stream SDK, lark-oapi.

---

### Task 1: RDSAI OpenAPI Surface

**Files:**
- Modify: `rds-copilot-bot-gateway/core/rds_copilot.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests proving `chat()` sends `CustomAgentId`, `list_conversations()` builds `GetConversations`, `list_custom_agents()` builds `ListCustomAgent`, and `list_skills()` builds `ListSkill`.
- [ ] Run `python3 -m unittest rds-copilot-bot-gateway.tests.test_session_and_fallback.RdsCopilotSseParsingTest -v` and verify the new tests fail because the methods or parameters are missing.
- [ ] Implement `Params` subclasses and methods in `rds_copilot.py`.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: Lock-Safe Store and Command Parsing

**Files:**
- Modify: `rds-copilot-bot-gateway/core/bot_core.py`
- Modify: `rds-copilot-bot-gateway/main.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests for lock-safe store behavior, `session status`, custom agent persistence, card persistence, and command parsing that lets `/sql-review xxx` pass through as user text.
- [ ] Run focused store and parser tests and verify failures.
- [ ] Add `threading.RLock` to `CopilotConversationStore`; add `get_card_enabled`, `set_card_enabled`, `get_agent`, `set_agent`, `clear_agent`, `set_conversation_id`, and `clear_conversation_id`.
- [ ] Move card command parsing into `bot_core.py` so DingTalk and Feishu can share it.
- [ ] Re-run focused tests and verify they pass.

### Task 3: Runtime Registry and Streaming Control

**Files:**
- Modify: `rds-copilot-bot-gateway/core/bot_core.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests for active state snapshot, event counting, task ID capture, cancellation, busy same-session protection, and long-running status formatting.
- [ ] Run the focused runtime tests and verify failures.
- [ ] Implement `ActiveConversationState`, `ActiveConversationRegistry`, cancellation helpers, and update `call_with_stream()` to update active state and pass `custom_agent_id`.
- [ ] Re-run focused runtime tests and verify they pass.

### Task 4: Shared Command Handlers

**Files:**
- Modify: `rds-copilot-bot-gateway/core/bot_core.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests for `/help`, `/btw`, `/stop`, `/session ls`, `/session <id>`, `/agent ls`, `/agent <name>`, `/agent default`, `/language`, `/tz`, and `/skills`.
- [ ] Run focused command handler tests and verify failures.
- [ ] Implement `BotContext`, `RuntimeCache`, `handle_control_command()`, and formatting helpers for conversations, agents, skills, and help.
- [ ] Re-run focused command handler tests and verify they pass.

### Task 5: DingTalk Bridge Integration

**Files:**
- Modify: `rds-copilot-bot-gateway/main.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests proving DingTalk uses shared command handlers, `/card status` works, normal text uses selected `CustomAgentId`, and busy sessions return a control hint.
- [ ] Run DingTalk-focused tests and verify failures.
- [ ] Update `CardBotHandler.process()`, `handle_reply_plain_message()`, and `handle_reply_and_update_card()` to use `bot_core` state and command handling.
- [ ] Re-run DingTalk-focused tests and verify they pass.

### Task 6: Feishu Bridge Integration

**Files:**
- Modify: `rds-copilot-bot-gateway/bridges/feishu.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`

- [ ] Add failing tests proving Feishu supports shared commands, `/card` returns unsupported, normal text uses selected `CustomAgentId`, and busy sessions return a control hint.
- [ ] Run Feishu-focused tests and verify failures.
- [ ] Update `FeishuBridge.handle_text_message()` to call shared command handlers and shared runtime.
- [ ] Re-run Feishu-focused tests and verify they pass.

### Task 7: Full Verification

**Files:**
- All changed implementation and test files

- [ ] Run `cd rds-copilot-bot-gateway && python3 -m unittest discover tests`.
- [ ] Run `cd rds-copilot-bot-gateway && python3 -m py_compile main.py bot_core.py feishu_bridge.py rds_copilot.py tests/test_session_and_fallback.py`.
- [ ] Run `git diff --check`.
- [ ] Review `git diff --stat` and verify only intended files changed.
