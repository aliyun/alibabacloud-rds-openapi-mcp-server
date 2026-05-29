#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
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


class GetConversationsParams(open_api_util_models.Params):
    def __init__(self):
        super().__init__()
        self.action = 'GetConversations'
        self.version = '2025-05-07'
        self.protocol = 'HTTPS'
        self.method = 'POST'


class ListCustomAgentParams(open_api_util_models.Params):
    def __init__(self):
        super().__init__()
        self.action = 'ListCustomAgent'
        self.version = '2025-05-07'
        self.protocol = 'HTTPS'
        self.method = 'POST'


class ListSkillParams(open_api_util_models.Params):
    def __init__(self):
        super().__init__()
        self.action = 'ListSkill'
        self.version = '2025-05-07'
        self.protocol = 'HTTPS'
        self.method = 'POST'


class BaseEvent:
    def __init__(self, task_id, conversion_id):
        self.task_id = task_id
        self.conversion_id = conversion_id


class StreamProgressEvent(BaseEvent):
    def __init__(self, task_id, conversion_id, event_type):
        super().__init__(task_id, conversion_id)
        self.event_type = event_type


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

    @staticmethod
    def _response_body(response):
        if response is None:
            return {}
        if isinstance(response, dict):
            body = response.get("body")
            if isinstance(body, dict):
                return body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            if isinstance(body, str):
                try:
                    parsed_body = json.loads(body)
                except json.JSONDecodeError:
                    return response
                return parsed_body if isinstance(parsed_body, dict) else response
            return response
        body = getattr(response, "body", None)
        if isinstance(body, dict):
            return body
        if body is not None and hasattr(body, "to_map"):
            return body.to_map()
        if hasattr(response, "to_map"):
            mapped = response.to_map()
            if isinstance(mapped, dict):
                return mapped.get("body") or mapped
        return {}

    @staticmethod
    def _is_conversations_response(body):
        return isinstance(body, dict) and any(key in body for key in ("Data", "HasMore", "Limit", "RequestId"))

    def list_conversations(self, last_id='', limit=10, pinned='', sort_by='CreatedAt'):
        query_params = {
            'Limit': str(limit),
        }
        if last_id:
            query_params['LastId'] = last_id
        if pinned != '':
            query_params['Pinned'] = str(pinned).lower() if isinstance(pinned, bool) else str(pinned)
        if sort_by:
            query_params['SortBy'] = sort_by
        request = open_api_util_models.OpenApiRequest(query=query_params)
        response = self.client.do_request(
            GetConversationsParams(),
            request,
            RuntimeOptions()
        )
        body = self._response_body(response)
        if sort_by and not self._is_conversations_response(body):
            fallback_query_params = dict(query_params)
            fallback_query_params.pop('SortBy', None)
            logger.warning("GetConversations returned unexpected body with SortBy={}; retrying without SortBy", sort_by)
            fallback_request = open_api_util_models.OpenApiRequest(query=fallback_query_params)
            fallback_response = self.client.do_request(
                GetConversationsParams(),
                fallback_request,
                RuntimeOptions()
            )
            return self._response_body(fallback_response)
        return body

    def list_custom_agents(self, page_number=1, page_size=20):
        query_params = {
            'PageNumber': str(page_number),
            'PageSize': str(page_size),
        }
        request = open_api_util_models.OpenApiRequest(query=query_params)
        response = self.client.do_request(
            ListCustomAgentParams(),
            request,
            RuntimeOptions()
        )
        return self._response_body(response)

    def list_skills(self, page_number=1, page_size=20, language='zh-CN'):
        query_params = {
            'PageNumber': str(page_number),
            'PageSize': str(page_size),
            'Language': language or 'zh-CN',
        }
        request = open_api_util_models.OpenApiRequest(query=query_params)
        response = self.client.do_request(
            ListSkillParams(),
            request,
            RuntimeOptions()
        )
        return self._response_body(response)

    async def chat_async(
        self,
        query,
        conversion_id='',
        trace_id='',
        custom_agent_id='',
        language='zh-CN',
        timezone='Asia/Shanghai',
        include_progress_events=False,
    ):
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
            inputs = {}
            if language:
                inputs['Language'] = language
            if timezone:
                inputs['Timezone'] = timezone
            if inputs:
                query_params['Inputs'] = json.dumps(inputs, ensure_ascii=False)
            if custom_agent_id:
                query_params['CustomAgentId'] = custom_agent_id
            logger.info(
                f"[trace_id={trace}] rds_sse_start, endpoint={self.endpoint}, api_id={self.app_id}, "
                f"conversation_id={conversion_id or ''}, custom_agent_id={custom_agent_id or ''}, "
                f"language={language or ''}, timezone={timezone or ''}, query_length={len(query or '')}"
            )
            chat_message_params = ChatMessageParams()
            chat_message_request = open_api_util_models.OpenApiRequest(query=query_params)
            responses = self.client.call_sseapi_async(chat_message_params, chat_message_request, RuntimeOptions())

            async for response in responses:
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
                if include_progress_events:
                    yield StreamProgressEvent(task_id, final_conversion_id, event_type)

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
        
        return

    def chat(
        self,
        query,
        conversion_id='',
        trace_id='',
        custom_agent_id='',
        language='zh-CN',
        timezone='Asia/Shanghai',
        include_progress_events=False,
    ):
        async_events = self.chat_async(
            query,
            conversion_id,
            trace_id=trace_id,
            custom_agent_id=custom_agent_id,
            language=language,
            timezone=timezone,
            include_progress_events=include_progress_events,
        )
        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    event = loop.run_until_complete(async_events.__anext__())
                except StopAsyncIteration:
                    break
                yield event
        finally:
            loop.run_until_complete(async_events.aclose())
            loop.close()
