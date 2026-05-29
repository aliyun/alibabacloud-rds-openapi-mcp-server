import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.rds_copilot import MessageEvent


def load_script_module():
    from scripts import e2e_rdsai_bridges

    return e2e_rdsai_bridges


class FakeCopilot:
    def __init__(self):
        self.chat_calls = []

    def chat(
        self,
        query,
        conversion_id="",
        trace_id="",
        custom_agent_id="",
        language="zh-CN",
        timezone="Asia/Shanghai",
    ):
        self.chat_calls.append((query, conversion_id, trace_id, custom_agent_id, language, timezone))
        yield MessageEvent("task-1", "conv-1", "pong")

    def list_conversations(self, **kwargs):
        return {"Data": [{"Id": "conv-1", "Name": "E2E"}], "HasMore": False}

    def list_custom_agents(self, **kwargs):
        return {"Data": [{"Id": "agent-1", "Name": "Agent"}], "TotalCount": 1}

    def list_skills(self, **kwargs):
        return {"Data": [{"Name": "sql-review"}], "TotalCount": 1, "PageNumber": 1}


class MultiMessageCopilot(FakeCopilot):
    def chat(
        self,
        query,
        conversion_id="",
        trace_id="",
        custom_agent_id="",
        language="zh-CN",
        timezone="Asia/Shanghai",
    ):
        self.chat_calls.append((query, conversion_id, trace_id, custom_agent_id, language, timezone))
        yield MessageEvent("task-1", "conv-1", "first")
        yield MessageEvent("task-1", "conv-1", "second")


class NonMessageCopilot(FakeCopilot):
    def chat(self, *args, **kwargs):
        self.chat_calls.append((args, kwargs))
        yield SimpleNamespace(task_id="task-1", conversion_id="conv-1")


class BadListCopilot(FakeCopilot):
    def list_conversations(self, **kwargs):
        return []


class E2EScriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_helpers_cover_truncation_invalid_env_and_validation_errors(self):
        e2e = load_script_module()

        self.assertEqual(e2e.preview_text("abcdef", limit=3), "abc...")
        self.assertIsNone(e2e._parse_known_env_line("not an env line"))
        self.assertEqual(e2e._parse_known_env_line("- ACCESS_KEY_ID=ak-from-bullet"), ("ACCESS_KEY_ID", "ak-from-bullet"))
        self.assertEqual(e2e._parse_known_env_line('ACCESS_KEY_ID="unterminated'), ("ACCESS_KEY_ID", "unterminated"))
        self.assertFalse(e2e._load_known_env_pairs("/tmp/does-not-exist-rdsai-e2e.env"))
        with patch.object(e2e, "load_dotenv", return_value=True):
            self.assertTrue(e2e.load_env_file(""))

        old_values = {key: os.environ.pop(key, None) for key in ("ACCESS_KEY_ID", "ACCESS_SECRET")}
        try:
            self.assertEqual(e2e.missing_rdsai_env(), ["ACCESS_KEY_ID", "ACCESS_SECRET"])
        finally:
            for key, value in old_values.items():
                if value is not None:
                    os.environ[key] = value

        with self.assertRaisesRegex(TypeError, "expected dict"):
            e2e._validate_mapping("BadAPI", [])

    async def test_env_file_loader_reads_current_dotenv_without_printing_secret(self):
        e2e = load_script_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = os.path.join(tmp_dir, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("ACCESS_KEY_ID=ak-from-dotenv\n")
                f.write("ACCESS_SECRET=secret-from-dotenv\n")

            old_ak = os.environ.pop("ACCESS_KEY_ID", None)
            old_sk = os.environ.pop("ACCESS_SECRET", None)
            try:
                loaded = e2e.load_env_file(env_path)
                self.assertTrue(loaded)
                self.assertEqual(os.getenv("ACCESS_KEY_ID"), "ak-from-dotenv")
                self.assertEqual(os.getenv("ACCESS_SECRET"), "secret-from-dotenv")
            finally:
                os.environ.pop("ACCESS_KEY_ID", None)
                os.environ.pop("ACCESS_SECRET", None)
                if old_ak is not None:
                    os.environ["ACCESS_KEY_ID"] = old_ak
                if old_sk is not None:
                    os.environ["ACCESS_SECRET"] = old_sk

    async def test_env_file_loader_reads_allowlisted_dotenv_exports(self):
        e2e = load_script_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = os.path.join(tmp_dir, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("# local secrets\n")
                f.write('export ACCESS_KEY_ID="ak-from-env"\n')
                f.write("ACCESS_SECRET='secret-from-env'\n")
                f.write("UNRELATED_SECRET=should-not-load\n")

            old_values = {key: os.environ.pop(key, None) for key in ("ACCESS_KEY_ID", "ACCESS_SECRET", "UNRELATED_SECRET")}
            try:
                loaded = e2e.load_env_file(env_path)
                self.assertTrue(loaded)
                self.assertEqual(os.getenv("ACCESS_KEY_ID"), "ak-from-env")
                self.assertEqual(os.getenv("ACCESS_SECRET"), "secret-from-env")
                self.assertIsNone(os.getenv("UNRELATED_SECRET"))
            finally:
                for key in ("ACCESS_KEY_ID", "ACCESS_SECRET", "UNRELATED_SECRET"):
                    os.environ.pop(key, None)
                    if old_values[key] is not None:
                        os.environ[key] = old_values[key]

    async def test_errors_are_redacted_before_printing(self):
        e2e = load_script_module()
        old_ak = os.environ.get("ACCESS_KEY_ID")
        old_sk = os.environ.get("ACCESS_SECRET")
        try:
            os.environ["ACCESS_KEY_ID"] = "ak-redact-me"
            os.environ["ACCESS_SECRET"] = "secret-redact-me"

            error = e2e.exception_text(RuntimeError("bad ak-redact-me secret-redact-me"))

            self.assertIn("***", error)
            self.assertNotIn("ak-redact-me", error)
            self.assertNotIn("secret-redact-me", error)
        finally:
            os.environ.pop("ACCESS_KEY_ID", None)
            os.environ.pop("ACCESS_SECRET", None)
            if old_ak is not None:
                os.environ["ACCESS_KEY_ID"] = old_ak
            if old_sk is not None:
                os.environ["ACCESS_SECRET"] = old_sk

    async def test_rdsai_smoke_uses_real_wrapper_shape_with_safe_preview(self):
        e2e = load_script_module()
        fake = FakeCopilot()

        result = e2e.run_rdsai_smoke("say pong", copilot_factory=lambda: fake)

        self.assertTrue(result.passed)
        self.assertIn("conversation_id=conv-1", result.detail)
        self.assertIn("preview=pong", result.detail)
        self.assertEqual(fake.chat_calls[0][0], "say pong")

    async def test_rdsai_smoke_consumes_full_stream_by_default_and_can_stop_after_first_message(self):
        e2e = load_script_module()

        full_fake = MultiMessageCopilot()
        full_result = e2e.run_rdsai_smoke("say pong", copilot_factory=lambda: full_fake)

        self.assertTrue(full_result.passed)
        self.assertIn("preview=firstsecond", full_result.detail)

        fast_fake = MultiMessageCopilot()
        fast_result = e2e.run_rdsai_smoke(
            "say pong",
            copilot_factory=lambda: fast_fake,
            first_message_only=True,
        )

        self.assertTrue(fast_result.passed)
        self.assertIn("preview=first", fast_result.detail)
        self.assertNotIn("second", fast_result.detail)

    async def test_rdsai_smoke_reports_no_content_and_api_shape_errors(self):
        e2e = load_script_module()

        no_content = e2e.run_rdsai_smoke("say pong", copilot_factory=NonMessageCopilot, max_events=1)
        self.assertFalse(no_content.passed)
        self.assertIn("no displayable", no_content.error)

        bad_api = e2e.run_rdsai_smoke("say pong", copilot_factory=BadListCopilot)
        self.assertFalse(bad_api.passed)
        self.assertIn("TypeError", bad_api.error)

    async def test_first_message_copilot_wrapper_stops_bridge_after_first_reply(self):
        e2e = load_script_module()
        fake = MultiMessageCopilot()
        wrapped = e2e.FirstMessageCopilot(lambda: fake)

        events = list(wrapped.chat("say pong"))

        self.assertEqual([event.text for event in events], ["first"])

    async def test_mock_bridge_e2e_drives_dingtalk_and_feishu_handlers(self):
        e2e = load_script_module()

        result = await e2e.run_mock_bridge_e2e(
            "say pong",
            copilot_factory=FakeCopilot,
            run_dingtalk=True,
            run_feishu=True,
            run_wecom=True,
            run_qqbot=True,
        )

        self.assertTrue(result.passed)
        self.assertIn("dingtalk_replies=", result.detail)
        self.assertIn("feishu_replies=", result.detail)
        self.assertIn("wecom_replies=", result.detail)
        self.assertIn("qqbot_replies=", result.detail)

        command_result = await e2e.run_mock_bridge_e2e(
            "/agent ls",
            copilot_factory=FakeCopilot,
            run_dingtalk=True,
            run_feishu=True,
            run_wecom=True,
            run_qqbot=True,
        )

        self.assertTrue(command_result.passed)
        self.assertIn("Agent", command_result.detail)

    async def test_mock_bridge_e2e_reports_empty_bridge_replies(self):
        e2e = load_script_module()

        with patch.object(e2e, "_run_dingtalk_mock_query", new=AsyncMock(return_value=[])):
            dingtalk_result = await e2e.run_mock_bridge_e2e(
                "say pong",
                copilot_factory=FakeCopilot,
                run_dingtalk=True,
                run_feishu=False,
                run_wecom=False,
                run_qqbot=False,
            )
        self.assertFalse(dingtalk_result.passed)
        self.assertIn("DingTalk mock produced no reply", dingtalk_result.error)

        with patch.object(e2e, "_run_feishu_mock_query", new=AsyncMock(return_value=[])):
            feishu_result = await e2e.run_mock_bridge_e2e(
                "say pong",
                copilot_factory=FakeCopilot,
                run_dingtalk=False,
                run_feishu=True,
                run_wecom=False,
                run_qqbot=False,
            )
        self.assertFalse(feishu_result.passed)
        self.assertIn("Feishu mock produced no reply", feishu_result.error)

        with patch.object(e2e, "_run_wecom_mock_query", new=AsyncMock(return_value=[])):
            wecom_result = await e2e.run_mock_bridge_e2e(
                "say pong",
                copilot_factory=FakeCopilot,
                run_dingtalk=False,
                run_feishu=False,
                run_wecom=True,
                run_qqbot=False,
            )
        self.assertFalse(wecom_result.passed)
        self.assertIn("WeCom mock produced no reply", wecom_result.error)

        with patch.object(e2e, "_run_qq_mock_query", new=AsyncMock(return_value=[])):
            qq_result = await e2e.run_mock_bridge_e2e(
                "say pong",
                copilot_factory=FakeCopilot,
                run_dingtalk=False,
                run_feishu=False,
                run_wecom=False,
                run_qqbot=True,
            )
        self.assertFalse(qq_result.passed)
        self.assertIn("QQ Bot mock produced no reply", qq_result.error)

    async def test_mock_bridge_e2e_times_out_stuck_platform_steps(self):
        e2e = load_script_module()

        async def stuck_query(*args, **kwargs):
            await asyncio.sleep(30)
            return ["late"]

        with patch.object(e2e, "_run_feishu_mock_query", new=AsyncMock(side_effect=stuck_query)), \
            patch.dict(os.environ, {"RDS_E2E_MOCK_BRIDGE_TIMEOUT_SECONDS": "0.01"}):
            result = await asyncio.wait_for(
                e2e.run_mock_bridge_e2e(
                    "say pong",
                    copilot_factory=FakeCopilot,
                    run_dingtalk=False,
                    run_feishu=True,
                    run_wecom=False,
                    run_qqbot=False,
                ),
                timeout=1,
            )
        self.assertFalse(result.passed)
        self.assertIn("Feishu mock timed out", result.error)

    async def test_dingtalk_mock_query_reports_unexpected_handler_status(self):
        e2e = load_script_module()

        with tempfile.TemporaryDirectory() as tmp_dir, \
            patch("bridges.dingtalk.CardBotHandler.process", new=AsyncMock(return_value=("ERR", "bad"))):
            with self.assertRaisesRegex(RuntimeError, "DingTalk handler returned"):
                await e2e._run_dingtalk_mock_query(
                    "say pong",
                    os.path.join(tmp_dir, "conversations.json"),
                    FakeCopilot,
                )

    async def test_cli_helpers_parse_run_and_main_paths(self):
        e2e = load_script_module()

        args = e2e.parse_args(
            [
                "--query",
                "hello",
                "--conversation-id",
                "conv-1",
                "--custom-agent-id",
                "agent-1",
                "--skip-feishu",
                "--first-message-only",
                "--max-events",
                "0",
            ]
        )
        self.assertEqual(args.query, "hello")
        self.assertTrue(args.skip_feishu)
        self.assertTrue(args.first_message_only)

        with patch("builtins.print") as print_mock:
            e2e.print_result(e2e.StepResult("Step", False, "detail", "error"))
        print_mock.assert_called_once()
        self.assertIn("FAIL", print_mock.call_args.args[0])

        missing_args = SimpleNamespace(
            env_file="",
            skip_rdsai_smoke=False,
            skip_dingtalk=False,
            skip_feishu=False,
            skip_wecom=False,
            skip_qqbot=False,
            query="hello",
            conversation_id="",
            custom_agent_id="",
            max_events=1,
            first_message_only=False,
        )
        old_values = {key: os.environ.pop(key, None) for key in ("ACCESS_KEY_ID", "ACCESS_SECRET")}
        try:
            missing = await e2e.run(missing_args)
        finally:
            for key, value in old_values.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(missing[0].name, "Credentials")

        loop_sensitive_args = SimpleNamespace(
            env_file="",
            skip_rdsai_smoke=False,
            skip_dingtalk=True,
            skip_feishu=True,
            skip_wecom=True,
            skip_qqbot=True,
            query="hello",
            conversation_id="",
            custom_agent_id="",
            max_events=1,
            first_message_only=False,
        )

        def loop_sensitive_smoke(*args, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(asyncio.sleep(0))
            finally:
                loop.close()
            return e2e.StepResult("RDSAI chat", True, "thread ok")

        with patch.object(e2e, "missing_rdsai_env", return_value=[]), \
            patch.object(e2e, "run_rdsai_smoke", side_effect=loop_sensitive_smoke):
            loop_sensitive_results = await e2e.run(loop_sensitive_args)
        self.assertEqual(loop_sensitive_results[0].detail, "thread ok")

        run_args = SimpleNamespace(
            env_file="secrets.env",
            skip_rdsai_smoke=False,
            skip_dingtalk=True,
            skip_feishu=False,
            skip_wecom=True,
            skip_qqbot=True,
            query="hello",
            conversation_id="conv-1",
            custom_agent_id="agent-1",
            max_events=0,
            first_message_only=True,
        )
        with patch.object(e2e, "load_env_file", return_value=True) as load_env, \
            patch.object(e2e, "missing_rdsai_env", return_value=[]), \
            patch.object(e2e, "run_rdsai_smoke", return_value=e2e.StepResult("RDSAI chat", True, "ok")) as smoke, \
            patch.object(e2e, "run_mock_bridge_e2e", new=AsyncMock(return_value=e2e.StepResult("Mock bot bridge E2E", True, "ok"))) as bridge:
            results = await e2e.run(run_args)
        load_env.assert_called_once_with("secrets.env")
        smoke.assert_called_once()
        bridge.assert_awaited_once()
        self.assertEqual(len(results), 2)

        with patch.object(e2e, "parse_args", return_value=run_args), \
            patch.object(e2e, "run", new=Mock(return_value="run-token")), \
            patch.object(e2e.asyncio, "run", return_value=[e2e.StepResult("Step", True, "ok")]), \
            patch.object(e2e, "print_result") as print_result:
            self.assertEqual(e2e.main(["--query", "hello"]), 0)
        print_result.assert_called_once()

        with patch.object(e2e, "parse_args", return_value=run_args), \
            patch.object(e2e, "run", new=Mock(return_value="run-token")), \
            patch.object(e2e.asyncio, "run", return_value=[e2e.StepResult("Step", False, "bad")]), \
            patch.object(e2e, "print_result"):
            self.assertEqual(e2e.main(["--query", "hello"]), 1)


if __name__ == "__main__":
    unittest.main()
