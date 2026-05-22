import os
import json
import logging
import asyncio
import argparse
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

    # 初始化 RdsCopilot 实例
    rds_copilot = RdsCopilot()

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
    try:
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
        
        # 完成时使用 content 变量标记结束
        await card_instance.async_streaming(
            card_instance_id,
            content_key="content",
            content_value=final_contents.get("content", ""),
            append=False,
            finished=True,
            failed=False,
        )
    except Exception as e:
        self.logger.exception(e)
        await card_instance.async_streaming(
            card_instance_id,
            content_key="content",
            content_value="",
            append=False,
            finished=False,
            failed=True,
        )
    
    # 返回最终的 conversion_id
    return final_contents.get("conversion_id", "")


class CardBotHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, logger: logging.Logger = logger):
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        if logger:
            self.logger = logger
        # 用户会话管理：{user_id: conversion_id}
        self.user_conversations = {}

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        self.logger.info(f"收到消息：{incoming_message}")

        if incoming_message.message_type != "text":
            self.reply_text("俺只看得懂文字喔~", incoming_message)
            return AckMessage.STATUS_OK, "OK"

        user_id = incoming_message.sender_id
        query_text = incoming_message.text.content.strip()
        
        # 检测 /new 命令，重置对话
        if query_text.lower() == "/new":
            if user_id in self.user_conversations:
                del self.user_conversations[user_id]
                self.logger.info(f"用户 {user_id} 重置对话")
            self.reply_text("已开启新对话 ✨", incoming_message)
            return AckMessage.STATUS_OK, "OK"

        # 获取用户的 conversion_id，如果不存在则为空字符串（新对话）
        conversion_id = self.user_conversations.get(user_id, "")
        
        # 创建异步任务处理回复
        async def handle_and_update_conversation():
            final_conversion_id = await handle_reply_and_update_card(
                self, incoming_message, conversion_id
            )
            # 更新用户的 conversion_id
            if final_conversion_id:
                self.user_conversations[user_id] = final_conversion_id

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
