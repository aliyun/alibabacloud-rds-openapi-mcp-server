import asyncio
import contextlib
import json
import os
import re
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Optional
from urllib import request as urllib_request

from loguru import logger

from core.bot_core import (
    BotContext,
    CopilotConversationStore,
    SessionSource,
    authorize_session_source,
    build_error_content,
    build_busy_content,
    build_no_message_content,
    call_with_stream,
    get_copilot_conversation_store,
    handle_control_command,
    run_still_working_notifier,
    should_accept_session_source,
)
from core.rds_copilot import RdsCopilot

try:
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient

    FEISHU_AVAILABLE = True
except ImportError:  # pragma: no cover - optional Feishu SDK fallback
    lark = None
    lark_ws_client = None
    CreateMessageRequest = None
    CreateMessageRequestBody = None
    ReplyMessageRequest = None
    ReplyMessageRequestBody = None
    EventDispatcherHandler = None
    FeishuWSClient = None
    FEISHU_AVAILABLE = False


FEISHU_PLATFORM = "feishu"
FEISHU_TYPING_REACTION = "Get"
FEISHU_FAILURE_REACTION = "CrossMark"
MAX_FEISHU_TEXT_LENGTH = 8000
FEISHU_DOMAIN_URL = "https://open.feishu.cn"
LARK_DOMAIN_URL = "https://open.larksuite.com"
FEISHU_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(^\s*---+\s*$)|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)|(<u>.+?</u>)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def _load_feishu_message_text(raw_content: str) -> str:
    try:
        payload = json.loads(raw_content or "{}")
    except json.JSONDecodeError:
        return raw_content or ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("text", "") or "").strip()


def validate_feishu_startup(app_id: str = "", app_secret: str = "", domain: str = ""):
    bridge = FeishuBridge(app_id=app_id, app_secret=app_secret, domain=domain, store=CopilotConversationStore(""))
    if not bridge.app_id or not bridge.app_secret:
        raise RuntimeError(
            "缺少飞书凭证：请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET，"
            "或在启动前 export。"
        )

    url = f"{bridge._domain_url()}{FEISHU_TENANT_TOKEN_PATH}"
    body = json.dumps({"app_id": bridge.app_id, "app_secret": bridge.app_secret}).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as e:
        raise RuntimeError(
            "飞书鉴权失败：请检查 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_DOMAIN 和机器人权限。"
        ) from e

    code = int(payload.get("code", -1))
    if code != 0:
        message = payload.get("msg") or payload.get("message") or "unknown error"
        raise RuntimeError(
            "飞书鉴权失败：请检查 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_DOMAIN 和机器人权限。"
            f"平台返回：{message} (code={code})"
        )


def _extract_sender_id(sender_id: Any) -> str:
    return (
        str(getattr(sender_id, "open_id", "") or "")
        or str(getattr(sender_id, "user_id", "") or "")
        or str(getattr(sender_id, "union_id", "") or "")
    )


def _build_feishu_markdown_post_payload(content: str) -> str:
    return json.dumps({"zh_cn": {"content": _build_feishu_markdown_post_rows(content)}}, ensure_ascii=False)


def _build_feishu_markdown_post_rows(content: str) -> list[list[dict[str, str]]]:
    if not content:
        return [[{"tag": "md", "text": ""}]]
    if "```" not in content:
        return [[{"tag": "md", "text": content}]]

    rows: list[list[dict[str, str]]] = []
    current: list[str] = []
    in_code_block = False

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        segment = "\n".join(current)
        if segment.strip():
            rows.append([{"tag": "md", "text": segment}])
        current = []

    for raw_line in content.splitlines():
        stripped_line = raw_line.strip()
        is_fence = bool(
            _MARKDOWN_FENCE_CLOSE_RE.match(stripped_line)
            if in_code_block
            else _MARKDOWN_FENCE_OPEN_RE.match(stripped_line)
        )
        if is_fence:
            if not in_code_block:
                flush_current()
            current.append(raw_line)
            in_code_block = not in_code_block
            if not in_code_block:
                flush_current()
            continue
        current.append(raw_line)

    flush_current()
    return rows or [[{"tag": "md", "text": content}]]


def _build_feishu_outbound_payload(content: str) -> tuple[str, str]:
    if _MARKDOWN_TABLE_RE.search(content):
        return "text", json.dumps({"text": content}, ensure_ascii=False)
    if _MARKDOWN_HINT_RE.search(content):
        return "post", _build_feishu_markdown_post_payload(content)
    return "text", json.dumps({"text": content}, ensure_ascii=False)


def _start_feishu_ws_client(ws_client: Any) -> None:
    if lark_ws_client is None:
        ws_client.start()
        return

    previous_loop = getattr(lark_ws_client, "loop", None)
    worker_loop = asyncio.new_event_loop()
    lark_ws_client.loop = worker_loop
    asyncio.set_event_loop(worker_loop)
    try:
        ws_client.start()
    finally:
        lark_ws_client.loop = previous_loop
        with contextlib.suppress(Exception):
            worker_loop.close()


class FeishuBridge:
    """Feishu/Lark long-connection bridge for RDS Copilot."""

    def __init__(
        self,
        *,
        app_id: str = "",
        app_secret: str = "",
        domain: str = "",
        store: Optional[CopilotConversationStore] = None,
        copilot_factory: Callable[[], RdsCopilot] = RdsCopilot,
    ):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.domain = (domain or os.getenv("FEISHU_DOMAIN", "feishu")).strip().lower()
        self.store = store or get_copilot_conversation_store()
        self.copilot_factory = copilot_factory
        self.client = None
        self.ws_client = None
        self.loop = None

    async def start_forever(self):
        if not FEISHU_AVAILABLE:
            raise RuntimeError("lark-oapi is not installed; install requirements.txt first")
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "缺少飞书凭证：请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET，"
                "或在启动前 export。"
            )

        self.loop = asyncio.get_running_loop()
        self.client = self._build_lark_client()
        event_handler = self._build_event_handler()
        self.ws_client = FeishuWSClient(
            app_id=self.app_id,
            app_secret=self.app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=event_handler,
            domain=self._domain_url(),
        )
        logger.info("Feishu bridge connected by long connection")
        await asyncio.to_thread(_start_feishu_ws_client, self.ws_client)

    def _build_lark_client(self):
        domain_builder = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret)
        if hasattr(domain_builder, "domain"):
            domain_builder = domain_builder.domain(self._domain_url())
        if hasattr(lark, "LogLevel"):
            domain_builder = domain_builder.log_level(lark.LogLevel.WARNING)
        return domain_builder.build()

    def _domain_url(self) -> str:
        return LARK_DOMAIN_URL if self.domain == "lark" else FEISHU_DOMAIN_URL

    def _build_event_handler(self):
        return (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )

    def _on_message_event(self, data: Any) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            logger.warning("Feishu event received before loop is ready")
            return
        future = asyncio.run_coroutine_threadsafe(self.handle_message_event_data(data), loop)
        future.add_done_callback(self._log_background_failure)

    @staticmethod
    def _log_background_failure(future):
        try:
            future.result()
        except Exception:
            logger.exception("Feishu background message handling failed")

    async def handle_message_event_data(self, data: Any):
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id_obj = getattr(sender, "sender_id", None)
        if not message or not sender_id_obj:
            logger.debug("Ignore malformed Feishu event without message or sender")
            return

        message_type = str(getattr(message, "message_type", "") or "").lower()
        if message_type != "text":
            await self.send_text(
                getattr(message, "chat_id", "") or "",
                "I can only process text messages.",
                reply_to_message_id=getattr(message, "message_id", "") or None,
            )
            return

        await self.handle_text_message(
            chat_id=getattr(message, "chat_id", "") or "",
            sender_id=_extract_sender_id(sender_id_obj),
            sender_id_alt=str(getattr(sender_id_obj, "union_id", "") or ""),
            sender_name=str(getattr(sender, "sender_type", "") or ""),
            chat_type="group" if str(getattr(message, "chat_type", "") or "").lower() == "group" else "dm",
            is_bot=str(getattr(sender, "sender_type", "") or "").lower() == "bot",
            message_id=getattr(message, "message_id", "") or "",
            text=_load_feishu_message_text(getattr(message, "content", "") or ""),
        )

    async def handle_text_message(
        self,
        *,
        chat_id: str,
        sender_id: str,
        sender_id_alt: str = "",
        sender_name: str = "",
        chat_type: str = "dm",
        is_bot: bool = False,
        message_id: str,
        text: str,
    ):
        query_text = (text or "").strip()
        source = SessionSource(
            FEISHU_PLATFORM,
            chat_id,
            chat_type or "dm",
            sender_id,
            user_name=sender_name,
            user_id_alt=sender_id_alt,
            is_bot=is_bot,
        )
        if not should_accept_session_source(source, query_text) or not authorize_session_source(source):
            logger.info("Unauthorized Feishu message ignored: chat_id=%s sender_id=%s", chat_id, sender_id)
            return

        context = BotContext(FEISHU_PLATFORM, chat_id, sender_id, self.store)
        control_result = await handle_control_command(
            query_text,
            context,
            self.copilot_factory,
            card_supported=False,
        )
        if control_result.handled:
            await self.send_text(chat_id, control_result.content, reply_to_message_id=message_id)
            return

        language = self.store.get_language(chat_id, sender_id, platform=FEISHU_PLATFORM)
        active_state = context.registry.try_start(context)
        if active_state is None:
            await self.send_text(chat_id, build_busy_content(language), reply_to_message_id=message_id)
            return

        session_enabled = self.store.is_session_enabled(chat_id, sender_id, platform=FEISHU_PLATFORM)
        conversion_id = self.store.get(chat_id, sender_id, platform=FEISHU_PLATFORM) if session_enabled else ""
        selected_agent = self.store.get_agent(chat_id, sender_id, platform=FEISHU_PLATFORM)
        custom_agent_id = selected_agent.get("id", "")
        timezone = self.store.get_timezone(chat_id, sender_id, platform=FEISHU_PLATFORM)
        reaction_id = await self.add_processing_reaction(message_id)
        final_display_content = ""
        notifier_task = asyncio.create_task(
            run_still_working_notifier(active_state, lambda content: self.send_text(chat_id, content), language=language)
        )
        try:
            final_contents = await call_with_stream(
                query_text,
                self._ignore_stream_update,
                self.copilot_factory(),
                conversion_id,
                custom_agent_id=custom_agent_id,
                language=language,
                timezone=timezone,
                active_state=active_state,
            )
            final_display_content = final_contents.get("content", "")
            current_conversion_id = final_contents.get("conversion_id", "")
            if current_conversion_id and session_enabled:
                self.store.set(chat_id, sender_id, current_conversion_id, platform=FEISHU_PLATFORM)
            if not final_display_content and final_contents.get("cancelled"):
                if reaction_id:
                    await self.remove_processing_reaction(message_id, reaction_id)
                return
            if not final_display_content:
                final_display_content = build_no_message_content(language)
            await self.send_text(chat_id, final_display_content, reply_to_message_id=message_id)
            if reaction_id:
                await self.remove_processing_reaction(message_id, reaction_id)
        except Exception as e:
            final_display_content = build_error_content(e, language)
            logger.exception("Feishu Copilot reply failed: %s", e)
            await self.send_text(chat_id, final_display_content, reply_to_message_id=message_id)
            if reaction_id:
                removed = await self.remove_processing_reaction(message_id, reaction_id)
                if removed:
                    await self.add_failure_reaction(message_id)
        finally:
            notifier_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notifier_task
            context.registry.finish(active_state)

    async def _ignore_stream_update(self, update_data: dict):
        return None

    async def send_text(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str = "",
    ) -> bool:
        if not self.client:
            logger.info("Feishu send skipped because client is not connected")
            return False

        message_text = (content or "").strip() or build_no_message_content()
        if len(message_text) > MAX_FEISHU_TEXT_LENGTH:
            message_text = message_text[:MAX_FEISHU_TEXT_LENGTH] + "\n\n...(truncated)"
        msg_type, payload = _build_feishu_outbound_payload(message_text)
        try:
            if reply_to_message_id:
                request = self._build_reply_message_request(
                    reply_to_message_id,
                    self._build_reply_message_body(
                        content=payload,
                        msg_type=msg_type,
                        reply_in_thread=False,
                        uuid_value=str(uuid.uuid4()),
                    ),
                )
                response = await asyncio.to_thread(self.client.im.v1.message.reply, request)
            else:
                request = self._build_create_message_request(
                    "chat_id",
                    self._build_create_message_body(
                        receive_id=chat_id,
                        msg_type=msg_type,
                        content=payload,
                        uuid_value=str(uuid.uuid4()),
                    ),
                )
                response = await asyncio.to_thread(self.client.im.v1.message.create, request)
            if response and getattr(response, "success", lambda: False)():
                return True
            logger.warning(
                "Feishu send rejected: code=%s msg=%s",
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
            return False
        except Exception:
            logger.exception("Feishu send failed")
            return False

    async def add_processing_reaction(self, message_id: str) -> str:
        return await self._add_reaction(message_id, FEISHU_TYPING_REACTION) or ""

    async def add_failure_reaction(self, message_id: str) -> str:
        return await self._add_reaction(message_id, FEISHU_FAILURE_REACTION) or ""

    async def _add_reaction(self, message_id: str, emoji_type: str) -> str:
        if not self.client or not message_id:
            return ""
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
            )

            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type({"emoji_type": emoji_type})
                .build()
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = await asyncio.to_thread(self.client.im.v1.message_reaction.create, request)
            if response and getattr(response, "success", lambda: False)():
                data = getattr(response, "data", None)
                return getattr(data, "reaction_id", "") or ""
        except Exception:
            logger.warning("Feishu add reaction failed", exc_info=True)
        return ""

    async def remove_processing_reaction(self, message_id: str, reaction_id: str) -> bool:
        if not self.client or not message_id or not reaction_id:
            return False
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            response = await asyncio.to_thread(self.client.im.v1.message_reaction.delete, request)
            return bool(response and getattr(response, "success", lambda: False)())
        except Exception:
            logger.warning("Feishu remove reaction failed", exc_info=True)
            return False

    @staticmethod
    def _build_reply_message_body(*, content: str, msg_type: str, reply_in_thread: bool, uuid_value: str) -> Any:
        if ReplyMessageRequestBody is not None:
            return (
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(msg_type)
                .reply_in_thread(reply_in_thread)
                .uuid(uuid_value)
                .build()
            )
        return SimpleNamespace(content=content, msg_type=msg_type, reply_in_thread=reply_in_thread, uuid=uuid_value)

    @staticmethod
    def _build_reply_message_request(message_id: str, request_body: Any) -> Any:
        if ReplyMessageRequest is not None:
            return ReplyMessageRequest.builder().message_id(message_id).request_body(request_body).build()
        return SimpleNamespace(message_id=message_id, request_body=request_body)

    @staticmethod
    def _build_create_message_body(*, receive_id: str, msg_type: str, content: str, uuid_value: str) -> Any:
        if CreateMessageRequestBody is not None:
            return (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(uuid_value)
                .build()
            )
        return SimpleNamespace(receive_id=receive_id, msg_type=msg_type, content=content, uuid=uuid_value)

    @staticmethod
    def _build_create_message_request(receive_id_type: str, request_body: Any) -> Any:
        if CreateMessageRequest is not None:
            return CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(request_body).build()
        return SimpleNamespace(receive_id_type=receive_id_type, request_body=request_body)


def run_feishu_bridge():
    asyncio.run(FeishuBridge().start_forever())
