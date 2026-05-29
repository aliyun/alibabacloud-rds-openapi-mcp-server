# Ecological Bridge Security, WeCom, and QQ Design

## Goal

Extend `rds-copilot-bot-gateway` from DingTalk and Feishu to four long-connection platforms: DingTalk, Feishu, WeCom AI Bot, and QQ Bot, with one env-driven security model before requests reach RDS Copilot.

## Architecture

Adapters normalize inbound messages into `SessionSource` with `platform`, `chat_id`, `chat_type`, `user_id`, `user_name`, `thread_id`, and `user_id_alt`. The shared authorization layer checks platform allow-all, platform allowlists, global allow-all, then denies by default. Pairing is intentionally not implemented.

Bridge modules keep platform protocol details isolated. `bot_core.py` owns command parsing, session persistence, RDS Copilot streaming, and authorization primitives. Platform bridges construct `BotContext` from `SessionSource`, call `handle_control_command()`, then call `call_with_stream()` for ordinary text.

## Authorization

Authorization order:

1. Platform allow-all env, such as `DINGTALK_ALLOW_ALL_USERS=true`.
2. Platform allowlist env, such as `FEISHU_ALLOWED_USERS=ou_x,union_y`.
3. Global allow-all env: `GATEWAY_ALLOW_ALL_USERS=true`.
4. Default deny.

Global `GATEWAY_ALLOWED_USERS` is supported as a practical env-only equivalent of a shared allowlist. Both `user_id` and `user_id_alt` are compared against allowlists.

Platform pre-filters:

- DingTalk may restrict chats with `DINGTALK_ALLOWED_CHATS` and may require mention/wake words in group traffic with `DINGTALK_REQUIRE_MENTION` and `DINGTALK_MENTION_PATTERNS`.
- Feishu may block bots unless `FEISHU_ALLOW_BOTS=true`, and group traffic defaults to requiring mention unless `FEISHU_GROUP_POLICY=open` or `FEISHU_REQUIRE_MENTION=false`.
- WeCom honors `WECOM_DM_POLICY` and `WECOM_GROUP_POLICY` with `open`, `allowlist`, or `disabled`; env allowlists are used for users and optional chat lists.
- QQ Bot honors `QQ_ALLOWED_USERS` for senders and `QQ_GROUP_ALLOWED_USERS` for group IDs.

Unauthorized messages are ignored rather than answered, to avoid information leakage in restricted deployments.

## WeCom AI Bot

Only WeCom AI Bot WebSocket mode is supported. The bridge uses `WECOM_BOT_ID`, `WECOM_SECRET`, and `WECOM_WEBSOCKET_URL` to open the gateway, sends `aibot_subscribe`, receives `aibot_msg_callback`, extracts text from `text`, `voice.content`, and simple `mixed` text items, and replies with `aibot_respond_msg` when an inbound `req_id` is available. Proactive `aibot_send_msg` is a fallback for direct chats when no reply `req_id` is available.

## QQ Bot

QQ Bot uses the official API v2 pattern: `QQ_APP_ID` and `QQ_CLIENT_SECRET` obtain an app access token from `https://bots.qq.com/app/getAppAccessToken`, gateway URL from `https://api.sgroup.qq.com/gateway`, and inbound events over WebSocket. The bridge handles C2C, group @, guild/channel, and direct-message dispatches, then replies via `/v2/users/{openid}/messages`, `/v2/groups/{group_openid}/messages`, or `/channels/{channel_id}/messages`.

## Testing

Tests cover:

- `SessionSource` construction and auth order for all four platforms.
- Deny-by-default behavior and allow-all/allowlist behavior.
- DingTalk and Feishu bridge authorization gates.
- WeCom payload parsing, auth gates, WebSocket frame construction, and reply flow.
- QQ payload parsing, auth gates, token/gateway handling with mocks, and REST reply routing.
- `RDS_BOT_BRIDGES` selection for `dingtalk`, `feishu`, `wecom`, `qqbot`, and `all`.
