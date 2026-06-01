import asyncio
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
from bridges import dingtalk as dingtalk_bridge
from core import bot_core
from core.rds_copilot import MessageEvent, RdsCopilot


class FakeText:
    def __init__(self, content):
        self.content = content


class FakeIncomingMessage:
    def __init__(self, content="hello", conversation_id="ding-conv-1", sender_id="sender-1", conversation_type="2"):
        self.text = FakeText(content)
        self.conversation_id = conversation_id
        self.sender_id = sender_id
        self.conversation_type = conversation_type
        self.sender_staff_id = "staff-1"
        self.message_id = "msg-1"
        self.message_type = "text"
        self.extensions = {}


class FakeCallback:
    data = {}


class FakeCardInstance:
    def __init__(self):
        self.streaming_calls = []
        self.put_card_data_calls = []

    async def async_create_and_deliver_card(self, *args, **kwargs):
        return "card-1"

    async def async_streaming(self, *args, **kwargs):
        self.streaming_calls.append((args, kwargs))

    async def async_put_card_data(self, *args, **kwargs):
        self.put_card_data_calls.append((args, kwargs))


class FakeHandler:
    def __init__(self):
        self.dingtalk_client = object()
        self.logger = logging.getLogger("test")

    def reply_text(self, text, message):
        self.last_reply = (text, message)


class FakeSseEvent:
    def __init__(self, data):
        self.data = data


class FakeSseResponse:
    def __init__(self, data):
        self.event = FakeSseEvent(data)


class FakeSseClient:
    def __init__(self, response_data):
        self.responses = [FakeSseResponse(data) for data in response_data]
        self.last_query = None

    def call_sseapi(self, params, request, runtime):
        self.last_query = getattr(request, "query", {})
        return self.responses

    async def call_sseapi_async(self, params, request, runtime):
        self.last_query = getattr(request, "query", {})
        for response in self.responses:
            yield response


class FakeRpcClient:
    def __init__(self, response_body=None):
        self.response_body = response_body or {}
        self.calls = []

    def do_request(self, params, request, runtime):
        query = getattr(request, "query", {})
        self.calls.append((params.action, query))
        return SimpleNamespace(body=self.response_body)


class FakeRdsCopilot:
    def __init__(self, events):
        self.events = events
        self.last_request_content = None
        self.last_conversion_id = None
        self.last_custom_agent_id = None
        self.last_language = None
        self.last_timezone = None

    def chat(
        self,
        request_content,
        conversion_id="",
        custom_agent_id="",
        language="zh-CN",
        timezone="Asia/Shanghai",
    ):
        self.last_request_content = request_content
        self.last_conversion_id = conversion_id
        self.last_custom_agent_id = custom_agent_id
        self.last_language = language
        self.last_timezone = timezone
        yield from self.events


class RdsCopilotSseParsingTest(unittest.TestCase):
    def test_skips_malformed_sse_event_and_continues_to_later_message(self):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.endpoint = "rdsai.test"
        copilot.app_id = "app-test"
        copilot.client = FakeSseClient(
            [
                '{"Event":"thinking","Answer":"checking","TaskId":"task-1","ConversationId":"conv-1"}',
                '{"Event":"tool_call","Answer":"unterminated',
                '{"Event":"message","Answer":"final answer","TaskId":"task-1","ConversationId":"conv-1"}',
            ]
        )

        events = list(copilot.chat("query"))

        messages = [event for event in events if isinstance(event, MessageEvent)]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "final answer")

    def test_chat_passes_custom_agent_id_when_selected(self):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.endpoint = "rdsai.test"
        copilot.app_id = "app-test"
        copilot.client = FakeSseClient(
            [
                '{"Event":"message","Answer":"agent answer","TaskId":"task-1","ConversationId":"conv-1"}',
            ]
        )

        list(copilot.chat("query", custom_agent_id="agent-1"))

        self.assertEqual(copilot.client.last_query["CustomAgentId"], "agent-1")

    def test_list_conversations_uses_get_conversations_action(self):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.client = FakeRpcClient({"Data": [{"Id": "conv-1", "Name": "A"}], "HasMore": False})

        result = copilot.list_conversations(limit=10, sort_by="CreatedAt")

        self.assertEqual(copilot.client.calls[0][0], "GetConversations")
        self.assertEqual(copilot.client.calls[0][1]["Limit"], "10")
        self.assertEqual(result["Data"][0]["Id"], "conv-1")

    def test_list_conversations_retries_without_sort_when_sort_returns_empty_object(self):
        class SortSensitiveRpcClient:
            def __init__(self):
                self.calls = []

            def do_request(self, params, request, runtime):
                query = getattr(request, "query", {})
                self.calls.append((params.action, dict(query)))
                if query.get("SortBy"):
                    return SimpleNamespace(body={})
                return SimpleNamespace(body={"Data": [{"Id": "conv-1", "Name": "A"}], "HasMore": "true"})

        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.client = SortSensitiveRpcClient()

        result = copilot.list_conversations(limit=10, sort_by="CreatedAt")

        self.assertEqual(result["Data"][0]["Id"], "conv-1")
        self.assertEqual(copilot.client.calls[0], ("GetConversations", {"Limit": "10", "SortBy": "CreatedAt"}))
        self.assertEqual(copilot.client.calls[1], ("GetConversations", {"Limit": "10"}))

    def test_list_custom_agents_uses_list_custom_agent_action(self):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.client = FakeRpcClient({"Data": [{"Id": "agent-1", "Name": "日志分析 Agent"}]})

        result = copilot.list_custom_agents(page_number=1, page_size=20)

        self.assertEqual(copilot.client.calls[0][0], "ListCustomAgent")
        self.assertEqual(copilot.client.calls[0][1]["PageNumber"], "1")
        self.assertEqual(result["Data"][0]["Name"], "日志分析 Agent")

    def test_list_skills_uses_list_skill_action(self):
        copilot = RdsCopilot.__new__(RdsCopilot)
        copilot.client = FakeRpcClient({"Data": [{"Id": "skill-1", "Name": "sql-review"}]})

        result = copilot.list_skills(page_number=2, page_size=20, language="en-US")

        self.assertEqual(copilot.client.calls[0][0], "ListSkill")
        self.assertEqual(copilot.client.calls[0][1]["PageNumber"], "2")
        self.assertEqual(copilot.client.calls[0][1]["Language"], "en-US")
        self.assertEqual(result["Data"][0]["Name"], "sql-review")


class LoggingConfigTest(unittest.TestCase):
    def test_default_log_file_is_rds_copilot_log(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main.get_log_file_path(), os.path.join(os.getcwd(), "rds-copilot.log"))

    def test_log_file_can_be_overridden_by_environment(self):
        with patch.dict(os.environ, {"RDS_COPILOT_LOG_FILE": "/tmp/custom-rds.log"}):
            self.assertEqual(main.get_log_file_path(), "/tmp/custom-rds.log")


class SessionStoreTest(unittest.TestCase):
    def test_session_command_matches_exact_text_only(self):
        self.assertEqual(bot_core.parse_session_command("/session on"), "on")
        self.assertEqual(bot_core.parse_session_command("$session on"), "on")
        self.assertEqual(bot_core.parse_session_command("  /session off  "), "off")
        self.assertEqual(bot_core.parse_session_command("  $session off  "), "off")
        self.assertEqual(bot_core.parse_session_command("  /session  "), "status")
        self.assertEqual(bot_core.parse_session_command("  $session  "), "status")
        self.assertEqual(bot_core.parse_session_command("  /session status  "), "")
        self.assertEqual(bot_core.parse_session_command("  $session status  "), "")
        self.assertEqual(bot_core.parse_session_command("  /session ls  "), "ls")
        self.assertEqual(bot_core.parse_session_command("  $session ls  "), "ls")
        self.assertEqual(bot_core.parse_session_command("  /session 60b335ca  "), "checkout")
        self.assertEqual(bot_core.parse_session_command("  $session 60b335ca  "), "checkout")
        self.assertEqual(bot_core.parse_session_command("  /session 60b335ca-124d-4ee1-864b-de554987abcd  "), "checkout")
        self.assertEqual(bot_core.parse_session_command("  $session 60b335ca-124d-4ee1-864b-de554987abcd  "), "checkout")
        self.assertEqual(bot_core.parse_session_command("  /session checkout abc123  "), "")
        self.assertEqual(bot_core.parse_session_command("  $session checkout abc123  "), "")
        self.assertEqual(bot_core.parse_session_command("  /session abc  "), "")
        self.assertEqual(bot_core.parse_session_command("  $session abc  "), "")
        self.assertEqual(bot_core.parse_session_command("/session  on"), "")
        self.assertEqual(bot_core.parse_session_command("$session  on"), "")
        self.assertEqual(bot_core.parse_session_command("/session on please"), "")
        self.assertEqual(bot_core.parse_session_command("$session on please"), "")
        self.assertEqual(bot_core.parse_session_command("How to use /session on?"), "")

    def test_card_command_matches_exact_text_only(self):
        self.assertEqual(bot_core.parse_card_command("/card on"), "on")
        self.assertEqual(bot_core.parse_card_command("$card on"), "on")
        self.assertEqual(bot_core.parse_card_command("  /card off  "), "off")
        self.assertEqual(bot_core.parse_card_command("  $card off  "), "off")
        self.assertEqual(bot_core.parse_card_command("  /card  "), "status")
        self.assertEqual(bot_core.parse_card_command("  $card  "), "status")
        self.assertEqual(bot_core.parse_card_command("  /card status  "), "")
        self.assertEqual(bot_core.parse_card_command("  $card status  "), "")
        self.assertEqual(bot_core.parse_card_command("/card  on"), "")
        self.assertEqual(bot_core.parse_card_command("$card  on"), "")
        self.assertEqual(bot_core.parse_card_command("/card off please"), "")
        self.assertEqual(bot_core.parse_card_command("$card off please"), "")
        self.assertEqual(bot_core.parse_card_command("How to use /card off?"), "")

    def test_session_is_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))

    def test_card_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            self.assertFalse(store.is_card_enabled("ding-conv-1", "sender-1"))

    def test_card_off_persists_by_dingtalk_conversation_and_sender(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = bot_core.CopilotConversationStore(store_path)

            store.set_card_enabled("ding-conv-1", "sender-1", False)

            reloaded_store = bot_core.CopilotConversationStore(store_path)
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-1"))
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-2"))

    def test_card_on_persists_by_dingtalk_conversation_and_sender(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = bot_core.CopilotConversationStore(store_path)

            store.set_card_enabled("ding-conv-1", "sender-1", True)

            reloaded_store = bot_core.CopilotConversationStore(store_path)
            self.assertTrue(reloaded_store.is_card_enabled("ding-conv-1", "sender-1"))
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-2"))

    def test_saving_conversation_preserves_card_setting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = bot_core.CopilotConversationStore(store_path)

            store.set_card_enabled("ding-conv-1", "sender-1", False)
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")

            reloaded_store = bot_core.CopilotConversationStore(store_path)
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(reloaded_store.get("ding-conv-1", "sender-1"), "copilot-conv-1")

    def test_session_off_persists_and_clears_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.set_session_enabled("ding-conv-1", "sender-1", False)

            reloaded_store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            self.assertFalse(reloaded_store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(reloaded_store.get("ding-conv-1", "sender-1"), "")

    def test_session_on_reenables_without_restoring_old_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.set_session_enabled("ding-conv-1", "sender-1", False)
            store.set_session_enabled("ding-conv-1", "sender-1", True)

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")

    def test_new_conversation_clears_id_but_keeps_session_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.clear("ding-conv-1", "sender-1")

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")

    def test_store_persists_custom_agent_and_preserves_it_on_new_conversation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.set_agent("ding-conv-1", "sender-1", "agent-1", "日志分析 Agent")
            store.clear("ding-conv-1", "sender-1")

            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")
            self.assertEqual(
                store.get_agent("ding-conv-1", "sender-1"),
                {"id": "agent-1", "name": "日志分析 Agent"},
            )

    def test_store_persists_language_and_timezone_preferences(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = bot_core.CopilotConversationStore(store_path)

            self.assertEqual(store.get_language("ding-conv-1", "sender-1"), "zh-CN")
            self.assertEqual(store.get_timezone("ding-conv-1", "sender-1"), "Asia/Shanghai")

            store.set_language("ding-conv-1", "sender-1", "en-US")
            store.set_timezone("ding-conv-1", "sender-1", "Asia/Tokyo")

            reloaded_store = bot_core.CopilotConversationStore(store_path)
            self.assertEqual(reloaded_store.get_language("ding-conv-1", "sender-1"), "en-US")
            self.assertEqual(reloaded_store.get_timezone("ding-conv-1", "sender-1"), "Asia/Tokyo")


class BotCoreRuntimeAndCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_slash_text_is_not_handled_as_control_command(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)

            result = await bot_core.handle_control_command("/sql-review select 1", context, FakeRpcClient())
            dollar_result = await bot_core.handle_control_command("$sql-review select 1", context, FakeRpcClient())

            self.assertFalse(result.handled)
            self.assertFalse(dollar_result.handled)

    async def test_invalid_single_argument_short_commands_return_help(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)

            skills_result = await bot_core.handle_control_command("/skills abc", context, FakeRpcClient())
            session_result = await bot_core.handle_control_command("/session fof", context, FakeRpcClient())
            dollar_session_result = await bot_core.handle_control_command("$session fof", context, FakeRpcClient())
            card_result = await bot_core.handle_control_command("/card status", context, FakeRpcClient())
            dollar_card_result = await bot_core.handle_control_command("$card status", context, FakeRpcClient())
            language_result = await bot_core.handle_control_command("/skills en-US", context, FakeRpcClient())

            self.assertTrue(skills_result.handled)
            self.assertIn("短命令参数不正确", skills_result.content)
            self.assertIn("### RDS Copilot 短命令", skills_result.content)
            self.assertTrue(session_result.handled)
            self.assertIn("`/session fof`", session_result.content)
            self.assertTrue(dollar_session_result.handled)
            self.assertIn("`$session fof`", dollar_session_result.content)
            self.assertTrue(card_result.handled)
            self.assertIn("`/card status`", card_result.content)
            self.assertTrue(dollar_card_result.handled)
            self.assertIn("`$card status`", dollar_card_result.content)
            self.assertTrue(language_result.handled)

    async def test_session_status_reports_on_and_current_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            store.set("ding-conv-1", "sender-1", "conv-1", platform="dingtalk")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)

            result = await bot_core.handle_control_command("/session", context, FakeRpcClient())
            legacy_status = await bot_core.handle_control_command("/session status", context, FakeRpcClient())

            self.assertTrue(result.handled)
            self.assertIn("会话状态：`on`", result.content)
            self.assertIn("ConversationId：`conv-1`", result.content)
            self.assertTrue(legacy_status.handled)
            self.assertIn("短命令参数不正确", legacy_status.content)

    async def test_session_ls_caches_conversations_and_checkout_short_id_switches_context(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)
            copilot = Mock()
            copilot.list_conversations.return_value = {
                "Data": [{"Id": "60b335ca-124d-4ee1-864b-de554987abcd", "Name": "搜索 RDS 资源", "CreatedAt": 1764055092}],
                "HasMore": False,
            }

            ls_result = await bot_core.handle_control_command("/session ls", context, copilot)
            checkout_result = await bot_core.handle_control_command("/session 60b335ca", context, copilot)

            self.assertTrue(ls_result.handled)
            self.assertIn("60b335ca", ls_result.content)
            self.assertTrue(checkout_result.handled)
            self.assertEqual(
                store.get("ding-conv-1", "sender-1", platform="dingtalk"),
                "60b335ca-124d-4ee1-864b-de554987abcd",
            )

    async def test_language_and_timezone_commands_persist_preferences_and_drive_skills(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)
            copilot = Mock()
            copilot.list_skills.return_value = {
                "Data": [{"Id": "skill-1", "Name": "sql-review"}],
                "TotalCount": 1,
                "PageNumber": 1,
            }

            language_options = await bot_core.handle_control_command("/language", context, copilot)
            language_set = await bot_core.handle_control_command("/language en-US", context, copilot)
            normalized_language_set = await bot_core.handle_control_command("/language zh_tw", context, copilot)
            invalid_language = await bot_core.handle_control_command("/language en", context, copilot)
            timezone_options = await bot_core.handle_control_command("/tz", context, copilot)
            timezone_set = await bot_core.handle_control_command("/tz Asia/Tokyo", context, copilot)
            invalid_timezone = await bot_core.handle_control_command("/tz Not/AZone", context, copilot)
            fuzzy_timezone = await bot_core.handle_control_command("/tz asia/shanghai", context, copilot)
            skills = await bot_core.handle_control_command("/skills", context, copilot)

            self.assertTrue(language_options.handled)
            for language in ("zh-CN", "zh-TW", "en-US", "ja-JP"):
                self.assertIn(language, language_options.content)
            self.assertIn("Language switched to `en-US`", language_set.content)
            self.assertIn("語言已切換為 `zh-TW`", normalized_language_set.content)
            self.assertIn("不支援的語言", invalid_language.content)
            self.assertIn("zh-CN", invalid_language.content)
            self.assertIn("Asia/Shanghai", timezone_options.content)
            self.assertIn("時區已切換為 `Asia/Tokyo`", timezone_set.content)
            self.assertIn("不支援的時區", invalid_timezone.content)
            self.assertIn("Asia/Shanghai", fuzzy_timezone.content)
            self.assertEqual(store.get_language("ding-conv-1", "sender-1", platform="dingtalk"), "zh-TW")
            self.assertEqual(store.get_timezone("ding-conv-1", "sender-1", platform="dingtalk"), "Asia/Tokyo")
            self.assertTrue(skills.handled)
            copilot.list_skills.assert_called_with(page_number=1, page_size=20, language="zh-TW")

    async def test_agent_ls_caches_agents_and_agent_name_selects_custom_agent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("feishu", "oc_chat_1", "ou_user_1", store)
            copilot = Mock()
            copilot.list_custom_agents.return_value = {
                "Data": [{"Id": "agent-1", "Name": "日志分析 Agent", "EnableTools": True, "Tools": ["describe_db_instances"]}]
            }

            default_status = await bot_core.handle_control_command("/agent", context, copilot)
            ls_result = await bot_core.handle_control_command("/agent ls", context, copilot)
            select_result = await bot_core.handle_control_command("/agent 日志分析 Agent", context, copilot)
            selected_status = await bot_core.handle_control_command("/agent", context, copilot)

            self.assertTrue(default_status.handled)
            self.assertIn("默认 RDS Copilot", default_status.content)
            self.assertTrue(ls_result.handled)
            self.assertIn("日志分析 Agent", ls_result.content)
            self.assertTrue(select_result.handled)
            self.assertIn("日志分析 Agent", selected_status.content)
            self.assertIn("agent-1", selected_status.content)
            self.assertEqual(
                store.get_agent("oc_chat_1", "ou_user_1", platform="feishu"),
                {"id": "agent-1", "name": "日志分析 Agent"},
            )

    async def test_skills_lists_skills_and_ends_with_usage_hint(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)
            copilot = Mock()
            copilot.list_skills.return_value = {
                "Data": [{"Id": "skill-1", "Name": "sql-review", "Description": "SQL 审查专家", "Dbtypes": ["MySQL"], "SkillType": "system"}],
                "TotalCount": 1,
                "PageNumber": 1,
                "PageSize": 20,
            }

            result = await bot_core.handle_control_command("/skills", context, copilot)

            self.assertTrue(result.handled)
            self.assertIn("sql-review", result.content)
            self.assertTrue(result.content.endswith("输入 /$skillname 使用技能，例如：/sql-review"))

    async def test_control_command_messages_follow_saved_language_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store)
            copilot = Mock()
            copilot.list_skills.return_value = {
                "Data": [{"Id": "skill-1", "Name": "sql-review", "Description": "SQL review", "Dbtypes": ["MySQL"]}],
                "TotalCount": 1,
                "PageNumber": 1,
            }

            default_help = await bot_core.handle_control_command("/help", context, copilot)
            self.assertTrue(default_help.handled)
            self.assertIn("### RDS Copilot 短命令", default_help.content)
            self.assertIn("`/session` 和 `$session` 等价", default_help.content)
            self.assertIn("- `/session|on|off|ls|<id>`", default_help.content)
            self.assertNotIn("RDS Copilot commands", default_help.content)

            english_switch = await bot_core.handle_control_command("/language en-US", context, copilot)
            english_help = await bot_core.handle_control_command("/help", context, copilot)
            self.assertIn("### RDS Copilot commands", english_help.content)
            self.assertIn("`/session` and `$session` are equivalent", english_help.content)
            self.assertIn("- `/help` - Show command help.", english_help.content)
            self.assertIn("**Language switched to `en-US`.**", english_switch.content)

            japanese_switch = await bot_core.handle_control_command("/language ja-JP", context, copilot)
            japanese_skills = await bot_core.handle_control_command("/skills", context, copilot)
            self.assertIn("**言語を `ja-JP` に切り替えました。**", japanese_switch.content)
            self.assertIn("### Skills（1ページ）", japanese_skills.content)
            self.assertIn("- `sql-review`", japanese_skills.content)
            self.assertTrue(japanese_skills.content.endswith("会話で `/$skillname` と入力するとスキルを使用できます。例: `/sql-review`"))
            copilot.list_skills.assert_called_with(page_number=1, page_size=20, language="ja-JP")

    async def test_active_registry_tracks_snapshot_and_stop_calls_copilot(self):
        store = bot_core.CopilotConversationStore("")
        registry = bot_core.ActiveConversationRegistry()
        context = bot_core.BotContext("dingtalk", "ding-conv-1", "sender-1", store, registry=registry)
        state = registry.start(context)
        state.record_task_id("task-1")
        state.record_message("partial answer")
        copilot = Mock()

        btw = await bot_core.handle_control_command("/btw", context, copilot)
        stop = await bot_core.handle_control_command("/stop", context, copilot)

        self.assertIn("partial answer", btw.content)
        self.assertEqual(stop.content, "已停止当前任务。")
        self.assertEqual(stop.response_contents(), ["partial answer", "已停止当前任务。"])
        self.assertIn("已停止", stop.content)
        copilot.stop_task.assert_called_once_with("task-1")

    def test_still_working_message_format_is_fixed(self):
        self.assertEqual(
            bot_core.format_still_working_message(elapsed_seconds=180, event_count=10, language="en-US"),
            "Still working... (3 min elapsed — events 10, receiving stream response)",
        )
        self.assertIn("仍在处理中", bot_core.format_still_working_message(180, 10, language="zh-CN"))


class CardFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_exception_sends_visible_english_error_and_finishes_card(self):
        card = FakeCardInstance()
        failed_stream = AsyncMock(side_effect=ConnectionError("Copilot connection reset"))

        with patch("bridges.dingtalk.dingtalk_stream.AICardReplier", return_value=card), \
            patch("bridges.dingtalk.RdsCopilot", return_value=object()), \
            patch("bridges.dingtalk.call_with_stream", failed_stream):
            result = await dingtalk_bridge.handle_reply_and_update_card(
                FakeHandler(),
                FakeIncomingMessage(),
                language="en-US",
            )

        self.assertEqual(result, "")
        final_call = card.streaming_calls[-1][1]
        self.assertTrue(final_call["finished"])
        self.assertFalse(final_call["failed"])
        self.assertIn("RDS AI diagnosis failed", final_call["content_value"])
        self.assertIn("msg-1", final_call["content_value"])
        self.assertNotIn("ConnectionError", final_call["content_value"])
        self.assertNotIn("Copilot connection reset", final_call["content_value"])

    async def test_no_message_response_sends_visible_english_fallback_and_finishes_card(self):
        card = FakeCardInstance()
        empty_stream = AsyncMock(
            return_value={"content": "", "preparations": [], "conversion_id": ""}
        )

        with patch("bridges.dingtalk.dingtalk_stream.AICardReplier", return_value=card), \
            patch("bridges.dingtalk.RdsCopilot", return_value=object()), \
            patch("bridges.dingtalk.call_with_stream", empty_stream):
            result = await dingtalk_bridge.handle_reply_and_update_card(
                FakeHandler(),
                FakeIncomingMessage(),
                language="en-US",
            )

        self.assertEqual(result, "")
        final_call = card.streaming_calls[-1][1]
        self.assertTrue(final_call["finished"])
        self.assertFalse(final_call["failed"])
        self.assertIn("RDS AI finished", final_call["content_value"])
        self.assertIn("no response content", final_call["content_value"])

    async def test_cancelled_card_response_finishes_without_no_message_fallback(self):
        card = FakeCardInstance()
        cancelled_stream = AsyncMock(
            return_value={"content": "", "preparations": [], "conversion_id": "", "cancelled": True}
        )

        with patch("bridges.dingtalk.dingtalk_stream.AICardReplier", return_value=card), \
            patch("bridges.dingtalk.RdsCopilot", return_value=object()), \
            patch("bridges.dingtalk.call_with_stream", cancelled_stream):
            result = await dingtalk_bridge.handle_reply_and_update_card(
                FakeHandler(),
                FakeIncomingMessage(),
                language="zh-CN",
            )

        self.assertEqual(result, "")
        final_call = card.streaming_calls[-1][1]
        self.assertTrue(final_call["finished"])
        self.assertEqual(final_call["content_value"], "")


class PlainReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_plain_reply_sends_final_content_to_session_webhook_and_stores_conversation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            handler = FakeHandler()
            incoming_message = FakeIncomingMessage("query")
            fake_copilot = FakeRdsCopilot(
                [
                    MessageEvent("task-1", "conv-new", "plain answer"),
                ]
            )

            with patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                patch("bridges.dingtalk.RdsCopilot", return_value=fake_copilot), \
                patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
                final_contents = await dingtalk_bridge.handle_reply_plain_message(
                    handler,
                    incoming_message,
                    {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"},
                    conversion_id="conv-parent",
                    session_enabled=True,
                    language="ja-JP",
                    timezone="Asia/Tokyo",
                )

            send_webhook.assert_awaited_once()
            _, args, _ = send_webhook.mock_calls[0]
            self.assertEqual(args[1], "plain answer")
            self.assertEqual(fake_copilot.last_conversion_id, "conv-parent")
            self.assertEqual(fake_copilot.last_language, "ja-JP")
            self.assertEqual(fake_copilot.last_timezone, "Asia/Tokyo")
            self.assertEqual(final_contents["conversion_id"], "conv-new")
            store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "conv-new")

    async def test_plain_reply_sends_i18n_error_fallback_on_exception(self):
        handler = FakeHandler()
        incoming_message = FakeIncomingMessage("query")

        with patch("bridges.dingtalk.RdsCopilot", side_effect=ConnectionError("connection reset")), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            await dingtalk_bridge.handle_reply_plain_message(
                handler,
                incoming_message,
                {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"},
                language="zh-CN",
            )

        send_webhook.assert_awaited_once()
        _, args, _ = send_webhook.mock_calls[0]
        self.assertIn("RDS AI 诊断失败", args[1])
        self.assertIn("msg-1", args[1])
        self.assertNotIn("ConnectionError", args[1])
        self.assertNotIn("connection reset", args[1])

    async def test_plain_reply_suppresses_empty_fallback_after_stop(self):
        handler = FakeHandler()
        incoming_message = FakeIncomingMessage("query")
        active_state = bot_core.ActiveConversationState("dingtalk", "ding-conv-1", "sender-1")
        active_state.request_cancel()

        with patch("bridges.dingtalk.RdsCopilot", return_value=FakeRdsCopilot([])), \
            patch("bridges.dingtalk.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            final_contents = await dingtalk_bridge.handle_reply_plain_message(
                handler,
                incoming_message,
                {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"},
                language="zh-CN",
                active_state=active_state,
            )

        send_webhook.assert_not_awaited()
        self.assertFalse(hasattr(handler, "last_reply"))
        self.assertTrue(final_contents["cancelled"])


class CardBotHandlerCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"DINGTALK_DM_ALLOW_POLICY": "open", "DINGTALK_GROUP_ALLOW_POLICY": "open"})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    async def test_session_off_command_updates_json_store_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/session off")):
            store = dingtalk_bridge.get_copilot_conversation_store()
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["@staff-1\n多轮对话保持已关闭。"])
            self.assertFalse(store.is_session_enabled("ding-conv-1", "sender-1", platform="dingtalk"))
            self.assertEqual(store.get("ding-conv-1", "sender-1", platform="dingtalk"), "")

    async def test_new_command_clears_json_conversation_id_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/new")):
            store = dingtalk_bridge.get_copilot_conversation_store()
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["@staff-1\n已开启新对话。"])
            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1", platform="dingtalk"))
            self.assertEqual(store.get("ding-conv-1", "sender-1", platform="dingtalk"), "")

    async def test_card_off_command_updates_json_store_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/card off")):
            store = dingtalk_bridge.get_copilot_conversation_store()
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["@staff-1\n卡片回复已关闭。"])
            self.assertFalse(store.is_card_enabled("ding-conv-1", "sender-1", platform="dingtalk"))

    async def test_card_off_routes_to_plain_reply_instead_of_ai_card(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = dingtalk_bridge.JsonCopilotConversationStore(store_path)
            store.set_card_enabled("ding-conv-1", "sender-1", False)
            store.set_language("ding-conv-1", "sender-1", "zh-TW")
            store.set_timezone("ding-conv-1", "sender-1", "Asia/Taipei")
            callback = FakeCallback()
            callback.data = {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"}
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))

            with patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
                patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
                patch("bridges.dingtalk.handle_reply_plain_message", new=AsyncMock()) as plain_reply, \
                patch("bridges.dingtalk.handle_reply_and_update_card", new=AsyncMock()) as card_reply:
                status, message = await handler.process(callback)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            plain_reply.assert_awaited_once()
            self.assertEqual(plain_reply.await_args.kwargs["language"], "zh-TW")
            self.assertEqual(plain_reply.await_args.kwargs["timezone"], "Asia/Taipei")
            card_reply.assert_not_awaited()

    async def test_default_card_setting_routes_to_plain_reply_instead_of_ai_card(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            callback = FakeCallback()
            callback.data = {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"}
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))

            with patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
                patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
                patch("bridges.dingtalk.handle_reply_plain_message", new=AsyncMock()) as plain_reply, \
                patch("bridges.dingtalk.handle_reply_and_update_card", new=AsyncMock()) as card_reply:
                status, message = await handler.process(callback)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            plain_reply.assert_awaited_once()
            card_reply.assert_not_awaited()

    async def test_card_status_reports_current_card_setting(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("bridges.dingtalk.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/card")):
            store = dingtalk_bridge.get_copilot_conversation_store()
            store.set_card_enabled("ding-conv-1", "sender-1", False)
            handler = dingtalk_bridge.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (dingtalk_bridge.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["@staff-1\n卡片回复：`off`"])

    async def test_reply_wrapper_swaps_thinking_to_done_reaction(self):
        handler = FakeHandler()
        incoming_message = FakeIncomingMessage("query")
        reply = AsyncMock()

        with patch("bridges.dingtalk.send_dingtalk_emotion", new=AsyncMock(return_value=True)) as send_emotion:
            await dingtalk_bridge.handle_reply_with_emotions(handler, incoming_message, reply())

        reply.assert_awaited_once()
        self.assertEqual(send_emotion.await_count, 3)
        self.assertEqual(send_emotion.await_args_list[0].args[:3], (handler, incoming_message, "🤔Thinking"))
        self.assertFalse(send_emotion.await_args_list[0].kwargs.get("recall", False))
        self.assertEqual(send_emotion.await_args_list[1].args[:3], (handler, incoming_message, "🤔Thinking"))
        self.assertTrue(send_emotion.await_args_list[1].kwargs["recall"])
        self.assertEqual(send_emotion.await_args_list[2].args[:3], (handler, incoming_message, "🥳Done"))
        self.assertFalse(send_emotion.await_args_list[2].kwargs.get("recall", False))


class BridgeSelectionTest(unittest.TestCase):
    def test_parse_bridge_names_defaults_to_dingtalk(self):
        self.assertEqual(main.parse_bridge_names(""), ["dingtalk"])

    def test_parse_bridge_names_supports_multiple_long_connection_bridges(self):
        self.assertEqual(
            main.parse_bridge_names(" dingtalk, feishu "),
            ["dingtalk", "feishu"],
        )

    def test_parse_bridge_names_rejects_unknown_bridge(self):
        with self.assertRaises(ValueError):
            main.parse_bridge_names("slack")


class FeishuBridgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"FEISHU_DM_ALLOW_POLICY": "open", "FEISHU_GROUP_ALLOW_POLICY": "open"})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    async def test_feishu_text_message_uses_generic_copilot_and_persists_session(self):
        from bridges.feishu import FeishuBridge

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            fake_copilot = FakeRdsCopilot(
                [
                    MessageEvent("task-1", "conv-new", "feishu answer"),
                ]
            )
            bridge = FeishuBridge(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                store=store,
                copilot_factory=lambda: fake_copilot,
            )
            bridge.send_text = AsyncMock(return_value=True)
            bridge.add_processing_reaction = AsyncMock(return_value="reaction-1")
            bridge.remove_processing_reaction = AsyncMock(return_value=True)

            await bridge.handle_text_message(
                chat_id="oc_chat_1",
                sender_id="ou_user_1",
                message_id="om_msg_1",
                text="query",
            )

            bridge.send_text.assert_awaited_once_with(
                "oc_chat_1",
                "feishu answer",
                reply_to_message_id="om_msg_1",
                source=bot_core.SessionSource("feishu", "oc_chat_1", "dm", "ou_user_1"),
            )
            self.assertEqual(fake_copilot.last_request_content, "query")
            self.assertEqual(fake_copilot.last_conversion_id, "")
            self.assertEqual(
                store.get("oc_chat_1", "ou_user_1", platform="feishu"),
                "conv-new",
            )

    async def test_feishu_text_message_passes_selected_custom_agent_language_and_timezone(self):
        from bridges.feishu import FeishuBridge

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot_core.CopilotConversationStore(f"{tmp_dir}/conversations.json")
            store.set_agent("oc_chat_1", "ou_user_1", "agent-1", "日志分析 Agent", platform="feishu")
            store.set_language("oc_chat_1", "ou_user_1", "ja-JP", platform="feishu")
            store.set_timezone("oc_chat_1", "ou_user_1", "Asia/Tokyo", platform="feishu")
            fake_copilot = FakeRdsCopilot(
                [
                    MessageEvent("task-1", "conv-new", "agent answer"),
                ]
            )
            bridge = FeishuBridge(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                store=store,
                copilot_factory=lambda: fake_copilot,
            )
            bridge.send_text = AsyncMock(return_value=True)
            bridge.add_processing_reaction = AsyncMock(return_value="reaction-1")
            bridge.remove_processing_reaction = AsyncMock(return_value=True)

            await bridge.handle_text_message(
                chat_id="oc_chat_1",
                sender_id="ou_user_1",
                message_id="om_msg_1",
                text="query",
            )

            self.assertEqual(fake_copilot.last_custom_agent_id, "agent-1")
            self.assertEqual(fake_copilot.last_language, "ja-JP")
            self.assertEqual(fake_copilot.last_timezone, "Asia/Tokyo")


if __name__ == "__main__":
    unittest.main()
