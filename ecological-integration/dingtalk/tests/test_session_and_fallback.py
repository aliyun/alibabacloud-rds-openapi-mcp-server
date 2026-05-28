import asyncio
import logging
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main
from rds_copilot import MessageEvent, RdsCopilot


class FakeText:
    def __init__(self, content):
        self.content = content


class FakeIncomingMessage:
    def __init__(self, content="hello", conversation_id="ding-conv-1", sender_id="sender-1"):
        self.text = FakeText(content)
        self.conversation_id = conversation_id
        self.sender_id = sender_id
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

    def call_sseapi(self, *args, **kwargs):
        return self.responses


class FakeRdsCopilot:
    def __init__(self, events):
        self.events = events
        self.last_request_content = None
        self.last_conversion_id = None

    def chat(self, request_content, conversion_id=""):
        self.last_request_content = request_content
        self.last_conversion_id = conversion_id
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


class SessionStoreTest(unittest.TestCase):
    def test_session_command_matches_exact_text_only(self):
        self.assertEqual(main.parse_session_command("/session on"), "on")
        self.assertEqual(main.parse_session_command("  /session off  "), "off")
        self.assertEqual(main.parse_session_command("/session  on"), "")
        self.assertEqual(main.parse_session_command("/session on please"), "")
        self.assertEqual(main.parse_session_command("How to use /session on?"), "")

    def test_card_command_matches_exact_text_only(self):
        self.assertEqual(main.parse_card_command("/card on"), "on")
        self.assertEqual(main.parse_card_command("  /card off  "), "off")
        self.assertEqual(main.parse_card_command("/card  on"), "")
        self.assertEqual(main.parse_card_command("/card off please"), "")
        self.assertEqual(main.parse_card_command("How to use /card off?"), "")

    def test_session_is_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))

    def test_card_is_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            self.assertTrue(store.is_card_enabled("ding-conv-1", "sender-1"))

    def test_card_off_persists_by_dingtalk_conversation_and_sender(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = main.JsonCopilotConversationStore(store_path)

            store.set_card_enabled("ding-conv-1", "sender-1", False)

            reloaded_store = main.JsonCopilotConversationStore(store_path)
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-1"))
            self.assertTrue(reloaded_store.is_card_enabled("ding-conv-1", "sender-2"))

    def test_saving_conversation_preserves_card_setting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = main.JsonCopilotConversationStore(store_path)

            store.set_card_enabled("ding-conv-1", "sender-1", False)
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")

            reloaded_store = main.JsonCopilotConversationStore(store_path)
            self.assertFalse(reloaded_store.is_card_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(reloaded_store.get("ding-conv-1", "sender-1"), "copilot-conv-1")

    def test_session_off_persists_and_clears_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.set_session_enabled("ding-conv-1", "sender-1", False)

            reloaded_store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")
            self.assertFalse(reloaded_store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(reloaded_store.get("ding-conv-1", "sender-1"), "")

    def test_session_on_reenables_without_restoring_old_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.set_session_enabled("ding-conv-1", "sender-1", False)
            store.set_session_enabled("ding-conv-1", "sender-1", True)

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")

    def test_new_conversation_clears_id_but_keeps_session_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            store.clear("ding-conv-1", "sender-1")

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")


class CardFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_exception_sends_visible_english_error_and_finishes_card(self):
        card = FakeCardInstance()
        failed_stream = AsyncMock(side_effect=ConnectionError("Copilot connection reset"))

        with patch("main.dingtalk_stream.AICardReplier", return_value=card), \
            patch("main.RdsCopilot", return_value=object()), \
            patch("main.call_with_stream", failed_stream):
            result = await main.handle_reply_and_update_card(
                FakeHandler(),
                FakeIncomingMessage(),
            )

        self.assertEqual(result, "")
        final_call = card.streaming_calls[-1][1]
        self.assertTrue(final_call["finished"])
        self.assertFalse(final_call["failed"])
        self.assertIn("RDS AI diagnosis failed", final_call["content_value"])
        self.assertIn("ConnectionError", final_call["content_value"])

    async def test_no_message_response_sends_visible_english_fallback_and_finishes_card(self):
        card = FakeCardInstance()
        empty_stream = AsyncMock(
            return_value={"content": "", "preparations": [], "conversion_id": ""}
        )

        with patch("main.dingtalk_stream.AICardReplier", return_value=card), \
            patch("main.RdsCopilot", return_value=object()), \
            patch("main.call_with_stream", empty_stream):
            result = await main.handle_reply_and_update_card(
                FakeHandler(),
                FakeIncomingMessage(),
            )

        self.assertEqual(result, "")
        final_call = card.streaming_calls[-1][1]
        self.assertTrue(final_call["finished"])
        self.assertFalse(final_call["failed"])
        self.assertIn("RDS AI finished", final_call["content_value"])
        self.assertIn("no response content", final_call["content_value"])


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
                patch("main.RdsCopilot", return_value=fake_copilot), \
                patch("main.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
                final_contents = await main.handle_reply_plain_message(
                    handler,
                    incoming_message,
                    {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"},
                    conversion_id="conv-parent",
                    session_enabled=True,
                )

            send_webhook.assert_awaited_once()
            _, args, _ = send_webhook.mock_calls[0]
            self.assertEqual(args[1], "plain answer")
            self.assertEqual(fake_copilot.last_conversion_id, "conv-parent")
            self.assertEqual(final_contents["conversion_id"], "conv-new")
            store = main.JsonCopilotConversationStore(store_path)
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "conv-new")

    async def test_plain_reply_sends_english_error_fallback_on_exception(self):
        handler = FakeHandler()
        incoming_message = FakeIncomingMessage("query")

        with patch("main.RdsCopilot", side_effect=ConnectionError("connection reset")), \
            patch("main.send_dingtalk_session_webhook", new=AsyncMock(return_value=True)) as send_webhook:
            await main.handle_reply_plain_message(
                handler,
                incoming_message,
                {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"},
            )

        send_webhook.assert_awaited_once()
        _, args, _ = send_webhook.mock_calls[0]
        self.assertIn("RDS AI diagnosis failed", args[1])
        self.assertIn("ConnectionError", args[1])


class CardBotHandlerCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_off_command_updates_json_store_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("main.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/session off")):
            store = main.get_copilot_conversation_store()
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            handler = main.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (main.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["Conversation context is disabled."])
            self.assertFalse(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")

    async def test_new_command_clears_json_conversation_id_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("main.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/new")):
            store = main.get_copilot_conversation_store()
            store.set("ding-conv-1", "sender-1", "copilot-conv-1")
            handler = main.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (main.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["Started a new conversation."])
            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))
            self.assertEqual(store.get("ding-conv-1", "sender-1"), "")

    async def test_card_off_command_updates_json_store_and_replies_in_english(self):
        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": f"{tmp_dir}/conversations.json"}), \
            patch("main.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("/card off")):
            store = main.get_copilot_conversation_store()
            handler = main.CardBotHandler(logger=logging.getLogger("test"))
            replies = []
            handler.reply_text = lambda text, message: replies.append(text)

            status, message = await handler.process(FakeCallback())

            self.assertEqual((status, message), (main.AckMessage.STATUS_OK, "OK"))
            self.assertEqual(replies, ["Card replies are disabled."])
            self.assertFalse(store.is_card_enabled("ding-conv-1", "sender-1"))

    async def test_card_off_routes_to_plain_reply_instead_of_ai_card(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_path = f"{tmp_dir}/conversations.json"
            store = main.JsonCopilotConversationStore(store_path)
            store.set_card_enabled("ding-conv-1", "sender-1", False)
            callback = FakeCallback()
            callback.data = {"sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=test"}
            handler = main.CardBotHandler(logger=logging.getLogger("test"))

            with patch.dict("os.environ", {"RDS_COPILOT_CONVERSATION_STORE_FILE": store_path}), \
                patch("main.dingtalk_stream.ChatbotMessage.from_dict", return_value=FakeIncomingMessage("query")), \
                patch("main.send_dingtalk_emotion", new=AsyncMock(return_value=True)), \
                patch("main.handle_reply_plain_message", new=AsyncMock()) as plain_reply, \
                patch("main.handle_reply_and_update_card", new=AsyncMock()) as card_reply:
                status, message = await handler.process(callback)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            self.assertEqual((status, message), (main.AckMessage.STATUS_OK, "OK"))
            plain_reply.assert_awaited_once()
            card_reply.assert_not_awaited()

    async def test_reply_wrapper_swaps_thinking_to_done_reaction(self):
        handler = FakeHandler()
        incoming_message = FakeIncomingMessage("query")
        reply = AsyncMock()

        with patch("main.send_dingtalk_emotion", new=AsyncMock(return_value=True)) as send_emotion:
            await main.handle_reply_with_emotions(handler, incoming_message, reply())

        reply.assert_awaited_once()
        self.assertEqual(send_emotion.await_count, 3)
        self.assertEqual(send_emotion.await_args_list[0].args[:3], (handler, incoming_message, "🤔Thinking"))
        self.assertFalse(send_emotion.await_args_list[0].kwargs.get("recall", False))
        self.assertEqual(send_emotion.await_args_list[1].args[:3], (handler, incoming_message, "🤔Thinking"))
        self.assertTrue(send_emotion.await_args_list[1].kwargs["recall"])
        self.assertEqual(send_emotion.await_args_list[2].args[:3], (handler, incoming_message, "🥳Done"))
        self.assertFalse(send_emotion.await_args_list[2].kwargs.get("recall", False))


if __name__ == "__main__":
    unittest.main()
