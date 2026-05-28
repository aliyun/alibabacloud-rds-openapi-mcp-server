#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json
import os
import time
import uuid

from loguru import logger
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi import utils_models as open_api_util_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from alibabacloud_tea_util.models import RuntimeOptions

markdown_code_languages = [
    # 编程语言
    "python",
    "javascript",
    "js",
    "typescript",
    "ts",
    "java",
    "c",
    "cpp",
    "csharp",
    "cs",
    "go",
    "golang",
    "rust",
    "ruby",
    "php",
    "swift",
    "kotlin",
    "scala",
    "r",
    "perl",
    "haskell",
    "lua",
    "matlab",
    "fortran",
    "objective-c",
    "objc",
    "dart",
    "elixir",
    "erlang",
    "clojure",
    "fsharp",
    "vbnet",
    "assembly",
    "asm",

    # 脚本与配置
    "bash",
    "shell",
    "zsh",
    "powershell",
    "ps1",
    "batch",
    "bat",
    "cmd",

    # 标记与数据格式
    "html",
    "xml",
    "svg",
    "mathml",
    "xhtml",
    "markdown",
    "md",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "properties",
    "dotenv",
    "env",

    # 样式表
    "css",
    "scss",
    "sass",
    "less",
    "stylus",

    # 模板与 DSL
    "jinja2",
    "django",
    "liquid",
    "handlebars",
    "hbs",
    "mustache",
    "twig",
    "pug",
    "jade",

    # 数据库与查询语言
    "sql",
    "mysql",
    "pgsql",
    "plsql",
    "sqlite",
    "cql",

    # 其他常用
    "diff",
    "patch",
    "makefile",
    "dockerfile",
    "docker",
    "nginx",
    "apache",
    "http",
    "graphql",
    "protobuf",
    "terraform",
    "hcl",
    "log",
    "plaintext",
    "text",
    "ascii",
]


class ChatMessageParams(open_api_util_models.Params):
    def __init__(self):
        super().__init__()
        self.action = 'ChatMessages'
        self.version = '2025-05-07'
        self.protocol = 'HTTPS'
        self.method = 'POST'


class ChatMessagesStopParams(open_api_util_models.Params):
    def __init__(self):
        super().__init__()
        self.action = 'ChatMessagesTaskStop'
        self.version = '2025-05-07'
        self.protocol = 'HTTPS'
        self.method = 'POST'


class BaseEvent:
    def __init__(self, task_id, conversion_id):
        self.task_id = task_id
        self.conversion_id = conversion_id


class MessageEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, text):
        super().__init__(task_id, conversion_id)
        self.text = text


class ToolCallStart(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text, tool_call_id):
        super().__init__(task_id, conversion_id)
        self.title = title
        self.text = text
        self.tool_call_id = f"t{tool_call_id.split('-')[-1]}"


class ToolCallPending(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text, tool_call_id):
        super().__init__(task_id, conversion_id)
        self.title = title
        self.text = text
        self.tool_call_id = f"t{tool_call_id.split('-')[-1]}"


class ToolCallEnd(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text, tool_call_id):
        super().__init__(task_id, conversion_id)
        self.title = title
        self.text = text
        self.tool_call_id = f"t{tool_call_id.split('-')[-1]}"


class DocumentEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text):
        super().__init__(task_id, conversion_id)
        self.document_id = f"d{str(uuid.uuid4()).split('-')[-1]}"
        self.title = title
        self.text = text


class SubTaskStartEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text):
        super().__init__(task_id, conversion_id)
        self.subtask_id = f"s{title.replace('_', '')}".lower()[:20]
        self.title = title
        self.text = text


class SubTaskEndEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, title, text):
        super().__init__(task_id, conversion_id)
        self.subtask_id = f"s{title.replace('_', '')}".lower()[:20]
        self.title = title
        self.text = text


class ChartEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, title, x_field, y_field, data):
        super().__init__(task_id, conversion_id)
        self.title = title
        self.x_field = x_field
        self.y_field = y_field
        self.data = data


class RdsCopilot:
    def __init__(self):
        self.endpoint = os.getenv('RDS_COPILOT_ENDPOINT', 'rdsai.aliyuncs.com')

        # 初始化OpenAPI配置
        config = open_api_models.Config(
            access_key_id=os.getenv('ACCESS_KEY_ID'),
            access_key_secret=os.getenv('ACCESS_SECRET'),
            protocol='https',
            region_id='cn-hangzhou',
            endpoint=self.endpoint,
            read_timeout=600_000,
            connect_timeout=10_000
        )
        self.app_id = 'app-iBuGU1VxEY42zrQRQfNAn3oj'
        self.client = OpenApiClient(config)
        self.code_mask_start = '```'
        self.code_mask_end = '```\n'

    @staticmethod
    def _preview(value, limit=200):
        if value is None:
            return ''
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        value = value.replace('\n', '\\n')
        if len(value) > limit:
            return value[:limit] + '...'
        return value

    @classmethod
    def _preview_raw_sse_data(cls, raw_data, limit=500):
        if raw_data is None:
            return ''
        if isinstance(raw_data, bytes):
            raw_text = raw_data.decode('utf-8', errors='replace')
        else:
            raw_text = str(raw_data)
        return cls._preview(raw_text, limit=limit)

    @classmethod
    def _raw_sse_edge_preview(cls, raw_data, limit=300):
        if raw_data is None:
            raw_text = ''
        elif isinstance(raw_data, bytes):
            raw_text = raw_data.decode('utf-8', errors='replace')
        else:
            raw_text = str(raw_data)
        return {
            'length': len(raw_text),
            'head': cls._preview(raw_text[:limit], limit=limit),
            'tail': cls._preview(raw_text[-limit:], limit=limit),
        }

    def _emit_tool_call_event(self, task_id, conversion_id, payload):
        """根据 tool_call 事件的 status 返回对应事件类型（EventMode=separate 时 payload 为单条事件体）"""
        tool_call_name = payload.get('tool_call_name') or payload.get('ToolCallName', '')
        status = payload.get('status') or payload.get('Status', '')
        tool_call_id = payload.get('tool_call_id') or payload.get('ToolCallId', '')
        text = json.dumps(payload, ensure_ascii=False)
        if status == 'start':
            return ToolCallStart(task_id, conversion_id, title=tool_call_name, text=text, tool_call_id=tool_call_id)
        if status == 'pending':
            return ToolCallPending(task_id, conversion_id, title=tool_call_name, text=text, tool_call_id=tool_call_id)
        return ToolCallEnd(task_id, conversion_id, title=tool_call_name, text=text, tool_call_id=tool_call_id)

    def stop_task(self, task_id):
        # 发送停止请求
        stop_query_params = {
            'TaskId': task_id,
            'ApiId': self.app_id
        }
        stop_request = open_api_util_models.OpenApiRequest(query=stop_query_params)
        response = self.client.do_request(
            ChatMessagesStopParams(),
            stop_request,
            RuntimeOptions()
        )

    def chat(self, query, conversion_id='', trace_id=''):
        """流式对话，使用 EventMode=separate：message / tool_call / doc 等各自独立事件，便于渲染与推送。
        
        Args:
            query: 用户查询文本
            conversion_id: 对话 ID，用于保持上下文
            
        Yields:
            各种事件对象（MessageEvent, ToolCallStart, ToolCallPending, ToolCallEnd, DocumentEvent）
            
        Returns:
            str: 最终的 conversation_id（注意：API 返回的是 ConversationId 或 ConversionId）
        """
        task_id = ""
        final_conversion_id = conversion_id
        trace = trace_id or "no-trace"
        start_at = time.monotonic()
        first_event_at = None
        first_message_at = None
        total_sse_event_count = 0
        message_event_count = 0
        tool_call_event_count = 0
        doc_event_count = 0
        unknown_event_count = 0
        malformed_event_count = 0
        try:
            query_params = {
                'Query': query,
                'ConversationId': conversion_id,
                'ApiId': self.app_id,
                'EventMode': 'separate',
            }
            logger.info(
                f"[trace_id={trace}] rds_sse_start, endpoint={self.endpoint}, api_id={self.app_id}, "
                f"conversation_id={conversion_id or ''}, query_length={len(query or '')}"
            )
            chat_message_params = ChatMessageParams()
            chat_message_request = open_api_util_models.OpenApiRequest(query=query_params)
            responses = self.client.call_sseapi(chat_message_params, chat_message_request, RuntimeOptions())

            for response in responses:
                total_sse_event_count += 1
                if first_event_at is None:
                    first_event_at = time.monotonic()
                    logger.info(
                        f"[trace_id={trace}] rds_first_event, "
                        f"first_event_cost={first_event_at - start_at:.2f}s"
                    )

                raw_data = response.event.data
                logger.info(
                    f"[trace_id={trace}] rds_sse_raw_event, "
                    f"seq={total_sse_event_count}, raw={self._preview_raw_sse_data(raw_data)}"
                )
                try:
                    response_body = json.loads(raw_data)
                except (json.JSONDecodeError, TypeError) as e:
                    malformed_event_count += 1
                    raw_edge = self._raw_sse_edge_preview(raw_data)
                    logger.warning(
                        f"[trace_id={trace}] rds_sse_malformed_event, "
                        f"seq={total_sse_event_count}, error={e}, "
                        f"raw_length={raw_edge['length']}, "
                        f"raw_head={raw_edge['head']}, raw_tail={raw_edge['tail']}"
                    )
                    continue
                if 'TaskId' in response_body:
                    task_id = response_body['TaskId']
                if 'ConversationId' in response_body:
                    final_conversion_id = response_body['ConversationId']
                elif 'ConversionId' in response_body:
                    final_conversion_id = response_body['ConversionId']

                event_type = (response_body.get('Event') or response_body.get('event') or '').strip().lower()
                logger.info(
                    f"[trace_id={trace}] rds_sse_event, "
                    f"seq={total_sse_event_count}, event_type={event_type or 'EMPTY'}, "
                    f"conversation_id={final_conversion_id}, task_id={task_id}, "
                    f"answer_preview={self._preview(response_body.get('Answer') or response_body.get('answer') or '')}, "
                    f"keys={list(response_body.keys())}"
                )

                if event_type == 'message':
                    if response_body.get('Answer'):
                        message_event_count += 1
                        if first_message_at is None:
                            first_message_at = time.monotonic()
                            logger.info(
                                f"[trace_id={trace}] rds_first_message, "
                                f"first_message_cost={first_message_at - start_at:.2f}s, "
                                f"message_preview={self._preview(response_body['Answer'])}"
                            )
                        yield MessageEvent(task_id, final_conversion_id, response_body['Answer'])

                elif event_type in ('tool_call', 'toolcall'):
                    tool_call_event_count += 1
                    # tool_call_name / status / tool_call_id 在 Answer 的 value 中，需解析 Answer（JSON 字符串）
                    answer_raw = response_body.get('Answer') or response_body.get('answer') or ''
                    payload = {}
                    try:
                        answer_obj = json.loads(answer_raw) if isinstance(answer_raw, str) else answer_raw
                        if isinstance(answer_obj, dict):
                            payload = {
                                'tool_call_name': answer_obj.get('tool_call_name') or answer_obj.get('ToolCallName', ''),
                                'status': answer_obj.get('status') or answer_obj.get('Status', ''),
                                'tool_call_id': answer_obj.get('tool_call_id') or answer_obj.get('ToolCallId', ''),
                            }
                            if 'response' in answer_obj:
                                payload['response'] = answer_obj['response']
                            if 'Response' in answer_obj:
                                payload['response'] = answer_obj['Response']
                    except (json.JSONDecodeError, TypeError):
                        payload = {
                            'tool_call_name': response_body.get('tool_call_name') or '',
                            'status': response_body.get('status') or '',
                            'tool_call_id': response_body.get('tool_call_id') or '',
                        }
                    yield self._emit_tool_call_event(task_id, final_conversion_id, payload)

                elif event_type == 'doc':
                    doc_event_count += 1
                    yield DocumentEvent(
                        task_id, final_conversion_id,
                        title=response_body.get('title', 'Documents'),
                        text=json.dumps(response_body, ensure_ascii=False)
                    )
                elif event_type:
                    unknown_event_count += 1
                    logger.info(
                        f"[trace_id={trace}] rds_event_not_rendered, event_type={event_type}"
                    )
        except Exception as e:
            logger.exception(f"[trace_id={trace}] rds_sse_exception: {e}")
            raise e
        finally:
            total_elapsed = time.monotonic() - start_at
            first_event_cost = None if first_event_at is None else first_event_at - start_at
            first_message_cost = None if first_message_at is None else first_message_at - start_at
            logger.info(
                f"[trace_id={trace}] rds_sse_summary, "
                f"total_elapsed={total_elapsed:.2f}s, "
                f"first_event_cost={first_event_cost if first_event_cost is None else round(first_event_cost, 2)}, "
                f"first_message_cost={first_message_cost if first_message_cost is None else round(first_message_cost, 2)}, "
                f"total_sse_event_count={total_sse_event_count}, "
                f"message_event_count={message_event_count}, "
                f"tool_call_event_count={tool_call_event_count}, "
                f"doc_event_count={doc_event_count}, "
                f"unknown_event_count={unknown_event_count}, "
                f"malformed_event_count={malformed_event_count}, "
                f"conversation_id={final_conversion_id}, api_id={self.app_id}"
            )
        
        # 生成器结束时返回最终的 conversion_id
        return final_conversion_id
