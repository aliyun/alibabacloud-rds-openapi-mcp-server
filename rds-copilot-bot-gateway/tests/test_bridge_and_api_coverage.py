import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib import error as urllib_error

import importlib
import main
from bridges import dingtalk as dingtalk_bridge
from bridges import feishu as feishu_bridge
from bridges import qq as qq_bridge
from bridges import wecom as wecom_bridge
from core import bot_core
from core import rds_copilot
from core.rds_copilot import (
    ChartEvent,
    DocumentEvent,
    MessageEvent,
    RdsCopilot,
    StreamProgressEvent,
    SubTaskEndEvent,
    SubTaskStartEvent,
    ToolCallEnd,
    ToolCallPending,
    ToolCallStart,
)


class FakeSseEvent:
    def __init__(self, data):
        self.data = data


class FakeSseResponse:
    def __init__(self, data):
        self.event = FakeSseEvent(data)


class FakeSseClient:
    def __init__(self, response_data=None, error=None):
        self.responses = [FakeSseResponse(data) for data in (response_data or [])]
        self.error = error
        self.last_query = None
        self.sync_calls = 0
        self.async_calls = 0

    def call_sseapi(self, params, request, runtime):
        self.sync_calls += 1
        self.last_query = getattr(request, "query", {})
        if self.error:
            raise self.error
        return self.responses

    async def call_sseapi_async(self, params, request, runtime):
        self.async_calls += 1
        self.last_query = getattr(request, "query", {})
        if self.error:
            raise self.error
        for response in self.responses:
            yield response


class FakeRpcClient:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def do_request(self, params, request, runtime):
        self.calls.append((params.action, getattr(request, "query", {})))
        return self.response


class FakeText:
    def __init__(self, content):
        self.content = content


class FakeIncomingMessage:
    def __init__(
        self,
        content="query",
        *,
        conversation_id="ding-conv-1",
        sender_id="sender-1",
        conversation_type="2",
        message_id="msg-1",
        message_type="text",
    ):
        self.text = FakeText(content)
        self.conversation_id = conversation_id
        self.sender_id = sender_id
        self.conversation_type = conversation_type
        self.message_id = message_id
        self.message_type = message_type
        self.robot_code = "robot-code-1"
        self.session_webhook = ""
        self.sender_staff_id = "staff-1"


class FakeHandler:
    def __init__(self):
        self.dingtalk_client = SimpleNamespace(get_access_token=lambda: "token-1")
        self.logger = Mock()
        self.replies = []

    def reply_text(self, text, message):
        self.replies.append((text, message))


class FakeCardInstance:
    def __init__(self, fail_put=False, fail_finalize=False):
        self.fail_put = fail_put
        self.fail_finalize = fail_finalize
        self.created = []
        self.streaming_calls = []
        self.put_calls = []

    async def async_create_and_deliver_card(self, template_id, card_data):
        self.created.append((template_id, card_data))
        return "card-1"

    async def async_streaming(self, *args, **kwargs):
        self.streaming_calls.append((args, kwargs))
        if kwargs.get("finished") and self.fail_finalize:
            raise RuntimeError("finalize failed")

    async def async_put_card_data(self, *args, **kwargs):
        self.put_calls.append((args, kwargs))
        if self.fail_put:
            raise RuntimeError("put failed")
        return True


class FakeCopilot:
    def __init__(self, events=None, error=None, stop_error=None):
        self.events = events or []
        self.error = error
        self.stop_error = stop_error
        self.stopped = []
        self.chat_calls = []

    def chat(
        self,
        query,
        conversion_id="",
        custom_agent_id="",
        language="zh-CN",
        timezone="Asia/Shanghai",
    ):
        self.chat_calls.append((query, conversion_id, custom_agent_id, language, timezone))
        if self.error:
            raise self.error
        yield from self.events

    def stop_task(self, task_id):
        self.stopped.append(task_id)
        if self.stop_error:
            raise self.stop_error

    def list_conversations(self, **kwargs):
        return {"Data": [], "HasMore": False}

    def list_custom_agents(self, **kwargs):
        return {"Data": []}

    def list_skills(self, **kwargs):
        return {"Data": [], "PageNumber": kwargs.get("page_number", 1), "TotalCount": 0}


class FactoryStyleCopilot(FakeCopilot):
    instances = []

    def __init__(self):
        super().__init__([MessageEvent("task-1", "conv-1", "factory answer")])
        type(self).instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    def list_conversations(self, **kwargs):
        return {
            "Data": [{"Id": "abcdef12-3456-7890-abcd-ef1234567890", "Name": "Factory Conversation"}],
            "HasMore": False,
        }

    def list_custom_agents(self, **kwargs):
        return {"Data": [{"Id": "agent-factory", "Name": "Factory Agent", "EnableTools": True, "Tools": []}]}

    def list_skills(self, **kwargs):
        return {"Data": [{"Name": "factory-skill"}], "PageNumber": kwargs.get("page_number", 1), "TotalCount": 1}


class BotSecurityCoverageTest(unittest.TestCase):
    def test_session_source_authorization_is_env_driven_and_deny_by_default(self):
        source = bot_core.SessionSource(
            platform="feishu",
            chat_id="oc_chat_1",
            chat_type="group",
            user_id="ou_user_1",
            user_name="Alice",
            thread_id="thread-1",
            user_id_alt="on_union_1",
        )
        self.assertEqual(source.identity_values(), {"ou_user_1", "on_union_1"})

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bot_core.authorize_session_source(source))

        with patch.dict(os.environ, {"FEISHU_ALLOW_ALL_USERS": "true"}, clear=True):
            self.assertTrue(bot_core.authorize_session_source(source))

        with patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "on_union_1"}, clear=True):
            self.assertTrue(bot_core.authorize_session_source(source))

        with patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "1"}, clear=True):
            self.assertTrue(bot_core.authorize_session_source(source))

        with patch.dict(os.environ, {"GATEWAY_ALLOWED_USERS": "ou_user_1"}, clear=True):
            self.assertTrue(bot_core.authorize_session_source(source))

    def test_platform_pre_filters_cover_dingtalk_feishu_wecom_and_qq(self):
        dingtalk_source = bot_core.SessionSource("dingtalk", "ding-chat-1", "group", "ding-user-1")
        with patch.dict(
            os.environ,
            {
                "DINGTALK_ALLOWED_CHATS": "ding-chat-1",
                "DINGTALK_REQUIRE_MENTION": "true",
                "DINGTALK_MENTION_PATTERNS": "@RDS",
            },
            clear=True,
        ):
            self.assertFalse(bot_core.should_accept_session_source(dingtalk_source, "hello"))
            self.assertTrue(bot_core.should_accept_session_source(dingtalk_source, "@RDS hello"))

        feishu_bot = bot_core.SessionSource("feishu", "oc_chat_1", "group", "ou_bot", is_bot=True)
        with patch.dict(os.environ, {"FEISHU_GROUP_POLICY": "open"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(feishu_bot, "hello"))
        with patch.dict(os.environ, {"FEISHU_GROUP_POLICY": "open", "FEISHU_ALLOW_BOTS": "true"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(feishu_bot, "hello"))

        wecom_group = bot_core.SessionSource("wecom", "wecom-room-1", "group", "wecom-user-1")
        with patch.dict(os.environ, {"WECOM_GROUP_POLICY": "disabled"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(wecom_group, "hello"))
        with patch.dict(os.environ, {"WECOM_GROUP_POLICY": "allowlist", "WECOM_ALLOWED_CHATS": "wecom-room-1"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(wecom_group, "hello"))

        qq_group = bot_core.SessionSource("qqbot", "qq-group-1", "group", "qq-user-1")
        with patch.dict(os.environ, {"QQ_GROUP_ALLOWED_USERS": "qq-group-2"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(qq_group, "hello"))
        with patch.dict(os.environ, {"QQ_GROUP_ALLOWED_USERS": "qq-group-1"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(qq_group, "hello"))

        with patch.dict(os.environ, {"DINGTALK_REQUIRE_MENTION": "true", "DINGTALK_FREE_RESPONSE_CHATS": "ding-chat-1"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(dingtalk_source, "hello"))
        with patch.dict(os.environ, {"FEISHU_GROUP_POLICY": "disabled"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(bot_core.SessionSource("feishu", "chat", "group", "user"), "hello"))
        with patch.dict(os.environ, {"FEISHU_BOT_NAME": "RDSBot"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(bot_core.SessionSource("feishu", "chat", "group", "user"), "@RDSBot hello"))
        with patch.dict(os.environ, {"WECOM_DM_POLICY": "disabled"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(bot_core.SessionSource("wecom", "user", "dm", "user"), "hello"))
        with patch.dict(os.environ, {"WECOM_DM_POLICY": "allowlist", "WECOM_ALLOWED_USERS": "wecom-user-1"}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(bot_core.SessionSource("wecom", "user", "dm", "wecom-user-1"), "hello"))
        with patch.dict(os.environ, {"QQ_GROUP_POLICY": "disabled"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(qq_group, "hello"))
        with patch.dict(os.environ, {"QQ_DM_POLICY": "disabled"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(bot_core.SessionSource("qqbot", "user", "dm", "user"), "hello"))
        self.assertTrue(bot_core.should_accept_session_source(bot_core.SessionSource("other", "chat", "dm", "user"), "hello"))

    def test_security_and_preference_helpers_cover_edge_branches(self):
        with patch.dict(os.environ, {"DINGTALK_ALLOWED_USERS": "*"}, clear=True):
            self.assertTrue(bot_core.authorize_session_source(bot_core.SessionSource("dingtalk", "chat", "dm", "user-1")))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bot_core.authorize_session_source(bot_core.SessionSource("dingtalk", "chat", "dm", "")))
        with patch.dict(os.environ, {"DINGTALK_ALLOWED_CHATS": "other-chat"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(bot_core.SessionSource("dingtalk", "chat", "group", "user-1"), "hello"))
        with patch.dict(os.environ, {"DINGTALK_REQUIRE_MENTION": "true"}, clear=True):
            self.assertFalse(bot_core.should_accept_session_source(bot_core.SessionSource("dingtalk", "chat", "group", "user-1"), "hello"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bot_core.should_accept_session_source(bot_core.SessionSource("wecom", "user-1", "dm", "user-1"), "hello"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(os.path.join(tmp_dir, "conversations.json"))
            store.set_language("chat", "user", "bad-language")
            store.set_timezone("chat", "user", "Not/AZone")
            self.assertEqual(store.get_language("chat", "user"), bot_core.DEFAULT_LANGUAGE)
            self.assertEqual(store.get_timezone("chat", "user"), bot_core.DEFAULT_TIMEZONE)

        async def run_card_unsupported():
            context = bot_core.BotContext("feishu", "chat", "user", bot_core.CopilotConversationStore(""))
            return await bot_core.handle_control_command("/card on", context, FakeCopilot(), card_supported=False)

        self.assertIn("不支持卡片回复", asyncio.run(run_card_unsupported()).content)

    def test_timezone_database_failure_and_empty_suggestions_are_safe(self):
        bot_core._timezones_cache = None
        with patch("core.bot_core.available_timezones", side_effect=RuntimeError("tz db down")):
            self.assertEqual(bot_core.suggest_timezones("Shanghai"), [])
        bot_core._timezones_cache = None
        self.assertFalse(bot_core._is_valid_timezone(""))
        with patch("core.bot_core._get_available_timezones", return_value=set()), \
            patch("core.bot_core.ZoneInfo", side_effect=bot_core.ZoneInfoNotFoundError):
            self.assertFalse(bot_core._is_valid_timezone("Not/AZone"))
        with patch("core.bot_core._get_available_timezones", return_value=set()):
            self.assertEqual(bot_core.suggest_timezones("Shanghai"), [])


class RdsCopilotApiCoverageTest(unittest.TestCase):
    def make_copilot(self, client):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.endpoint = "rdsai.test"
        copilot.app_id = "app-test"
        copilot.client = client
        copilot.code_mask_start = "```"
        copilot.code_mask_end = "```\n"
        return copilot

    def test_openapi_param_classes_match_rdsai_actions(self):
        self.assertEqual(rds_copilot.ChatMessageParams().action, "ChatMessages")
        self.assertEqual(rds_copilot.ChatMessagesStopParams().action, "ChatMessagesTaskStop")
        self.assertEqual(rds_copilot.GetConversationsParams().action, "GetConversations")
        self.assertEqual(rds_copilot.ListCustomAgentParams().action, "ListCustomAgent")
        self.assertEqual(rds_copilot.ListSkillParams().action, "ListSkill")

    def test_event_classes_keep_payload_fields(self):
        self.assertEqual(MessageEvent("task", "conv", "answer").text, "answer")
        self.assertEqual(StreamProgressEvent("task", "conv", "workflow_started").event_type, "workflow_started")
        self.assertEqual(ToolCallStart("task", "conv", "tool", "{}", "call-abc").tool_call_id, "tabc")
        self.assertEqual(ToolCallPending("task", "conv", "tool", "{}", "call-def").tool_call_id, "tdef")
        self.assertEqual(ToolCallEnd("task", "conv", "tool", "{}", "call-ghi").tool_call_id, "tghi")
        self.assertTrue(DocumentEvent("task", "conv", "doc", "{}").document_id.startswith("d"))
        self.assertTrue(SubTaskStartEvent("task", "conv", "Sub_Task", "text").subtask_id.startswith("ssubtask"))
        self.assertTrue(SubTaskEndEvent("task", "conv", "Sub_Task", "text").subtask_id.startswith("ssubtask"))
        self.assertEqual(ChartEvent("task", "conv", "chart", "x", "y", [{"x": 1}]).data, [{"x": 1}])

    def test_preview_helpers_handle_none_bytes_objects_and_truncation(self):
        self.assertEqual(RdsCopilot._preview(None), "")
        self.assertEqual(RdsCopilot._preview({"a": 1}), '{"a": 1}')
        self.assertEqual(RdsCopilot._preview("a\nb"), "a\\nb")
        self.assertTrue(RdsCopilot._preview("x" * 10, limit=3).endswith("..."))
        self.assertEqual(RdsCopilot._preview_raw_sse_data(None), "")
        self.assertIn("bad", RdsCopilot._preview_raw_sse_data(b"bad\xff"))
        self.assertEqual(RdsCopilot._raw_sse_edge_preview(None)["length"], 0)
        edge = RdsCopilot._raw_sse_edge_preview(b"abcdef", limit=3)
        self.assertEqual(edge["length"], 6)
        self.assertEqual(edge["head"], "abc")
        self.assertEqual(edge["tail"], "def")

    def test_response_body_accepts_sdk_body_to_map_and_response_to_map(self):
        self.assertEqual(RdsCopilot._response_body(None), {})
        self.assertEqual(RdsCopilot._response_body({"ok": True}), {"ok": True})
        self.assertEqual(RdsCopilot._response_body({"body": {"Data": [1]}, "headers": {}, "statusCode": 200}), {"Data": [1]})
        self.assertEqual(RdsCopilot._response_body({"body": '{"Data":[2]}', "headers": {}, "statusCode": 200}), {"Data": [2]})
        self.assertEqual(RdsCopilot._response_body({"body": b'{"Data":[3]}', "headers": {}, "statusCode": 200}), {"Data": [3]})
        invalid_body = {"body": "{bad-json", "headers": {}, "statusCode": 200}
        self.assertEqual(RdsCopilot._response_body(invalid_body), invalid_body)
        self.assertEqual(RdsCopilot._response_body(SimpleNamespace(body={"body": True})), {"body": True})
        body = SimpleNamespace(to_map=lambda: {"Data": [1]})
        self.assertEqual(RdsCopilot._response_body(SimpleNamespace(body=body)), {"Data": [1]})
        response = SimpleNamespace(to_map=lambda: {"body": {"RequestId": "req-1"}})
        self.assertEqual(RdsCopilot._response_body(response), {"RequestId": "req-1"})
        self.assertEqual(RdsCopilot._response_body(SimpleNamespace(to_map=lambda: {"RequestId": "req-2"})), {"RequestId": "req-2"})
        self.assertEqual(RdsCopilot._response_body(SimpleNamespace()), {})

    def test_init_builds_openapi_client_from_environment(self):
        captured = {}

        def fake_config(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

        with patch.dict(os.environ, {"RDS_COPILOT_ENDPOINT": "rdsai.unit.test", "ACCESS_KEY_ID": "ak", "ACCESS_SECRET": "sk"}), \
            patch("core.rds_copilot.open_api_models.Config", side_effect=fake_config), \
            patch("core.rds_copilot.OpenApiClient", return_value="client"):
            copilot = RdsCopilot()

        self.assertEqual(copilot.endpoint, "rdsai.unit.test")
        self.assertEqual(copilot.client, "client")
        self.assertEqual(captured["access_key_id"], "ak")
        self.assertEqual(copilot.code_mask_end, "```\n")

    def test_rpc_methods_send_documented_query_shapes(self):
        client = FakeRpcClient(SimpleNamespace(body={"Data": []}))
        copilot = self.make_copilot(client)

        copilot.stop_task("task-1")
        conversations = copilot.list_conversations(last_id="last-1", limit=5, pinned=True, sort_by="UpdatedAt")
        agents = copilot.list_custom_agents(page_number="2", page_size="3")
        skills = copilot.list_skills(page_number="4", page_size="5", language="")

        self.assertEqual(client.calls[0], ("ChatMessagesTaskStop", {"TaskId": "task-1", "ApiId": "app-test"}))
        self.assertEqual(client.calls[1], ("GetConversations", {"Limit": "5", "LastId": "last-1", "Pinned": "true", "SortBy": "UpdatedAt"}))
        self.assertEqual(client.calls[2], ("ListCustomAgent", {"PageNumber": "2", "PageSize": "3"}))
        self.assertEqual(client.calls[3], ("ListSkill", {"PageNumber": "4", "PageSize": "5", "Language": "zh-CN"}))
        self.assertEqual(conversations, {"Data": []})
        self.assertEqual(agents, {"Data": []})
        self.assertEqual(skills, {"Data": []})

    def test_chat_passes_language_and_timezone_as_capitalized_inputs(self):
        client = FakeSseClient(['{"Event":"message","Answer":"ok","TaskId":"task-1","ConversationId":"conv-1"}'])
        copilot = self.make_copilot(client)

        list(copilot.chat("query", language="en-US", timezone="Asia/Tokyo"))

        inputs = json.loads(copilot.client.last_query["Inputs"])
        self.assertEqual(inputs, {"Language": "en-US", "Timezone": "Asia/Tokyo"})
        self.assertEqual(client.async_calls, 1)
        self.assertEqual(client.sync_calls, 0)

    def test_chat_parses_message_tool_doc_unknown_conversion_id_and_bad_json(self):
        responses = [
            b'{"Event":"message","Answer":"hello","TaskId":"task-1","ConversionId":"conv-old"}',
            json.dumps(
                {
                    "Event": "tool_call",
                    "Answer": json.dumps(
                        {
                            "tool_call_name": "DescribeDBInstances",
                            "status": "start",
                            "tool_call_id": "tool-1",
                            "response": {"ok": True},
                        }
                    ),
                    "TaskId": "task-1",
                    "ConversationId": "conv-new",
                }
            ),
            json.dumps(
                {
                    "Event": "toolcall",
                    "Answer": {"ToolCallName": "DescribeDBInstances", "Status": "pending", "ToolCallId": "tool-2", "Response": {"ok": True}},
                    "TaskId": "task-1",
                    "ConversationId": "conv-new",
                }
            ),
            '{"Event":"tool_call","Answer":"not-json","tool_call_name":"Fallback","status":"end","tool_call_id":"tool-3","TaskId":"task-1"}',
            '{"Event":"doc","title":"Docs","TaskId":"task-1","ConversationId":"conv-new"}',
            '{"Event":"thinking","Answer":"working","TaskId":"task-1","ConversationId":"conv-new"}',
            "{bad-json",
            '{"Event":"message","answer":"lowercase only","TaskId":"task-1","ConversationId":"conv-new"}',
        ]
        copilot = self.make_copilot(FakeSseClient(responses))

        events = list(copilot.chat("query", "conv-parent", trace_id="trace-1"))

        self.assertIsInstance(events[0], MessageEvent)
        self.assertEqual(events[0].conversion_id, "conv-old")
        self.assertIsInstance(events[1], ToolCallStart)
        self.assertIsInstance(events[2], ToolCallPending)
        self.assertIsInstance(events[3], ToolCallEnd)
        self.assertIsInstance(events[4], DocumentEvent)
        self.assertNotIn("CustomAgentId", copilot.client.last_query)

    def test_chat_can_emit_progress_events_for_unrendered_sse_task_ids(self):
        copilot = self.make_copilot(
            FakeSseClient(
                [
                    '{"Event":"workflow_started","TaskId":"task-raw","ConversationId":"conv-raw"}',
                    '{"Event":"message","Answer":"ok","TaskId":"task-raw","ConversationId":"conv-raw"}',
                ]
            )
        )

        events = list(copilot.chat("query", include_progress_events=True))

        self.assertIsInstance(events[0], StreamProgressEvent)
        self.assertEqual(events[0].task_id, "task-raw")
        self.assertEqual(events[0].event_type, "workflow_started")
        self.assertIsInstance(events[2], MessageEvent)

    def test_chat_does_not_log_payload_content_by_default(self):
        sensitive_answer = "customer_password=secret-value"
        raw_event = json.dumps({"Event": "message", "Answer": sensitive_answer, "TaskId": "task-1", "ConversationId": "conv-1"})
        copilot = self.make_copilot(FakeSseClient([raw_event]))
        log_messages = []
        sink_id = rds_copilot.logger.add(lambda message: log_messages.append(str(message)), level="INFO")
        try:
            list(copilot.chat("query", trace_id="trace-no-payload"))
        finally:
            rds_copilot.logger.remove(sink_id)

        joined_logs = "\n".join(log_messages)
        self.assertIn("trace-no-payload", joined_logs)
        self.assertNotIn(sensitive_answer, joined_logs)
        self.assertNotIn("raw=", joined_logs)
        self.assertNotIn("answer_preview=", joined_logs)

    def test_chat_raises_client_exception_after_logging_summary(self):
        copilot = self.make_copilot(FakeSseClient(error=RuntimeError("429 TooManyRequests")))

        with self.assertRaisesRegex(RuntimeError, "TooManyRequests"):
            list(copilot.chat("query"))


class BotCoreCoverageTest(unittest.IsolatedAsyncioTestCase):
    async def test_store_handles_empty_keys_bad_files_invalid_items_and_platform_scopes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = os.path.join(tmp_dir, "nested", "conversations.json")
            store = bot_core.CopilotConversationStore(store_path)
            self.assertEqual(store._load(), {"version": 1, "conversations": {}})
            self.assertEqual(store._key("", "sender"), "")
            self.assertEqual(store.get("", "sender"), "")
            store.set("chat", "sender", "")
            store.set_agent("chat", "sender", "", "agent")
            store.clear_agent("", "sender")
            store.set("chat", "sender", "conv-ding", platform="dingtalk")
            store.set("chat", "sender", "conv-feishu", platform="feishu")
            self.assertEqual(store.get("chat", "sender", platform="dingtalk"), "conv-ding")
            self.assertEqual(store.get("chat", "sender", platform="feishu"), "conv-feishu")

            with open(store_path, "w", encoding="utf-8") as f:
                f.write("{bad json")
            os.chmod(store_path, 0o644)
            self.assertEqual(store.get("chat", "sender"), "")
            self.assertEqual(os.stat(store_path).st_mode & 0o777, 0o600)
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump({"conversations": {"bad": "not-dict"}}, f)
            self.assertEqual(store._get_item("bad", "sender"), {})
            key = json.dumps(["chat", "sender"], ensure_ascii=False, separators=(",", ":"))
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump({"conversations": {key: "not-dict"}}, f)
            store.set("chat", "sender", "conv-recovered")
            self.assertEqual(store.get("chat", "sender"), "conv-recovered")
            self.assertEqual(os.stat(store_path).st_mode & 0o777, 0o600)
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            self.assertTrue(store.is_session_enabled("chat", "sender"))

        memory_store = bot_core.CopilotConversationStore("")
        memory_store._save({"conversations": {}})

    async def test_runtime_cache_active_state_and_small_helpers_cover_edge_branches(self):
        cache = bot_core.RuntimeCache()
        self.assertEqual(cache.resolve_conversation("", "", "", ""), "")
        self.assertEqual(cache.resolve_conversation("", "", "", "0123456789abcdef"), "0123456789abcdef")
        cache.set_conversations("p", "c", "s", [{"Id": "abc1111111111111"}, {"id": "abc2222222222222"}])
        self.assertEqual(cache.resolve_conversation("p", "c", "s", "abc"), "")
        self.assertEqual(cache.resolve_conversation("p", "c", "s", "abc11111"), "abc1111111111111")
        self.assertEqual(cache.resolve_agent_by_name("p", "c", "s", ""), {})
        cache.set_agents("p", "c", "s", [{"Name": "agent-a", "Id": "agent-1"}])
        self.assertEqual(cache.resolve_agent_by_name("p", "c", "s", "missing"), {})

        state = bot_core.ActiveConversationState("p", "c", "s")
        state.record_task_id("")
        state.record_message("")
        state.record_tool("")
        self.assertEqual(state.snapshot()["phase"], "receiving stream response")
        self.assertEqual(state.should_send_stop(), (False, ""))
        state.record_task_id("task-1")
        state.request_cancel()
        self.assertEqual(state.should_send_stop(), (True, "task-1"))
        self.assertEqual(state.should_send_stop(), (False, ""))

        registry = bot_core.ActiveConversationRegistry()
        context = bot_core.BotContext("p", "c", "s", bot_core.CopilotConversationStore(""), cache=cache, registry=registry)
        active = registry.start(context)
        self.assertIsNone(registry.try_start(context))
        self.assertTrue(registry.is_active(context))
        registry.finish(active)
        self.assertFalse(registry.is_active(context))

        user_error = bot_core.build_error_content(ValueError("secret internal detail"), language="zh-CN", trace_id="trace-123")
        self.assertIn("RDS AI 诊断失败", user_error)
        self.assertIn("trace-123", user_error)
        self.assertNotIn("ValueError", user_error)
        self.assertNotIn("secret internal detail", user_error)
        self.assertIn("no response content", bot_core.build_no_message_content(language="en-US"))
        self.assertIn("未返回回复内容", bot_core.build_no_message_content(language="zh-CN"))
        self.assertIn("/btw", bot_core.build_busy_content(language="zh-CN"))
        self.assertEqual(bot_core._short_id("123456789"), "12345678")
        self.assertTrue(bot_core._truncate("x" * 130).endswith("..."))
        self.assertEqual(bot_core._format_created_at("not-a-time"), "not-a-time")
        self.assertIn("没有找到最近对话", bot_core.format_conversations([]))
        conversations_text = bot_core.format_conversations(
            [
                {"Id": "olderid-full", "Name": "Older", "CreatedAt": 100, "Introduction": "hidden intro"},
                {"Id": "neweridfull", "Name": "Newer", "CreatedAt": 200},
                {"Id": "updatedfull", "Name": "Updated", "CreatedAt": 50, "UpdatedAt": 300},
            ],
            True,
        )
        conversation_lines = conversations_text.splitlines()
        self.assertEqual(conversation_lines[1], f"- `updatedf` `{bot_core._format_created_at(300)}` Updated")
        self.assertEqual(conversation_lines[2], f"- `neweridf` `{bot_core._format_created_at(200)}` Newer")
        self.assertNotIn("id=", conversations_text)
        self.assertNotIn("hidden intro", conversations_text)
        self.assertIn("还有更多对话", conversations_text)
        self.assertIn("没有找到 Custom Agent", bot_core.format_agents([]))
        self.assertIn("工具: `off`", bot_core.format_agents([{"Id": "a", "Name": "Agent", "EnableTools": False}]))
        self.assertIn("没有找到 Skill", bot_core.format_skills([], total_count=0))
        self.assertIn("/help", bot_core.format_help())
        self.assertEqual(await bot_core.maybe_await("value"), "value")
        self.assertEqual(bot_core._get_session_checkout_token("/session abcdef12"), "abcdef12")
        self.assertEqual(bot_core._get_session_checkout_token("$session abcdef12"), "abcdef12")
        self.assertEqual(bot_core._get_session_checkout_token("/session checkout abc"), "")
        self.assertEqual(bot_core._get_agent_name("/agent Agent Name"), "Agent Name")
        self.assertEqual(bot_core._get_agent_name("$agent Agent Name"), "Agent Name")
        self.assertEqual(bot_core._get_agent_name("/agent"), "")
        self.assertEqual(bot_core._parse_skills_args("/skills 0"), (1, True))
        self.assertEqual(bot_core._parse_skills_args("$skills 0"), (1, True))
        self.assertEqual(bot_core._parse_skills_args("/skills en-US"), (1, False))
        self.assertEqual(bot_core._normalize_language("EN_us"), "en-US")
        self.assertEqual(bot_core._normalize_language("zh-cn"), "zh-CN")
        self.assertEqual(bot_core._normalize_language("en"), "")
        with patch.dict(bot_core.I18N_MESSAGES, {"en-US": {}}, clear=False):
            self.assertEqual(bot_core._t("en-US", "none"), "无")
        self.assertTrue(bot_core._is_valid_timezone("Asia/Shanghai"))
        self.assertFalse(bot_core._is_valid_timezone("asia/shanghai"))
        self.assertEqual(bot_core.suggest_timezones(""), [])
        self.assertIn("Asia/Shanghai", bot_core.suggest_timezones("Asia/Shanghi"))
        self.assertIn("Asia/Shanghai", bot_core.format_unsupported_timezone("asia/shanghai"))

    async def test_get_copilot_and_notifier_branches(self):
        fake = FakeCopilot()
        self.assertIs(bot_core._get_copilot(fake), fake)
        self.assertIs(bot_core._get_copilot(lambda: fake), fake)
        with patch("core.bot_core.RdsCopilot", return_value=fake):
            self.assertIs(bot_core._get_copilot(None), fake)
        FactoryStyleCopilot.reset()
        self.assertIsInstance(bot_core._get_copilot(FactoryStyleCopilot), FactoryStyleCopilot)
        self.assertEqual(len(FactoryStyleCopilot.instances), 1)

        state = bot_core.ActiveConversationState("p", "c", "s")
        sent = []
        await bot_core.run_still_working_notifier(state, sent.append, interval_seconds=0)
        with patch.dict(os.environ, {"RDS_BOT_STILL_WORKING_INTERVAL_SECONDS": "bad"}), \
            patch("core.bot_core.asyncio.sleep", new=AsyncMock(side_effect=lambda delay: state.mark_done())):
            await bot_core.run_still_working_notifier(state, sent.append)

        state = bot_core.ActiveConversationState("p", "c", "s")
        state.started_at -= 180
        task = asyncio.create_task(
            bot_core.run_still_working_notifier(state, sent.append, interval_seconds=0.001, language="zh-CN")
        )
        await asyncio.sleep(0.01)
        state.mark_done()
        await task
        self.assertTrue(any("仍在处理中" in item and "3 分钟" in item for item in sent))

        cancelled_state = bot_core.ActiveConversationState("p", "c", "s")
        cancelled_state.request_cancel()
        cancelled_sent = []
        sleep_count = 0

        async def sleep_until_cancel_checked(delay):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                cancelled_state.mark_done()

        with patch("core.bot_core.asyncio.sleep", new=AsyncMock(side_effect=sleep_until_cancel_checked)):
            await bot_core.run_still_working_notifier(
                cancelled_state,
                cancelled_sent.append,
                interval_seconds=0.001,
                language="zh-CN",
            )
        self.assertEqual(cancelled_sent, [])

    async def test_control_commands_cover_status_lists_checkout_agent_skills_and_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(os.path.join(tmp_dir, "conversations.json"))
            store.set_language("chat", "sender", "en-US", platform="dingtalk")
            cache = bot_core.RuntimeCache()
            registry = bot_core.ActiveConversationRegistry()
            context = bot_core.BotContext("dingtalk", "chat", "sender", store, cache=cache, registry=registry)
            copilot = Mock()
            copilot.list_conversations.return_value = {
                "Data": [
                    {"Id": "oldconv1234567890", "Name": "Old Conversation", "CreatedAt": 1764055091000},
                    {"Id": "abcdef1234567890", "Name": "Conversation", "CreatedAt": 1764055092000},
                ],
                "HasMore": True,
            }
            copilot.list_custom_agents.return_value = {"Data": [{"Id": "agent-1", "Name": "Agent A", "EnableTools": True, "Tools": [1]}]}
            copilot.list_skills.return_value = {"Data": [{"Name": "sql-review"}], "PageNumber": 2, "TotalCount": 1}

            self.assertIn("RDS Copilot commands", (await bot_core.handle_control_command("/help", context, copilot)).content)
            self.assertIn("RDS Copilot commands", (await bot_core.handle_control_command("$help", context, copilot)).content)
            self.assertIn("No active task", (await bot_core.handle_control_command("/btw", context, copilot)).content)
            self.assertIn("No active task", (await bot_core.handle_control_command("$btw", context, copilot)).content)
            self.assertIn("No active task to stop", (await bot_core.handle_control_command("/stop", context, copilot)).content)
            empty_state = registry.start(context)
            self.assertIn(
                "No displayable content",
                (await bot_core.handle_control_command("/btw", context, copilot)).content,
            )
            registry.finish(empty_state)
            self.assertIn("enabled", (await bot_core.handle_control_command("/card on", context, copilot)).content)
            self.assertIn("enabled", (await bot_core.handle_control_command("$card on", context, copilot)).content)
            self.assertIn("Card replies: `on`", (await bot_core.handle_control_command("/card", context, copilot)).content)
            self.assertIn("Card replies: `on`", (await bot_core.handle_control_command("$card", context, copilot)).content)
            invalid_card = await bot_core.handle_control_command("/card status", context, copilot)
            invalid_dollar_card = await bot_core.handle_control_command("$card status", context, copilot)
            self.assertTrue(invalid_card.handled)
            self.assertIn("Invalid command argument", invalid_card.content)
            self.assertTrue(invalid_dollar_card.handled)
            self.assertIn("`$card status`", invalid_dollar_card.content)
            self.assertIn("disabled", (await bot_core.handle_control_command("/card off", context, copilot)).content)
            self.assertIn("enabled", (await bot_core.handle_control_command("/session on", context, copilot)).content)
            self.assertIn("enabled", (await bot_core.handle_control_command("$session on", context, copilot)).content)
            self.assertIn("Session: `on`", (await bot_core.handle_control_command("/session", context, copilot)).content)
            self.assertIn("Session: `on`", (await bot_core.handle_control_command("$session", context, copilot)).content)
            invalid_session = await bot_core.handle_control_command("/session status", context, copilot)
            invalid_dollar_session = await bot_core.handle_control_command("$session status", context, copilot)
            self.assertTrue(invalid_session.handled)
            self.assertIn("Invalid command argument", invalid_session.content)
            self.assertTrue(invalid_dollar_session.handled)
            self.assertIn("`$session status`", invalid_dollar_session.content)
            sessions = await bot_core.handle_control_command("/session ls", context, copilot)
            copilot.list_conversations.assert_called_with(limit=10, sort_by="UpdatedAt")
            self.assertLess(sessions.content.index("abcdef12"), sessions.content.index("oldconv1"))
            self.assertNotIn("id=", sessions.content)
            self.assertIn("Conversation", sessions.content)
            self.assertIn("Checked out", (await bot_core.handle_control_command("/session abcdef12", context, copilot)).content)
            self.assertIn("Checked out", (await bot_core.handle_control_command("$session abcdef12", context, copilot)).content)
            self.assertIn("Checked out", (await bot_core.handle_control_command("/session 01234567-89ab-cdef-0123-456789abcdef", context, copilot)).content)
            self.assertIn("disabled", (await bot_core.handle_control_command("/session off", context, copilot)).content)
            self.assertIn("disabled", (await bot_core.handle_control_command("$session off", context, copilot)).content)
            self.assertIn("Session: `off`", (await bot_core.handle_control_command("/session", context, copilot)).content)
            self.assertIn("Started a new conversation", (await bot_core.handle_control_command("/new", context, copilot)).content)
            self.assertIn("Started a new conversation", (await bot_core.handle_control_command("$new", context, copilot)).content)
            self.assertIn("Agent A", (await bot_core.handle_control_command("/agent ls", context, copilot)).content)
            self.assertIn("Agent A", (await bot_core.handle_control_command("$agent ls", context, copilot)).content)
            self.assertIn("default", (await bot_core.handle_control_command("/agent", context, copilot)).content)
            self.assertIn("default", (await bot_core.handle_control_command("$agent", context, copilot)).content)
            self.assertIn("Agent not found", (await bot_core.handle_control_command("/agent Missing", context, copilot)).content)
            self.assertIn("selected", (await bot_core.handle_control_command("/agent Agent A", context, copilot)).content)
            self.assertIn("selected", (await bot_core.handle_control_command("$agent Agent A", context, copilot)).content)
            self.assertIn("Agent A", (await bot_core.handle_control_command("/agent", context, copilot)).content)
            self.assertIn("default", (await bot_core.handle_control_command("/agent default", context, copilot)).content)
            self.assertIn("default", (await bot_core.handle_control_command("$agent default", context, copilot)).content)
            self.assertIn("言語を `ja-JP`", (await bot_core.handle_control_command("/language JA_jp", context, copilot)).content)
            self.assertIn("言語を `ja-JP`", (await bot_core.handle_control_command("$language JA_jp", context, copilot)).content)
            self.assertIn("タイムゾーンを `Asia/Tokyo`", (await bot_core.handle_control_command("/tz Asia/Tokyo", context, copilot)).content)
            self.assertIn("タイムゾーンを `Asia/Tokyo`", (await bot_core.handle_control_command("$tz Asia/Tokyo", context, copilot)).content)
            self.assertIn("未対応の言語", (await bot_core.handle_control_command("/language klingon", context, copilot)).content)
            fuzzy_timezone = await bot_core.handle_control_command("/tz asia/shanghai", context, copilot)
            self.assertIn("未対応のタイムゾーン", fuzzy_timezone.content)
            self.assertIn("Asia/Shanghai", fuzzy_timezone.content)
            self.assertIn("未対応のタイムゾーン", (await bot_core.handle_control_command("/tz Mars/Base", context, copilot)).content)
            self.assertIn("sql-review", (await bot_core.handle_control_command("/skills 2", context, copilot)).content)
            self.assertIn("sql-review", (await bot_core.handle_control_command("$skills 2", context, copilot)).content)
            copilot.list_skills.assert_called_with(page_number=2, page_size=20, language="ja-JP")
            self.assertIn("コマンド引数が正しくありません", (await bot_core.handle_control_command("/skills ja-JP", context, copilot)).content)
            self.assertIn("`$skills ja-JP`", (await bot_core.handle_control_command("$skills ja-JP", context, copilot)).content)
            self.assertIn("コマンド引数が正しくありません", (await bot_core.handle_control_command("/skills klingon", context, copilot)).content)
            self.assertIn("コマンド引数が正しくありません", (await bot_core.handle_control_command("/session abc", context, copilot)).content)
            self.assertIn("`$session abc`", (await bot_core.handle_control_command("$session abc", context, copilot)).content)
            self.assertFalse((await bot_core.handle_control_command("/session normal question", context, copilot)).handled)
            self.assertFalse((await bot_core.handle_control_command("$session normal question", context, copilot)).handled)
            self.assertFalse((await bot_core.handle_control_command("/session checkout abcdef12", context, copilot)).handled)
            self.assertFalse((await bot_core.handle_control_command("/sql-review select 1", context, copilot)).handled)
            self.assertFalse((await bot_core.handle_control_command("$sql-review select 1", context, copilot)).handled)

            empty_cache_context = bot_core.BotContext("dingtalk", "other", "sender", store, cache=bot_core.RuntimeCache(), registry=registry)
            self.assertIn(
                "没有找到这个对话",
                (await bot_core.handle_control_command("/session abcdef12", empty_cache_context, copilot)).content,
            )

    async def test_control_commands_accept_copilot_class_factory(self):
        FactoryStyleCopilot.reset()
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(os.path.join(tmp_dir, "conversations.json"))
            context = bot_core.BotContext("feishu", "chat", "sender", store)

            agents = await bot_core.handle_control_command("/agent ls", context, FactoryStyleCopilot)
            sessions = await bot_core.handle_control_command("/session ls", context, FactoryStyleCopilot)
            skills = await bot_core.handle_control_command("/skills", context, FactoryStyleCopilot)

        self.assertIn("Factory Agent", agents.content)
        self.assertIn("Factory Conversation", sessions.content)
        self.assertIn("factory-skill", skills.content)
        self.assertGreaterEqual(len(FactoryStyleCopilot.instances), 3)

    async def test_call_with_stream_covers_tools_duplicates_documents_unknown_stop_and_exceptions(self):
        updates = []
        tool_payload = json.dumps({"tool_call_name": "DescribeDBInstances", "tool_call_id": "tool-1", "status": "start"})
        copilot = FakeCopilot(
            [
                MessageEvent("task-1", "conv-1", "hello"),
                ToolCallStart("task-1", "conv-1", "DescribeDBInstances", tool_payload, "tool-1"),
                ToolCallPending("task-1", "conv-1", "DescribeDBInstances", tool_payload, "tool-1"),
                ToolCallEnd("task-1", "conv-1", "Bad", "{bad", "tool-2"),
                DocumentEvent("task-1", "conv-1", "Docs", "{}"),
                SimpleNamespace(task_id="task-1", conversion_id="conv-1"),
            ]
        )

        result = await bot_core.call_with_stream(
            "query",
            updates.append,
            copilot,
            custom_agent_id="agent-1",
            language="zh-TW",
            timezone="Asia/Taipei",
        )

        self.assertEqual(result["content"], "hello")
        self.assertEqual(result["conversion_id"], "conv-1")
        self.assertEqual(result["preparations"], [{"name": "DescribeDBInstances"}])
        self.assertEqual(copilot.chat_calls[0], ("query", "", "agent-1", "zh-TW", "Asia/Taipei"))

        active_state = bot_core.ActiveConversationState("p", "c", "s")
        stopping_copilot = FakeCopilot([MessageEvent("task-2", "conv-2", "part")], stop_error=RuntimeError("stop failed"))
        active_state.record_task_id("task-2")
        active_state.request_cancel()
        result = await bot_core.call_with_stream("query", updates.append, stopping_copilot, active_state=active_state)
        self.assertEqual(result["content"], "part")
        self.assertEqual(stopping_copilot.stopped, ["task-2"])

        active_state = bot_core.ActiveConversationState("p", "c", "s")
        await bot_core.call_with_stream(
            "query",
            updates.append,
            FakeCopilot([ToolCallStart("task-3", "conv-3", "Tool", tool_payload, "tool-3")]),
            active_state=active_state,
        )
        self.assertEqual(active_state.snapshot()["phase"], "running: DescribeDBInstances")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await bot_core.call_with_stream("query", updates.append, FakeCopilot(error=RuntimeError("boom")))

    async def test_call_with_stream_records_task_id_from_async_progress_before_stop(self):
        class AsyncProgressCopilot:
            def __init__(self):
                self.release = asyncio.Event()
                self.stopped = []
                self.chat_calls = []

            async def chat_async(
                self,
                query,
                conversion_id="",
                custom_agent_id="",
                language="zh-CN",
                timezone="Asia/Shanghai",
                include_progress_events=False,
            ):
                self.chat_calls.append((query, conversion_id, custom_agent_id, language, timezone, include_progress_events))
                if include_progress_events:
                    yield SimpleNamespace(task_id="task-from-workflow", conversion_id="conv-progress")
                await self.release.wait()
                yield MessageEvent("task-from-workflow", "conv-progress", "final")

            def stop_task(self, task_id):
                self.stopped.append(task_id)
                self.release.set()

        updates = []
        registry = bot_core.ActiveConversationRegistry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(os.path.join(tmp_dir, "conversations.json"))
            context = bot_core.BotContext("dingtalk", "chat-1", "sender-1", store, registry=registry)
            active_state = registry.start(context)
            copilot = AsyncProgressCopilot()
            stream_task = asyncio.create_task(
                bot_core.call_with_stream("query", updates.append, copilot, active_state=active_state)
            )

            for _ in range(100):
                if active_state.snapshot()["task_id"] == "task-from-workflow":
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(active_state.snapshot()["task_id"], "task-from-workflow")
            result = await bot_core.handle_control_command("/stop", context, copilot)

            self.assertTrue(result.handled)
            self.assertEqual(copilot.stopped, ["task-from-workflow"])
            stream_result = await asyncio.wait_for(stream_task, timeout=1)
            self.assertEqual(stream_result["content"], "final")
            self.assertEqual(copilot.chat_calls[0][-1], True)

    async def test_call_with_stream_async_path_handles_rendered_events_and_errors(self):
        class AsyncRenderedCopilot:
            async def chat_async(
                self,
                query,
                conversion_id="",
                custom_agent_id="",
                language="zh-CN",
                timezone="Asia/Shanghai",
                include_progress_events=False,
            ):
                yield MessageEvent("task-1", "conv-1", "hello")
                payload = json.dumps({"tool_call_name": "DescribeDBInstances", "tool_call_id": "tool-1", "status": "start"})
                yield ToolCallStart("task-1", "conv-1", "DescribeDBInstances", payload, "tool-1")
                yield ToolCallPending("task-1", "conv-1", "DescribeDBInstances", payload, "tool-1")
                yield ToolCallEnd("task-1", "conv-1", "Bad", "{bad", "tool-2")
                yield DocumentEvent("task-1", "conv-1", "Docs", "{}")
                yield object()

        class AsyncFailingCopilot:
            async def chat_async(self, *args, **kwargs):
                raise RuntimeError("async boom")
                yield

        class BadSignatureAsyncChat:
            @property
            def __signature__(self):
                raise ValueError("bad signature")

            async def __call__(self, *args, **kwargs):
                yield MessageEvent("task-bad-signature", "conv-bad-signature", "ok")

        class BadSignatureCopilot:
            def __init__(self):
                self.chat_async = BadSignatureAsyncChat()

        updates = []
        active_state = bot_core.ActiveConversationState("p", "c", "s")
        result = await bot_core.call_with_stream("query", updates.append, AsyncRenderedCopilot(), active_state=active_state)

        self.assertEqual(result["content"], "hello")
        self.assertEqual(result["preparations"], [{"name": "DescribeDBInstances"}])
        self.assertEqual(active_state.snapshot()["event_count"], 6)

        with self.assertRaisesRegex(RuntimeError, "async boom"):
            await bot_core.call_with_stream("query", updates.append, AsyncFailingCopilot())

        signature_result = await bot_core.call_with_stream("query", updates.append, BadSignatureCopilot())
        self.assertEqual(signature_result["content"], "ok")


class FeishuBridgeCoverageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "true"})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def make_bridge(self, store=None):
        return feishu_bridge.FeishuBridge(
            app_id="cli_xxx",
            app_secret="secret_xxx",
            store=store or bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: FakeCopilot([MessageEvent("task-1", "conv-1", "answer")]),
        )

    async def test_start_forever_validates_sdk_and_credentials_then_builds_clients(self):
        bridge = self.make_bridge()
        with patch("bridges.feishu.FEISHU_AVAILABLE", False):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                await bridge.start_forever()

        with patch("bridges.feishu.FEISHU_AVAILABLE", True), patch.dict(os.environ, {}, clear=True):
            bridge = feishu_bridge.FeishuBridge(app_id="", app_secret="", store=bot_core.CopilotConversationStore(""))
            with self.assertRaisesRegex(RuntimeError, "FEISHU_APP_ID"):
                await bridge.start_forever()

        class FakeFeishuAuthResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        with patch("bridges.feishu.urllib_request.urlopen", return_value=FakeFeishuAuthResponse({"code": 0})):
            feishu_bridge.validate_feishu_startup(app_id="cli_xxx", app_secret="secret_xxx")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FEISHU_APP_ID"):
                feishu_bridge.validate_feishu_startup(app_id="", app_secret="")

        with patch("bridges.feishu.urllib_request.urlopen", side_effect=OSError("network down")):
            with self.assertRaisesRegex(RuntimeError, "飞书鉴权失败[\\s\\S]*network down"):
                feishu_bridge.validate_feishu_startup(app_id="cli_xxx", app_secret="secret_xxx")

        feishu_http_error = urllib_error.HTTPError(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            403,
            "Forbidden",
            {},
            io.BytesIO("proxy blocked feishu".encode("utf-8")),
        )
        with patch("bridges.feishu.urllib_request.urlopen", side_effect=feishu_http_error):
            with self.assertRaisesRegex(RuntimeError, "飞书鉴权失败[\\s\\S]*proxy blocked feishu"):
                feishu_bridge.validate_feishu_startup(app_id="cli_xxx", app_secret="secret_xxx")

        with patch("bridges.feishu.urllib_request.urlopen", return_value=FakeFeishuAuthResponse({"code": 999, "msg": "bad app"})):
            with self.assertRaisesRegex(RuntimeError, "飞书鉴权失败.*bad app"):
                feishu_bridge.validate_feishu_startup(app_id="cli_xxx", app_secret="secret_xxx")

        bridge = self.make_bridge()
        ws_instance = SimpleNamespace(start=Mock())
        with patch.object(bridge, "_build_lark_client", return_value=object()) as build_client, \
            patch.object(bridge, "_build_event_handler", return_value=object()) as build_handler, \
            patch.object(bridge, "_load_bot_identity") as load_bot_identity, \
            patch("bridges.feishu.FeishuWSClient", return_value=ws_instance), \
            patch("bridges.feishu.FEISHU_AVAILABLE", True):
            await bridge.start_forever()
        load_bot_identity.assert_called_once()
        build_client.assert_called_once()
        build_handler.assert_called_once()
        ws_instance.start.assert_called_once()

    async def test_start_forever_rehomes_lark_ws_module_loop_in_worker_thread(self):
        bridge = self.make_bridge()
        running_loop = asyncio.get_running_loop()
        fake_ws_module = SimpleNamespace(loop=running_loop)

        class LoopSensitiveWsClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                fake_ws_module.loop.run_until_complete(asyncio.sleep(0))

        with patch.object(bridge, "_build_lark_client", return_value=object()), \
            patch.object(bridge, "_build_event_handler", return_value=object()), \
            patch.object(bridge, "_load_bot_identity"), \
            patch("bridges.feishu.FeishuWSClient", side_effect=lambda **kwargs: LoopSensitiveWsClient(**kwargs)), \
            patch("bridges.feishu.lark_ws_client", fake_ws_module, create=True), \
            patch("bridges.feishu.FEISHU_AVAILABLE", True):
            await bridge.start_forever()

    async def test_start_feishu_ws_client_without_lark_ws_module(self):
        ws_client = SimpleNamespace(start=Mock())
        with patch("bridges.feishu.lark_ws_client", None):
            feishu_bridge._start_feishu_ws_client(ws_client)
        ws_client.start.assert_called_once()

    async def test_feishu_helpers_parse_text_sender_domain_and_background_failures(self):
        self.assertEqual(feishu_bridge._load_feishu_message_text('{"text":" hello "}'), "hello")
        self.assertEqual(feishu_bridge._load_feishu_message_text("raw text"), "raw text")
        self.assertEqual(feishu_bridge._load_feishu_message_text("[1,2]"), "")
        self.assertEqual(feishu_bridge._extract_sender_id(SimpleNamespace(open_id="", user_id="u", union_id="un")), "u")
        self.assertEqual(feishu_bridge._extract_sender_id(SimpleNamespace(open_id="", user_id="", union_id="un")), "un")
        self.assertEqual(feishu_bridge._extract_sender_id({"open_id": "ou_dict", "user_id": "u", "union_id": "un"}), "ou_dict")
        mention_message = SimpleNamespace(mentions=[{"id": {"open_id": "ou_bot_1", "user_id": "u_bot_1"}, "name": "RDS Bot"}])
        with patch.dict(os.environ, {"FEISHU_BOT_OPEN_ID": "ou_bot_1"}, clear=True):
            self.assertTrue(feishu_bridge._is_message_mentioning_bot(mention_message, "hello"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feishu_bridge._is_message_mentioning_bot(mention_message, "hello"))
        self.assertEqual(feishu_bridge._strip_feishu_leading_mentions("@_user_1 /session", mention_message), "/session")
        self.assertEqual(feishu_bridge._strip_feishu_leading_mentions("hello @_user_1", mention_message), "hello @_user_1")
        self.assertEqual(feishu_bridge.FeishuBridge(app_id="a", app_secret="s", domain="lark")._domain_url(), feishu_bridge.LARK_DOMAIN_URL)
        self.assertEqual(feishu_bridge.FeishuBridge(app_id="a", app_secret="s", domain="feishu")._domain_url(), feishu_bridge.FEISHU_DOMAIN_URL)
        self.assertEqual(feishu_bridge._build_feishu_markdown_post_rows(""), [[{"tag": "md", "text": ""}]])
        code_rows = feishu_bridge._build_feishu_markdown_post_rows("before\n```sql\nselect 1\n```\nafter")
        self.assertEqual([row[0]["text"] for row in code_rows], ["before", "```sql\nselect 1\n```", "after"])
        leading_code_rows = feishu_bridge._build_feishu_markdown_post_rows("```sql\nselect 1\n```")
        self.assertEqual(leading_code_rows[0][0]["text"], "```sql\nselect 1\n```")
        table_type, table_payload = feishu_bridge._build_feishu_outbound_payload("| a |\n|---|\n| b |")
        self.assertEqual(table_type, "text")
        self.assertIn("| a |", json.loads(table_payload)["text"])

        future = Mock()
        future.result.side_effect = RuntimeError("background")
        feishu_bridge.FeishuBridge._log_background_failure(future)

    async def test_load_bot_identity_fetches_tokens_and_fails_loudly_when_mentions_require_it(self):
        bridge = self.make_bridge()
        with patch("bridges.feishu.fetch_feishu_bot_info", return_value={"open_id": "ou_bot_1", "app_name": "RDS Bot"}):
            bridge._load_bot_identity()
        self.assertIn("ou_bot_1", bridge.bot_tokens)
        self.assertIn("RDS Bot", bridge.bot_tokens)

        bridge = self.make_bridge()
        with patch.dict(os.environ, {"FEISHU_GROUP_POLICY": "mention"}, clear=True), \
            patch("bridges.feishu.fetch_feishu_bot_info", side_effect=RuntimeError("bot info failed")):
            with self.assertRaisesRegex(RuntimeError, "无法获取飞书机器人身份"):
                bridge._load_bot_identity()

        bridge = self.make_bridge()
        with patch.dict(os.environ, {"FEISHU_GROUP_POLICY": "open"}, clear=True), \
            patch("bridges.feishu.fetch_feishu_bot_info", side_effect=RuntimeError("bot info failed")):
            bridge._load_bot_identity()
        self.assertEqual(bridge.bot_tokens, set())

    async def test_build_lark_client_event_handler_builders_and_entrypoint(self):
        class ClientBuilder:
            def __init__(self):
                self.calls = []

            def app_id(self, value):
                self.calls.append(("app_id", value))
                return self

            def app_secret(self, value):
                self.calls.append(("app_secret", value))
                return self

            def domain(self, value):
                self.calls.append(("domain", value))
                return self

            def log_level(self, value):
                self.calls.append(("log_level", value))
                return self

            def build(self):
                return SimpleNamespace(calls=self.calls)

        class HandlerBuilder:
            def __init__(self):
                self.registered = None

            def register_p2_im_message_receive_v1(self, callback):
                self.registered = callback
                return self

            def build(self):
                return SimpleNamespace(registered=self.registered)

        bridge = feishu_bridge.FeishuBridge(app_id="cli", app_secret="secret", domain="lark")
        with patch(
            "bridges.feishu.lark",
            SimpleNamespace(
                Client=SimpleNamespace(builder=lambda: ClientBuilder()),
                LogLevel=SimpleNamespace(WARNING="warning"),
            ),
        ):
            client = bridge._build_lark_client()
        self.assertIn(("domain", feishu_bridge.LARK_DOMAIN_URL), client.calls)

        with patch("bridges.feishu.EventDispatcherHandler", SimpleNamespace(builder=lambda token, key: HandlerBuilder())):
            handler = bridge._build_event_handler()
        self.assertEqual(handler.registered, bridge._on_message_event)

        class Builder:
            def __init__(self):
                self.values = {}

            def content(self, value):
                self.values["content"] = value
                return self

            def msg_type(self, value):
                self.values["msg_type"] = value
                return self

            def reply_in_thread(self, value):
                self.values["reply_in_thread"] = value
                return self

            def uuid(self, value):
                self.values["uuid"] = value
                return self

            def message_id(self, value):
                self.values["message_id"] = value
                return self

            def request_body(self, value):
                self.values["request_body"] = value
                return self

            def receive_id(self, value):
                self.values["receive_id"] = value
                return self

            def receive_id_type(self, value):
                self.values["receive_id_type"] = value
                return self

            def build(self):
                return SimpleNamespace(**self.values)

        sdk_body = SimpleNamespace(builder=lambda: Builder())
        with patch("bridges.feishu.ReplyMessageRequestBody", sdk_body), \
            patch("bridges.feishu.ReplyMessageRequest", sdk_body), \
            patch("bridges.feishu.CreateMessageRequestBody", sdk_body), \
            patch("bridges.feishu.CreateMessageRequest", sdk_body):
            reply_body = feishu_bridge.FeishuBridge._build_reply_message_body(
                content="{}", msg_type="text", reply_in_thread=True, uuid_value="uuid-1"
            )
            reply_request = feishu_bridge.FeishuBridge._build_reply_message_request("msg-1", reply_body)
            create_body = feishu_bridge.FeishuBridge._build_create_message_body(
                receive_id="chat-1", msg_type="text", content="{}", uuid_value="uuid-2"
            )
            create_request = feishu_bridge.FeishuBridge._build_create_message_request("chat_id", create_body)
        self.assertTrue(reply_body.reply_in_thread)
        self.assertEqual(reply_request.message_id, "msg-1")
        self.assertEqual(create_body.receive_id, "chat-1")
        self.assertEqual(create_request.receive_id_type, "chat_id")

        with patch("bridges.feishu.FeishuBridge") as bridge_cls, patch("bridges.feishu.asyncio.run") as run:
            bridge_cls.return_value.start_forever.return_value = object()
            feishu_bridge.run_feishu_bridge()
        run.assert_called_once()

    async def test_on_message_event_requires_loop_and_schedules_coroutine(self):
        bridge = self.make_bridge()
        bridge._on_message_event(SimpleNamespace())

        loop = SimpleNamespace(is_closed=lambda: False)
        bridge.loop = loop
        with patch("bridges.feishu.asyncio.run_coroutine_threadsafe") as run_threadsafe:
            run_threadsafe.return_value = SimpleNamespace(add_done_callback=Mock())
            bridge.handle_message_event_data = Mock(return_value=object())
            bridge._on_message_event(SimpleNamespace(event=SimpleNamespace()))
        run_threadsafe.assert_called_once()

    async def test_event_data_ignores_malformed_and_replies_to_authorized_non_text(self):
        bridge = self.make_bridge()
        bridge.send_text = AsyncMock(return_value=True)
        await bridge.handle_message_event_data(SimpleNamespace(event=SimpleNamespace()))
        bridge.send_text.assert_not_awaited()

        data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(chat_id="chat-1", message_id="msg-1", message_type="image", content="{}"),
            )
        )
        with patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_1"}, clear=True):
            await bridge.handle_message_event_data(data)
        bridge.send_text.assert_awaited_once_with(
            "chat-1",
            "I can only process text messages.",
            reply_to_message_id="msg-1",
            source=bot_core.SessionSource("feishu", "chat-1", "dm", "ou_1"),
        )

    async def test_unauthorized_feishu_non_text_message_is_ignored(self):
        bridge = self.make_bridge()
        bridge.send_text = AsyncMock(return_value=True)
        data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(chat_id="chat-1", message_id="msg-1", message_type="image", content="{}"),
            )
        )

        with patch.dict(os.environ, {}, clear=True):
            await bridge.handle_message_event_data(data)

        bridge.send_text.assert_not_awaited()

    async def test_handle_event_data_accepts_group_mentions_and_dict_sender_ids(self):
        bridge = self.make_bridge()
        bridge.send_text = AsyncMock(return_value=True)
        event_data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_type="user",
                    sender_id={"open_id": "ou_user_1", "union_id": "on_user_1"},
                ),
                message=SimpleNamespace(
                    chat_id="oc_group_1",
                    chat_type="group",
                    message_id="om_msg_1",
                    message_type="text",
                    content=json.dumps({"text": "hello"}),
                    mentions=[
                        {
                            "id": {"open_id": "ou_bot_1", "user_id": "u_bot_1"},
                            "name": "RDS Bot",
                        }
                    ],
                ),
            )
        )

        with patch.dict(os.environ, {"FEISHU_ALLOW_ALL_USERS": "true", "FEISHU_BOT_OPEN_ID": "ou_bot_1"}, clear=True):
            await bridge.handle_message_event_data(event_data)

        bridge.send_text.assert_awaited_with(
            "oc_group_1",
            "answer",
            reply_to_message_id="om_msg_1",
            source=bot_core.SessionSource(
                "feishu",
                "oc_group_1",
                "group",
                "ou_user_1",
                user_id_alt="on_user_1",
            ),
        )

    async def test_group_mention_prefix_is_stripped_before_control_command_matching(self):
        bridge = self.make_bridge()
        bridge.bot_tokens = {"ou_bot_1"}
        bridge.send_text = AsyncMock(return_value=True)
        event_data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_type="user",
                    sender_id={"open_id": "ou_user_1", "union_id": "on_user_1"},
                ),
                message=SimpleNamespace(
                    chat_id="oc_group_1",
                    chat_type="group",
                    message_id="om_msg_1",
                    message_type="text",
                    content=json.dumps({"text": "@_user_1 /session"}),
                    mentions=[
                        {
                            "key": "@_user_1",
                            "id": {"open_id": "ou_bot_1", "user_id": "u_bot_1"},
                            "name": "RDS Bot",
                        }
                    ],
                ),
            )
        )

        with patch.dict(os.environ, {"FEISHU_ALLOW_ALL_USERS": "true"}, clear=True):
            await bridge.handle_message_event_data(event_data)

        bridge.send_text.assert_awaited_once()
        self.assertIn("ConversationId", bridge.send_text.await_args.args[1])
        self.assertNotIn("answer", bridge.send_text.await_args.args[1])

    async def test_handle_text_message_no_message_exception_and_reaction_failure_paths(self):
        store = bot_core.CopilotConversationStore("")
        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: FakeCopilot([]),
        )
        bridge.send_text = AsyncMock(return_value=True)
        bridge.add_processing_reaction = AsyncMock(return_value="reaction-1")
        bridge.remove_processing_reaction = AsyncMock(return_value=True)
        await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
        self.assertIn("未返回回复内容", bridge.send_text.await_args.args[1])

        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: FakeCopilot([]),
        )
        bridge.send_text = AsyncMock(return_value=True)
        bridge.add_processing_reaction = AsyncMock(return_value="reaction-1")
        bridge.remove_processing_reaction = AsyncMock(return_value=True)
        with patch(
            "bridges.feishu.call_with_stream",
            new=AsyncMock(return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": True}),
        ):
            await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
        bridge.send_text.assert_not_awaited()
        bridge.remove_processing_reaction.assert_awaited_once_with("msg", "reaction-1")

        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: FakeCopilot(error=ConnectionError("reset")),
        )
        bridge.send_text = AsyncMock(return_value=True)
        bridge.add_processing_reaction = AsyncMock(return_value="reaction-1")
        bridge.remove_processing_reaction = AsyncMock(return_value=True)
        bridge.add_failure_reaction = AsyncMock(return_value="failure-1")
        await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
        self.assertIn("RDS AI 诊断失败", bridge.send_text.await_args.args[1])
        self.assertIn("msg", bridge.send_text.await_args.args[1])
        self.assertNotIn("ConnectionError", bridge.send_text.await_args.args[1])
        self.assertNotIn("reset", bridge.send_text.await_args.args[1])
        bridge.add_failure_reaction.assert_awaited_once_with("msg")

        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: FakeCopilot(error=ConnectionError("reset")),
        )
        bridge.send_text = AsyncMock(return_value=True)
        bridge.add_processing_reaction = AsyncMock(return_value="")
        bridge.remove_processing_reaction = AsyncMock(return_value=False)
        bridge.add_failure_reaction = AsyncMock()
        await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
        bridge.add_failure_reaction.assert_not_awaited()

    async def test_handle_text_message_control_command_and_busy_paths(self):
        store = bot_core.CopilotConversationStore("")
        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: FakeCopilot([MessageEvent("task-1", "conv-1", "answer")]),
        )
        bridge.send_text = AsyncMock(return_value=True)

        await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="/help")
        self.assertIn("/session", bridge.send_text.await_args.args[1])

        stop_copilot = FakeCopilot()
        registry = bot_core.ActiveConversationRegistry()
        state = registry.start(bot_core.BotContext("feishu", "chat", "sender", store, registry=registry))
        state.record_task_id("task-stop-1")
        state.record_message("partial feishu answer")
        stop_bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=store,
            copilot_factory=lambda: stop_copilot,
        )
        stop_bridge.send_text = AsyncMock(return_value=True)
        with patch("core.bot_core.get_active_registry", return_value=registry):
            await stop_bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="/stop")
        self.assertEqual([call.args[1] for call in stop_bridge.send_text.await_args_list], ["partial feishu answer", "已停止当前任务。"])
        self.assertEqual(stop_copilot.stopped, ["task-stop-1"])

        registry = bot_core.ActiveConversationRegistry()
        registry.start(bot_core.BotContext("feishu", "chat", "sender", store, registry=registry))
        with patch("core.bot_core.get_active_registry", return_value=registry):
            await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
        self.assertIn("/btw", bridge.send_text.await_args.args[1])

    async def test_unauthorized_feishu_message_is_ignored_before_copilot(self):
        bridge = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: FakeCopilot([MessageEvent("task-1", "conv-1", "answer")]),
        )
        bridge.send_text = AsyncMock(return_value=True)
        log_messages = []
        sink_id = feishu_bridge.logger.add(lambda message: log_messages.append(str(message)), level="INFO")
        with patch.dict(os.environ, {}, clear=True):
            try:
                await bridge.handle_text_message(chat_id="chat", sender_id="sender", message_id="msg", text="query")
            finally:
                feishu_bridge.logger.remove(sink_id)
        bridge.send_text.assert_not_awaited()
        joined_logs = "\n".join(log_messages)
        self.assertIn("chat_id=chat", joined_logs)
        self.assertIn("sender_id=sender", joined_logs)
        self.assertIn("pre_filter_allowed=", joined_logs)
        self.assertIn("authorized=", joined_logs)

    async def test_send_text_covers_reply_create_rejected_exception_truncate_and_fallback_builders(self):
        class Response:
            def __init__(self, ok=True):
                self.ok = ok
                self.code = 230099
                self.msg = "rejected"

            def success(self):
                return self.ok

        class MessageApi:
            def __init__(self):
                self.reply_calls = []
                self.create_calls = []
                self.reply_response = Response(True)
                self.create_response = Response(True)

            def reply(self, request):
                self.reply_calls.append(request)
                return self.reply_response

            def create(self, request):
                self.create_calls.append(request)
                return self.create_response

        message_api = MessageApi()
        bridge = self.make_bridge()
        bridge.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))

        with patch("bridges.feishu.ReplyMessageRequestBody", None), \
            patch("bridges.feishu.ReplyMessageRequest", None), \
            patch("bridges.feishu.CreateMessageRequestBody", None), \
            patch("bridges.feishu.CreateMessageRequest", None):
            self.assertTrue(await bridge.send_text("chat", "### Agents\n- `agent-1` test", reply_to_message_id="msg-md"))
            self.assertTrue(await bridge.send_text("chat", "hello", reply_to_message_id="msg-1"))
            self.assertTrue(await bridge.send_text("chat", "x" * (feishu_bridge.MAX_FEISHU_TEXT_LENGTH + 5)))

        self.assertEqual(message_api.reply_calls[0].message_id, "msg-md")
        self.assertEqual(message_api.reply_calls[0].request_body.msg_type, "post")
        markdown_payload = json.loads(message_api.reply_calls[0].request_body.content)
        self.assertEqual(markdown_payload["zh_cn"]["content"][0][0]["tag"], "md")
        self.assertIn("### Agents", markdown_payload["zh_cn"]["content"][0][0]["text"])
        self.assertEqual(message_api.reply_calls[1].message_id, "msg-1")
        self.assertEqual(message_api.reply_calls[1].request_body.msg_type, "text")
        create_payload = json.loads(message_api.create_calls[0].request_body.content)
        self.assertTrue(create_payload["text"].endswith("...(truncated)"))

        group_source = bot_core.SessionSource("feishu", "chat", "group", "ou_user_1", user_name="Alice")
        self.assertTrue(
            await bridge.send_text(
                "chat",
                "group hello",
                reply_to_message_id="msg-group",
                source=group_source,
            )
        )
        group_payload = json.loads(message_api.reply_calls[-1].request_body.content)
        self.assertEqual(group_payload["text"], '<at user_id="ou_user_1">Alice</at>\ngroup hello')
        self.assertTrue(
            await bridge.send_text(
                "chat",
                "### Agents\n- `agent-1` test",
                reply_to_message_id="msg-group-md",
                source=group_source,
            )
        )
        group_markdown_payload = json.loads(message_api.reply_calls[-1].request_body.content)
        self.assertEqual(group_markdown_payload["zh_cn"]["content"][0][0]["tag"], "at")
        self.assertEqual(group_markdown_payload["zh_cn"]["content"][0][0]["user_id"], "ou_user_1")
        self.assertEqual(group_markdown_payload["zh_cn"]["content"][0][0]["user_name"], "Alice")
        self.assertEqual(group_markdown_payload["zh_cn"]["content"][1][0]["tag"], "md")
        self.assertIn("### Agents", group_markdown_payload["zh_cn"]["content"][1][0]["text"])

        dm_source = bot_core.SessionSource("feishu", "chat", "dm", "ou_user_1", user_name="Alice")
        self.assertTrue(
            await bridge.send_text(
                "chat",
                "dm hello",
                reply_to_message_id="msg-dm",
                source=dm_source,
            )
        )
        dm_payload = json.loads(message_api.reply_calls[-1].request_body.content)
        self.assertEqual(dm_payload["text"], "dm hello")
        self.assertTrue(
            await bridge.send_text(
                "chat",
                "### Agents\n- `agent-1` test",
                reply_to_message_id="msg-dm-md",
                source=dm_source,
            )
        )
        dm_markdown_payload = json.loads(message_api.reply_calls[-1].request_body.content)
        self.assertEqual(dm_markdown_payload["zh_cn"]["content"][0][0]["tag"], "md")

        message_api.reply_response = Response(False)
        self.assertFalse(await bridge.send_text("chat", "hello", reply_to_message_id="msg-2"))
        message_api.reply = Mock(side_effect=RuntimeError("send failed"))
        self.assertFalse(await bridge.send_text("chat", "", reply_to_message_id="msg-3"))

        disconnected = self.make_bridge()
        self.assertFalse(await disconnected.send_text("chat", "hello"))

    async def test_reactions_cover_success_missing_inputs_delete_failure_and_import_exceptions(self):
        class Response:
            def __init__(self, ok=True, reaction_id="reaction-1"):
                self.ok = ok
                self.data = SimpleNamespace(reaction_id=reaction_id)

            def success(self):
                return self.ok

        class ReactionApi:
            def __init__(self):
                self.create_calls = []
                self.delete_calls = []
                self.create_response = Response(True)
                self.delete_response = Response(True)

            def create(self, request):
                self.create_calls.append(request)
                return self.create_response

            def delete(self, request):
                self.delete_calls.append(request)
                return self.delete_response

        reaction_api = ReactionApi()
        bridge = self.make_bridge()
        bridge.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_reaction=reaction_api)))

        self.assertEqual(await bridge.add_processing_reaction("msg-1"), "reaction-1")
        self.assertEqual(reaction_api.create_calls[-1].request_body.reaction_type, {"emoji_type": "Get"})
        self.assertEqual(await bridge.add_failure_reaction("msg-1"), "reaction-1")
        self.assertTrue(await bridge.remove_processing_reaction("msg-1", "reaction-1"))
        self.assertEqual(await bridge._add_reaction("", "Typing"), "")
        self.assertFalse(await bridge.remove_processing_reaction("", "reaction-1"))

        reaction_api.create_response = Response(False)
        self.assertEqual(await bridge._add_reaction("msg-1", "Typing"), "")
        reaction_api.create = Mock(side_effect=RuntimeError("create failed"))
        self.assertEqual(await bridge._add_reaction("msg-1", "Typing"), "")
        reaction_api.delete = Mock(side_effect=RuntimeError("delete failed"))
        self.assertFalse(await bridge.remove_processing_reaction("msg-1", "reaction-1"))


class WeComBridgeCoverageTest(unittest.IsolatedAsyncioTestCase):
    async def test_wecom_extracts_source_checks_auth_and_replies(self):
        store = bot_core.CopilotConversationStore("")
        fake_copilot = FakeCopilot([MessageEvent("task-1", "conv-1", "wecom answer")])
        bridge = wecom_bridge.WeComBridge(
            bot_id="bot-1",
            secret="secret-1",
            store=store,
            copilot_factory=lambda: fake_copilot,
        )
        sent = []
        bridge._send_frame = AsyncMock(side_effect=lambda frame: sent.append(frame))

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "room-1",
                "chattype": "group",
                "from": {"userid": "user-1", "name": "Alice"},
                "msgtype": "text",
                "text": {"content": "query"},
            },
        }

        with patch.dict(os.environ, {}, clear=True):
            await bridge.handle_payload(payload)
        self.assertEqual(sent, [])

        with patch.dict(os.environ, {"WECOM_ALLOWED_USERS": "user-1"}, clear=True):
            await bridge.handle_payload(payload)

        self.assertEqual(fake_copilot.chat_calls[0][0], "query")
        self.assertEqual(sent[-1]["cmd"], "aibot_respond_msg")
        self.assertEqual(sent[-1]["headers"]["req_id"], "req-1")
        self.assertIn("wecom answer", sent[-1]["body"]["markdown"]["content"])

    async def test_wecom_helpers_and_startup_validation(self):
        text = wecom_bridge.extract_wecom_text(
            {
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "part 1"}},
                        {"msgtype": "text", "text": {"content": "part 2"}},
                    ]
                },
            }
        )
        self.assertEqual(text, "part 1\npart 2")

        source = wecom_bridge.source_from_wecom_body(
            {
                "chatid": "",
                "chattype": "single",
                "from": {"userid": "user-1", "name": "Alice"},
            }
        )
        self.assertEqual(source.chat_id, "user-1")
        self.assertEqual(source.chat_type, "dm")

        with patch.dict(os.environ, {}, clear=True):
            bridge = wecom_bridge.WeComBridge(bot_id="", secret="", store=bot_core.CopilotConversationStore(""))
            with self.assertRaisesRegex(RuntimeError, "WECOM_BOT_ID"):
                await bridge.start_forever()

    async def test_wecom_connect_once_uses_heartbeat_and_ping_loop(self):
        with patch.dict(os.environ, {"WECOM_RECONNECT_BASE_SECONDS": "bad", "WECOM_RECONNECT_MAX_SECONDS": "2"}, clear=True):
            self.assertEqual(wecom_bridge._calculate_reconnect_delay(2), 2)

        class FakeWebSocket:
            def __init__(self):
                self.closed = False
                self.sent = []
                self.handshake_returned = False

            async def send_json(self, frame):
                self.sent.append(frame)

            async def receive(self):
                if not self.handshake_returned:
                    self.handshake_returned = True
                    req_id = self.sent[0]["headers"]["req_id"]
                    return SimpleNamespace(
                        type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                        data=json.dumps({"headers": {"req_id": req_id}, "errcode": 0}),
                    )
                return SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.CLOSED)

            async def close(self):
                self.closed = True

        class FakeSession:
            def __init__(self, ws):
                self.ws = ws
                self.ws_connect_kwargs = None
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                self.ws_connect_kwargs = (url, kwargs)
                return self.ws

            async def close(self):
                self.closed = True

        ws = FakeWebSocket()
        session = FakeSession(ws)
        bridge = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            heartbeat_seconds=3,
        )
        bridge._running = True
        wecom_logs = []
        sink_id = wecom_bridge.logger.add(lambda message: wecom_logs.append(str(message)), level="INFO")
        with patch("bridges.wecom.aiohttp.ClientSession", return_value=session):
            try:
                with self.assertRaisesRegex(RuntimeError, "websocket closed"):
                    await bridge._connect_once()
            finally:
                wecom_bridge.logger.remove(sink_id)
        self.assertIn("WeCom bridge connected by websocket", "\n".join(wecom_logs))
        self.assertEqual(session.ws_connect_kwargs[1]["heartbeat"], 6)
        self.assertEqual(ws.sent[0]["cmd"], wecom_bridge.APP_CMD_SUBSCRIBE)
        self.assertEqual(ws.sent[0]["body"]["bot_id"], "bot")
        self.assertEqual(ws.sent[0]["body"]["device_id"], bridge.device_id)
        self.assertTrue(session.closed)

        pings = []
        bridge.heartbeat_seconds = 0
        bridge.ws = SimpleNamespace(closed=False)
        bridge._running = True

        async def send_ping(frame):
            pings.append(frame)
            bridge._running = False
            return True

        bridge._send_frame = AsyncMock(side_effect=send_ping)
        await bridge._heartbeat_loop()
        self.assertEqual(pings[0]["cmd"], wecom_bridge.APP_CMD_PING)

        with patch("bridges.wecom.AIOHTTP_AVAILABLE", False):
            with self.assertRaisesRegex(RuntimeError, "aiohttp"):
                await wecom_bridge.WeComBridge(bot_id="bot", secret="secret").check_startup()

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "WECOM_BOT_ID"):
                await wecom_bridge.WeComBridge(bot_id="", secret="", store=bot_core.CopilotConversationStore("")).check_startup()

        startup_ws = FakeWebSocket()
        startup_session = FakeSession(startup_ws)
        startup_bridge = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            heartbeat_seconds=3,
        )
        with patch("bridges.wecom.aiohttp.ClientSession", return_value=startup_session):
            await startup_bridge.check_startup()
        self.assertEqual(startup_ws.sent[0]["cmd"], wecom_bridge.APP_CMD_SUBSCRIBE)
        self.assertTrue(startup_ws.closed)

        class AuthFailedWebSocket(FakeWebSocket):
            async def receive(self):
                req_id = self.sent[0]["headers"]["req_id"]
                return SimpleNamespace(
                    type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                    data=json.dumps(
                        {
                            "headers": {"req_id": req_id},
                            "errcode": 40001,
                            "errmsg": "invalid bot secret",
                            "trace_id": "trace-wecom-1",
                        }
                    ),
                )

        failed_startup = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
        )
        with patch("bridges.wecom.aiohttp.ClientSession", return_value=FakeSession(AuthFailedWebSocket())):
            with self.assertRaisesRegex(RuntimeError, "企业微信 WeCom 鉴权失败[\\s\\S]*invalid bot secret[\\s\\S]*trace-wecom-1"):
                await failed_startup.check_startup()

        check_bridge = SimpleNamespace(check_startup=AsyncMock(return_value=None))
        with patch("bridges.wecom.WeComBridge", return_value=check_bridge):
            await asyncio.to_thread(wecom_bridge.validate_wecom_startup)
        check_bridge.check_startup.assert_awaited_once()

    async def test_wecom_receive_text_heartbeat_failure_and_ping_errors(self):
        class FakeWebSocket:
            def __init__(self, messages=None):
                self.closed = False
                self.sent = []
                self.messages = list(messages or [])
                self.handshake_returned = False

            async def send_json(self, frame):
                self.sent.append(frame)

            async def receive(self):
                if not self.handshake_returned:
                    self.handshake_returned = True
                    req_id = self.sent[0]["headers"]["req_id"]
                    return SimpleNamespace(
                        type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                        data=json.dumps({"headers": {"req_id": req_id}, "errcode": 0}),
                    )
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.sleep(30)

            async def close(self):
                self.closed = True

        class FakeSession:
            def __init__(self, ws):
                self.ws = ws

            async def ws_connect(self, url, **kwargs):
                return self.ws

            async def close(self):
                return None

        text_then_close = FakeWebSocket(
            [
                SimpleNamespace(
                    type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                    data=json.dumps({"cmd": wecom_bridge.APP_CMD_CALLBACK, "body": {}}),
                ),
                SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.CLOSED),
            ]
        )
        bridge = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            heartbeat_seconds=30,
        )
        bridge._running = True
        bridge.handle_payload = AsyncMock()
        with patch("bridges.wecom.aiohttp.ClientSession", return_value=FakeSession(text_then_close)):
            with self.assertRaisesRegex(RuntimeError, "websocket closed"):
                await bridge._connect_once()
        bridge.handle_payload.assert_awaited_once()

        heartbeat_stopped = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            heartbeat_seconds=30,
        )
        heartbeat_stopped._running = True
        heartbeat_stopped._heartbeat_loop = AsyncMock(return_value=None)
        with patch("bridges.wecom.aiohttp.ClientSession", return_value=FakeSession(FakeWebSocket())):
            with self.assertRaisesRegex(RuntimeError, "heartbeat loop stopped"):
                await heartbeat_stopped._connect_once()

        auth_closed = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        auth_closed.ws = SimpleNamespace(
            receive=AsyncMock(return_value=SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.CLOSED))
        )
        with self.assertRaisesRegex(RuntimeError, "closed during authentication"):
            await auth_closed._wait_for_handshake("req-auth")

        no_ws = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await no_ws._wait_for_handshake("req-auth")

        timeout_ws = SimpleNamespace(
            receive=AsyncMock(return_value=SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.TEXT, data="{}"))
        )
        timeout_bridge = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        timeout_bridge.ws = timeout_ws
        with patch("bridges.wecom.WECOM_CONNECT_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(TimeoutError, "Timed out"):
                await timeout_bridge._wait_for_handshake("req-auth")

        handshake_messages = [
            SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.TEXT, data="{bad"),
            SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.TEXT, data=json.dumps({"cmd": wecom_bridge.APP_CMD_PING})),
            SimpleNamespace(
                type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                data=json.dumps({"cmd": "other", "headers": {"req_id": "other"}}),
            ),
            SimpleNamespace(
                type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                data=json.dumps({"headers": {"req_id": "req-auth"}, "errcode": 0}),
            ),
        ]
        handshake_bridge = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        handshake_bridge.ws = SimpleNamespace(receive=AsyncMock(side_effect=handshake_messages))
        self.assertEqual(await handshake_bridge._wait_for_handshake("req-auth"), {"headers": {"req_id": "req-auth"}, "errcode": 0})
        self.assertEqual(wecom_bridge.WeComBridge._payload_req_id({"headers": "bad"}), "")
        self.assertIsNone(wecom_bridge.WeComBridge._parse_json("[1,2]"))
        await handshake_bridge.handle_payload({"cmd": wecom_bridge.APP_CMD_PING})
        await handshake_bridge.handle_payload({"cmd": wecom_bridge.APP_CMD_EVENT_CALLBACK})

        auth_failed = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        auth_failed._running = True

        class AuthFailedWebSocket(FakeWebSocket):
            async def receive(self):
                if not self.handshake_returned:
                    self.handshake_returned = True
                    req_id = self.sent[0]["headers"]["req_id"]
                    return SimpleNamespace(
                        type=wecom_bridge.aiohttp.WSMsgType.TEXT,
                        data=json.dumps({"headers": {"req_id": req_id}, "errcode": 40001, "errmsg": "bad credentials"}),
                    )
                return SimpleNamespace(type=wecom_bridge.aiohttp.WSMsgType.CLOSED)

        with patch("bridges.wecom.aiohttp.ClientSession", return_value=FakeSession(AuthFailedWebSocket())):
            with self.assertRaisesRegex(RuntimeError, "bad credentials"):
                await auth_failed._connect_once()

        ping_errors = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        ping_errors.heartbeat_seconds = 0
        ping_errors._running = True
        ping_errors.ws = None
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await ping_errors._heartbeat_loop()
        ping_errors.ws = SimpleNamespace(closed=False)
        ping_errors._send_frame = AsyncMock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await ping_errors._heartbeat_loop()

    async def test_wecom_control_busy_error_and_send_branches(self):
        self.assertEqual(wecom_bridge.extract_wecom_text({"msgtype": "voice", "voice": {"content": "voice text"}}), "voice text")
        self.assertEqual(wecom_bridge.extract_wecom_text({"msgtype": "mixed", "mixed": {"msg_item": ["bad"]}}), "")

        bridge = wecom_bridge.WeComBridge(bot_id="bot", secret="secret", store=bot_core.CopilotConversationStore(""))
        self.assertFalse(await bridge._send_frame({"cmd": "x"}))
        fake_ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        bridge.ws = fake_ws
        self.assertTrue(await bridge._send_frame({"cmd": "x"}))
        fake_ws.send_json.assert_awaited_once_with({"cmd": "x"})

        sent = []
        bridge._send_frame = AsyncMock(side_effect=lambda frame: sent.append(frame) or True)
        with patch.dict(os.environ, {"WECOM_ALLOWED_USERS": "user-1"}, clear=True):
            await bridge.handle_payload({"cmd": "ignored", "body": {}})
            await bridge.handle_payload(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-empty"},
                    "body": {
                        "chatid": "room-1",
                        "chattype": "group",
                        "from": {"userid": "user-1"},
                        "msgtype": "text",
                        "text": {"content": ""},
                    },
                }
            )
            await bridge.handle_payload(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-help"},
                    "body": {
                        "chatid": "room-1",
                        "chattype": "group",
                        "from": {"userid": "user-1"},
                        "msgtype": "text",
                        "text": {"content": "@bot /help"},
                    },
                }
            )
        self.assertIn("/session", sent[-1]["body"]["markdown"]["content"])

        proactive = []
        bridge._send_frame = AsyncMock(side_effect=lambda frame: proactive.append(frame) or True)
        self.assertTrue(await bridge.send_text("chat-1", "hello"))
        self.assertEqual(proactive[-1]["cmd"], "aibot_send_msg")
        self.assertTrue(
            await bridge.send_text(
                "chat-1",
                "dm hello",
                source=bot_core.SessionSource("wecom", "chat-1", "dm", "user-1"),
            )
        )
        self.assertEqual(proactive[-1]["body"]["markdown"]["content"], "dm hello")
        self.assertTrue(
            await bridge.send_text(
                "room-1",
                "group hello",
                source=bot_core.SessionSource("wecom", "room-1", "group", "user-1"),
            )
        )
        self.assertEqual(proactive[-1]["body"]["markdown"]["content"], "group hello")
        self.assertNotIn("<@user-1>", proactive[-1]["body"]["markdown"]["content"])

        registry = bot_core.ActiveConversationRegistry()
        source = bot_core.SessionSource("wecom", "chat-1", "dm", "user-1")
        registry.start(bot_core.BotContext("wecom", "chat-1", "user-1", bridge.store, registry=registry))
        with patch("core.bot_core.get_active_registry", return_value=registry):
            await bridge.handle_text_message(source=source, text="query", reply_req_id="req-busy")
        self.assertIn("/btw", proactive[-1]["body"]["markdown"]["content"])

        stop_copilot = FakeCopilot()
        bridge.copilot_factory = lambda: stop_copilot
        stop_registry = bot_core.ActiveConversationRegistry()
        stop_state = stop_registry.start(bot_core.BotContext("wecom", "chat-1", "user-1", bridge.store, registry=stop_registry))
        stop_state.record_task_id("task-stop-wecom")
        stop_state.record_message("partial wecom answer")
        proactive.clear()
        bridge._send_frame = AsyncMock(side_effect=lambda frame: proactive.append(frame) or True)
        with patch("core.bot_core.get_active_registry", return_value=stop_registry):
            await bridge.handle_text_message(source=source, text="/stop", reply_req_id="req-stop")
        self.assertEqual([frame["body"]["markdown"]["content"] for frame in proactive], ["partial wecom answer", "已停止当前任务。"])
        self.assertEqual(proactive[0]["cmd"], "aibot_respond_msg")
        self.assertEqual(proactive[1]["cmd"], "aibot_send_msg")

        failing = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: FakeCopilot(error=ConnectionError("reset")),
        )
        failing._send_frame = AsyncMock(side_effect=lambda frame: proactive.append(frame) or True)
        await failing.handle_text_message(source=source, text="query", reply_req_id="req-error")
        self.assertIn("RDS AI 诊断失败", proactive[-1]["body"]["markdown"]["content"])
        self.assertIn("req-error", proactive[-1]["body"]["markdown"]["content"])
        self.assertNotIn("ConnectionError", proactive[-1]["body"]["markdown"]["content"])
        self.assertNotIn("reset", proactive[-1]["body"]["markdown"]["content"])

        cancelled = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
        )
        cancelled._send_frame = AsyncMock(return_value=True)
        with patch(
            "bridges.wecom.call_with_stream",
            new=AsyncMock(return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": True}),
        ):
            await cancelled.handle_text_message(source=source, text="query", reply_req_id="req-cancel")
        cancelled._send_frame.assert_not_awaited()

        no_message = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
        )
        no_message_frames = []
        no_message._send_frame = AsyncMock(side_effect=lambda frame: no_message_frames.append(frame) or True)
        with patch(
            "bridges.wecom.call_with_stream",
            new=AsyncMock(return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": False}),
        ):
            await no_message.handle_text_message(source=source, text="query", reply_req_id="req-empty-response")
        self.assertIn("未返回回复内容", no_message_frames[-1]["body"]["markdown"]["content"])


class QQBridgeCoverageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.qq_env_patcher = patch.dict(os.environ, {"QQ_HTTP_VERIFY": "true"})
        self.qq_env_patcher.start()

    def tearDown(self):
        self.qq_env_patcher.stop()

    async def test_qq_normalizes_events_checks_auth_and_routes_reply(self):
        fake_copilot = FakeCopilot([MessageEvent("task-1", "conv-1", "qq answer")])
        bridge = qq_bridge.QQBridge(
            app_id="app-1",
            client_secret="secret-1",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: fake_copilot,
        )
        bridge._api_request = AsyncMock(return_value={"id": "sent-1"})

        c2c = {
            "id": "msg-1",
            "content": "query",
            "author": {"user_openid": "user-openid-1"},
        }
        with patch.dict(os.environ, {}, clear=True):
            await bridge.handle_event("C2C_MESSAGE_CREATE", c2c)
        bridge._api_request.assert_not_awaited()

        with patch.dict(os.environ, {"QQ_ALLOWED_USERS": "user-openid-1"}, clear=True):
            await bridge.handle_event("C2C_MESSAGE_CREATE", c2c)
        bridge._api_request.assert_awaited_with(
            "POST",
            "/v2/users/user-openid-1/messages",
            {"markdown": {"content": "qq answer"}, "msg_type": 2},
        )

        bridge._api_request.reset_mock()
        group = {
            "id": "msg-2",
            "group_openid": "group-openid-1",
            "content": "<@!bot> group query",
            "author": {"member_openid": "member-openid-1"},
        }
        with patch.dict(os.environ, {"QQ_ALLOWED_USERS": "member-openid-1", "QQ_GROUP_ALLOWED_USERS": "group-openid-1"}, clear=True):
            await bridge.handle_event("GROUP_AT_MESSAGE_CREATE", group)
        bridge._api_request.assert_awaited_with(
            "POST",
            "/v2/groups/group-openid-1/messages",
            {"markdown": {"content": "<@member-openid-1>\nqq answer"}, "msg_type": 2, "msg_id": "msg-2"},
        )

    async def test_qq_token_gateway_payload_and_startup_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            bridge = qq_bridge.QQBridge(app_id="", client_secret="", store=bot_core.CopilotConversationStore(""))
            with self.assertRaisesRegex(RuntimeError, "QQ_APP_ID"):
                await bridge.start_forever()

        check_bridge = SimpleNamespace(ensure_access_token=AsyncMock(return_value="token-1"))
        with patch("bridges.qq.QQBridge", return_value=check_bridge):
            await asyncio.to_thread(qq_bridge.validate_qq_startup)
        check_bridge.ensure_access_token.assert_awaited_once()

        bridge = qq_bridge.QQBridge(app_id="app-1", client_secret="secret-1")
        calls = []

        async def fake_request(method, path, body=None):
            calls.append((method, path, body))
            if path == qq_bridge.QQ_TOKEN_URL:
                return {"access_token": "token-1", "expires_in": 7200}
            if path == "/gateway":
                return {"url": "wss://gateway.qq"}
            return {}

        bridge._api_request = fake_request
        self.assertEqual(await bridge.ensure_access_token(), "token-1")
        self.assertEqual(await bridge.get_gateway_url(), "wss://gateway.qq")
        self.assertIn(("POST", qq_bridge.QQ_TOKEN_URL, {"appId": "app-1", "clientSecret": "secret-1"}), calls)
        self.assertIn(("GET", "/gateway", None), calls)

        missing_token = qq_bridge.QQBridge(app_id="app-1", client_secret="secret-1")
        missing_token._api_request = AsyncMock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "access_token"):
            await missing_token.ensure_access_token()

        missing_gateway = qq_bridge.QQBridge(app_id="app-1", client_secret="secret-1")
        missing_gateway.access_token = "token-1"
        missing_gateway.token_expires_at = time.time() + 3600
        missing_gateway._api_request = AsyncMock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "gateway"):
            await missing_gateway.get_gateway_url()

        auth_failed = qq_bridge.QQBridge(app_id="bad-app", client_secret="bad-secret")
        response = SimpleNamespace(
            status_code=403,
            text="抱歉，您要访问的网站不在安全策略默认允许的范围内。请打开云壳-防护记录-域名拦截申请加白。",
        )
        token_error = RuntimeError("403 Forbidden")
        token_error.response = response
        auth_failed.http_client = SimpleNamespace(
            request=AsyncMock(
                return_value=SimpleNamespace(raise_for_status=Mock(side_effect=token_error))
            )
        )
        with self.assertRaisesRegex(RuntimeError, "QQ Bot 鉴权失败[\\s\\S]*域名拦截"):
            await auth_failed.ensure_access_token()

    async def test_qq_helpers_gateway_control_busy_error_and_send_branches(self):
        with patch.dict(os.environ, {"QQ_RECONNECT_BASE_SECONDS": "bad", "QQ_RECONNECT_MAX_SECONDS": "2"}, clear=True):
            self.assertEqual(qq_bridge._calculate_reconnect_delay(2), 2)
        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "off"}, clear=True):
            self.assertFalse(qq_bridge._read_bool_env("QQ_HTTP_VERIFY"))
        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "off"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "QQ_HTTP_VERIFY=false"):
                qq_bridge.QQBridge(app_id="app", client_secret="secret")
        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "off", "RDS_BOT_ENV": "dev"}, clear=True):
            self.assertFalse(qq_bridge.QQBridge(app_id="app", client_secret="secret").tls_verify)

        guild_source, guild_text = qq_bridge.source_and_text_from_qq_event(
            "GUILD_MESSAGE_CREATE",
            {"channel_id": "channel-1", "content": "guild", "author": {"id": "user-1", "username": "Alice"}},
        )
        self.assertEqual((guild_source.chat_type, guild_source.chat_id, guild_text), ("channel", "channel-1", "guild"))
        dm_source, _ = qq_bridge.source_and_text_from_qq_event(
            "DIRECT_MESSAGE_CREATE",
            {"guild_id": "guild-1", "content": "dm", "author": {"id": "user-1"}},
        )
        self.assertEqual(dm_source.chat_id, "guild-1")
        unknown_source, unknown_text = qq_bridge.source_and_text_from_qq_event("OTHER", {"content": "x"})
        self.assertEqual((unknown_source.chat_id, unknown_text), ("", "x"))
        group_source, group_text = qq_bridge.source_and_text_from_qq_event(
            "GROUP_AT_MESSAGE_CREATE",
            {
                "id": "msg-2",
                "group_openid": "group-openid-1",
                "content": "<@!bot> group",
                "author": {"member_openid": "member-openid-1"},
            },
        )
        self.assertEqual((group_source.thread_id, group_text), ("msg-2", "group"))

        bridge = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        bridge._api_request = AsyncMock(return_value={"id": "sent"})
        await bridge.send_text(dm_source, "channel dm")
        bridge._api_request.assert_awaited_with("POST", "/channels/guild-1/messages", {"markdown": {"content": "channel dm"}, "msg_type": 2})
        bridge._api_request.reset_mock()
        await bridge.send_text(guild_source, "channel")
        bridge._api_request.assert_awaited_with(
            "POST",
            "/channels/channel-1/messages",
            {"markdown": {"content": "<@user-1>\nchannel"}, "msg_type": 2},
        )
        await bridge.handle_event("C2C_MESSAGE_CREATE", {"id": "empty", "content": "", "author": {"user_openid": "u"}})

        bridge.ensure_access_token = AsyncMock(return_value="token-1")
        bridge.ws = SimpleNamespace(send_json=AsyncMock())
        await bridge.handle_gateway_payload({"op": 10, "d": {"heartbeat_interval": 30000}})
        bridge.ws.send_json.assert_awaited_once()
        if bridge._heartbeat_task:
            bridge._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge._heartbeat_task
        bridge.handle_event = AsyncMock()
        await bridge.handle_gateway_payload({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m"}})
        bridge.handle_event.assert_awaited_once()

        bridge.sequence = 251
        await bridge._send_heartbeat()
        self.assertEqual(bridge.ws.send_json.await_args.args[0], {"op": 1, "d": 251})
        self.assertFalse(bridge._heartbeat_ack_received)
        await bridge.handle_gateway_payload({"op": 11})
        self.assertTrue(bridge._heartbeat_ack_received)

        bridge._heartbeat_ack_received = False
        bridge._running = True
        with self.assertRaisesRegex(RuntimeError, "heartbeat ack timeout"):
            await bridge._heartbeat_loop(0)
        bridge._running = False
        bridge.ws = None
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await bridge._send_heartbeat()

        resume = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        resume.ensure_access_token = AsyncMock(return_value="token-1")
        resume.ws = SimpleNamespace(send_json=AsyncMock())
        await resume.handle_gateway_payload({"op": 0, "s": 42, "t": "READY", "d": {"session_id": "session-1"}})
        self.assertEqual(resume.session_id, "session-1")
        self.assertEqual(resume.sequence, 42)
        await resume.handle_gateway_payload({"op": 10, "d": {"heartbeat_interval": 45000}})
        self.assertEqual(resume.ws.send_json.await_args.args[0]["op"], 6)
        if resume._heartbeat_task:
            resume._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resume._heartbeat_task
        await resume.handle_gateway_payload({"op": 0, "s": 99, "t": "RESUMED", "d": ""})
        self.assertEqual(resume.sequence, 99)
        await resume.handle_gateway_payload({"op": 1})
        self.assertEqual(resume.ws.send_json.await_args.args[0]["op"], 1)
        with self.assertRaisesRegex(RuntimeError, "requested reconnect"):
            await resume.handle_gateway_payload({"op": 7})
        with self.assertRaisesRegex(RuntimeError, "invalid session"):
            await resume.handle_gateway_payload({"op": 9})

        old_heartbeat_task = asyncio.create_task(asyncio.sleep(30))
        resume._heartbeat_task = old_heartbeat_task
        resume._start_heartbeat(1000)
        self.assertTrue(old_heartbeat_task.cancelled() or old_heartbeat_task.cancelling())
        resume._heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resume._heartbeat_task

        cancelled = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        cancelled._api_request = AsyncMock(return_value={"id": "sent"})
        with patch(
            "bridges.qq.call_with_stream",
            new=AsyncMock(return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": True}),
        ):
            await cancelled.handle_text_message(source=guild_source, text="query")
        cancelled._api_request.assert_not_awaited()

        no_message = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        no_message._api_request = AsyncMock(return_value={"id": "sent"})
        with patch(
            "bridges.qq.call_with_stream",
            new=AsyncMock(return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": False}),
        ):
            await no_message.handle_text_message(source=guild_source, text="query")
        self.assertIn("未返回回复内容", no_message._api_request.await_args.args[2]["markdown"]["content"])

    async def test_qq_reconnect_connect_once_and_heartbeat_success_paths(self):
        bridge = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        sleeps = []

        async def fake_connect_once():
            raise RuntimeError("down")

        async def fake_sleep(delay):
            sleeps.append(delay)
            bridge._running = False

        fake_http_client = SimpleNamespace(aclose=AsyncMock())
        bridge._connect_once = fake_connect_once
        with patch("bridges.qq.httpx.AsyncClient", return_value=fake_http_client), patch("bridges.qq.asyncio.sleep", side_effect=fake_sleep):
            await bridge.start_forever()
        self.assertEqual(sleeps, [qq_bridge._calculate_reconnect_delay(0)])
        fake_http_client.aclose.assert_awaited_once()

        class FakeWebSocket:
            def __init__(self, messages=None):
                self.closed = False
                self.messages = list(messages or [])
                self.sent = []

            async def receive(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.sleep(30)

            async def send_json(self, payload):
                self.sent.append(payload)

            async def close(self):
                self.closed = True

        class FakeSession:
            def __init__(self, ws):
                self.ws = ws
                self.closed = False
                self.ws_connect_kwargs = None

            async def ws_connect(self, url, **kwargs):
                self.ws_connect_kwargs = (url, kwargs)
                return self.ws

            async def close(self):
                self.closed = True

        close_ws = FakeWebSocket([SimpleNamespace(type=qq_bridge.aiohttp.WSMsgType.CLOSED)])
        session = FakeSession(close_ws)
        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "true"}, clear=True):
            connect_once = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        connect_once._running = True
        connect_once.get_gateway_url = AsyncMock(return_value="wss://gateway.qq")
        qq_logs = []
        sink_id = qq_bridge.logger.add(lambda message: qq_logs.append(str(message)), level="INFO")
        with patch("bridges.qq.aiohttp.ClientSession", return_value=session):
            try:
                with self.assertRaisesRegex(RuntimeError, "websocket closed"):
                    await connect_once._connect_once()
            finally:
                qq_bridge.logger.remove(sink_id)
        self.assertIn("QQ bridge connected by websocket", "\n".join(qq_logs))
        self.assertEqual(session.ws_connect_kwargs, ("wss://gateway.qq", {}))
        self.assertTrue(close_ws.closed)
        self.assertTrue(session.closed)

        no_verify_ws = FakeWebSocket([SimpleNamespace(type=qq_bridge.aiohttp.WSMsgType.CLOSED)])
        no_verify_session = FakeSession(no_verify_ws)
        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "false", "RDS_BOT_ENV": "dev"}, clear=True):
            no_verify_bridge = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        no_verify_bridge._running = True
        no_verify_bridge.get_gateway_url = AsyncMock(return_value="wss://gateway.qq")
        with patch("bridges.qq.aiohttp.ClientSession", return_value=no_verify_session):
            with self.assertRaisesRegex(RuntimeError, "websocket closed"):
                await no_verify_bridge._connect_once()
        self.assertEqual(no_verify_session.ws_connect_kwargs, ("wss://gateway.qq", {"ssl": False}))

        hello_then_close_ws = FakeWebSocket(
            [
                SimpleNamespace(
                    type=qq_bridge.aiohttp.WSMsgType.TEXT,
                    data=json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}),
                ),
                SimpleNamespace(type=qq_bridge.aiohttp.WSMsgType.CLOSED),
            ]
        )
        hello_session = FakeSession(hello_then_close_ws)
        hello_bridge = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        hello_bridge._running = True
        hello_bridge.get_gateway_url = AsyncMock(return_value="wss://gateway.qq")
        hello_bridge.ensure_access_token = AsyncMock(return_value="token-1")
        with patch("bridges.qq.aiohttp.ClientSession", return_value=hello_session):
            with self.assertRaisesRegex(RuntimeError, "websocket closed"):
                await hello_bridge._connect_once()
        self.assertEqual(hello_then_close_ws.sent[0]["op"], 2)
        self.assertIsNone(hello_bridge._heartbeat_task)

        heartbeat_error = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        heartbeat_error._running = True
        heartbeat_error.ws = FakeWebSocket()

        async def failing_heartbeat():
            raise RuntimeError("hb down")

        heartbeat_error._heartbeat_task = asyncio.create_task(failing_heartbeat())
        with self.assertRaisesRegex(RuntimeError, "hb down"):
            await heartbeat_error._receive_gateway_loop()

        heartbeat_stopped = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        heartbeat_stopped._running = True
        heartbeat_stopped.ws = FakeWebSocket()

        async def stopped_heartbeat():
            return None

        heartbeat_stopped._heartbeat_task = asyncio.create_task(stopped_heartbeat())
        with self.assertRaisesRegex(RuntimeError, "heartbeat loop stopped"):
            await heartbeat_stopped._receive_gateway_loop()

        heartbeat_ok = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        sent = []

        async def send_json(payload):
            sent.append(payload)
            heartbeat_ok._running = False

        heartbeat_ok._running = True
        heartbeat_ok.sequence = 7
        heartbeat_ok.ws = SimpleNamespace(closed=False, send_json=AsyncMock(side_effect=send_json))
        heartbeat_ok._heartbeat_ack_received = True
        await heartbeat_ok._heartbeat_loop(0)
        self.assertEqual(sent, [{"op": 1, "d": 7}])

        control = qq_bridge.QQBridge(app_id="app", client_secret="secret", store=bot_core.CopilotConversationStore(""))
        control._api_request = AsyncMock(return_value={"id": "sent"})
        source = bot_core.SessionSource("qqbot", "user-1", "dm", "user-1")
        await control.handle_text_message(source=source, text="/help")
        self.assertIn("/session", control._api_request.await_args.args[2]["markdown"]["content"])

        stop_copilot = FakeCopilot()
        stop_control = qq_bridge.QQBridge(
            app_id="app",
            client_secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: stop_copilot,
        )
        stop_control._api_request = AsyncMock(return_value={"id": "sent"})
        stop_registry = bot_core.ActiveConversationRegistry()
        stop_state = stop_registry.start(bot_core.BotContext("qqbot", "user-1", "user-1", stop_control.store, registry=stop_registry))
        stop_state.record_task_id("task-stop-qq")
        stop_state.record_message("partial qq answer")
        with patch("core.bot_core.get_active_registry", return_value=stop_registry):
            await stop_control.handle_text_message(source=source, text="/stop")
        self.assertEqual([call.args[2]["markdown"]["content"] for call in stop_control._api_request.await_args_list], ["partial qq answer", "已停止当前任务。"])
        self.assertEqual(stop_copilot.stopped, ["task-stop-qq"])

        registry = bot_core.ActiveConversationRegistry()
        registry.start(bot_core.BotContext("qqbot", "user-1", "user-1", control.store, registry=registry))
        with patch("core.bot_core.get_active_registry", return_value=registry):
            await control.handle_text_message(source=source, text="query")
        self.assertIn("/btw", control._api_request.await_args.args[2]["markdown"]["content"])

        failing = qq_bridge.QQBridge(
            app_id="app",
            client_secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=lambda: FakeCopilot(error=ConnectionError("reset")),
        )
        failing._api_request = AsyncMock(return_value={"id": "sent"})
        await failing.handle_text_message(source=source, text="query")
        self.assertIn("RDS AI 诊断失败", failing._api_request.await_args.args[2]["markdown"]["content"])
        self.assertNotIn("ConnectionError", failing._api_request.await_args.args[2]["markdown"]["content"])
        self.assertNotIn("reset", failing._api_request.await_args.args[2]["markdown"]["content"])


class BridgeFactoryControlCommandRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_feishu_wecom_and_qq_short_commands_accept_copilot_class_factory(self):
        FactoryStyleCopilot.reset()

        feishu = feishu_bridge.FeishuBridge(
            app_id="cli",
            app_secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=FactoryStyleCopilot,
        )
        feishu.send_text = AsyncMock(return_value=True)
        await feishu.handle_text_message(
            chat_id="feishu-chat",
            sender_id="feishu-user",
            message_id="feishu-message",
            text="/agent ls",
        )
        self.assertIn("Factory Agent", feishu.send_text.await_args.args[1])

        wecom = wecom_bridge.WeComBridge(
            bot_id="bot",
            secret="secret",
            store=bot_core.CopilotConversationStore(""),
            copilot_factory=FactoryStyleCopilot,
        )
        wecom.send_text = AsyncMock(return_value=True)
        await wecom.handle_text_message(
            source=bot_core.SessionSource("wecom", "wecom-chat", "dm", "wecom-user"),
            text="/skills",
            reply_req_id="req-1",
        )
        self.assertIn("factory-skill", wecom.send_text.await_args.args[1])

        with patch.dict(os.environ, {"QQ_HTTP_VERIFY": "true"}):
            qq = qq_bridge.QQBridge(
                app_id="app",
                client_secret="secret",
                store=bot_core.CopilotConversationStore(""),
                copilot_factory=FactoryStyleCopilot,
            )
        qq.send_text = AsyncMock(return_value=True)
        await qq.handle_text_message(
            source=bot_core.SessionSource("qqbot", "qq-chat", "dm", "qq-user"),
            text="/session ls",
        )
        self.assertIn("Factory Conversation", qq.send_text.await_args.args[1])


class DingTalkBridgeCoverageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "true"})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    async def test_dingtalk_bridge_is_split_out_of_main_entrypoint(self):
        dingtalk_bridge = importlib.import_module("bridges.dingtalk")

        self.assertTrue(hasattr(dingtalk_bridge, "CardBotHandler"))
        self.assertTrue(hasattr(dingtalk_bridge, "run_dingtalk_bridge"))
        self.assertFalse(hasattr(main, "CardBotHandler"))
        self.assertFalse(hasattr(main, "handle_reply_and_update_card"))

    async def test_main_import_loads_dotenv_from_current_working_directory(self):
        integration_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp_dir:
            with open(os.path.join(tmp_dir, ".env"), "w", encoding="utf-8") as f:
                f.write("DINGTALK_APP_CLIENT_ID=dotenv-client-id\n")
                f.write("DINGTALK_APP_CLIENT_SECRET=dotenv-client-secret\n")

            env = os.environ.copy()
            env.pop("DINGTALK_APP_CLIENT_ID", None)
            env.pop("DINGTALK_APP_CLIENT_SECRET", None)
            env["PYTHONPATH"] = integration_dir + os.pathsep + env.get("PYTHONPATH", "")

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os, main; "
                        "print(os.getenv('DINGTALK_APP_CLIENT_ID', '')); "
                        "print(os.getenv('DINGTALK_APP_CLIENT_SECRET', ''))"
                    ),
                ],
                cwd=tmp_dir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dotenv-client-id\ndotenv-client-secret", result.stdout)

    async def test_options_logging_bridge_parsing_and_json_helpers(self):
        with patch.dict(os.environ, {"DINGTALK_APP_CLIENT_ID": "cid", "DINGTALK_APP_CLIENT_SECRET": "secret"}), \
            patch("bridges.dingtalk.argparse.ArgumentParser.parse_args", return_value=argparse.Namespace(client_id="cid")):
            options = dingtalk_bridge.define_options()
        self.assertEqual(options.client_secret, "secret")

        with patch.dict(os.environ, {}, clear=True), \
            patch("bridges.dingtalk.argparse.ArgumentParser.parse_args", return_value=argparse.Namespace(client_id="")):
            with self.assertRaises(SystemExit):
                dingtalk_bridge.define_options()

        self.assertEqual(main.parse_bridge_names("all"), ["dingtalk", "feishu", "wecom", "qqbot"])
        self.assertEqual(main.parse_bridge_names("dingtalk,,feishu"), ["dingtalk", "feishu"])
        self.assertEqual(main.parse_bridge_names("dingtalk,dingtalk"), ["dingtalk"])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(main.EnvironmentConfigurationError, "ACCESS_KEY_ID"):
                main.validate_startup_environment(["dingtalk", "feishu"])
        valid_env = {
            "ACCESS_KEY_ID": "ak",
            "ACCESS_SECRET": "sk",
            "DINGTALK_APP_CLIENT_ID": "ding-id",
            "DINGTALK_APP_CLIENT_SECRET": "ding-secret",
            "DINGTALK_ALLOW_ALL_USERS": "true",
            "FEISHU_APP_ID": "feishu-id",
            "FEISHU_APP_SECRET": "feishu-secret",
            "FEISHU_ALLOW_ALL_USERS": "true",
        }
        with patch.dict(os.environ, valid_env, clear=True):
            main.validate_startup_environment(["dingtalk", "feishu"])
        with patch("bridges.dingtalk.validate_dingtalk_startup") as validate_ding, \
            patch("bridges.feishu.validate_feishu_startup") as validate_feishu, \
            patch("bridges.wecom.validate_wecom_startup") as validate_wecom, \
            patch("bridges.qq.validate_qq_startup") as validate_qq:
            main.validate_bridge_startup("dingtalk")
            main.validate_bridge_startup("feishu")
            await asyncio.to_thread(main.validate_bridge_startup, "wecom")
            await asyncio.to_thread(main.validate_bridge_startup, "qqbot")
        validate_ding.assert_called_once()
        validate_feishu.assert_called_once()
        validate_wecom.assert_called_once()
        validate_qq.assert_called_once()
        with self.assertRaisesRegex(ValueError, "Unsupported bridge"):
            main.validate_bridge_startup("slack")
        with patch("main.validate_bridge_startup") as validate_one:
            main.validate_selected_bridge_startup(["dingtalk"])
        validate_one.assert_called_once_with("dingtalk")
        self.assertTrue(bot_core.is_new_conversation_command("/new"))
        self.assertTrue(bot_core.is_new_conversation_command("$new"))
        self.assertEqual(dingtalk_bridge.parse_session_command("/session on"), "on")
        self.assertEqual(dingtalk_bridge.parse_session_command("$session on"), "on")
        self.assertEqual(dingtalk_bridge.parse_card_command("/card"), "status")
        self.assertEqual(dingtalk_bridge.parse_card_command("$card"), "status")
        self.assertEqual(dingtalk_bridge.parse_card_command("/card status"), "")
        self.assertEqual(dingtalk_bridge.parse_card_command("$card status"), "")
        self.assertTrue(dingtalk_bridge.is_new_conversation_command("/new"))
        self.assertTrue(dingtalk_bridge.is_new_conversation_command("$new"))
        stream_client = dingtalk_bridge.build_dingtalk_stream_client(SimpleNamespace(client_id="client", client_secret="secret"))
        self.assertIsInstance(stream_client, dingtalk_bridge.ObservableDingTalkStreamClient)
        self.assertEqual(dingtalk_bridge.convert_json_values_to_string({"a": 1, "b": "x"}), {"a": "1", "b": "x"})
        self.assertEqual(dingtalk_bridge.get_conversation_store_file_path(), bot_core.get_conversation_store_file_path())
        error_content = dingtalk_bridge.build_error_card_content(RuntimeError("secret detail"), language="en-US", trace_id="trace-1")
        self.assertIn("RDS AI diagnosis failed", error_content)
        self.assertIn("trace-1", error_content)
        self.assertNotIn("RuntimeError", error_content)
        self.assertNotIn("secret detail", error_content)
        self.assertIn("no response content", dingtalk_bridge.build_no_message_card_content(language="en-US"))
        with patch("main.logger.add") as add:
            main._LOGGING_CONFIGURED = False
            main.configure_file_logging()
            main.configure_file_logging()
        self.assertEqual(add.call_count, 2)

    async def test_extract_and_send_session_webhook_success_truncate_and_errors(self):
        incoming = SimpleNamespace(session_webhook=" https://hook-from-attr ")
        self.assertEqual(dingtalk_bridge.extract_session_webhook({}, incoming), "https://hook-from-attr")
        self.assertEqual(
            dingtalk_bridge.extract_session_webhook({"outer": [{"sessionWebhook": " https://hook-from-json "}]}, None),
            "https://hook-from-json",
        )

        posted = {}

        async def fake_to_thread(func, session_webhook, payload):
            posted["url"] = session_webhook
            posted["payload"] = payload
            return "ok"

        with patch("bridges.dingtalk.asyncio.to_thread", new=fake_to_thread):
            self.assertTrue(await dingtalk_bridge.send_dingtalk_session_webhook("https://hook", "x" * (dingtalk_bridge.MAX_PLAIN_MESSAGE_LENGTH + 1)))
        self.assertTrue(posted["payload"]["markdown"]["text"].endswith("...(truncated)"))

        with patch("bridges.dingtalk.asyncio.to_thread", new=fake_to_thread):
            self.assertTrue(
                await dingtalk_bridge.send_dingtalk_session_webhook(
                    "https://hook",
                    "group hello",
                    mention_user_id="staff-1",
                )
            )
        self.assertEqual(posted["payload"]["at"], {"atUserIds": ["staff-1"], "isAtAll": False})
        self.assertEqual(posted["payload"]["markdown"]["text"], "@staff-1\ngroup hello")

        single_message = FakeIncomingMessage(
            "/help",
            conversation_id="ding-single-conv-1",
            sender_id="sender-1",
            conversation_type="1",
        )
        self.assertFalse(dingtalk_bridge._is_dingtalk_group_message(single_message))
        self.assertEqual(dingtalk_bridge._dingtalk_group_mention_user_id(single_message), "")
        self.assertEqual(
            dingtalk_bridge._with_dingtalk_group_mention("single hello", dingtalk_bridge._dingtalk_group_mention_user_id(single_message)),
            "single hello",
        )
        with patch("bridges.dingtalk.asyncio.to_thread", new=fake_to_thread):
            self.assertTrue(
                await dingtalk_bridge.send_dingtalk_session_webhook(
                    "https://hook",
                    "single hello",
                    mention_user_id=dingtalk_bridge._dingtalk_group_mention_user_id(single_message),
                )
            )
        self.assertNotIn("at", posted["payload"])
        self.assertEqual(posted["payload"]["markdown"]["text"], "single hello")

        with self.assertRaisesRegex(ValueError, "sessionWebhook"):
            await dingtalk_bridge.send_dingtalk_session_webhook("", "content")

        with patch("bridges.dingtalk.asyncio.to_thread", new=AsyncMock(side_effect=dingtalk_bridge.urllib_error.URLError("bad gateway"))):
            with self.assertRaises(dingtalk_bridge.urllib_error.URLError):
                await dingtalk_bridge.send_dingtalk_session_webhook("https://hook", "")

        class HttpResponse:
            status = 500

            def read(self, size):
                return b"bad"

            def getcode(self):
                return 500

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        response = HttpResponse()
        with patch("bridges.dingtalk.urllib_request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                dingtalk_bridge._post_dingtalk_session_webhook("https://hook", {"msgtype": "text"})

        class OkResponse(HttpResponse):
            status = 200

            def read(self, size):
                return b"ok"

            def getcode(self):
                return 200

        with patch("bridges.dingtalk.urllib_request.urlopen", return_value=OkResponse()):
            self.assertEqual(dingtalk_bridge._post_dingtalk_session_webhook("https://hook", {"msgtype": "text"}), "ok")

    async def test_dingtalk_robot_client_access_token_and_emotions(self):
        self.assertEqual(await dingtalk_bridge.get_dingtalk_access_token(SimpleNamespace(dingtalk_client=None)), "")
        self.assertEqual(await dingtalk_bridge.get_dingtalk_access_token(SimpleNamespace(dingtalk_client=SimpleNamespace(get_access_token=lambda: "token"))), "token")

        async def async_token():
            return "async-token"

        self.assertEqual(
            await dingtalk_bridge.get_dingtalk_access_token(SimpleNamespace(dingtalk_client=SimpleNamespace(get_access_token=async_token))),
            "async-token",
        )

        with patch("bridges.dingtalk.DINGTALK_ROBOT_SDK_AVAILABLE", False):
            self.assertIsNone(dingtalk_bridge.get_dingtalk_robot_client())

        fake_client_instance = object()
        with patch("bridges.dingtalk.DINGTALK_ROBOT_SDK_AVAILABLE", True), \
            patch("bridges.dingtalk.open_api_models", SimpleNamespace(Config=lambda: SimpleNamespace())), \
            patch("bridges.dingtalk.dingtalk_robot_client", SimpleNamespace(Client=lambda config: fake_client_instance)):
            dingtalk_bridge._DINGTALK_ROBOT_CLIENT = None
            self.assertIs(dingtalk_bridge.get_dingtalk_robot_client(), fake_client_instance)
            self.assertIs(dingtalk_bridge.get_dingtalk_robot_client(), fake_client_instance)

        class Model:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class RuntimeOptions:
            pass

        robot_sdk = SimpleNamespace(
            robot_reply_emotion_with_options_async=AsyncMock(return_value=True),
            robot_recall_emotion_with_options_async=AsyncMock(return_value=True),
        )
        models = SimpleNamespace(
            RobotRecallEmotionRequestTextEmotion=Model,
            RobotRecallEmotionRequest=Model,
            RobotRecallEmotionHeaders=Model,
            RobotReplyEmotionRequestTextEmotion=Model,
            RobotReplyEmotionRequest=Model,
            RobotReplyEmotionHeaders=Model,
        )
        handler = FakeHandler()
        incoming = FakeIncomingMessage()
        with patch("bridges.dingtalk.get_dingtalk_robot_client", return_value=robot_sdk), \
            patch("bridges.dingtalk.dingtalk_robot_models", models), \
            patch("bridges.dingtalk.tea_util_models", SimpleNamespace(RuntimeOptions=RuntimeOptions)):
            self.assertTrue(await dingtalk_bridge.send_dingtalk_emotion(handler, incoming, dingtalk_bridge.THINKING_EMOTION))
            self.assertTrue(await dingtalk_bridge.send_dingtalk_emotion(handler, incoming, dingtalk_bridge.THINKING_EMOTION, recall=True))

        with patch("bridges.dingtalk.get_dingtalk_robot_client", return_value=None):
            self.assertFalse(await dingtalk_bridge.send_dingtalk_emotion(handler, incoming, dingtalk_bridge.THINKING_EMOTION))

        missing_message = FakeIncomingMessage(message_id="")
        with patch("bridges.dingtalk.get_dingtalk_robot_client", return_value=robot_sdk):
            self.assertFalse(await dingtalk_bridge.send_dingtalk_emotion(handler, missing_message, dingtalk_bridge.THINKING_EMOTION))
        no_token_handler = SimpleNamespace(dingtalk_client=SimpleNamespace(get_access_token=lambda: ""))
        with patch("bridges.dingtalk.get_dingtalk_robot_client", return_value=robot_sdk):
            self.assertFalse(await dingtalk_bridge.send_dingtalk_emotion(no_token_handler, incoming, dingtalk_bridge.THINKING_EMOTION))
        robot_sdk.robot_reply_emotion_with_options_async = AsyncMock(side_effect=RuntimeError("sdk failed"))
        with patch("bridges.dingtalk.get_dingtalk_robot_client", return_value=robot_sdk), \
            patch("bridges.dingtalk.dingtalk_robot_models", models), \
            patch("bridges.dingtalk.tea_util_models", SimpleNamespace(RuntimeOptions=RuntimeOptions)):
            self.assertFalse(await dingtalk_bridge.send_dingtalk_emotion(handler, incoming, dingtalk_bridge.THINKING_EMOTION))

    async def test_emotion_switcher_noops_duplicate_and_finish_without_current(self):
        handler = FakeHandler()
        incoming = FakeIncomingMessage()
        switcher = dingtalk_bridge.DingTalkEmotionSwitcher(handler, incoming)
        with patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)) as send_emotion:
            await switcher.set_state("")
            await switcher.set_state(dingtalk_bridge.THINKING_EMOTION)
            await switcher.set_state(dingtalk_bridge.THINKING_EMOTION)
            await switcher.set_state("Custom")
            await switcher.finish()
            await switcher.finish()
        self.assertEqual(send_emotion.await_count, 6)

    async def test_plain_reply_fallbacks_to_reply_text_when_webhook_missing_or_send_fails(self):
        handler = FakeHandler()
        incoming = FakeIncomingMessage()
        fake_copilot = FakeCopilot([MessageEvent("task-1", "conv-1", "answer")])

        with patch("bridges.dingtalk.RdsCopilot", return_value=fake_copilot):
            await dingtalk_bridge.handle_reply_plain_message(handler, incoming, {}, session_enabled=False)
        self.assertEqual(handler.replies[-1][0], "@staff-1\nanswer")

        handler = FakeHandler()
        with patch("bridges.dingtalk.RdsCopilot", return_value=fake_copilot), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(side_effect=RuntimeError("webhook failed"))):
            await dingtalk_bridge.handle_reply_plain_message(handler, incoming, {"sessionWebhook": "https://hook"}, session_enabled=False)
        self.assertEqual(handler.replies[-1][0], "@staff-1\nanswer")

        handler = FakeHandler()
        empty_copilot = FakeCopilot([])
        with patch("bridges.dingtalk.RdsCopilot", return_value=empty_copilot), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            await dingtalk_bridge.handle_reply_plain_message(handler, incoming, {"sessionWebhook": "https://hook"})
        self.assertIn("未返回回复内容", send_webhook.await_args.args[1])

    async def test_card_reply_success_error_update_failure_and_finalize_failure(self):
        incoming = FakeIncomingMessage()
        card = FakeCardInstance()
        with patch("bridges.dingtalk.dingtalk_stream.AICardReplier", return_value=card), \
            patch("bridges.dingtalk.RdsCopilot", return_value=FakeCopilot([MessageEvent("task-1", "conv-1", "answer")])):
            result = await dingtalk_bridge.handle_reply_and_update_card(FakeHandler(), incoming, custom_agent_id="agent-1")
        self.assertEqual(result, "conv-1")
        self.assertTrue(card.streaming_calls[-1][1]["finished"])

        card = FakeCardInstance(fail_put=True, fail_finalize=True)
        with patch("bridges.dingtalk.dingtalk_stream.AICardReplier", return_value=card), \
            patch("bridges.dingtalk.RdsCopilot", return_value=FakeCopilot(error=RuntimeError("copilot failed"))):
            result = await dingtalk_bridge.handle_reply_and_update_card(FakeHandler(), incoming)
        self.assertEqual(result, "")
        self.assertTrue(card.put_calls)

    async def test_card_bot_process_non_text_busy_card_plain_and_send_status_fallbacks(self):
        callback = SimpleNamespace(data={})
        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock()
        non_text = FakeIncomingMessage(message_type="image")
        with patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=non_text):
            self.assertEqual(await handler.process(callback), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        handler.reply_text.assert_called_with("@staff-1\nI can only process text messages.", non_text)

        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock()
        command_message = FakeIncomingMessage("/agent ls")
        command_message.session_webhook = "https://hook"
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": os.path.join(tmp_dir, "conversations.json"), "GATEWAY_ALLOW_ALL_USERS": "true"}), \
            patch("core.bot_core.RdsCopilot", return_value=FakeCopilot()), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=command_message), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            self.assertEqual(await handler.process(callback), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        send_webhook.assert_awaited_once()
        self.assertIn("###", send_webhook.await_args.args[1])
        handler.reply_text.assert_not_called()

        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock(return_value={"ok": True})
        command_message = FakeIncomingMessage("/help")
        command_message.session_webhook = "https://hook"
        with patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(side_effect=RuntimeError("webhook failed"))):
            await handler.reply_command_content("### Help", command_message, {"sessionWebhook": "https://hook"})
        handler.reply_text.assert_called_once_with("@staff-1\n### Help", command_message)

        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock()
        single_command_message = FakeIncomingMessage(
            "/help",
            conversation_id="ding-single-conv-1",
            sender_id="sender-1",
            conversation_type="1",
        )
        single_command_message.session_webhook = "https://hook"
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": os.path.join(tmp_dir, "conversations.json"), "GATEWAY_ALLOW_ALL_USERS": "true"}), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=single_command_message), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            self.assertEqual(await handler.process(callback), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        send_webhook.assert_awaited_once()
        self.assertEqual(send_webhook.await_args.kwargs["mention_user_id"], "")
        handler.reply_text.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = os.path.join(tmp_dir, "conversations.json")
            store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
            registry = bot_core.ActiveConversationRegistry()
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store, registry=registry)
            registry.start(context)
            handler = dingtalk_bridge.CardBotHandler(logger=Mock())
            handler.reply_text = Mock()
            with patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                patch("core.bot_core.get_active_registry", return_value=registry), \
                patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")):
                await handler.process(callback)
            self.assertIn("/btw", handler.reply_text.call_args.args[0])

        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = os.path.join(tmp_dir, "conversations.json")
            store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
            registry = bot_core.ActiveConversationRegistry()
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store, registry=registry)
            state = registry.start(context)
            state.record_task_id("task-stop-ding")
            state.record_message("partial dingtalk answer")
            stop_copilot = FakeCopilot()
            handler = dingtalk_bridge.CardBotHandler(logger=Mock())
            handler.reply_text = Mock()
            with patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path, "GATEWAY_ALLOW_ALL_USERS": "true"}), \
                patch("core.bot_core.get_active_registry", return_value=registry), \
                patch("core.bot_core.RdsCopilot", return_value=stop_copilot), \
                patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/stop")):
                await handler.process(callback)
            self.assertEqual(
                [call.args[0] for call in handler.reply_text.call_args_list],
                ["@staff-1\npartial dingtalk answer", "@staff-1\n已停止当前任务。"],
            )
            self.assertEqual(stop_copilot.stopped, ["task-stop-ding"])

        async def run_process(card_enabled, callback_data):
            with tempfile.TemporaryDirectory() as tmp_dir:
                store_path = os.path.join(tmp_dir, "conversations.json")
                store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
                store.set_card_enabled("ding-conv-1", "sender-1", card_enabled)
                store.set_agent("ding-conv-1", "sender-1", "agent-1", "Agent")
                store.set_language("ding-conv-1", "sender-1", "en-US")
                store.set_timezone("ding-conv-1", "sender-1", "America/Los_Angeles")
                registry = bot_core.ActiveConversationRegistry()
                handler = dingtalk_bridge.CardBotHandler(logger=Mock())
                handler.reply_text = Mock()
                created_tasks = []
                original_create_task = asyncio.create_task

                def track_task(coro):
                    task = original_create_task(coro)
                    created_tasks.append(task)
                    return task

                with patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                    patch("core.bot_core.get_active_registry", return_value=registry), \
                    patch("bridges.dingtalk.asyncio.create_task", side_effect=track_task), \
                    patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
                    patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
                    patch("bridges.dingtalk.handle_reply_and_update_card", new=AsyncMock(return_value="conv-new")) as card_reply, \
                    patch("bridges.dingtalk.handle_reply_plain_message", new=AsyncMock(return_value={"conversion_id": "conv-new"})) as plain_reply, \
                    patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
                    callback = SimpleNamespace(data=callback_data)
                    status = await handler.process(callback)
                    await asyncio.gather(*created_tasks, return_exceptions=True)
                stored_conversation_id = store.get("ding-conv-1", "sender-1")
                return status, stored_conversation_id, handler, card_reply, plain_reply, send_webhook

        status, stored_conversation_id, handler, card_reply, plain_reply, _ = await run_process(True, {})
        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        card_reply.assert_awaited_once()
        self.assertEqual(card_reply.await_args.kwargs["language"], "en-US")
        self.assertEqual(card_reply.await_args.kwargs["timezone"], "America/Los_Angeles")
        self.assertEqual(stored_conversation_id, "conv-new")
        plain_reply.assert_not_awaited()

        status, _, _, card_reply, plain_reply, _ = await run_process(False, {"sessionWebhook": "https://hook"})
        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        plain_reply.assert_awaited_once()
        self.assertEqual(plain_reply.await_args.kwargs["language"], "en-US")
        self.assertEqual(plain_reply.await_args.kwargs["timezone"], "America/Los_Angeles")
        card_reply.assert_not_awaited()

    async def test_unauthorized_dingtalk_message_is_ignored_before_copilot(self):
        callback = SimpleNamespace(data={})
        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock()
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": os.path.join(tmp_dir, "conversations.json")}, clear=True), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
            patch("bridges.dingtalk.handle_reply_and_update_card", new=AsyncMock()) as card_reply, \
            patch("bridges.dingtalk.handle_reply_plain_message", new=AsyncMock()) as plain_reply:
            status = await handler.process(callback)

        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        handler.reply_text.assert_not_called()
        card_reply.assert_not_awaited()
        plain_reply.assert_not_awaited()

    async def test_unauthorized_dingtalk_non_text_message_is_ignored_before_reply(self):
        callback = SimpleNamespace(data={})
        handler = dingtalk_bridge.CardBotHandler(logger=Mock())
        handler.reply_text = Mock()
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": os.path.join(tmp_dir, "conversations.json")}, clear=True), \
            patch(
                "bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict",
                return_value=FakeIncomingMessage("query", message_type="image"),
            ):
            status = await handler.process(callback)

        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        handler.reply_text.assert_not_called()

    async def test_card_bot_still_working_status_uses_webhook_or_reply_text(self):
        async def fake_notifier(active_state, send_callback, **kwargs):
            await send_callback("Still working... (3 min elapsed — events 10, receiving stream response)")

        async def slow_card_reply(*args, **kwargs):
            await asyncio.sleep(0.001)
            return ""

        async def run_once(callback_data):
            with tempfile.TemporaryDirectory() as tmp_dir:
                store_path = os.path.join(tmp_dir, "conversations.json")
                store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
                store.set_card_enabled("ding-conv-1", "sender-1", True)
                handler = dingtalk_bridge.CardBotHandler(logger=Mock())
                handler.reply_text = Mock()
                created_tasks = []
                original_create_task = asyncio.create_task

                def track_task(coro):
                    task = original_create_task(coro)
                    created_tasks.append(task)
                    return task

                with patch.dict(os.environ, {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                    patch("core.bot_core.get_active_registry", return_value=bot_core.ActiveConversationRegistry()), \
                    patch("bridges.dingtalk.asyncio.create_task", side_effect=track_task), \
                    patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
                    patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
                    patch("bridges.dingtalk.run_still_working_notifier", new=fake_notifier), \
                    patch("bridges.dingtalk.handle_reply_and_update_card", new=AsyncMock(side_effect=slow_card_reply)) as card_reply, \
                    patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
                    status = await handler.process(SimpleNamespace(data=callback_data))
                    await asyncio.gather(*created_tasks, return_exceptions=True)
                return status, handler, send_webhook, card_reply

        status, handler, send_webhook, card_reply = await run_once({"sessionWebhook": "https://hook"})
        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        send_webhook.assert_awaited()
        card_reply.assert_awaited_once()

        status, handler, send_webhook, card_reply = await run_once({})
        self.assertEqual(status, (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
        handler.reply_text.assert_any_call(
            "@staff-1\nStill working... (3 min elapsed — events 10, receiving stream response)",
            unittest.mock.ANY,
        )
        send_webhook.assert_not_awaited()
        card_reply.assert_awaited_once()

    async def test_run_bridge_entrypoints_are_wired_with_mocks(self):
        with patch("bridges.dingtalk.define_options", return_value=SimpleNamespace(client_id="cid", client_secret="secret")), \
            patch("bridges.dingtalk.dingtalk_stream.Credential", return_value="credential") as credential, \
            patch("bridges.dingtalk.ObservableDingTalkStreamClient") as client_cls, \
            patch("bridges.dingtalk.logging.getLogger") as get_logger:
            sdk_logger = get_logger.return_value
            client = client_cls.return_value
            dingtalk_bridge.run_dingtalk_bridge()
        credential.assert_called_once_with("cid", "secret")
        get_logger.assert_any_call("dingtalk_stream")
        get_logger.assert_any_call("dingtalk_stream.client")
        self.assertEqual(
            sdk_logger.setLevel.call_args_list[:2],
            [unittest.mock.call(logging.WARNING), unittest.mock.call(logging.WARNING)],
        )
        client.register_callback_handler.assert_called_once()
        client.start_forever.assert_called_once()

        class FakeDingStartupResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        fake_success_client = SimpleNamespace(
            callback_handler_map={dingtalk_bridge.dingtalk_stream.ChatbotMessage.TOPIC: object()},
            get_host_ip=lambda: "127.0.0.1",
        )
        with patch("bridges.dingtalk.define_options", return_value=SimpleNamespace(client_id="cid", client_secret="secret")), \
            patch("bridges.dingtalk.build_dingtalk_stream_client", return_value=fake_success_client), \
            patch("bridges.dingtalk.urllib_request.urlopen", return_value=FakeDingStartupResponse({"endpoint": "wss://example", "ticket": "ticket"})) as startup_urlopen:
            dingtalk_bridge.validate_dingtalk_startup()
            startup_urlopen.assert_called_once()

        with patch("bridges.dingtalk.define_options", return_value=SimpleNamespace(client_id="cid", client_secret="secret")), \
            patch("bridges.dingtalk.build_dingtalk_stream_client", return_value=fake_success_client), \
            patch(
                "bridges.dingtalk.urllib_request.urlopen",
                return_value=FakeDingStartupResponse({"code": "InvalidClient", "message": "bad ding app"}),
            ):
            with self.assertRaisesRegex(RuntimeError, "钉钉 Stream 鉴权失败"):
                dingtalk_bridge.validate_dingtalk_startup()

        dingtalk_http_error = urllib_error.HTTPError(
            "https://api.dingtalk.com/v1.0/gateway/connections/open",
            403,
            "Forbidden",
            {},
            io.BytesIO("ding blocked by gateway".encode("utf-8")),
        )
        fake_ding_client = SimpleNamespace(
            callback_handler_map={dingtalk_bridge.dingtalk_stream.ChatbotMessage.TOPIC: object()},
            credential=SimpleNamespace(client_id="cid", client_secret="secret"),
            get_host_ip=lambda: "127.0.0.1",
        )
        with patch("bridges.dingtalk.define_options", return_value=SimpleNamespace(client_id="cid", client_secret="secret")), \
            patch("bridges.dingtalk.build_dingtalk_stream_client", return_value=fake_ding_client), \
            patch("bridges.dingtalk.urllib_request.urlopen", side_effect=dingtalk_http_error):
            with self.assertRaisesRegex(RuntimeError, "钉钉 Stream 鉴权失败[\\s\\S]*ding blocked by gateway"):
                dingtalk_bridge.validate_dingtalk_startup()

        attempts = []
        delays = []

        def flaky_runner():
            attempts.append("run")
            if len(attempts) == 1:
                raise RuntimeError("bridge down")

        async def fake_sleep(delay):
            delays.append(delay)

        stderr = io.StringIO()
        with patch("main.sys.stderr", stderr):
            await main.supervise_bridge("fake", flaky_runner, sleep=fake_sleep, max_restarts=1)
        self.assertEqual(attempts, ["run", "run"])
        self.assertEqual(delays, [main.calculate_restart_delay(0)])
        self.assertIn("fake bridge 启动/运行失败", stderr.getvalue())
        with patch.dict(os.environ, {"RDS_BRIDGE_RESTART_BASE_SECONDS": "bad"}, clear=True):
            self.assertEqual(main.calculate_restart_delay(0), main.DEFAULT_BRIDGE_RESTART_BASE_SECONDS)
        with self.assertRaises(asyncio.CancelledError):
            await main.supervise_bridge("cancel", lambda: (_ for _ in ()).throw(asyncio.CancelledError()), max_restarts=0)

        checked_bridges = []

        def fake_startup_check(bridge_name):
            checked_bridges.append(bridge_name)
            if bridge_name == "qqbot":
                raise RuntimeError("bad credential")

        with patch("main.validate_bridge_startup", side_effect=fake_startup_check):
            with self.assertRaisesRegex(main.EnvironmentConfigurationError, "启动检查失败[\\s\\S]*qqbot"):
                main.validate_selected_bridge_startup(["dingtalk", "feishu", "qqbot"])
        self.assertEqual(checked_bridges, ["qqbot"])

        config_stderr = io.StringIO()
        with patch("main.configure_file_logging"), \
            patch("main.parse_bridge_names", return_value=["qqbot"]), \
            patch.dict(os.environ, {}, clear=True), \
            patch("main.sys.stderr", config_stderr):
            with self.assertRaises(SystemExit) as cm:
                main.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("QQ_APP_ID", config_stderr.getvalue())

        bridge_error_stderr = io.StringIO()
        with patch("main.configure_file_logging"), \
            patch("main.parse_bridge_names", side_effect=ValueError("Unsupported bridge: slack")), \
            patch("main.sys.stderr", bridge_error_stderr):
            with self.assertRaises(SystemExit) as cm:
                main.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("Unsupported bridge: slack", bridge_error_stderr.getvalue())

        with patch("bridges.dingtalk.run_dingtalk_bridge") as run_ding, \
            patch("bridges.feishu.run_feishu_bridge") as run_feishu, \
            patch("bridges.wecom.run_wecom_bridge") as run_wecom, \
            patch("bridges.qq.run_qq_bridge") as run_qq:
            await main.run_selected_bridges(["dingtalk", "feishu", "wecom", "qqbot"], max_restarts=0)
        run_ding.assert_called_once()
        run_feishu.assert_called_once()
        run_wecom.assert_called_once()
        run_qq.assert_called_once()

        startup_stderr = io.StringIO()
        with patch("main.configure_file_logging"), \
            patch("main.parse_bridge_names", return_value=["qqbot"]), \
            patch("main.validate_startup_environment"), \
            patch("main.validate_selected_bridge_startup", side_effect=main.EnvironmentConfigurationError("启动失败：qqbot")), \
            patch("main.run_selected_bridges") as run_selected_after_failed_startup, \
            patch("main.sys.stderr", startup_stderr):
            with self.assertRaises(SystemExit) as cm:
                main.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("启动失败：qqbot", startup_stderr.getvalue())
        run_selected_after_failed_startup.assert_not_called()

        with patch("main.configure_file_logging"), \
            patch("main.parse_bridge_names", return_value=["dingtalk"]), \
            patch("main.validate_startup_environment"), \
            patch("main.validate_selected_bridge_startup"), \
            patch("main.run_selected_bridges", new=Mock(return_value=object())) as run_selected_single, \
            patch("main.asyncio.run") as asyncio_run_single:
            main.main()
        run_selected_single.assert_called_once_with(["dingtalk"])
        asyncio_run_single.assert_called_once()
        with patch("main.configure_file_logging"), \
            patch("main.parse_bridge_names", return_value=["dingtalk", "feishu"]), \
            patch("main.validate_startup_environment"), \
            patch("main.validate_selected_bridge_startup"), \
            patch("main.run_selected_bridges", new=Mock(return_value=object())) as run_selected, \
            patch("main.asyncio.run") as asyncio_run:
            main.main()
        run_selected.assert_called_once_with(["dingtalk", "feishu"])
        asyncio_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
