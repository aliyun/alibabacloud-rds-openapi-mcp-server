import os
import json
import logging
import asyncio
import argparse
import time
import inspect
import contextlib
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from urllib import error as urllib_error
from urllib import request as urllib_request

from loguru import logger
from dingtalk_stream import AckMessage
import dingtalk_stream

from typing import Any, Callable, Optional
try:
    from alibabacloud_dingtalk.robot_1_0 import (
        client as dingtalk_robot_client,
        models as dingtalk_robot_models,
    )
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    DINGTALK_ROBOT_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional DingTalk robot SDK fallback
    dingtalk_robot_client = None
    dingtalk_robot_models = None
    open_api_models = None
    tea_util_models = None
    DINGTALK_ROBOT_SDK_AVAILABLE = False
from core.rds_copilot import (
    RdsCopilot, 
    MessageEvent, 
    ToolCallStart, 
    ToolCallPending, 
    ToolCallEnd, 
    DocumentEvent
)
from core import bot_core
from core.bot_core import (
    BotContext,
    CopilotConversationStore as SharedCopilotConversationStore,
    SessionSource,
    authorize_session_source,
    build_busy_content,
    get_active_registry,
    handle_control_command,
    run_still_working_notifier,
    should_accept_session_source,
)


def define_options():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client_id",
        dest="client_id",
        default=os.getenv("DINGTALK_APP_CLIENT_ID"),
        help="app_key or suite_key from https://open-dev.digntalk.com",
    )
    options = parser.parse_args()
    options.client_secret = os.getenv("DINGTALK_APP_CLIENT_SECRET")
    if not options.client_id or not options.client_secret:
        parser.error(
            "缺少钉钉凭证：请在 .env 中配置 DINGTALK_APP_CLIENT_ID 和 "
            "DINGTALK_APP_CLIENT_SECRET，或在启动前 export。"
        )
    return options


def convert_json_values_to_string(obj: dict) -> dict:
    """将字典中的非字符串值转换为JSON字符串，用于卡片数据更新"""
    result = {}
    for key, value in obj.items():
        if isinstance(value, str):
            result[key] = value
        else:
            result[key] = json.dumps(value, ensure_ascii=False)
    return result


def build_error_card_content(error: Exception, language: str = bot_core.DEFAULT_LANGUAGE) -> str:
    """Build user-visible localized error content for failed Copilot calls."""
    return bot_core.build_error_content(error, language=language)


def build_no_message_card_content(language: str = bot_core.DEFAULT_LANGUAGE) -> str:
    """Build user-visible localized fallback content when Copilot returns no message."""
    return bot_core.build_no_message_content(language=language)


CONVERSATION_STORE_FILE_ENV = "RDS_COPILOT_CONVERSATION_STORE_FILE"
DEFAULT_CONVERSATION_STORE_FILE = "copilot_conversations.json"
SESSION_ON_COMMAND = "/session on"
SESSION_OFF_COMMAND = "/session off"
CARD_ON_COMMAND = "/card on"
CARD_OFF_COMMAND = "/card off"
MAX_PLAIN_MESSAGE_LENGTH = 20000


def parse_session_command(text: str) -> str:
    """Only match exact session commands so normal Copilot questions are not intercepted."""
    return bot_core.parse_session_command(text)


def parse_card_command(text: str) -> str:
    """Only match exact card commands so normal Copilot questions are not intercepted."""
    return bot_core.parse_card_command(text)


def is_new_conversation_command(text: str) -> bool:
    return bot_core.is_new_conversation_command(text)


def get_conversation_store_file_path() -> str:
    return os.getenv(
        CONVERSATION_STORE_FILE_ENV,
        os.path.join(os.getcwd(), DEFAULT_CONVERSATION_STORE_FILE),
    )


class JsonCopilotConversationStore(SharedCopilotConversationStore):
    """Backward-compatible DingTalk store alias backed by the shared locked store."""
    pass


def get_copilot_conversation_store() -> JsonCopilotConversationStore:
    return JsonCopilotConversationStore(get_conversation_store_file_path())


def extract_session_webhook(callback_data: Any, incoming_message: Any = None) -> str:
    """Extract DingTalk sessionWebhook from the SDK message or raw callback payload."""
    for attr_name in ("session_webhook", "sessionWebhook"):
        value = getattr(incoming_message, attr_name, "") if incoming_message else ""
        if isinstance(value, str) and value.strip():
            return value.strip()

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("sessionWebhook", "session_webhook") and isinstance(child, str):
                    return child.strip()
                result = walk(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = walk(child)
                if result:
                    return result
        return ""

    return walk(callback_data)


def _post_dingtalk_session_webhook(session_webhook: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        session_webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=15) as response:
        body = response.read(1024).decode("utf-8", errors="replace")
        status_code = getattr(response, "status", response.getcode())
    if status_code >= 300:
        raise RuntimeError(f"DingTalk sessionWebhook HTTP {status_code}: {body[:200]}")
    return body


async def send_dingtalk_session_webhook(
    session_webhook: str,
    content: str,
    *,
    trace_id: str = "",
):
    """Send a normal DingTalk markdown message when card mode is disabled."""
    if not session_webhook:
        raise ValueError("sessionWebhook is empty")

    message_text = (content or "").strip() or build_no_message_card_content()
    if len(message_text) > MAX_PLAIN_MESSAGE_LENGTH:
        message_text = message_text[:MAX_PLAIN_MESSAGE_LENGTH] + "\n\n...(truncated)"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "RDS AI",
            "text": message_text,
        },
    }
    try:
        await asyncio.to_thread(_post_dingtalk_session_webhook, session_webhook, payload)
        logger.info(
            f"[trace_id={trace_id}] plain message sent by sessionWebhook, "
            f"content_length={len(message_text)}"
        )
        return True
    except urllib_error.URLError as e:
        logger.warning(f"[trace_id={trace_id}] sessionWebhook send failed: {e}")
        raise


# 线程池：在异步循环外执行同步阻塞的 RDS HTTP 请求，避免 [Errno 9] Bad file descriptor
_chat_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rds_chat")
_DINGTALK_ROBOT_CLIENT = None
THINKING_EMOTION = "🤔Thinking"
DONE_EMOTION = "🥳Done"


def get_dingtalk_robot_client():
    global _DINGTALK_ROBOT_CLIENT
    if not DINGTALK_ROBOT_SDK_AVAILABLE:
        return None
    if _DINGTALK_ROBOT_CLIENT is None:
        sdk_config = open_api_models.Config()
        sdk_config.protocol = "https"
        sdk_config.region_id = "central"
        _DINGTALK_ROBOT_CLIENT = dingtalk_robot_client.Client(sdk_config)
    return _DINGTALK_ROBOT_CLIENT


async def get_dingtalk_access_token(self: dingtalk_stream.ChatbotHandler) -> str:
    dingtalk_client = getattr(self, "dingtalk_client", None)
    get_access_token = getattr(dingtalk_client, "get_access_token", None)
    if not callable(get_access_token):
        return ""

    token = get_access_token()
    if inspect.isawaitable(token):
        token = await token
    return token or ""


async def send_dingtalk_emotion(
    self: dingtalk_stream.ChatbotHandler,
    incoming_message: dingtalk_stream.ChatbotMessage,
    emoji_name: str,
    *,
    recall: bool = False,
    trace_id: str = "",
) -> bool:
    """Add or recall a DingTalk reaction. Failures are logged and never block replies."""
    trace = trace_id or getattr(incoming_message, "message_id", "") or "no-trace"
    robot_sdk = get_dingtalk_robot_client()
    if not robot_sdk:
        logger.warning(f"[trace_id={trace}] DingTalk robot SDK unavailable, skip emotion")
        return False

    open_msg_id = getattr(incoming_message, "message_id", "") or ""
    open_conversation_id = getattr(incoming_message, "conversation_id", "") or ""
    if not open_msg_id or not open_conversation_id:
        logger.warning(f"[trace_id={trace}] missing message_id or conversation_id, skip emotion")
        return False

    try:
        token = await get_dingtalk_access_token(self)
        if not token:
            logger.warning(f"[trace_id={trace}] missing DingTalk access token, skip emotion")
            return False

        robot_code = (
            getattr(incoming_message, "robot_code", "")
            or getattr(incoming_message, "robotCode", "")
            or os.getenv("DINGTALK_ROBOT_CODE")
            or os.getenv("DINGTALK_APP_CLIENT_ID")
            or ""
        )
        emotion_kwargs = {
            "robot_code": robot_code,
            "open_msg_id": open_msg_id,
            "open_conversation_id": open_conversation_id,
            "emotion_type": 2,
            "emotion_name": emoji_name,
        }
        runtime = tea_util_models.RuntimeOptions()

        if recall:
            emotion_kwargs["text_emotion"] = (
                dingtalk_robot_models.RobotRecallEmotionRequestTextEmotion(
                    emotion_id="2659900",
                    emotion_name=emoji_name,
                    text=emoji_name,
                    background_id="im_bg_1",
                )
            )
            request = dingtalk_robot_models.RobotRecallEmotionRequest(**emotion_kwargs)
            sdk_headers = dingtalk_robot_models.RobotRecallEmotionHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await robot_sdk.robot_recall_emotion_with_options_async(request, sdk_headers, runtime)
        else:
            emotion_kwargs["text_emotion"] = (
                dingtalk_robot_models.RobotReplyEmotionRequestTextEmotion(
                    emotion_id="2659900",
                    emotion_name=emoji_name,
                    text=emoji_name,
                    background_id="im_bg_1",
                )
            )
            request = dingtalk_robot_models.RobotReplyEmotionRequest(**emotion_kwargs)
            sdk_headers = dingtalk_robot_models.RobotReplyEmotionHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await robot_sdk.robot_reply_emotion_with_options_async(request, sdk_headers, runtime)

        action = "recall" if recall else "reply"
        logger.info(
            f"[trace_id={trace}] DingTalk emotion {action} success, "
            f"emoji={emoji_name}, message_id={open_msg_id}"
        )
        return True
    except Exception as e:
        action = "recall" if recall else "reply"
        logger.warning(
            f"[trace_id={trace}] DingTalk emotion {action} failed, "
            f"emoji={emoji_name}, error={e}"
        )
        return False


class DingTalkEmotionSwitcher:
    """Keep one reaction state per reply: start with Thinking, then recall it and add Done."""

    def __init__(self, handler: dingtalk_stream.ChatbotHandler, incoming_message: dingtalk_stream.ChatbotMessage):
        self.handler = handler
        self.incoming_message = incoming_message
        self.current_emoji = ""
        self.trace_id = incoming_message.message_id or f"trace-{int(time.time() * 1000)}"

    async def set_state(self, emoji_name: str):
        if not emoji_name or emoji_name == self.current_emoji:
            return

        previous_emoji = self.current_emoji
        if previous_emoji:
            await send_dingtalk_emotion(
                self.handler,
                self.incoming_message,
                previous_emoji,
                recall=True,
                trace_id=self.trace_id,
            )

        await send_dingtalk_emotion(
            self.handler,
            self.incoming_message,
            emoji_name,
            trace_id=self.trace_id,
        )
        self.current_emoji = emoji_name

    async def finish(self):
        if self.current_emoji:
            await send_dingtalk_emotion(
                self.handler,
                self.incoming_message,
                self.current_emoji,
                recall=True,
                trace_id=self.trace_id,
            )
            self.current_emoji = ""

        await send_dingtalk_emotion(
            self.handler,
            self.incoming_message,
            DONE_EMOTION,
            trace_id=self.trace_id,
        )


async def handle_reply_with_emotions(
    self: dingtalk_stream.ChatbotHandler,
    incoming_message: dingtalk_stream.ChatbotMessage,
    reply_coro,
    emotion_switcher: Optional[DingTalkEmotionSwitcher] = None,
):
    switcher = emotion_switcher or DingTalkEmotionSwitcher(self, incoming_message)
    await switcher.set_state(THINKING_EMOTION)
    try:
        return await reply_coro
    finally:
        await switcher.finish()


async def call_with_stream(
    request_content: str,
    update_card_callback: Callable,
    rds_copilot: RdsCopilot,
    conversion_id: str = '',
    *,
    custom_agent_id: str = "",
    language: str = bot_core.DEFAULT_LANGUAGE,
    timezone: str = bot_core.DEFAULT_TIMEZONE,
    active_state=None,
):
    """Compatibility wrapper around the shared stream runner."""
    return await bot_core.call_with_stream(
        request_content,
        update_card_callback,
        rds_copilot,
        conversion_id,
        custom_agent_id=custom_agent_id,
        language=language,
        timezone=timezone,
        active_state=active_state,
    )


async def handle_reply_plain_message(
    self: dingtalk_stream.ChatbotHandler,
    incoming_message: dingtalk_stream.ChatbotMessage,
    callback_data: Any,
    conversion_id: str = "",
    session_enabled: bool = False,
    custom_agent_id: str = "",
    language: str = bot_core.DEFAULT_LANGUAGE,
    timezone: str = bot_core.DEFAULT_TIMEZONE,
    active_state=None,
):
    """Consume the Copilot stream without creating a card, then send the final text as a normal message."""
    trace_id = incoming_message.message_id or f"trace-{int(time.time() * 1000)}"
    final_contents = {"content": "", "conversion_id": ""}
    final_display_content = ""

    async def update_plain_callback(update_data: dict):
        return None

    try:
        rds_copilot = RdsCopilot()
        final_contents = await call_with_stream(
            incoming_message.text.content,
            update_plain_callback,
            rds_copilot,
            conversion_id,
            custom_agent_id=custom_agent_id,
            language=language,
            timezone=timezone,
            active_state=active_state,
        )

        current_conversion_id = final_contents.get("conversion_id", "")
        if current_conversion_id and session_enabled:
            get_copilot_conversation_store().set(
                incoming_message.conversation_id,
                incoming_message.sender_id,
                current_conversion_id,
            )

        final_display_content = final_contents.get("content", "")
        if not final_display_content and final_contents.get("cancelled"):
            self.logger.info(f"[trace_id={trace_id}] Copilot task was stopped; skip empty fallback reply")
            return final_contents
        if not final_display_content:
            final_display_content = build_no_message_card_content(language)
            self.logger.warning(
                f"[trace_id={trace_id}] no message returned from RDS AI in plain mode"
            )
    except Exception as e:
        final_display_content = build_error_card_content(e, language)
        self.logger.exception(f"[trace_id={trace_id}] handle plain reply failed: {e}")

    session_webhook = extract_session_webhook(callback_data, incoming_message)
    try:
        if session_webhook:
            await send_dingtalk_session_webhook(
                session_webhook,
                final_display_content,
                trace_id=trace_id,
            )
        else:
            self.logger.warning(f"[trace_id={trace_id}] sessionWebhook missing, fallback to reply_text")
            self.reply_text(final_display_content, incoming_message)
    except Exception as e:
        self.logger.exception(f"[trace_id={trace_id}] send plain reply failed: {e}")
        self.reply_text(final_display_content, incoming_message)

    return final_contents


async def handle_reply_and_update_card(
    self: dingtalk_stream.ChatbotHandler,
    incoming_message: dingtalk_stream.ChatbotMessage,
    conversion_id: str = '',
    custom_agent_id: str = "",
    language: str = bot_core.DEFAULT_LANGUAGE,
    timezone: str = bot_core.DEFAULT_TIMEZONE,
    active_state=None,
):
    # 卡片模板 ID
    card_template_id = "b22243cf-3171-4097-8c2c-43c3706ef8af.schema"
    
    # 初始化卡片数据，包含所有可能更新的变量
    card_data = {
        "content": "",
        "query": incoming_message.text.content,
        "preparations": [],
        "charts": [],
        "config": {"autoLayout": True},
    }
    card_instance = dingtalk_stream.AICardReplier(
        self.dingtalk_client, incoming_message
    )
    # 先投放卡片: https://open.dingtalk.com/document/orgapp/create-and-deliver-cards
    card_instance_id = await card_instance.async_create_and_deliver_card(
        card_template_id, convert_json_values_to_string(card_data)
    )

    # 流式更新卡片的回调函数
    async def update_card_callback(update_data: dict):
        """更新卡片数据
        
        Args:
            update_data: 要更新的卡片数据字典，例如 {"content": "...", "preparations": [...]}
        """
        # 使用 async_put_card_data 更新卡片数据
        cardUpdateOptions = {
            "updateCardDataByKey": True,
            "updatePrivateDataByKey": True,
        }
        return await card_instance.async_put_card_data(
            card_instance_id,
            card_data=convert_json_values_to_string(update_data),
            cardUpdateOptions=cardUpdateOptions,
        )

    final_contents = {"content": "", "conversion_id": ""}
    final_display_content = ""
    final_card_finalized = False
    try:
        # 初始化 RdsCopilot 放在 try 内，确保初始化失败时也能结束卡片并展示错误。
        rds_copilot = RdsCopilot()

        # 先设置输入中状态
        await card_instance.async_streaming(
            card_instance_id,
            content_key="content",
            content_value="",
            append=False,
            finished=False,
            failed=False,
        )
        
        # 处理流式响应并更新卡片
        final_contents = await call_with_stream(
            incoming_message.text.content,
            update_card_callback,
            rds_copilot,
            conversion_id,
            custom_agent_id=custom_agent_id,
            language=language,
            timezone=timezone,
            active_state=active_state,
        )

        final_display_content = final_contents.get("content", "")
        if not final_display_content and final_contents.get("cancelled"):
            self.logger.info("RDS AI task was stopped; skip no-message card fallback.")
        if not final_display_content:
            if not final_contents.get("cancelled"):
                final_display_content = build_no_message_card_content(language)
                self.logger.warning("RDS AI finished without response content.")
    except Exception as e:
        final_display_content = build_error_card_content(e, language)
        self.logger.exception(f"Failed to handle RDS AI card response: {e}")
        try:
            await update_card_callback({"content": final_display_content, "preparations": []})
        except Exception as update_error:
            self.logger.exception(f"Failed to push error content to DingTalk card: {update_error}")
    finally:
        try:
            # Always finish the AI card stream. Keep failed=False so visible fallback text is rendered by DingTalk clients.
            await card_instance.async_streaming(
                card_instance_id,
                content_key="content",
                content_value=final_display_content,
                append=False,
                finished=True,
                failed=False,
            )
            final_card_finalized = True
        except Exception as finalize_error:
            self.logger.exception(f"Failed to finalize DingTalk AI card: {finalize_error}")
        self.logger.info(
            f"DingTalk AI card finalized, finalized={final_card_finalized}, "
            f"content_length={len(final_display_content)}"
        )
    
    # 返回最终的 conversion_id
    return final_contents.get("conversion_id", "")


class CardBotHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, logger: logging.Logger = logger):
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        if logger:
            self.logger = logger

    def reply_command_content(self, content: str, incoming_message):
        if getattr(incoming_message, "session_webhook", "") and hasattr(self, "reply_markdown"):
            try:
                result = self.reply_markdown("RDS Copilot", content, incoming_message)
                if result is not None:
                    return result
            except Exception:
                self.logger.warning("DingTalk markdown command reply failed", exc_info=True)
        return self.reply_text(content, incoming_message)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        self.logger.info(f"收到消息：{incoming_message}")

        if incoming_message.message_type != "text":
            self.reply_text("I can only process text messages.", incoming_message)
            return AckMessage.STATUS_OK, "OK"

        store = get_copilot_conversation_store()
        dingtalk_conversation_id = incoming_message.conversation_id
        sender_id = incoming_message.sender_id
        query_text = incoming_message.text.content.strip()
        source = SessionSource(
            platform="dingtalk",
            chat_id=dingtalk_conversation_id or sender_id,
            chat_type="group" if dingtalk_conversation_id and dingtalk_conversation_id != sender_id else "dm",
            user_id=sender_id,
            user_name=getattr(incoming_message, "sender_nick", "") or getattr(incoming_message, "sender_name", "") or "",
            user_id_alt=getattr(incoming_message, "sender_staff_id", "") or "",
            is_bot=False,
        )
        if not should_accept_session_source(source, query_text) or not authorize_session_source(source):
            self.logger.info(
                "Unauthorized DingTalk message ignored: chat_id=%s sender_id=%s",
                source.chat_id,
                source.user_id,
            )
            return AckMessage.STATUS_OK, "OK"
        context = BotContext("dingtalk", dingtalk_conversation_id, sender_id, store)

        control_result = await handle_control_command(query_text, context, card_supported=True)
        if control_result.handled:
            self.reply_command_content(control_result.content, incoming_message)
            return AckMessage.STATUS_OK, "OK"

        active_state = context.registry.try_start(context)
        if active_state is None:
            language = store.get_language(dingtalk_conversation_id, sender_id)
            self.reply_text(build_busy_content(language), incoming_message)
            return AckMessage.STATUS_OK, "OK"

        session_enabled = store.is_session_enabled(dingtalk_conversation_id, sender_id)
        card_enabled = store.is_card_enabled(dingtalk_conversation_id, sender_id)
        selected_agent = store.get_agent(dingtalk_conversation_id, sender_id)
        custom_agent_id = selected_agent.get("id", "")
        language = store.get_language(dingtalk_conversation_id, sender_id)
        timezone = store.get_timezone(dingtalk_conversation_id, sender_id)
        # 默认保持多轮上下文；用户可以通过 /session off 为当前会话和发送人关闭。
        conversion_id = store.get(dingtalk_conversation_id, sender_id) if session_enabled else ""

        async def send_status(content: str):
            session_webhook = extract_session_webhook(callback.data, incoming_message)
            if session_webhook:
                await send_dingtalk_session_webhook(
                    session_webhook,
                    content,
                    trace_id=incoming_message.message_id or "",
                )
            else:
                self.reply_text(content, incoming_message)
        
        # 创建异步任务处理回复
        async def handle_and_update_conversation():
            notifier_task = asyncio.create_task(run_still_working_notifier(active_state, send_status, language=language))
            try:
                final_conversion_id = await handle_reply_and_update_card(
                    self,
                    incoming_message,
                    conversion_id,
                    custom_agent_id=custom_agent_id,
                    language=language,
                    timezone=timezone,
                    active_state=active_state,
                )
                # 按“钉钉会话 + 发送人”持久化 ConversationId，服务重启后仍可保持上下文。
                if (
                    final_conversion_id
                    and session_enabled
                    and store.is_session_enabled(dingtalk_conversation_id, sender_id)
                ):
                    store.set(dingtalk_conversation_id, sender_id, final_conversion_id)
            finally:
                notifier_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notifier_task
                context.registry.finish(active_state)

        async def handle_plain_and_finish():
            notifier_task = asyncio.create_task(run_still_working_notifier(active_state, send_status, language=language))
            try:
                return await handle_reply_plain_message(
                    self,
                    incoming_message,
                    callback.data,
                    conversion_id=conversion_id,
                    session_enabled=session_enabled,
                    custom_agent_id=custom_agent_id,
                    language=language,
                    timezone=timezone,
                    active_state=active_state,
                )
            finally:
                notifier_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notifier_task
                context.registry.finish(active_state)

        emotion_switcher = DingTalkEmotionSwitcher(self, incoming_message)
        if card_enabled:
            asyncio.create_task(
                handle_reply_with_emotions(
                    self,
                    incoming_message,
                    handle_and_update_conversation(),
                    emotion_switcher=emotion_switcher,
                )
            )
        else:
            asyncio.create_task(
                handle_reply_with_emotions(
                    self,
                    incoming_message,
                    handle_plain_and_finish(),
                    emotion_switcher=emotion_switcher,
                )
            )
        return AckMessage.STATUS_OK, "OK"


def run_dingtalk_bridge():
    options = define_options()

    client = build_dingtalk_stream_client(options)
    client.start_forever()


def build_dingtalk_stream_client(options):
    credential = dingtalk_stream.Credential(options.client_id, options.client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    for logger_name in ("dingtalk_stream", "dingtalk_stream.client"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, CardBotHandler()
    )
    return client


def validate_dingtalk_startup():
    options = define_options()
    client = build_dingtalk_stream_client(options)
    connection = client.open_connection()
    if not connection:
        raise RuntimeError(
            "钉钉 Stream 鉴权失败：请检查 DINGTALK_APP_CLIENT_ID、"
            "DINGTALK_APP_CLIENT_SECRET 和机器人 Stream 模式权限。"
        )
