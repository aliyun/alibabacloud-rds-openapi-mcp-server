import asyncio
import contextlib
import json
import os
import uuid
from typing import Any, Callable, Optional

from loguru import logger

from core.bot_core import (
    BotContext,
    CopilotConversationStore,
    SessionSource,
    authorize_session_source,
    build_busy_content,
    build_error_content,
    build_no_message_content,
    call_with_stream,
    format_identity_source,
    get_copilot_conversation_store,
    handle_control_command,
    is_identity_command,
    run_still_working_notifier,
    should_accept_session_source,
)
from core.error_detail import payload_detail
from core.rds_copilot import RdsCopilot

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency fallback
    aiohttp = None
    AIOHTTP_AVAILABLE = False


WECOM_PLATFORM = "wecom"
DEFAULT_WECOM_WEBSOCKET_URL = "wss://openws.work.weixin.qq.com"
APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_PING = "ping"
MAX_WECOM_TEXT_LENGTH = 4000
WECOM_CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_WECOM_HEARTBEAT_SECONDS = 30.0
DEFAULT_WECOM_RECONNECT_BASE_SECONDS = 3.0
DEFAULT_WECOM_RECONNECT_MAX_SECONDS = 60.0


def _read_positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _calculate_reconnect_delay(restart_index: int) -> float:
    base = _read_positive_float_env("WECOM_RECONNECT_BASE_SECONDS", DEFAULT_WECOM_RECONNECT_BASE_SECONDS)
    max_delay = _read_positive_float_env("WECOM_RECONNECT_MAX_SECONDS", DEFAULT_WECOM_RECONNECT_MAX_SECONDS)
    return min(max_delay, base * (2 ** max(0, restart_index)))


def validate_wecom_startup():
    asyncio.run(WeComBridge().check_startup())


def extract_wecom_text(body: dict[str, Any]) -> str:
    msgtype = str(body.get("msgtype") or "").lower()
    parts: list[str] = []
    if msgtype == "mixed":
        mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
        for item in mixed.get("msg_item") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("msgtype") or "").lower() == "text":
                text_block = item.get("text") if isinstance(item.get("text"), dict) else {}
                content = str(text_block.get("content") or "").strip()
                if content:
                    parts.append(content)
    else:
        text_block = body.get("text") if isinstance(body.get("text"), dict) else {}
        content = str(text_block.get("content") or "").strip()
        if content:
            parts.append(content)
        voice_block = body.get("voice") if isinstance(body.get("voice"), dict) else {}
        voice_text = str(voice_block.get("content") or "").strip()
        if voice_text:
            parts.append(voice_text)
    return "\n".join(parts).strip()


def source_from_wecom_body(body: dict[str, Any]) -> SessionSource:
    sender = body.get("from") if isinstance(body.get("from"), dict) else {}
    user_id = str(sender.get("userid") or "").strip()
    user_name = str(sender.get("name") or user_id).strip()
    is_group = str(body.get("chattype") or "").lower() == "group"
    chat_id = str(body.get("chatid") or user_id).strip()
    return SessionSource(
        platform=WECOM_PLATFORM,
        chat_id=chat_id,
        chat_type="group" if is_group else "dm",
        user_id=user_id,
        user_name=user_name,
    )


class WeComBridge:
    def __init__(
        self,
        *,
        bot_id: str = "",
        secret: str = "",
        websocket_url: str = "",
        store: Optional[CopilotConversationStore] = None,
        copilot_factory: Callable[[], RdsCopilot] = RdsCopilot,
        heartbeat_seconds: float | None = None,
    ):
        self.bot_id = bot_id or os.getenv("WECOM_BOT_ID", "")
        self.secret = secret or os.getenv("WECOM_SECRET", "")
        self.websocket_url = websocket_url or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WECOM_WEBSOCKET_URL)
        self.store = store or get_copilot_conversation_store()
        self.copilot_factory = copilot_factory
        self.session = None
        self.ws = None
        self._running = False
        self.device_id = uuid.uuid4().hex
        self.heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else _read_positive_float_env("WECOM_HEARTBEAT_SECONDS", DEFAULT_WECOM_HEARTBEAT_SECONDS)
        )

    async def start_forever(self):  # pragma: no cover - real WebSocket loop is covered by integration smoke tests
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is not installed; install requirements.txt first")
        if not self.bot_id or not self.secret:
            raise RuntimeError(
                "缺少企业微信凭证：请在 .env 中配置 WECOM_BOT_ID 和 WECOM_SECRET，"
                "或在启动前 export。"
            )

        self._running = True
        restart_index = 0
        while self._running:
            try:
                await self._connect_once()
                restart_index = 0
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as e:
                if not self._running:
                    break
                delay = _calculate_reconnect_delay(restart_index)
                restart_index += 1
                logger.error(
                    "企业微信 WeCom websocket 已断开，{:.1f}s 后自动重连，错误={}: {}",
                    delay,
                    e.__class__.__name__,
                    str(e),
                )
                await asyncio.sleep(delay)

    async def _connect_once(self):
        self.session = aiohttp.ClientSession(trust_env=True)
        heartbeat = self.heartbeat_seconds * 2 if self.heartbeat_seconds > 0 else None
        receive_timeout = max((self.heartbeat_seconds or DEFAULT_WECOM_HEARTBEAT_SECONDS) * 3, 60)
        heartbeat_task = None
        try:
            self.ws = await self.session.ws_connect(
                self.websocket_url,
                heartbeat=heartbeat,
                timeout=WECOM_CONNECT_TIMEOUT_SECONDS,
                receive_timeout=receive_timeout,
            )
            await self._subscribe()
            logger.info(
                "WeCom bridge connected by websocket, url={}, heartbeat_seconds={}",
                self.websocket_url,
                self.heartbeat_seconds,
            )
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            while self._running:
                receive_task = asyncio.create_task(self.ws.receive())
                done, pending = await asyncio.wait(
                    {receive_task, heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat_task in done:
                    receive_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receive_task
                    heartbeat_task.result()
                    raise RuntimeError("WeCom heartbeat loop stopped")

                message = receive_task.result()
                if message.type == aiohttp.WSMsgType.TEXT:
                    payload = self._parse_json(message.data)
                    if payload:
                        await self.handle_payload(payload)
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    raise RuntimeError("WeCom websocket closed")
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.session:
                await self.session.close()
            self.ws = None
            self.session = None

    async def check_startup(self):
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is not installed; install requirements.txt first")
        if not self.bot_id or not self.secret:
            raise RuntimeError(
                "缺少企业微信凭证：请在 .env 中配置 WECOM_BOT_ID 和 WECOM_SECRET，"
                "或在启动前 export。"
            )
        self.session = aiohttp.ClientSession(trust_env=True)
        try:
            self.ws = await self.session.ws_connect(
                self.websocket_url,
                heartbeat=None,
                timeout=WECOM_CONNECT_TIMEOUT_SECONDS,
                receive_timeout=WECOM_CONNECT_TIMEOUT_SECONDS,
            )
            await self._subscribe()
        finally:
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.session:
                await self.session.close()
            self.ws = None
            self.session = None

    async def _subscribe(self):
        req_id = self._new_req_id("subscribe")
        await self._send_frame(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {
                    "bot_id": self.bot_id,
                    "secret": self.secret,
                    "device_id": self.device_id,
                },
            }
        )
        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in (0, None):
            errmsg = auth_payload.get("errmsg") or "authentication failed"
            detail = payload_detail(auth_payload)
            raise RuntimeError(
                "企业微信 WeCom 鉴权失败：请检查 WECOM_BOT_ID 和 WECOM_SECRET。"
                f"网关返回：{errmsg} (errcode={errcode})。完整响应：{detail}"
            )

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(max(self.heartbeat_seconds, 0))
            if not self.ws or getattr(self.ws, "closed", False):
                raise RuntimeError("WeCom websocket is not connected")
            ok = await self._send_frame(
                {
                    "cmd": APP_CMD_PING,
                    "headers": {"req_id": self._new_req_id("ping")},
                    "body": {},
                }
            )
            if not ok:
                raise RuntimeError("WeCom heartbeat send failed")

    async def _wait_for_handshake(self, req_id: str) -> dict[str, Any]:
        if not self.ws:
            raise RuntimeError("WeCom websocket is not connected")

        deadline = asyncio.get_running_loop().time() + WECOM_CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")

            message = await asyncio.wait_for(self.ws.receive(), timeout=remaining)
            if message.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(message.data)
                if not payload:
                    continue
                if str(payload.get("cmd") or "") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("Ignoring WeCom pre-auth payload: {}", payload.get("cmd") or payload)
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                raise RuntimeError("WeCom websocket closed during authentication")

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: dict[str, Any]) -> str:
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Failed to parse WeCom payload: {!r}", raw)
            return None
        return payload if isinstance(payload, dict) else None

    async def _send_frame(self, frame: dict[str, Any]):
        if not self.ws or getattr(self.ws, "closed", False):
            logger.info("WeCom send skipped because websocket is not connected")
            return False
        await self.ws.send_json(frame)
        return True

    async def handle_payload(self, payload: dict[str, Any]):
        cmd = str(payload.get("cmd") or "")
        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
            return
        if cmd not in {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}:
            return
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        source = source_from_wecom_body(body)
        text = extract_wecom_text(body)
        if source.chat_type == "group":
            text = text.lstrip()
            if text.startswith("@"):
                text = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
        if not text:
            return
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        reply_req_id = str(headers.get("req_id") or "")
        if is_identity_command(text):
            language = self.store.get_language(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
            await self.send_text(
                source.chat_id,
                format_identity_source(source, language),
                reply_req_id=reply_req_id,
                source=source,
            )
            return
        if not should_accept_session_source(source, text) or not authorize_session_source(source):
            logger.info("Unauthorized WeCom message ignored: chat_id={} user_id={}", source.chat_id, source.user_id)
            return
        await self.handle_text_message(
            source=source,
            text=text,
            reply_req_id=reply_req_id,
            message_id=str(body.get("msgid") or ""),
        )

    async def handle_text_message(
        self,
        *,
        source: SessionSource,
        text: str,
        reply_req_id: str = "",
        message_id: str = "",
    ):
        query_text = (text or "").strip()
        if is_identity_command(query_text):
            language = self.store.get_language(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
            await self.send_text(
                source.chat_id,
                format_identity_source(source, language),
                reply_req_id=reply_req_id,
                source=source,
            )
            return

        context = BotContext(source.platform, source.chat_id, source.user_id, self.store)
        control_result = await handle_control_command(
            query_text,
            context,
            self.copilot_factory,
            card_supported=False,
            source=source,
        )
        if control_result.handled:
            for index, content in enumerate(control_result.response_contents()):
                await self.send_text(
                    source.chat_id,
                    content,
                    reply_req_id=reply_req_id if index == 0 else "",
                    source=source,
                )
            return

        language = self.store.get_language(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
        active_state = context.registry.try_start(context)
        if active_state is None:
            await self.send_text(source.chat_id, build_busy_content(language), reply_req_id=reply_req_id, source=source)
            return

        session_enabled = self.store.is_session_enabled(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
        conversion_id = self.store.get(source.chat_id, source.user_id, platform=WECOM_PLATFORM) if session_enabled else ""
        selected_agent = self.store.get_agent(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
        timezone = self.store.get_timezone(source.chat_id, source.user_id, platform=WECOM_PLATFORM)
        notifier_task = asyncio.create_task(
            run_still_working_notifier(
                active_state,
                lambda content: self.send_text(source.chat_id, content, reply_req_id=reply_req_id, source=source),
                language=language,
            )
        )
        try:
            final_contents = await call_with_stream(
                query_text,
                self._ignore_stream_update,
                self.copilot_factory(),
                conversion_id,
                custom_agent_id=selected_agent.get("id", ""),
                language=language,
                timezone=timezone,
                active_state=active_state,
            )
            final_content = final_contents.get("content", "")
            current_conversion_id = final_contents.get("conversion_id", "")
            if current_conversion_id and session_enabled:
                self.store.set(source.chat_id, source.user_id, current_conversion_id, platform=WECOM_PLATFORM)
            if not final_content and final_contents.get("cancelled"):
                return
            if not final_content:
                final_content = build_no_message_content(language)
            await self.send_text(source.chat_id, final_content, reply_req_id=reply_req_id, source=source)
        except Exception as e:
            logger.exception("WeCom Copilot reply failed: {}", e)
            await self.send_text(
                source.chat_id,
                build_error_content(e, language, trace_id=message_id or reply_req_id),
                reply_req_id=reply_req_id,
                source=source,
            )
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
        reply_req_id: str = "",
        source: Optional[SessionSource] = None,
    ) -> bool:
        message_text = (content or "").strip() or build_no_message_content()
        body = {"msgtype": "markdown", "markdown": {"content": message_text[:MAX_WECOM_TEXT_LENGTH]}}
        if reply_req_id:
            frame = {"cmd": APP_CMD_RESPONSE, "headers": {"req_id": reply_req_id}, "body": body}
        else:
            body = {"chatid": chat_id, **body}
            frame = {"cmd": APP_CMD_SEND, "headers": {"req_id": self._new_req_id("send")}, "body": body}
        return bool(await self._send_frame(frame))


def run_wecom_bridge():  # pragma: no cover - thin process entrypoint
    asyncio.run(WeComBridge().start_forever())
