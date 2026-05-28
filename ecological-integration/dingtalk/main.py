import os
import json
import logging
import asyncio
import argparse
import time
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from dingtalk_stream import AckMessage
import dingtalk_stream

from typing import Callable
from rds_copilot import (
    RdsCopilot, 
    MessageEvent, 
    ToolCallStart, 
    ToolCallPending, 
    ToolCallEnd, 
    DocumentEvent
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
        parser.error("DINGTALK_APP_CLIENT_ID and DINGTALK_APP_CLIENT_SECRET must be set in environment variables.")
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


def build_error_card_content(error: Exception) -> str:
    """Build user-visible English error content for failed Copilot calls."""
    error_type = error.__class__.__name__
    error_message = str(error).strip()
    error_detail = f"{error_type}: {error_message}" if error_message else error_type
    if len(error_detail) > 500:
        error_detail = f"{error_detail[:500]}..."
    return (
        "RDS AI diagnosis failed and could not generate a complete response.\n\n"
        f"Error: {error_detail}\n\n"
        "Please try again later or contact the service maintainer to check the logs."
    )


def build_no_message_card_content() -> str:
    """Build user-visible English fallback content when Copilot returns no message."""
    return (
        "RDS AI finished, but no response content was returned.\n\n"
        "Please try again later or contact the service maintainer to check the logs."
    )


CONVERSATION_STORE_FILE_ENV = "RDS_COPILOT_CONVERSATION_STORE_FILE"
DEFAULT_CONVERSATION_STORE_FILE = "copilot_conversations.json"
SESSION_ON_COMMAND = "/session on"
SESSION_OFF_COMMAND = "/session off"


def parse_session_command(text: str) -> str:
    """Only match exact session commands so normal Copilot questions are not intercepted."""
    normalized_text = (text or "").strip()
    if normalized_text == SESSION_ON_COMMAND:
        return "on"
    if normalized_text == SESSION_OFF_COMMAND:
        return "off"
    return ""


def is_new_conversation_command(text: str) -> bool:
    return (text or "").strip().lower() == "/new"


def get_conversation_store_file_path() -> str:
    return os.getenv(
        CONVERSATION_STORE_FILE_ENV,
        os.path.join(os.getcwd(), DEFAULT_CONVERSATION_STORE_FILE),
    )


class JsonCopilotConversationStore:
    """Persist Copilot ConversationId by DingTalk conversation and sender."""

    def __init__(self, file_path: str):
        self.file_path = file_path

    @staticmethod
    def _key(dingtalk_conversation_id: str, sender_id: str) -> str:
        if not dingtalk_conversation_id or not sender_id:
            return ""
        return json.dumps(
            [dingtalk_conversation_id, sender_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _empty_data(self) -> dict:
        return {"version": 1, "conversations": {}}

    def _load(self) -> dict:
        if not self.file_path or not os.path.exists(self.file_path):
            return self._empty_data()

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load Copilot conversation store: {e}")
            return self._empty_data()

        if not isinstance(data, dict) or not isinstance(data.get("conversations"), dict):
            return self._empty_data()
        return data

    def _save(self, data: dict):
        store_dir = os.path.dirname(os.path.abspath(self.file_path))
        os.makedirs(store_dir, exist_ok=True)
        tmp_file_path = f"{self.file_path}.tmp.{os.getpid()}"
        with open(tmp_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_file_path, self.file_path)

    def get(self, dingtalk_conversation_id: str, sender_id: str) -> str:
        key = self._key(dingtalk_conversation_id, sender_id)
        if not key:
            return ""

        item = self._load().get("conversations", {}).get(key, {})
        if not isinstance(item, dict):
            return ""
        return item.get("copilot_conversation_id") or ""

    def is_session_enabled(self, dingtalk_conversation_id: str, sender_id: str) -> bool:
        key = self._key(dingtalk_conversation_id, sender_id)
        if not key:
            return True

        item = self._load().get("conversations", {}).get(key)
        if not isinstance(item, dict) or "session_enabled" not in item:
            return True
        return item.get("session_enabled") is True

    def set_session_enabled(self, dingtalk_conversation_id: str, sender_id: str, enabled: bool):
        key = self._key(dingtalk_conversation_id, sender_id)
        if not key:
            return

        data = self._load()
        conversations = data.setdefault("conversations", {})
        item = conversations.get(key, {})
        if not isinstance(item, dict):
            item = {}

        item.update(
            {
                "dingtalk_conversation_id": dingtalk_conversation_id,
                "sender_id": sender_id,
                "session_enabled": bool(enabled),
                "updated_at": int(time.time()),
            }
        )
        if not enabled:
            item["copilot_conversation_id"] = ""

        conversations[key] = item
        self._save(data)

    def set(self, dingtalk_conversation_id: str, sender_id: str, copilot_conversation_id: str):
        key = self._key(dingtalk_conversation_id, sender_id)
        if not key or not copilot_conversation_id:
            return

        data = self._load()
        conversations = data.setdefault("conversations", {})
        item = conversations.get(key, {})
        if not isinstance(item, dict):
            item = {}
        item.update(
            {
                "copilot_conversation_id": copilot_conversation_id,
                "dingtalk_conversation_id": dingtalk_conversation_id,
                "sender_id": sender_id,
                "session_enabled": item.get("session_enabled", True) is True,
                "updated_at": int(time.time()),
            }
        )
        conversations[key] = item
        self._save(data)

    def clear(self, dingtalk_conversation_id: str, sender_id: str):
        key = self._key(dingtalk_conversation_id, sender_id)
        if not key:
            return

        data = self._load()
        conversations = data.setdefault("conversations", {})
        item = conversations.get(key, {})
        if not isinstance(item, dict):
            item = {
                "dingtalk_conversation_id": dingtalk_conversation_id,
                "sender_id": sender_id,
                "session_enabled": True,
            }
        item["copilot_conversation_id"] = ""
        item["updated_at"] = int(time.time())
        conversations[key] = item
        self._save(data)


def get_copilot_conversation_store() -> JsonCopilotConversationStore:
    return JsonCopilotConversationStore(get_conversation_store_file_path())


# 线程池：在异步循环外执行同步阻塞的 RDS HTTP 请求，避免 [Errno 9] Bad file descriptor
_chat_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rds_chat")


async def call_with_stream(
    request_content: str,
    update_card_callback: Callable,
    rds_copilot: RdsCopilot,
    conversion_id: str = '',
):
    """处理流式响应（EventMode=separate）：message 流式更新 content，tool_call 只更新 preparations。
    rds_copilot.chat 为同步阻塞调用，在线程池中执行，避免阻塞 asyncio 导致连接异常。
    
    Args:
        request_content: 用户查询文本
        update_card_callback: 更新卡片的回调函数
        rds_copilot: RDS Copilot 实例
        conversion_id: 对话 ID，用于保持上下文
        
    Returns:
        dict: 包含 content、preparations 和 conversion_id 的字典
    """
    full_content = ""
    preparations = []
    seen_tool_call_ids = set()  # 相同 tool_call_id 只往卡片推送一次
    event_queue = Queue()
    final_conversion_id = conversion_id

    def run_chat_in_thread():
        nonlocal final_conversion_id
        try:
            chat_gen = rds_copilot.chat(request_content, conversion_id)
            for event in chat_gen:
                # 从事件对象中获取最新的 conversion_id
                if hasattr(event, 'conversion_id') and event.conversion_id:
                    final_conversion_id = event.conversion_id
                event_queue.put(event)
        except Exception as e:
            logger.error(f"对话过程出错：{e}")
            event_queue.put(e)
        finally:
            event_queue.put(None)

    loop = asyncio.get_event_loop()
    chat_task = loop.run_in_executor(_chat_executor, run_chat_in_thread)

    while True:
        event = await loop.run_in_executor(_chat_executor, event_queue.get)
        if event is None:
            break
        if isinstance(event, Exception):
            raise event

        event_type_name = type(event).__name__
        if isinstance(event, MessageEvent) and event.text:
            full_content += event.text
            await update_card_callback({"content": full_content})
            logger.debug(f"流式更新 content，当前长度: {len(full_content)}")

        elif isinstance(event, (ToolCallStart, ToolCallPending, ToolCallEnd)):
            try:
                tool_call_data = json.loads(event.text)
                tool_call_name = tool_call_data.get("tool_call_name") or tool_call_data.get("ToolCallName", "")
                tool_call_id = tool_call_data.get("tool_call_id") or tool_call_data.get("ToolCallId", "")
                logger.info(f"[卡片] 收到 {event_type_name}，tool_call_name={tool_call_name!r}，tool_call_id={tool_call_id!r}")
                # 相同 tool_call_id 只推送一次
                if tool_call_id and tool_call_id not in seen_tool_call_ids:
                    seen_tool_call_ids.add(tool_call_id)
                    if tool_call_name:
                        preparations.append({"name": tool_call_name})
                        await update_card_callback({"preparations": preparations})
                        logger.info(f"[卡片] 已推送 preparations（tool_call_id 首次），当前: {[p['name'] for p in preparations]}")
                elif not tool_call_id:
                    if tool_call_name and tool_call_name not in {p["name"] for p in preparations}:
                        preparations.append({"name": tool_call_name})
                        await update_card_callback({"preparations": preparations})
                        logger.info(f"[卡片] 已推送 preparations（无 tool_call_id），当前: {[p['name'] for p in preparations]}")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"解析 tool_call 事件失败: {e}，event.text 前200字: {event.text[:200]!r}")

        elif isinstance(event, DocumentEvent):
            logger.info(f"[卡片] 收到 DocumentEvent: {event.title}")

        else:
            logger.info(f"[卡片] 未处理的事件类型: {event_type_name}")

    await chat_task

    logger.info(
        f"Request: {request_content[:80]}... | content 长度: {len(full_content)} | preparations: {len(preparations)} | conversion_id: {final_conversion_id}"
    )
    return {"content": full_content, "preparations": preparations, "conversion_id": final_conversion_id}


async def handle_reply_and_update_card(self: dingtalk_stream.ChatbotHandler, incoming_message: dingtalk_stream.ChatbotMessage, conversion_id: str = ''):
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
            incoming_message.text.content, update_card_callback, rds_copilot, conversion_id
        )

        final_display_content = final_contents.get("content", "")
        if not final_display_content:
            final_display_content = build_no_message_card_content()
            self.logger.warning("RDS AI finished without response content.")
    except Exception as e:
        final_display_content = build_error_card_content(e)
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
        session_command = parse_session_command(query_text)

        if session_command:
            session_enabled = session_command == "on"
            store.set_session_enabled(
                dingtalk_conversation_id,
                sender_id,
                session_enabled,
            )
            self.logger.info(
                f"Copilot conversation retention changed, "
                f"session_enabled={session_enabled}, "
                f"dingtalk_conversation_id={dingtalk_conversation_id}, sender_id={sender_id}"
            )
            if session_enabled:
                self.reply_text("Conversation context is enabled.", incoming_message)
            else:
                self.reply_text("Conversation context is disabled.", incoming_message)
            return AckMessage.STATUS_OK, "OK"
        
        # 检测 /new 命令，重置对话
        if is_new_conversation_command(query_text):
            store.clear(dingtalk_conversation_id, sender_id)
            self.logger.info(
                f"Copilot conversation id cleared, "
                f"dingtalk_conversation_id={dingtalk_conversation_id}, sender_id={sender_id}"
            )
            self.reply_text("Started a new conversation.", incoming_message)
            return AckMessage.STATUS_OK, "OK"

        session_enabled = store.is_session_enabled(dingtalk_conversation_id, sender_id)
        # 默认保持多轮上下文；用户可以通过 /session off 为当前会话和发送人关闭。
        conversion_id = store.get(dingtalk_conversation_id, sender_id) if session_enabled else ""
        
        # 创建异步任务处理回复
        async def handle_and_update_conversation():
            final_conversion_id = await handle_reply_and_update_card(
                self, incoming_message, conversion_id
            )
            # 按“钉钉会话 + 发送人”持久化 ConversationId，服务重启后仍可保持上下文。
            if (
                final_conversion_id
                and session_enabled
                and store.is_session_enabled(dingtalk_conversation_id, sender_id)
            ):
                store.set(dingtalk_conversation_id, sender_id, final_conversion_id)

        asyncio.create_task(handle_and_update_conversation())
        return AckMessage.STATUS_OK, "OK"


def main():
    options = define_options()

    credential = dingtalk_stream.Credential(options.client_id, options.client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, CardBotHandler()
    )
    client.start_forever()


if __name__ == "__main__":
    main()
