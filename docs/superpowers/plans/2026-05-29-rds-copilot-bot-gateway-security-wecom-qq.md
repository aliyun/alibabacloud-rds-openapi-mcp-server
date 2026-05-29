# Ecological Bridge Security WeCom QQ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add env-driven authorization plus WeCom AI Bot and QQ Bot long-connection bridges.

**Architecture:** `bot_core.py` defines normalized `SessionSource` and shared authorization helpers. `main.py`, `feishu_bridge.py`, `wecom_bridge.py`, and `qq_bridge.py` turn platform payloads into `SessionSource`, drop unauthorized events, and reuse the existing RDS Copilot command/streaming flow.

**Tech Stack:** Python 3.12, unittest, aiohttp/httpx optional runtime dependencies, existing RDS Copilot OpenAPI client.

---

### Task 1: Shared Security Model

**Files:**
- Modify: `rds-copilot-bot-gateway/core/bot_core.py`
- Test: `rds-copilot-bot-gateway/tests/test_bridge_and_api_coverage.py`

- [ ] Write failing tests for `SessionSource`, allow-all, allowlist, alternate IDs, global allow-all, and deny-by-default.
- [ ] Implement `SessionSource`, `authorize_session_source()`, env parsing, and per-platform pre-filter helpers.
- [ ] Run the focused tests and confirm they pass.

### Task 2: DingTalk and Feishu Authorization Gates

**Files:**
- Modify: `rds-copilot-bot-gateway/main.py`
- Modify: `rds-copilot-bot-gateway/bridges/feishu.py`
- Test: `rds-copilot-bot-gateway/tests/test_session_and_fallback.py`
- Test: `rds-copilot-bot-gateway/tests/test_bridge_and_api_coverage.py`

- [ ] Write failing tests proving unauthorized DingTalk/Feishu messages are acknowledged or ignored without calling RDS Copilot.
- [ ] Build `SessionSource` from each platform payload and call the shared auth helpers before commands or chat.
- [ ] Run focused DingTalk/Feishu tests and confirm they pass.

### Task 3: WeCom AI Bot Bridge

**Files:**
- Create: `rds-copilot-bot-gateway/bridges/wecom.py`
- Modify: `rds-copilot-bot-gateway/main.py`
- Modify: `rds-copilot-bot-gateway/requirements.txt`
- Test: `rds-copilot-bot-gateway/tests/test_bridge_and_api_coverage.py`

- [ ] Write failing tests for WeCom text extraction, source normalization, deny/allow auth, reply frame construction, and bridge selection.
- [ ] Implement WebSocket subscribe/listen helpers and text reply using `aibot_respond_msg`/`aibot_send_msg`.
- [ ] Reuse `handle_control_command()` and `call_with_stream()` for text messages.
- [ ] Run focused WeCom tests and confirm they pass.

### Task 4: QQ Bot Bridge

**Files:**
- Create: `rds-copilot-bot-gateway/bridges/qq.py`
- Modify: `rds-copilot-bot-gateway/main.py`
- Modify: `rds-copilot-bot-gateway/requirements.txt`
- Test: `rds-copilot-bot-gateway/tests/test_bridge_and_api_coverage.py`

- [ ] Write failing tests for C2C/group/guild payload normalization, deny/allow auth, token/gateway calls, REST send routing, and bridge selection.
- [ ] Implement token cache, gateway connection, dispatch parsing, and reply senders.
- [ ] Reuse `handle_control_command()` and `call_with_stream()` for text messages.
- [ ] Run focused QQ tests and confirm they pass.

### Task 5: Documentation and Verification

**Files:**
- Modify: `rds-copilot-bot-gateway/README.md`

- [ ] Document four-platform env variables, security defaults, and setup links.
- [ ] Run `python3 -m coverage run --source=. -m unittest discover tests`.
- [ ] Run `python3 -m coverage report -m` and keep total coverage at 99% or higher.
