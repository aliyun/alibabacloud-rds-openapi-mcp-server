#!/usr/bin/env python
import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv


INTEGRATION_DIR = Path(__file__).resolve().parents[1]
if str(INTEGRATION_DIR) not in sys.path:  # pragma: no cover - import bootstrap for direct script execution
    sys.path.insert(0, str(INTEGRATION_DIR))

load_dotenv()

from bridges import dingtalk as dingtalk_bridge
from bridges.feishu import FeishuBridge
from bridges.qq import QQBridge
from bridges.wecom import WeComBridge
from core import bot_core
from core.rds_copilot import MessageEvent, RdsCopilot


DEFAULT_QUERY = "请只回复 pong，不要解释。"
REQUIRED_RDSAI_ENV = ("ACCESS_KEY_ID", "ACCESS_SECRET")
MOCK_BRIDGE_TIMEOUT_ENV = "RDS_E2E_MOCK_BRIDGE_TIMEOUT_SECONDS"
DEFAULT_MOCK_BRIDGE_TIMEOUT_SECONDS = 120.0
LOADABLE_ENV_NAMES = {
    "ACCESS_KEY_ID",
    "ACCESS_SECRET",
    "DINGTALK_APP_CLIENT_ID",
    "DINGTALK_APP_CLIENT_SECRET",
    "DINGTALK_DM_ALLOW_LIST",
    "DINGTALK_DM_ALLOW_POLICY",
    "DINGTALK_FREE_RESPONSE_CHATS",
    "DINGTALK_GROUP_ALLOW_LIST",
    "DINGTALK_GROUP_ALLOW_POLICY",
    "DINGTALK_MENTION_PATTERNS",
    "DINGTALK_REQUIRE_MENTION",
    "DINGTALK_ROBOT_CODE",
    "FEISHU_ALLOW_BOTS",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_BOT_NAME",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_USER_ID",
    "FEISHU_DM_ALLOW_LIST",
    "FEISHU_DM_ALLOW_POLICY",
    "FEISHU_DOMAIN",
    "FEISHU_GROUP_ALLOW_LIST",
    "FEISHU_GROUP_ALLOW_POLICY",
    "FEISHU_GROUP_POLICY",
    "QQ_APP_ID",
    "QQ_CLIENT_SECRET",
    "QQ_DM_ALLOW_LIST",
    "QQ_DM_ALLOW_POLICY",
    "QQ_GROUP_ALLOW_LIST",
    "QQ_GROUP_ALLOW_POLICY",
    "QQ_RECONNECT_BASE_SECONDS",
    "QQ_RECONNECT_MAX_SECONDS",
    "RDS_BOT_BRIDGES",
    "RDS_BOT_STILL_WORKING_INTERVAL_SECONDS",
    "RDS_COPILOT_CHAT_WORKERS",
    "RDS_COPILOT_CONVERSATION_STORE_FILE",
    "RDS_COPILOT_ENDPOINT",
    "RDS_COPILOT_LOG_FILE",
    "RDS_E2E_MOCK_BRIDGE_TIMEOUT_SECONDS",
    "WECOM_BOT_ID",
    "WECOM_DM_ALLOW_LIST",
    "WECOM_DM_ALLOW_POLICY",
    "WECOM_GROUP_ALLOW_LIST",
    "WECOM_GROUP_ALLOW_POLICY",
    "WECOM_HEARTBEAT_SECONDS",
    "WECOM_RECONNECT_BASE_SECONDS",
    "WECOM_RECONNECT_MAX_SECONDS",
    "WECOM_SECRET",
    "WECOM_WEBSOCKET_URL",
}
SECRET_ENV_NAMES = {
    "ACCESS_KEY_ID",
    "ACCESS_SECRET",
    "DINGTALK_APP_CLIENT_SECRET",
    "FEISHU_APP_SECRET",
    "QQ_CLIENT_SECRET",
    "WECOM_SECRET",
}


@dataclass
class StepResult:
    name: str
    passed: bool
    detail: str
    error: str = ""


class FirstMessageCopilot:
    """E2E adapter: stop bridge/smoke checks once the first visible reply arrives."""

    def __init__(self, copilot_factory: Callable[[], RdsCopilot]):
        self._copilot = copilot_factory()

    def __getattr__(self, name: str):
        return getattr(self._copilot, name)

    def chat(self, *args, **kwargs):
        for event in self._copilot.chat(*args, **kwargs):
            yield event
            if isinstance(event, MessageEvent):
                return


def preview_text(value: str, limit: int = 200) -> str:
    text = (value or "").strip().replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _parse_known_env_line(line: str) -> tuple[str, str] | None:
    stripped = (line or "").strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("```"):
        return None
    while stripped.startswith(("-", "*")):
        stripped = stripped[1:].strip()
    stripped = stripped.strip("`").strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()

    match = re.match(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", stripped)
    if not match:
        return None

    key, raw_value = match.groups()
    if key not in LOADABLE_ENV_NAMES:
        return None

    raw_value = raw_value.strip().strip("`")
    try:
        parts = shlex.split(raw_value, comments=False)
    except ValueError:
        value = raw_value.strip("\"'")
    else:
        value = parts[0] if parts else ""
    return key, value


def _load_known_env_pairs(env_file: str) -> bool:
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    parsed_any = False
    for line in lines:
        pair = _parse_known_env_line(line)
        if not pair:
            continue
        parsed_any = True
        key, value = pair
        if key not in os.environ:
            os.environ[key] = value
    return parsed_any


def load_env_file(env_file: str = "") -> bool:
    if not env_file:
        return load_dotenv()

    # Parse allowlisted keys quietly first so explicit env files do not leak
    # unrelated secrets into the test process.
    return _load_known_env_pairs(env_file) or load_dotenv(dotenv_path=env_file, override=False)


def redact_sensitive_values(value: str) -> str:
    redacted = value or ""
    for key in SECRET_ENV_NAMES:
        secret = os.getenv(key)
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "***")
    return redacted


def exception_text(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {redact_sensitive_values(str(exc))}"


def missing_rdsai_env() -> list[str]:
    return [key for key in REQUIRED_RDSAI_ENV if not os.getenv(key)]


def _validate_mapping(name: str, value: object) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{name} returned {type(value).__name__}, expected dict")
    return f"{name}=ok keys={','.join(sorted(str(key) for key in value.keys())[:6]) or 'none'}"


def run_rdsai_smoke(
    query: str = DEFAULT_QUERY,
    *,
    copilot_factory: Callable[[], RdsCopilot] = RdsCopilot,
    conversation_id: str = "",
    custom_agent_id: str = "",
    max_events: int = 30,
    first_message_only: bool = False,
) -> StepResult:
    try:
        copilot = FirstMessageCopilot(copilot_factory) if first_message_only else copilot_factory()
        api_checks = [
            _validate_mapping(
                "GetConversations",
                copilot.list_conversations(limit=3, sort_by="CreatedAt"),
            ),
            _validate_mapping(
                "ListCustomAgent",
                copilot.list_custom_agents(page_number=1, page_size=3),
            ),
            _validate_mapping(
                "ListSkill",
                copilot.list_skills(page_number=1, page_size=5, language="zh-CN"),
            ),
        ]

        chunks = []
        final_conversation_id = conversation_id
        task_seen = False
        event_count = 0
        for event in copilot.chat(
            query,
            conversation_id,
            trace_id="local-e2e-rdsai",
            custom_agent_id=custom_agent_id,
        ):
            event_count += 1
            task_seen = task_seen or bool(getattr(event, "task_id", ""))
            final_conversation_id = getattr(event, "conversion_id", "") or final_conversation_id
            if isinstance(event, MessageEvent):
                chunks.append(event.text)
                if first_message_only:
                    break
            if event_count >= max_events:
                break

        content = "".join(chunks).strip()
        if not content:
            return StepResult(
                "RDSAI chat",
                False,
                f"events={event_count} conversation_id={final_conversation_id or 'none'}",
                "RDSAI returned no displayable message content",
            )
        detail = (
            f"events={event_count} conversation_id={final_conversation_id or 'none'} "
            f"task_seen={'yes' if task_seen else 'no'} preview={preview_text(content)} "
            + " ".join(api_checks)
        )
        return StepResult("RDSAI chat", True, detail)
    except Exception as exc:
        return StepResult("RDSAI chat", False, "real RDSAI call failed", exception_text(exc))


class _FakeDingTalkText:
    def __init__(self, content: str):
        self.content = content


class _FakeDingTalkMessage:
    def __init__(self, content: str):
        self.text = _FakeDingTalkText(content)
        self.conversation_id = "mock-dingtalk-conversation"
        self.sender_id = "mock-dingtalk-sender"
        self.sender_staff_id = "mock-dingtalk-staff"
        self.message_id = "mock-dingtalk-message"
        self.message_type = "text"
        self.robot_code = "mock-robot-code"
        self.session_webhook = "mock://session-webhook"


async def _run_dingtalk_mock_query(query: str, store_path: str, copilot_factory: Callable[[], RdsCopilot]) -> list[str]:
    replies = []
    webhook_messages = []
    incoming_message = _FakeDingTalkMessage(query)
    callback = SimpleNamespace(data={"sessionWebhook": "mock://session-webhook"})
    store = dingtalk_bridge.JsonCopilotConversationStore(store_path)

    handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("rdsai-e2e-dingtalk"))
    handler.reply_text = lambda text, message: replies.append(text)
    handler.reply_markdown = lambda title, text, message: replies.append(text) or {"ok": True}

    async def fake_send_webhook(session_webhook: str, content: str, *, trace_id: str = "", mention_user_id: str = ""):
        webhook_messages.append(content)
        return True

    created_tasks = []
    original_create_task = asyncio.create_task

    def track_task(coro):
        task = original_create_task(coro)
        created_tasks.append(task)
        return task

    with patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path, "DINGTALK_DM_ALLOW_POLICY": "open", "DINGTALK_GROUP_ALLOW_POLICY": "open"}), \
        patch("core.bot_core.RdsCopilot", side_effect=copilot_factory), \
        patch("bridges.dingtalk.RdsCopilot", side_effect=copilot_factory), \
        patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming_message), \
        patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
        patch("bridges.dingtalk.send_dingtalk_session_webhook", new=fake_send_webhook), \
        patch("bridges.dingtalk.asyncio.create_task", side_effect=track_task):
        status, message = await handler.process(callback)
        if (status, message) != (dingtalk_bridge.AckMessage.STATUS_OK, "OK"):
            raise RuntimeError(f"DingTalk handler returned {(status, message)}")
        await asyncio.gather(*created_tasks, return_exceptions=False)

    return replies + webhook_messages


async def _run_feishu_mock_query(query: str, store_path: str, copilot_factory: Callable[[], RdsCopilot]) -> list[str]:
    replies = []
    bridge = FeishuBridge(
        app_id="mock-feishu-app",
        app_secret="mock-feishu-secret",
        store=bot_core.CopilotConversationStore(store_path),
        copilot_factory=copilot_factory,
    )
    bridge.send_text = AsyncMock(side_effect=lambda chat_id, content, **kwargs: replies.append(content) or True)
    bridge.add_processing_reaction = AsyncMock(return_value="mock-reaction")
    bridge.remove_processing_reaction = AsyncMock(return_value=True)
    bridge.add_failure_reaction = AsyncMock(return_value="mock-failure-reaction")

    event_data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="mock-feishu-user", user_id="", union_id="")),
            message=SimpleNamespace(
                chat_id="mock-feishu-chat",
                message_id="mock-feishu-message",
                message_type="text",
                content=json.dumps({"text": query}, ensure_ascii=False),
            ),
        )
    )
    with patch.dict(os.environ, {"FEISHU_DM_ALLOW_POLICY": "open", "FEISHU_GROUP_ALLOW_POLICY": "open"}):
        await bridge.handle_message_event_data(event_data)
    return replies


async def _run_wecom_mock_query(query: str, store_path: str, copilot_factory: Callable[[], RdsCopilot]) -> list[str]:
    frames = []
    bridge = WeComBridge(
        bot_id="mock-wecom-bot",
        secret="mock-wecom-secret",
        store=bot_core.CopilotConversationStore(store_path),
        copilot_factory=copilot_factory,
    )
    bridge._send_frame = AsyncMock(side_effect=lambda frame: frames.append(frame) or True)
    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "mock-wecom-req"},
        "body": {
            "msgid": "mock-wecom-message",
            "chatid": "mock-wecom-chat",
            "chattype": "group",
            "from": {"userid": "mock-wecom-user", "name": "Mock WeCom User"},
            "msgtype": "text",
            "text": {"content": query},
        },
    }
    with patch.dict(os.environ, {"WECOM_GROUP_ALLOW_LIST": "mock-wecom-chat"}):
        await bridge.handle_payload(payload)
    return [frame["body"]["markdown"]["content"] for frame in frames if frame.get("body", {}).get("markdown")]


async def _run_qq_mock_query(query: str, store_path: str, copilot_factory: Callable[[], RdsCopilot]) -> list[str]:
    bodies = []
    bridge = QQBridge(
        app_id="mock-qq-app",
        client_secret="mock-qq-secret",
        store=bot_core.CopilotConversationStore(store_path),
        copilot_factory=copilot_factory,
    )
    bridge._api_request = AsyncMock(side_effect=lambda method, path, body=None: bodies.append(body or {}) or {"id": "mock-qq-sent"})
    event = {
        "id": "mock-qq-message",
        "content": query,
        "author": {"user_openid": "mock-qq-user"},
    }
    with patch.dict(os.environ, {"QQ_DM_ALLOW_LIST": "mock-qq-user"}):
        await bridge.handle_event("C2C_MESSAGE_CREATE", event)
    return [
        str(body["markdown"].get("content") or "")
        for body in bodies
        if isinstance(body.get("markdown"), dict) and body["markdown"].get("content")
    ]


async def run_mock_bridge_e2e(
    query: str = DEFAULT_QUERY,
    *,
    copilot_factory: Callable[[], RdsCopilot] = RdsCopilot,
    run_dingtalk: bool = True,
    run_feishu: bool = True,
    run_wecom: bool = True,
    run_qqbot: bool = True,
    first_message_only: bool = False,
) -> StepResult:
    try:
        bridge_copilot_factory = (lambda: FirstMessageCopilot(copilot_factory)) if first_message_only else copilot_factory
        timeout_seconds = float(os.getenv(MOCK_BRIDGE_TIMEOUT_ENV, str(DEFAULT_MOCK_BRIDGE_TIMEOUT_SECONDS)))
        details = []

        async def run_platform(label: str, coro):
            try:
                return await asyncio.wait_for(coro, timeout=max(timeout_seconds, 0.001))
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{label} mock timed out after {timeout_seconds:.1f}s") from exc

        with tempfile.TemporaryDirectory(prefix="rdsai-bridge-e2e-") as tmp_dir:
            if run_dingtalk:
                dingtalk_replies = await run_platform(
                    "DingTalk",
                    _run_dingtalk_mock_query(
                        query,
                        os.path.join(tmp_dir, "dingtalk-conversations.json"),
                        bridge_copilot_factory,
                    ),
                )
                if not dingtalk_replies:
                    raise RuntimeError("DingTalk mock produced no reply")
                details.append(f"dingtalk_replies={len(dingtalk_replies)} preview={preview_text(dingtalk_replies[-1])}")

            if run_feishu:
                feishu_replies = await run_platform(
                    "Feishu",
                    _run_feishu_mock_query(
                        query,
                        os.path.join(tmp_dir, "feishu-conversations.json"),
                        bridge_copilot_factory,
                    ),
                )
                if not feishu_replies:
                    raise RuntimeError("Feishu mock produced no reply")
                details.append(f"feishu_replies={len(feishu_replies)} preview={preview_text(feishu_replies[-1])}")

            if run_wecom:
                wecom_replies = await run_platform(
                    "WeCom",
                    _run_wecom_mock_query(
                        query,
                        os.path.join(tmp_dir, "wecom-conversations.json"),
                        bridge_copilot_factory,
                    ),
                )
                if not wecom_replies:
                    raise RuntimeError("WeCom mock produced no reply")
                details.append(f"wecom_replies={len(wecom_replies)} preview={preview_text(wecom_replies[-1])}")

            if run_qqbot:
                qq_replies = await run_platform(
                    "QQ Bot",
                    _run_qq_mock_query(
                        query,
                        os.path.join(tmp_dir, "qq-conversations.json"),
                        bridge_copilot_factory,
                    ),
                )
                if not qq_replies:
                    raise RuntimeError("QQ Bot mock produced no reply")
                details.append(f"qqbot_replies={len(qq_replies)} preview={preview_text(qq_replies[-1])}")

        return StepResult("Mock bot bridge E2E", True, " ".join(details))
    except Exception as exc:
        return StepResult("Mock bot bridge E2E", False, "mock bridge flow failed", exception_text(exc))


def print_result(result: StepResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    line = f"{result.name}: {status} {redact_sensitive_values(result.detail)}".strip()
    if result.error:
        line = f"{line} error={redact_sensitive_values(result.error)}"
    print(line)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real RDSAI calls through mocked bot bridge inputs.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Prompt used for the real RDSAI chat call.")
    parser.add_argument("--conversation-id", default="", help="Optional existing RDSAI ConversationId.")
    parser.add_argument("--custom-agent-id", default="", help="Optional RDSAI CustomAgentId.")
    parser.add_argument("--env-file", default="", help="Optional .env file to load before running.")
    parser.add_argument("--skip-rdsai-smoke", action="store_true", help="Skip standalone RDSAI API/list/chat smoke checks.")
    parser.add_argument("--skip-dingtalk", action="store_true", help="Skip mocked DingTalk bridge input.")
    parser.add_argument("--skip-feishu", action="store_true", help="Skip mocked Feishu bridge input.")
    parser.add_argument("--skip-wecom", action="store_true", help="Skip mocked WeCom bridge input.")
    parser.add_argument("--skip-qqbot", action="store_true", help="Skip mocked QQ Bot bridge input.")
    parser.add_argument("--max-events", type=int, default=30, help="Maximum SSE events to consume in standalone RDSAI smoke.")
    parser.add_argument(
        "--first-message-only",
        action="store_true",
        help="Stop each real RDSAI bridge call after the first visible message chunk for a faster smoke run.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> list[StepResult]:
    if args.env_file:
        load_env_file(args.env_file)

    results = []
    missing = missing_rdsai_env()
    needs_rdsai = not args.skip_rdsai_smoke or not (
        args.skip_dingtalk and args.skip_feishu and args.skip_wecom and args.skip_qqbot
    )
    if missing and needs_rdsai:
        return [
            StepResult(
                "Credentials",
                False,
                "missing required environment variables",
                "missing " + ", ".join(missing) + "; export them or pass --env-file",
            )
        ]

    if not args.skip_rdsai_smoke:
        results.append(
            await asyncio.to_thread(
                run_rdsai_smoke,
                args.query,
                conversation_id=args.conversation_id,
                custom_agent_id=args.custom_agent_id,
                max_events=max(1, args.max_events),
                first_message_only=args.first_message_only,
            )
        )

    if not (args.skip_dingtalk and args.skip_feishu and args.skip_wecom and args.skip_qqbot):
        results.append(
            await run_mock_bridge_e2e(
                args.query,
                run_dingtalk=not args.skip_dingtalk,
                run_feishu=not args.skip_feishu,
                run_wecom=not args.skip_wecom,
                run_qqbot=not args.skip_qqbot,
                first_message_only=args.first_message_only,
            )
        )

    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = asyncio.run(run(args))
    for result in results:
        print_result(result)
    return 0 if results and all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover - direct script execution
    raise SystemExit(main())
