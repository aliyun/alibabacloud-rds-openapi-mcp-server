import logging
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main


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


class SessionStoreTest(unittest.TestCase):
    def test_session_command_matches_exact_text_only(self):
        self.assertEqual(main.parse_session_command("/session on"), "on")
        self.assertEqual(main.parse_session_command("  /session off  "), "off")
        self.assertEqual(main.parse_session_command("/session  on"), "")
        self.assertEqual(main.parse_session_command("/session on please"), "")
        self.assertEqual(main.parse_session_command("How to use /session on?"), "")

    def test_session_is_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = main.JsonCopilotConversationStore(f"{tmp_dir}/conversations.json")

            self.assertTrue(store.is_session_enabled("ding-conv-1", "sender-1"))

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


if __name__ == "__main__":
    unittest.main()
