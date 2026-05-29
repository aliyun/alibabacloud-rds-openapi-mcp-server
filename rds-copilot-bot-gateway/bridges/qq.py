import asyncio
import contextlib
import json
import os
import re
import time
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
    get_copilot_conversation_store,
    handle_control_command,
    run_still_working_notifier,
    should_accept_session_source,
)
from core.error_detail import http_error_detail
from core.rds_copilot import RdsCopilot

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency fallback
    aiohttp = None
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency fallback
    httpx = None
    HTTPX_AVAILABLE = False


QQ_PLATFORM = "qqbot"
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
MAX_QQ_TEXT_LENGTH = 4000
QQ_MSG_TYPE_TEXT = 0
QQ_MSG_TYPE_MARKDOWN = 2
QQ_INTENTS = (1 << 25) | (1 << 30) | (1 << 12)
DEFAULT_QQ_RECONNECT_BASE_SECONDS = 3.0
DEFAULT_QQ_RECONNECT_MAX_SECONDS = 60.0


def _read_bool_env(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def _read_positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _calculate_reconnect_delay(restart_index: int) -> float:
    base = _read_positive_float_env("QQ_RECONNECT_BASE_SECONDS", DEFAULT_QQ_RECONNECT_BASE_SECONDS)
    max_delay = _read_positive_float_env("QQ_RECONNECT_MAX_SECONDS", DEFAULT_QQ_RECONNECT_MAX_SECONDS)
    return min(max_delay, base * (2 ** max(0, restart_index)))


def validate_qq_startup():
    asyncio.run(QQBridge().ensure_access_token())


def strip_qq_mention(text: str) -> str:
    return re.sub(r"^\s*<@!?\w+>\s*", "", text or "").strip()


def source_and_text_from_qq_event(event_type: str, data: dict[str, Any]) -> tuple[SessionSource, str]:
    content = str(data.get("content") or "").strip()
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    if event_type == "C2C_MESSAGE_CREATE":
        user_openid = str(author.get("user_openid") or "").strip()
        return SessionSource(QQ_PLATFORM, user_openid, "dm", user_openid), content
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        group_openid = str(data.get("group_openid") or "").strip()
        member_openid = str(author.get("member_openid") or "").strip()
        return SessionSource(QQ_PLATFORM, group_openid, "group", member_openid), strip_qq_mention(content)
    if event_type in {"GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE"}:
        channel_id = str(data.get("channel_id") or "").strip()
        user_id = str(author.get("id") or "").strip()
        user_name = str(author.get("username") or "").strip()
        return SessionSource(QQ_PLATFORM, channel_id, "channel", user_id, user_name=user_name), content
    if event_type == "DIRECT_MESSAGE_CREATE":
        guild_id = str(data.get("guild_id") or "").strip()
        user_id = str(author.get("id") or "").strip()
        return SessionSource(QQ_PLATFORM, guild_id, "dm", user_id), content
    return SessionSource(QQ_PLATFORM, "", "dm", ""), content


class QQBridge:
    def __init__(
        self,
        *,
        app_id: str = "",
        client_secret: str = "",
        store: Optional[CopilotConversationStore] = None,
        copilot_factory: Callable[[], RdsCopilot] = RdsCopilot,
    ):
        self.app_id = app_id or os.getenv("QQ_APP_ID", "")
        self.client_secret = client_secret or os.getenv("QQ_CLIENT_SECRET", "")
        self.store = store or get_copilot_conversation_store()
        self.copilot_factory = copilot_factory
        self.http_client = None
        self.session = None
        self.ws = None
        self.access_token = ""
        self.token_expires_at = 0.0
        self._running = False
        self.sequence = None
        self.session_id = ""
        self._heartbeat_task = None
        self._heartbeat_ack_received = True
        self.tls_verify = _read_bool_env("QQ_HTTP_VERIFY", True)

    async def start_forever(self):  # pragma: no cover - real WebSocket loop is covered by integration smoke tests
        if not AIOHTTP_AVAILABLE or not HTTPX_AVAILABLE:
            raise RuntimeError("aiohttp and httpx are required; install requirements.txt first")
        if not self.app_id or not self.client_secret:
            raise RuntimeError(
                "缺少 QQ Bot 凭证：请在 .env 中配置 QQ_APP_ID 和 QQ_CLIENT_SECRET，"
                "或在启动前 export。"
            )

        self._running = True
        self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=self.tls_verify, trust_env=True)
        restart_index = 0
        try:
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
                        "QQ websocket 已断开，{:.1f}s 后自动重连，错误={}: {}",
                        delay,
                        e.__class__.__name__,
                        str(e),
                    )
                    await asyncio.sleep(delay)
        finally:
            if self.http_client:
                await self.http_client.aclose()
                self.http_client = None

    async def _connect_once(self):
        self.session = aiohttp.ClientSession()
        self._heartbeat_task = None
        try:
            gateway_url = await self.get_gateway_url()
            ws_kwargs = {} if self.tls_verify else {"ssl": False}
            self.ws = await self.session.ws_connect(gateway_url, **ws_kwargs)
            await self._receive_gateway_loop()
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
                self._heartbeat_task = None
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.session:
                await self.session.close()
            self.ws = None
            self.session = None

    async def _receive_gateway_loop(self):
        while self._running:
            receive_task = asyncio.create_task(self.ws.receive())
            tasks = {receive_task}
            if self._heartbeat_task:
                tasks.add(self._heartbeat_task)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if self._heartbeat_task and self._heartbeat_task in done:
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
                self._heartbeat_task.result()
                raise RuntimeError("QQ heartbeat loop stopped")

            message = receive_task.result()
            if message.type == aiohttp.WSMsgType.TEXT:
                await self.handle_gateway_payload(json.loads(message.data))
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                raise RuntimeError("QQ websocket closed")

    async def ensure_access_token(self) -> str:
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token
        data = await self._api_request(
            "POST",
            QQ_TOKEN_URL,
            {"appId": self.app_id, "clientSecret": self.client_secret},
        )
        self.access_token = str(data.get("access_token") or data.get("accessToken") or "")
        expires_in = int(data.get("expires_in") or data.get("expiresIn") or 7200)
        self.token_expires_at = time.time() + expires_in
        if not self.access_token:
            raise RuntimeError("QQ access token response is missing access_token")
        return self.access_token

    async def get_gateway_url(self) -> str:
        await self.ensure_access_token()
        data = await self._api_request("GET", "/gateway")
        gateway_url = str(data.get("url") or "").strip()
        if not gateway_url:
            raise RuntimeError("QQ gateway response is missing url")
        return gateway_url

    async def _api_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:  # pragma: no cover - external QQ HTTP API
        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx is not installed")
        close_client = False
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=self.tls_verify, trust_env=True)
            close_client = True
        try:
            url = path if path.startswith("http") else f"{QQ_API_BASE}{path}"
            headers = {}
            if path != QQ_TOKEN_URL:
                token = await self.ensure_access_token() if not self.access_token else self.access_token
                headers["Authorization"] = f"QQBot {token}"
            response = await self.http_client.request(method, url, json=body, headers=headers)
            try:
                response.raise_for_status()
            except Exception as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if path == QQ_TOKEN_URL and status_code in (401, 403):
                    detail = http_error_detail(e)
                    suffix = f" QQ/网络返回：{detail}" if detail else ""
                    raise RuntimeError(
                        "QQ Bot 鉴权失败：获取 access token 被拒绝。"
                        "请检查 QQ_APP_ID 和 QQ_CLIENT_SECRET 是否正确，"
                        "并确认当前网络允许访问 bots.qq.com。"
                        f"{suffix}"
                    ) from e
                raise
            data = response.json()
            return data if isinstance(data, dict) else {}
        finally:
            if close_client and self.http_client is not None:
                await self.http_client.aclose()
                self.http_client = None

    async def handle_gateway_payload(self, payload: dict[str, Any]):
        op = payload.get("op")
        event_type = str(payload.get("t") or "")
        data = payload.get("d")
        if op == 10 and self.ws:
            interval_ms = 45000
            if isinstance(data, dict):
                interval_ms = int(data.get("heartbeat_interval") or interval_ms)
            await self._send_gateway_auth()
            self._start_heartbeat(interval_ms)
            return
        if op == 1:
            await self._send_heartbeat()
            return
        if op == 7:
            raise RuntimeError("QQ gateway requested reconnect")
        if op == 9:
            self.session_id = ""
            self.sequence = None
            raise RuntimeError("QQ gateway returned invalid session")
        if op == 11:
            self._heartbeat_ack_received = True
            return
        if op == 0 and payload.get("s") is not None:
            self.sequence = payload.get("s")
        if op == 0 and event_type == "RESUMED":
            return
        if op == 0 and isinstance(data, dict) and event_type in {
            "READY",
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "DIRECT_MESSAGE_CREATE",
            "GUILD_MESSAGE_CREATE",
            "GUILD_AT_MESSAGE_CREATE",
        }:
            if event_type == "READY":
                self.session_id = str(data.get("session_id") or "")
                return
            await self.handle_event(event_type, data)

    async def _send_gateway_auth(self):
        if self.session_id and self.sequence is not None:
            await self._send_resume()
            return
        await self._send_identify()

    async def _send_identify(self):
        token = await self.ensure_access_token()
        await self.ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": QQ_INTENTS,
                    "shard": [0, 1],
                    "properties": {"$os": "macOS", "$browser": "rds-copilot", "$device": "rds-copilot"},
                },
            }
        )

    async def _send_resume(self):
        token = await self.ensure_access_token()
        await self.ws.send_json(
            {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self.session_id,
                    "seq": self.sequence,
                },
            }
        )

    def _start_heartbeat(self, interval_ms: int):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        self._heartbeat_ack_received = True
        interval_seconds = max(interval_ms / 1000.0, 1.0)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval_seconds))

    async def _send_heartbeat(self):
        if not self.ws or getattr(self.ws, "closed", False):
            raise RuntimeError("QQ websocket is not connected")
        self._heartbeat_ack_received = False
        await self.ws.send_json({"op": 1, "d": self.sequence})

    async def _heartbeat_loop(self, interval_seconds: float):
        while self._running:
            await asyncio.sleep(max(interval_seconds, 0))
            if not self._heartbeat_ack_received:
                raise RuntimeError("QQ heartbeat ack timeout")
            await self._send_heartbeat()

    async def handle_event(self, event_type: str, data: dict[str, Any]):
        source, text = source_and_text_from_qq_event(event_type, data)
        if not source.chat_id or not source.user_id or not text:
            return
        if not should_accept_session_source(source, text) or not authorize_session_source(source):
            logger.info("Unauthorized QQ message ignored: chat_id=%s user_id=%s", source.chat_id, source.user_id)
            return
        await self.handle_text_message(source=source, text=text)

    async def handle_text_message(self, *, source: SessionSource, text: str):
        query_text = (text or "").strip()
        context = BotContext(source.platform, source.chat_id, source.user_id, self.store)
        control_result = await handle_control_command(query_text, context, self.copilot_factory, card_supported=False)
        if control_result.handled:
            for content in control_result.response_contents():
                await self.send_text(source, content)
            return

        language = self.store.get_language(source.chat_id, source.user_id, platform=QQ_PLATFORM)
        active_state = context.registry.try_start(context)
        if active_state is None:
            await self.send_text(source, build_busy_content(language))
            return

        session_enabled = self.store.is_session_enabled(source.chat_id, source.user_id, platform=QQ_PLATFORM)
        conversion_id = self.store.get(source.chat_id, source.user_id, platform=QQ_PLATFORM) if session_enabled else ""
        selected_agent = self.store.get_agent(source.chat_id, source.user_id, platform=QQ_PLATFORM)
        timezone = self.store.get_timezone(source.chat_id, source.user_id, platform=QQ_PLATFORM)
        notifier_task = asyncio.create_task(
            run_still_working_notifier(active_state, lambda content: self.send_text(source, content), language=language)
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
                self.store.set(source.chat_id, source.user_id, current_conversion_id, platform=QQ_PLATFORM)
            if not final_content and final_contents.get("cancelled"):
                return
            if not final_content:
                final_content = build_no_message_content(language)
            await self.send_text(source, final_content)
        except Exception as e:
            logger.exception("QQ Copilot reply failed: {}", e)
            await self.send_text(source, build_error_content(e, language))
        finally:
            notifier_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notifier_task
            context.registry.finish(active_state)

    async def _ignore_stream_update(self, update_data: dict):
        return None

    async def send_text(self, source: SessionSource, content: str) -> bool:
        message_text = ((content or "").strip() or build_no_message_content())[:MAX_QQ_TEXT_LENGTH]
        body = {"markdown": {"content": message_text}, "msg_type": QQ_MSG_TYPE_MARKDOWN}
        if source.chat_type == "dm" and source.chat_id == source.user_id:
            path = f"/v2/users/{source.chat_id}/messages"
        elif source.chat_type == "dm":
            path = f"/channels/{source.chat_id}/messages"
        elif source.chat_type == "channel":
            path = f"/channels/{source.chat_id}/messages"
        else:
            path = f"/v2/groups/{source.chat_id}/messages"
        await self._api_request("POST", path, body)
        return True


def run_qq_bridge():  # pragma: no cover - thin process entrypoint
    asyncio.run(QQBridge().start_forever())
