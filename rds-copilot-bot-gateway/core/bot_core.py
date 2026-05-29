import asyncio
import difflib
import inspect
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Queue
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from loguru import logger

from core.rds_copilot import (
    DocumentEvent,
    MessageEvent,
    RdsCopilot,
    StreamProgressEvent,
    ToolCallEnd,
    ToolCallPending,
    ToolCallStart,
)


CONVERSATION_STORE_FILE_ENV = "RDS_COPILOT_CONVERSATION_STORE_FILE"
DEFAULT_CONVERSATION_STORE_FILE = "copilot_conversations.json"
NEW_CONVERSATION_COMMAND = "/new"
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_TIMEZONE = "Asia/Shanghai"
SUPPORTED_LANGUAGE_VALUES = ("zh-CN", "zh-TW", "en-US", "ja-JP")
SUPPORTED_SKILL_LANGUAGES = set(SUPPORTED_LANGUAGE_VALUES)
STILL_WORKING_INTERVAL_ENV = "RDS_BOT_STILL_WORKING_INTERVAL_SECONDS"
CONVERSATION_SHORT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$")
CONVERSATION_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CONVERSATION_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")

_store_locks_guard = threading.RLock()
_store_locks: dict[str, threading.RLock] = {}
_timezones_lock = threading.RLock()
_timezones_cache: set[str] | None = None


def _get_store_lock(file_path: str) -> threading.RLock:
    normalized = os.path.abspath(file_path or "__memory__")
    with _store_locks_guard:
        lock = _store_locks.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _store_locks[normalized] = lock
        return lock


@dataclass
class SessionSource:
    platform: str
    chat_id: str
    chat_type: str = "dm"
    user_id: str = ""
    user_name: str = ""
    thread_id: str = ""
    user_id_alt: str = ""
    is_bot: bool = False

    def identity_values(self) -> set[str]:
        return {value for value in (self.user_id, self.user_id_alt) if value}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _normalize_allow_entry(entry: str, platform: str = "") -> str:
    value = str(entry or "").strip()
    if platform:
        value = re.sub(rf"^{re.escape(platform)}:", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(user|group|chat):", "", value, flags=re.IGNORECASE)
    return value.strip()


def _matches_allowlist(values: set[str], entries: list[str], platform: str = "") -> bool:
    if not entries:
        return False
    normalized_values = {str(value).strip() for value in values if str(value).strip()}
    normalized_values_lower = {value.lower() for value in normalized_values}
    for entry in entries:
        normalized_entry = _normalize_allow_entry(entry, platform)
        if normalized_entry == "*":
            return True
        if normalized_entry in normalized_values or normalized_entry.lower() in normalized_values_lower:
            return True
    return False


PLATFORM_ALLOWED_USERS_ENV = {
    "dingtalk": "DINGTALK_ALLOWED_USERS",
    "feishu": "FEISHU_ALLOWED_USERS",
    "wecom": "WECOM_ALLOWED_USERS",
    "qqbot": "QQ_ALLOWED_USERS",
}

PLATFORM_ALLOW_ALL_ENV = {
    "dingtalk": "DINGTALK_ALLOW_ALL_USERS",
    "feishu": "FEISHU_ALLOW_ALL_USERS",
    "wecom": "WECOM_ALLOW_ALL_USERS",
    "qqbot": "QQ_ALLOW_ALL_USERS",
}


def authorize_session_source(source: SessionSource) -> bool:
    platform = (source.platform or "").strip().lower()
    identities = source.identity_values()
    if not identities:
        return False

    allow_all_env = PLATFORM_ALLOW_ALL_ENV.get(platform, "")
    if allow_all_env and _env_flag(allow_all_env):
        return True

    allowed_users_env = PLATFORM_ALLOWED_USERS_ENV.get(platform, "")
    if allowed_users_env and _matches_allowlist(identities, _split_env_list(allowed_users_env), platform):
        return True

    if _matches_allowlist(identities, _split_env_list("GATEWAY_ALLOWED_USERS"), platform):
        return True

    return _env_flag("GATEWAY_ALLOW_ALL_USERS")


def _source_chat_matches(source: SessionSource, env_name: str) -> bool:
    return _matches_allowlist({source.chat_id}, _split_env_list(env_name), source.platform)


def _text_matches_patterns(text: str, env_name: str) -> bool:
    patterns = _split_env_list(env_name)
    if not patterns:
        return False
    lowered_text = (text or "").lower()
    return any(pattern.lower() in lowered_text for pattern in patterns)


def should_accept_session_source(source: SessionSource, text: str = "", *, mentioned: bool = False) -> bool:
    platform = (source.platform or "").strip().lower()
    chat_type = (source.chat_type or "dm").strip().lower()

    if platform == "dingtalk":
        allowed_chats = _split_env_list("DINGTALK_ALLOWED_CHATS")
        if allowed_chats and not _source_chat_matches(source, "DINGTALK_ALLOWED_CHATS"):
            return False
        if chat_type == "group" and _env_flag("DINGTALK_REQUIRE_MENTION"):
            if _source_chat_matches(source, "DINGTALK_FREE_RESPONSE_CHATS"):
                return True
            return mentioned or _text_matches_patterns(text, "DINGTALK_MENTION_PATTERNS")
        return True

    if platform == "feishu":
        if source.is_bot and not _env_flag("FEISHU_ALLOW_BOTS"):
            return False
        if chat_type == "group":
            policy = os.getenv("FEISHU_GROUP_POLICY", "mention").strip().lower()
            if policy == "disabled":
                return False
            require_mention = _env_flag("FEISHU_REQUIRE_MENTION", default=(policy != "open"))
            if require_mention:
                bot_tokens = {
                    os.getenv("FEISHU_BOT_OPEN_ID", "").strip(),
                    os.getenv("FEISHU_BOT_USER_ID", "").strip(),
                    os.getenv("FEISHU_BOT_NAME", "").strip(),
                }
                bot_tokens = {token for token in bot_tokens if token}
                mentioned_by_token = any(token in (text or "") for token in bot_tokens)
                return mentioned or mentioned_by_token
        return True

    if platform == "wecom":
        if chat_type == "group":
            policy = os.getenv("WECOM_GROUP_POLICY", "open").strip().lower()
            if policy == "disabled":
                return False
            if policy == "allowlist":
                return _source_chat_matches(source, "WECOM_ALLOWED_CHATS")
            return True
        policy = os.getenv("WECOM_DM_POLICY", "open").strip().lower()
        if policy == "disabled":
            return False
        if policy == "allowlist":
            return _matches_allowlist(source.identity_values(), _split_env_list("WECOM_ALLOWED_USERS"), platform)
        return True

    if platform == "qqbot":
        if chat_type == "group":
            policy = os.getenv("QQ_GROUP_POLICY", "open").strip().lower()
            if policy == "disabled":
                return False
            group_entries = _split_env_list("QQ_GROUP_ALLOWED_USERS")
            if group_entries:
                return _matches_allowlist({source.chat_id}, group_entries, platform)
        else:
            policy = os.getenv("QQ_DM_POLICY", "open").strip().lower()
            if policy == "disabled":
                return False
        return True

    return True


def parse_session_command(text: str) -> str:
    normalized_text = _normalize_command_prefix(text)
    if normalized_text == "/session":
        return "status"
    if normalized_text == "/session on":
        return "on"
    if normalized_text == "/session off":
        return "off"
    if normalized_text == "/session ls":
        return "ls"
    parts = normalized_text.split(maxsplit=1)
    if len(parts) == 2 and parts[0] == "/session" and _is_conversation_id_token(parts[1].strip()):
        return "checkout"
    return ""


def _is_conversation_id_token(token: str) -> bool:
    normalized = (token or "").strip()
    if not normalized or " " in normalized:
        return False
    return bool(
        CONVERSATION_SHORT_ID_RE.fullmatch(normalized)
        or CONVERSATION_UUID_RE.fullmatch(normalized)
        or CONVERSATION_OPAQUE_ID_RE.fullmatch(normalized)
    )


def _normalize_language(language: str) -> str:
    token = (language or "").strip().replace("_", "-")
    parts = token.split("-")
    if len(parts) == 2 and parts[0] and parts[1]:
        token = f"{parts[0].lower()}-{parts[1].upper()}"
    return token if token in SUPPORTED_SKILL_LANGUAGES else ""


def _normalize_command_prefix(text: str) -> str:
    normalized = (text or "").strip()
    if normalized.startswith("$"):
        return "/" + normalized[1:]
    return normalized


I18N_MESSAGES = {
    "zh-CN": {
        "help_title": "RDS Copilot 短命令",
        "help_items": [
            ("/help", "查看短命令帮助。"),
            ("/btw", "查看当前正在运行任务已经收到的回复内容。"),
            ("/stop", "停止当前正在运行的 RDS AI 任务。"),
            ("/card|on|off", "查看或管理钉钉 AI 卡片回复。"),
            ("/session|on|off|ls|<id>", "查看或管理多轮对话上下文。"),
            ("/new", "开启新对话。"),
            ("/agent|ls|<agent-name>|default", "查看或管理 Custom Agent。"),
            ("/language [zh-CN|zh-TW|en-US|ja-JP]", "查看或切换短命令语言。"),
            ("/tz [IANA timezone]", "查看或切换当前时区。"),
            ("/skills [page]", "查看 Skill 列表。"),
        ],
        "help_prefix_hint": "提示：短命令支持 `/` 或 `$` 前缀，例如 `/session` 和 `$session` 等价。",
        "invalid_command_argument": "短命令参数不正确：`{command}`。请参考下面的帮助。",
        "no_active_task": "当前没有正在运行的任务。",
        "no_active_task_stop": "当前没有可停止的任务。",
        "no_displayable_content": "还没有收到可展示的回复内容。",
        "stopped_task": "已停止当前任务。",
        "error_failed": "RDS AI 诊断失败，未能生成完整回复。",
        "error_label": "错误",
        "error_retry": "请稍后重试，或联系服务维护者检查日志。",
        "no_message": "RDS AI 已结束，但未返回回复内容。",
        "busy": "已有任务正在运行。输入 `/btw` 查看当前回复，或输入 `/stop` 停止当前任务。",
        "still_working": "仍在处理中...（已耗时 {minutes} 分钟 - events {events}，正在接收流式响应）",
        "language_title": "语言设置",
        "language_current": "当前语言：`{language}`",
        "language_available": "可选语言：`{values}`",
        "language_hint": "输入 `/language <language>` 切换语言。",
        "unsupported_language": "不支持的语言：`{language}`",
        "language_switched": "语言已切换为 `{language}`。",
        "timezone_title": "时区设置",
        "timezone_current": "当前时区：`{timezone}`",
        "timezone_hint": "输入 `/tz <IANA timezone>` 切换时区，例如 `/tz Asia/Shanghai`。",
        "unsupported_timezone": "不支持的时区：`{timezone}`",
        "timezone_suggestions": "你是不是想选：{suggestions}",
        "timezone_switched": "时区已切换为 `{timezone}`。",
        "card_unsupported": "当前平台不支持卡片回复。",
        "card_status": "卡片回复：`{status}`",
        "card_enabled": "卡片回复已开启。",
        "card_disabled": "卡片回复已关闭。",
        "session_enabled": "多轮对话保持已开启。",
        "session_disabled": "多轮对话保持已关闭。",
        "session_status": "会话状态：`{status}`\nConversationId：`{conversation_id}`",
        "none": "无",
        "conversation_not_found": "没有找到这个对话。请先运行 `/session ls`，或提供完整 ConversationId。",
        "checked_out": "已切换到对话：`{conversation_id}`",
        "new_conversation": "已开启新对话。",
        "agent_not_found": "没有找到这个 Agent。请先运行 `/agent ls`。",
        "agent_cleared": "已清除 Custom Agent，恢复默认 RDS Copilot。",
        "agent_selected": "已选择 Custom Agent：`{name}`",
        "agent_status_default": "当前 Custom Agent：默认 RDS Copilot。",
        "agent_status_selected": "当前 Custom Agent：`{name}`\nAgentId：`{agent_id}`",
        "no_conversations": "没有找到最近对话。",
        "conversations_title": "最近对话：",
        "conversation_untitled": "未命名对话",
        "more_conversations": "还有更多对话可用。",
        "no_agents": "没有找到 Custom Agent。",
        "agents_title": "Custom Agents：",
        "agent_unnamed": "未命名 Agent",
        "tools": "工具",
        "skills_title": "Skills（第 {page} 页）：",
        "no_skills": "没有找到 Skill。",
        "total": "总数：{total}",
        "skill_hint": "输入 /$skillname 使用技能，例如：/sql-review",
    },
    "zh-TW": {
        "help_title": "RDS Copilot 短命令",
        "help_items": [
            ("/help", "查看短命令說明。"),
            ("/btw", "查看目前執行中任務已收到的回覆內容。"),
            ("/stop", "停止目前執行中的 RDS AI 任務。"),
            ("/card|on|off", "查看或管理釘釘 AI 卡片回覆。"),
            ("/session|on|off|ls|<id>", "查看或管理多輪對話上下文。"),
            ("/new", "開啟新對話。"),
            ("/agent|ls|<agent-name>|default", "查看或管理 Custom Agent。"),
            ("/language [zh-CN|zh-TW|en-US|ja-JP]", "查看或切換短命令語言。"),
            ("/tz [IANA timezone]", "查看或切換目前時區。"),
            ("/skills [page]", "查看 Skill 列表。"),
        ],
        "help_prefix_hint": "提示：短命令支援 `/` 或 `$` 前綴，例如 `/session` 和 `$session` 等價。",
        "invalid_command_argument": "短命令參數不正確：`{command}`。請參考下面的說明。",
        "no_active_task": "目前沒有正在執行的任務。",
        "no_active_task_stop": "目前沒有可停止的任務。",
        "no_displayable_content": "尚未收到可顯示的回覆內容。",
        "stopped_task": "已停止目前任務。",
        "error_failed": "RDS AI 診斷失敗，未能產生完整回覆。",
        "error_label": "錯誤",
        "error_retry": "請稍後重試，或聯絡服務維護者檢查日誌。",
        "no_message": "RDS AI 已結束，但未返回回覆內容。",
        "busy": "已有任務正在執行。輸入 `/btw` 查看目前回覆，或輸入 `/stop` 停止目前任務。",
        "still_working": "仍在處理中...（已耗時 {minutes} 分鐘 - events {events}，正在接收串流回應）",
        "language_title": "語言設定",
        "language_current": "目前語言：`{language}`",
        "language_available": "可選語言：`{values}`",
        "language_hint": "輸入 `/language <language>` 切換語言。",
        "unsupported_language": "不支援的語言：`{language}`",
        "language_switched": "語言已切換為 `{language}`。",
        "timezone_title": "時區設定",
        "timezone_current": "目前時區：`{timezone}`",
        "timezone_hint": "輸入 `/tz <IANA timezone>` 切換時區，例如 `/tz Asia/Shanghai`。",
        "unsupported_timezone": "不支援的時區：`{timezone}`",
        "timezone_suggestions": "你是不是想選：{suggestions}",
        "timezone_switched": "時區已切換為 `{timezone}`。",
        "card_unsupported": "目前平台不支援卡片回覆。",
        "card_status": "卡片回覆：`{status}`",
        "card_enabled": "卡片回覆已開啟。",
        "card_disabled": "卡片回覆已關閉。",
        "session_enabled": "多輪對話保持已開啟。",
        "session_disabled": "多輪對話保持已關閉。",
        "session_status": "會話狀態：`{status}`\nConversationId：`{conversation_id}`",
        "none": "無",
        "conversation_not_found": "沒有找到這個對話。請先執行 `/session ls`，或提供完整 ConversationId。",
        "checked_out": "已切換到對話：`{conversation_id}`",
        "new_conversation": "已開啟新對話。",
        "agent_not_found": "沒有找到這個 Agent。請先執行 `/agent ls`。",
        "agent_cleared": "已清除 Custom Agent，恢復預設 RDS Copilot。",
        "agent_selected": "已選擇 Custom Agent：`{name}`",
        "agent_status_default": "目前 Custom Agent：預設 RDS Copilot。",
        "agent_status_selected": "目前 Custom Agent：`{name}`\nAgentId：`{agent_id}`",
        "no_conversations": "沒有找到最近對話。",
        "conversations_title": "最近對話：",
        "conversation_untitled": "未命名對話",
        "more_conversations": "還有更多對話可用。",
        "no_agents": "沒有找到 Custom Agent。",
        "agents_title": "Custom Agents：",
        "agent_unnamed": "未命名 Agent",
        "tools": "工具",
        "skills_title": "Skills（第 {page} 頁）：",
        "no_skills": "沒有找到 Skill。",
        "total": "總數：{total}",
        "skill_hint": "輸入 /$skillname 使用技能，例如：/sql-review",
    },
    "en-US": {
        "help_title": "RDS Copilot commands",
        "help_items": [
            ("/help", "Show command help."),
            ("/btw", "Show the answer received by the current running task."),
            ("/stop", "Stop the current RDS AI task."),
            ("/card|on|off", "View or manage DingTalk AI card replies."),
            ("/session|on|off|ls|<id>", "View or manage conversation context."),
            ("/new", "Start a new conversation."),
            ("/agent|ls|<agent-name>|default", "View or manage Custom Agent selection."),
            ("/language [zh-CN|zh-TW|en-US|ja-JP]", "View or switch command language."),
            ("/tz [IANA timezone]", "View or switch timezone."),
            ("/skills [page]", "List Skills."),
        ],
        "help_prefix_hint": "Tip: commands support either `/` or `$` prefixes; `/session` and `$session` are equivalent.",
        "invalid_command_argument": "Invalid command argument: `{command}`. See the command help below.",
        "no_active_task": "No active task.",
        "no_active_task_stop": "No active task to stop.",
        "no_displayable_content": "No displayable content has been received yet.",
        "stopped_task": "Stopped current task.",
        "error_failed": "RDS AI diagnosis failed and could not generate a complete response.",
        "error_label": "Error",
        "error_retry": "Please try again later or contact the service maintainer to check the logs.",
        "no_message": "RDS AI finished, but no response content was returned.",
        "busy": "A task is already running. Use `/btw` to view the current response or `/stop` to stop it.",
        "still_working": "Still working... ({minutes} min elapsed — events {events}, receiving stream response)",
        "language_title": "Language",
        "language_current": "Current language: `{language}`",
        "language_available": "Available languages: `{values}`",
        "language_hint": "Use `/language <language>` to switch.",
        "unsupported_language": "Unsupported language: `{language}`",
        "language_switched": "Language switched to `{language}`.",
        "timezone_title": "Timezone",
        "timezone_current": "Current timezone: `{timezone}`",
        "timezone_hint": "Use `/tz <IANA timezone>` to switch, for example `/tz Asia/Shanghai`.",
        "unsupported_timezone": "Unsupported timezone: `{timezone}`",
        "timezone_suggestions": "Did you mean: {suggestions}",
        "timezone_switched": "Timezone switched to `{timezone}`.",
        "card_unsupported": "Card replies are not supported on this platform.",
        "card_status": "Card replies: `{status}`",
        "card_enabled": "Card replies are enabled.",
        "card_disabled": "Card replies are disabled.",
        "session_enabled": "Conversation context is enabled.",
        "session_disabled": "Conversation context is disabled.",
        "session_status": "Session: `{status}`\nConversationId: `{conversation_id}`",
        "none": "none",
        "conversation_not_found": "Conversation not found. Run `/session ls` first or provide a full ConversationId.",
        "checked_out": "Checked out conversation: `{conversation_id}`",
        "new_conversation": "Started a new conversation.",
        "agent_not_found": "Agent not found. Run `/agent ls` first.",
        "agent_cleared": "Custom agent cleared. Using default RDS Copilot.",
        "agent_selected": "Custom agent selected: `{name}`",
        "agent_status_default": "Current Custom Agent: default RDS Copilot.",
        "agent_status_selected": "Current Custom Agent: `{name}`\nAgentId: `{agent_id}`",
        "no_conversations": "No recent conversations found.",
        "conversations_title": "Recent conversations:",
        "conversation_untitled": "Untitled",
        "more_conversations": "More conversations are available.",
        "no_agents": "No custom agents found.",
        "agents_title": "Agents:",
        "agent_unnamed": "Unnamed Agent",
        "tools": "tools",
        "skills_title": "Skills (page {page}):",
        "no_skills": "No skills found.",
        "total": "Total: {total}",
        "skill_hint": "Type `/$skillname` in the conversation to use a skill, for example `/sql-review`.",
    },
    "ja-JP": {
        "help_title": "RDS Copilot コマンド",
        "help_items": [
            ("/help", "短縮コマンドのヘルプを表示します。"),
            ("/btw", "実行中タスクで受信済みの回答を表示します。"),
            ("/stop", "実行中の RDS AI タスクを停止します。"),
            ("/card|on|off", "DingTalk AI カード返信を表示または管理します。"),
            ("/session|on|off|ls|<id>", "会話コンテキストを表示または管理します。"),
            ("/new", "新しい会話を開始します。"),
            ("/agent|ls|<agent-name>|default", "Custom Agent を表示または管理します。"),
            ("/language [zh-CN|zh-TW|en-US|ja-JP]", "コマンド言語を表示または切り替えます。"),
            ("/tz [IANA timezone]", "現在のタイムゾーンを表示または切り替えます。"),
            ("/skills [page]", "Skill 一覧を表示します。"),
        ],
        "help_prefix_hint": "ヒント: コマンドは `/` または `$` プレフィックスに対応しています。`/session` と `$session` は同じです。",
        "invalid_command_argument": "コマンド引数が正しくありません: `{command}`。以下のヘルプを参照してください。",
        "no_active_task": "実行中のタスクはありません。",
        "no_active_task_stop": "停止できる実行中タスクはありません。",
        "no_displayable_content": "表示できる回答はまだ受信していません。",
        "stopped_task": "現在のタスクを停止しました。",
        "error_failed": "RDS AI の診断に失敗し、完全な回答を生成できませんでした。",
        "error_label": "エラー",
        "error_retry": "しばらくしてから再試行するか、サービス管理者にログ確認を依頼してください。",
        "no_message": "RDS AI は終了しましたが、回答内容は返されませんでした。",
        "busy": "タスクはすでに実行中です。`/btw` で現在の回答を確認するか、`/stop` で停止してください。",
        "still_working": "処理を継続中...（{minutes} 分経過 - events {events}、ストリーム応答を受信中）",
        "language_title": "言語設定",
        "language_current": "現在の言語: `{language}`",
        "language_available": "選択可能な言語: `{values}`",
        "language_hint": "`/language <language>` で言語を切り替えます。",
        "unsupported_language": "未対応の言語: `{language}`",
        "language_switched": "言語を `{language}` に切り替えました。",
        "timezone_title": "タイムゾーン設定",
        "timezone_current": "現在のタイムゾーン: `{timezone}`",
        "timezone_hint": "`/tz <IANA timezone>` でタイムゾーンを切り替えます。例: `/tz Asia/Shanghai`",
        "unsupported_timezone": "未対応のタイムゾーン: `{timezone}`",
        "timezone_suggestions": "もしかして: {suggestions}",
        "timezone_switched": "タイムゾーンを `{timezone}` に切り替えました。",
        "card_unsupported": "現在のプラットフォームはカード返信に対応していません。",
        "card_status": "カード返信: `{status}`",
        "card_enabled": "カード返信を有効にしました。",
        "card_disabled": "カード返信を無効にしました。",
        "session_enabled": "会話コンテキストを有効にしました。",
        "session_disabled": "会話コンテキストを無効にしました。",
        "session_status": "セッション: `{status}`\nConversationId: `{conversation_id}`",
        "none": "なし",
        "conversation_not_found": "会話が見つかりません。先に `/session ls` を実行するか、完全な ConversationId を指定してください。",
        "checked_out": "会話を切り替えました: `{conversation_id}`",
        "new_conversation": "新しい会話を開始しました。",
        "agent_not_found": "Agent が見つかりません。先に `/agent ls` を実行してください。",
        "agent_cleared": "Custom Agent をクリアし、デフォルトの RDS Copilot を使用します。",
        "agent_selected": "Custom Agent を選択しました: `{name}`",
        "agent_status_default": "現在の Custom Agent: デフォルトの RDS Copilot。",
        "agent_status_selected": "現在の Custom Agent: `{name}`\nAgentId: `{agent_id}`",
        "no_conversations": "最近の会話は見つかりません。",
        "conversations_title": "最近の会話:",
        "conversation_untitled": "無題の会話",
        "more_conversations": "さらに会話があります。",
        "no_agents": "Custom Agent は見つかりません。",
        "agents_title": "Agents:",
        "agent_unnamed": "無題の Agent",
        "tools": "ツール",
        "skills_title": "Skills（{page}ページ）:",
        "no_skills": "Skill は見つかりません。",
        "total": "合計: {total}",
        "skill_hint": "会話で `/$skillname` と入力するとスキルを使用できます。例: `/sql-review`",
    },
}


def _message_language(language: str = "") -> str:
    return _normalize_language(language) or DEFAULT_LANGUAGE


def _t(message_language: str, key: str, **kwargs: Any) -> Any:
    resolved_language = _message_language(message_language)
    value = I18N_MESSAGES.get(resolved_language, I18N_MESSAGES[DEFAULT_LANGUAGE]).get(key)
    if value is None:
        value = I18N_MESSAGES[DEFAULT_LANGUAGE][key]
    if isinstance(value, str) and kwargs:
        return value.format(**kwargs)
    return value


def _code_join(values: tuple[str, ...] | list[str]) -> str:
    return "`, `".join(str(value) for value in values)


def _get_available_timezones() -> set[str]:
    global _timezones_cache
    with _timezones_lock:
        if _timezones_cache is None:
            try:
                _timezones_cache = set(available_timezones())
            except Exception as e:
                logger.warning(f"Failed to load local timezone database: {e}")
                _timezones_cache = set()
        return _timezones_cache


def _is_valid_timezone(timezone: str) -> bool:
    normalized = (timezone or "").strip()
    if not normalized:
        return False
    known_timezones = _get_available_timezones()
    if known_timezones and normalized not in known_timezones:
        return False
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _timezone_match_key(timezone: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (timezone or "").lower())


def suggest_timezones(timezone: str, limit: int = 5) -> list[str]:
    query = (timezone or "").strip()
    if not query:
        return []
    known_timezones = sorted(_get_available_timezones())
    if not known_timezones:
        return []

    suggestions: list[str] = []
    query_lower = query.lower()
    query_key = _timezone_match_key(query)

    def add(value: str) -> None:
        if value and value not in suggestions and len(suggestions) < limit:
            suggestions.append(value)

    for candidate in known_timezones:
        if candidate.lower() == query_lower:
            add(candidate)

    for candidate in known_timezones:
        candidate_lower = candidate.lower()
        candidate_key = _timezone_match_key(candidate)
        if query_lower in candidate_lower or (query_key and query_key in candidate_key):
            add(candidate)

    lower_to_original = {candidate.lower(): candidate for candidate in known_timezones}
    close_matches = difflib.get_close_matches(query_lower, list(lower_to_original.keys()), n=limit * 2, cutoff=0.55)
    for match in close_matches:
        add(lower_to_original[match])

    return suggestions


def format_unsupported_timezone(timezone: str, language: str = DEFAULT_LANGUAGE) -> str:
    lines = [
        f"### {_t(language, 'timezone_title')}",
        "",
        f"- {_t(language, 'unsupported_timezone', timezone=timezone)}",
        f"- {_t(language, 'timezone_hint')}",
    ]
    suggestions = suggest_timezones(timezone)
    if suggestions:
        lines.append(f"- {_t(language, 'timezone_suggestions', suggestions=', '.join(f'`{item}`' for item in suggestions))}")
    return "\n".join(lines)


def parse_card_command(text: str) -> str:
    normalized_text = _normalize_command_prefix(text)
    if normalized_text == "/card":
        return "status"
    if normalized_text == "/card on":
        return "on"
    if normalized_text == "/card off":
        return "off"
    return ""


def is_new_conversation_command(text: str) -> bool:
    return _normalize_command_prefix(text).lower() == NEW_CONVERSATION_COMMAND


def build_error_content(error: Exception, language: str = DEFAULT_LANGUAGE) -> str:
    error_type = error.__class__.__name__
    error_message = str(error).strip()
    error_detail = f"{error_type}: {error_message}" if error_message else error_type
    if len(error_detail) > 500:
        error_detail = f"{error_detail[:500]}..."
    return (
        f"{_t(language, 'error_failed')}\n\n"
        f"{_t(language, 'error_label')}: {error_detail}\n\n"
        f"{_t(language, 'error_retry')}"
    )


def build_no_message_content(language: str = DEFAULT_LANGUAGE) -> str:
    return f"{_t(language, 'no_message')}\n\n{_t(language, 'error_retry')}"


def build_busy_content(language: str = DEFAULT_LANGUAGE) -> str:
    return str(_t(language, "busy"))


def get_conversation_store_file_path() -> str:
    return os.getenv(
        CONVERSATION_STORE_FILE_ENV,
        os.path.join(os.getcwd(), DEFAULT_CONVERSATION_STORE_FILE),
    )


class CopilotConversationStore:
    """Persist Copilot state by platform conversation and sender."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = _get_store_lock(file_path)

    @staticmethod
    def _key(conversation_id: str, sender_id: str, platform: str = "") -> str:
        if not conversation_id or not sender_id:
            return ""
        key_parts = [conversation_id, sender_id]
        if platform and platform != "dingtalk":
            key_parts = [platform, conversation_id, sender_id]
        return json.dumps(key_parts, ensure_ascii=False, separators=(",", ":"))

    def _empty_data(self) -> dict:
        return {"version": 1, "conversations": {}}

    def _load_unlocked(self) -> dict:
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

    def _load(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def _save_unlocked(self, data: dict):
        if not self.file_path:
            return
        store_dir = os.path.dirname(os.path.abspath(self.file_path))
        os.makedirs(store_dir, exist_ok=True)
        tmp_file_path = f"{self.file_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_file_path, self.file_path)

    def _save(self, data: dict):
        with self._lock:
            self._save_unlocked(data)

    def _get_item(self, conversation_id: str, sender_id: str, platform: str = "") -> dict:
        key = self._key(conversation_id, sender_id, platform)
        if not key:
            return {}
        with self._lock:
            item = self._load_unlocked().get("conversations", {}).get(key, {})
        return item if isinstance(item, dict) else {}

    def _update_item(
        self,
        conversation_id: str,
        sender_id: str,
        platform: str,
        updater: Callable[[dict], None],
    ) -> None:
        key = self._key(conversation_id, sender_id, platform)
        if not key:
            return
        with self._lock:
            data = self._load_unlocked()
            conversations = data.setdefault("conversations", {})
            item = conversations.get(key, {})
            if not isinstance(item, dict):
                item = {}
            item.update(
                {
                    "platform": platform,
                    "conversation_id": conversation_id,
                    "sender_id": sender_id,
                    "updated_at": int(time.time()),
                }
            )
            updater(item)
            conversations[key] = item
            self._save_unlocked(data)

    def get(self, conversation_id: str, sender_id: str, platform: str = "") -> str:
        return self._get_item(conversation_id, sender_id, platform).get("copilot_conversation_id") or ""

    def set(
        self,
        conversation_id: str,
        sender_id: str,
        copilot_conversation_id: str,
        platform: str = "",
    ):
        self.set_conversation_id(conversation_id, sender_id, copilot_conversation_id, platform=platform)

    def set_conversation_id(
        self,
        conversation_id: str,
        sender_id: str,
        copilot_conversation_id: str,
        platform: str = "",
    ):
        if not copilot_conversation_id:
            return

        def updater(item: dict) -> None:
            item["copilot_conversation_id"] = copilot_conversation_id
            item["session_enabled"] = item.get("session_enabled", True) is True

        self._update_item(conversation_id, sender_id, platform, updater)

    def clear(self, conversation_id: str, sender_id: str, platform: str = ""):
        self.clear_conversation_id(conversation_id, sender_id, platform=platform)

    def clear_conversation_id(self, conversation_id: str, sender_id: str, platform: str = ""):
        def updater(item: dict) -> None:
            item["copilot_conversation_id"] = ""
            item["session_enabled"] = item.get("session_enabled", True) is True

        self._update_item(conversation_id, sender_id, platform, updater)

    def is_session_enabled(self, conversation_id: str, sender_id: str, platform: str = "") -> bool:
        item = self._get_item(conversation_id, sender_id, platform)
        if "session_enabled" not in item:
            return True
        return item.get("session_enabled") is True

    def set_session_enabled(
        self,
        conversation_id: str,
        sender_id: str,
        enabled: bool,
        platform: str = "",
    ):
        def updater(item: dict) -> None:
            item["session_enabled"] = bool(enabled)
            if not enabled:
                item["copilot_conversation_id"] = ""

        self._update_item(conversation_id, sender_id, platform, updater)

    def is_card_enabled(self, conversation_id: str, sender_id: str, platform: str = "") -> bool:
        item = self._get_item(conversation_id, sender_id, platform)
        return item.get("card_enabled") is True

    def set_card_enabled(
        self,
        conversation_id: str,
        sender_id: str,
        enabled: bool,
        platform: str = "",
    ):
        def updater(item: dict) -> None:
            item["card_enabled"] = bool(enabled)

        self._update_item(conversation_id, sender_id, platform, updater)

    def get_agent(self, conversation_id: str, sender_id: str, platform: str = "") -> dict:
        item = self._get_item(conversation_id, sender_id, platform)
        agent_id = item.get("custom_agent_id") or ""
        agent_name = item.get("custom_agent_name") or ""
        if not agent_id:
            return {}
        return {"id": agent_id, "name": agent_name}

    def set_agent(
        self,
        conversation_id: str,
        sender_id: str,
        agent_id: str,
        agent_name: str,
        platform: str = "",
    ):
        if not agent_id:
            return

        def updater(item: dict) -> None:
            item["custom_agent_id"] = agent_id
            item["custom_agent_name"] = agent_name or ""

        self._update_item(conversation_id, sender_id, platform, updater)

    def clear_agent(self, conversation_id: str, sender_id: str, platform: str = ""):
        def updater(item: dict) -> None:
            item["custom_agent_id"] = ""
            item["custom_agent_name"] = ""

        self._update_item(conversation_id, sender_id, platform, updater)

    def get_language(self, conversation_id: str, sender_id: str, platform: str = "") -> str:
        language = self._get_item(conversation_id, sender_id, platform).get("language") or DEFAULT_LANGUAGE
        return _normalize_language(language) or DEFAULT_LANGUAGE

    def set_language(self, conversation_id: str, sender_id: str, language: str, platform: str = ""):
        normalized = _normalize_language(language)
        if not normalized:
            return

        def updater(item: dict) -> None:
            item["language"] = normalized

        self._update_item(conversation_id, sender_id, platform, updater)

    def get_timezone(self, conversation_id: str, sender_id: str, platform: str = "") -> str:
        timezone = self._get_item(conversation_id, sender_id, platform).get("timezone") or DEFAULT_TIMEZONE
        return timezone if _is_valid_timezone(timezone) else DEFAULT_TIMEZONE

    def set_timezone(self, conversation_id: str, sender_id: str, timezone: str, platform: str = ""):
        normalized = (timezone or "").strip()
        if not _is_valid_timezone(normalized):
            return

        def updater(item: dict) -> None:
            item["timezone"] = normalized

        self._update_item(conversation_id, sender_id, platform, updater)


def get_copilot_conversation_store() -> CopilotConversationStore:
    return CopilotConversationStore(get_conversation_store_file_path())


class RuntimeCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._conversations: dict[tuple[str, str, str], list[dict]] = {}
        self._agents: dict[tuple[str, str, str], list[dict]] = {}

    @staticmethod
    def _context_key(platform: str, chat_id: str, sender_id: str) -> tuple[str, str, str]:
        return platform or "", chat_id or "", sender_id or ""

    def set_conversations(self, platform: str, chat_id: str, sender_id: str, conversations: list[dict]) -> None:
        with self._lock:
            self._conversations[self._context_key(platform, chat_id, sender_id)] = list(conversations or [])

    def resolve_conversation(self, platform: str, chat_id: str, sender_id: str, token: str) -> str:
        normalized = (token or "").strip()
        if not _is_conversation_id_token(normalized):
            return ""
        if CONVERSATION_UUID_RE.fullmatch(normalized) or CONVERSATION_OPAQUE_ID_RE.fullmatch(normalized):
            return normalized
        with self._lock:
            conversations = self._conversations.get(self._context_key(platform, chat_id, sender_id), [])
            matches = [item.get("Id") or item.get("id") or "" for item in conversations]
        normalized_lower = normalized.lower()
        matches = [item_id for item_id in matches if item_id.lower().startswith(normalized_lower)]
        if len(matches) == 1:
            return matches[0]
        return ""

    def set_agents(self, platform: str, chat_id: str, sender_id: str, agents: list[dict]) -> None:
        with self._lock:
            self._agents[self._context_key(platform, chat_id, sender_id)] = list(agents or [])

    def resolve_agent_by_name(self, platform: str, chat_id: str, sender_id: str, name: str) -> dict:
        normalized = (name or "").strip()
        if not normalized:
            return {}
        with self._lock:
            agents = self._agents.get(self._context_key(platform, chat_id, sender_id), [])
            for item in agents:
                if str(item.get("Name") or "") == normalized:
                    return dict(item)
        return {}


_runtime_cache = RuntimeCache()


def get_runtime_cache() -> RuntimeCache:
    return _runtime_cache


class ActiveConversationState:
    def __init__(self, platform: str, chat_id: str, sender_id: str):
        self.platform = platform or ""
        self.chat_id = chat_id or ""
        self.sender_id = sender_id or ""
        self.started_at = time.time()
        self._lock = threading.RLock()
        self.task_id = ""
        self.message_buffer = ""
        self.event_count = 0
        self.phase = "receiving stream response"
        self.cancel_requested = False
        self.done = False
        self._stop_sent = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.platform, self.chat_id, self.sender_id

    def record_task_id(self, task_id: str) -> None:
        if not task_id:
            return
        with self._lock:
            self.task_id = task_id

    def record_event(self) -> None:
        with self._lock:
            self.event_count += 1

    def record_message(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.message_buffer += text
            self.phase = "receiving stream response"

    def record_tool(self, tool_name: str) -> None:
        with self._lock:
            self.phase = f"running: {tool_name}" if tool_name else "receiving stream response"

    def request_cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True

    def should_send_stop(self) -> tuple[bool, str]:
        with self._lock:
            if self.cancel_requested and self.task_id and not self._stop_sent:
                self._stop_sent = True
                return True, self.task_id
        return False, ""

    def mark_done(self) -> None:
        with self._lock:
            self.done = True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "platform": self.platform,
                "chat_id": self.chat_id,
                "sender_id": self.sender_id,
                "task_id": self.task_id,
                "message_buffer": self.message_buffer,
                "event_count": self.event_count,
                "phase": self.phase,
                "started_at": self.started_at,
                "cancel_requested": self.cancel_requested,
                "done": self.done,
            }


class ActiveConversationRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str, str], ActiveConversationState] = {}

    @staticmethod
    def _key_from_context(context: "BotContext") -> tuple[str, str, str]:
        return context.platform or "", context.chat_id or "", context.sender_id or ""

    def start(self, context: "BotContext") -> ActiveConversationState:
        key = self._key_from_context(context)
        with self._lock:
            state = ActiveConversationState(*key)
            self._states[key] = state
            return state

    def try_start(self, context: "BotContext") -> Optional[ActiveConversationState]:
        key = self._key_from_context(context)
        with self._lock:
            current = self._states.get(key)
            if current and not current.snapshot()["done"]:
                return None
            state = ActiveConversationState(*key)
            self._states[key] = state
            return state

    def get(self, context: "BotContext") -> Optional[ActiveConversationState]:
        key = self._key_from_context(context)
        with self._lock:
            state = self._states.get(key)
            if state and not state.snapshot()["done"]:
                return state
        return None

    def is_active(self, context: "BotContext") -> bool:
        return self.get(context) is not None

    def finish(self, state: ActiveConversationState) -> None:
        state.mark_done()
        with self._lock:
            if self._states.get(state.key) is state:
                self._states.pop(state.key, None)


_active_registry = ActiveConversationRegistry()


def get_active_registry() -> ActiveConversationRegistry:
    return _active_registry


@dataclass
class BotContext:
    platform: str
    chat_id: str
    sender_id: str
    store: CopilotConversationStore
    cache: RuntimeCache | None = None
    registry: ActiveConversationRegistry | None = None

    def __post_init__(self):
        if self.cache is None:
            self.cache = get_runtime_cache()
        if self.registry is None:
            self.registry = get_active_registry()


@dataclass
class ControlCommandResult:
    handled: bool
    content: str = ""
    contents: list[str] | None = None

    def response_contents(self) -> list[str]:
        if self.contents is not None:
            return [content for content in self.contents if content]
        return [self.content] if self.content else []


def _short_id(value: str) -> str:
    return str(value or "")[:8]


def _truncate(value: str, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _format_created_at(value: Any) -> str:
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _conversation_time_raw(item: dict) -> Any:
    for key in ("UpdatedAt", "updated_at", "UpdateTime", "update_time", "UpdatedTime", "updated_time", "CreatedAt", "created_at"):
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _conversation_sort_value(item: dict) -> int:
    value = _conversation_time_raw(item)
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return timestamp
    except (TypeError, ValueError, OSError):
        return 0


def sort_conversations_by_time_desc(conversations: list[dict]) -> list[dict]:
    return sorted(conversations, key=_conversation_sort_value, reverse=True)


def format_conversations(conversations: list[dict], has_more: bool = False, language: str = DEFAULT_LANGUAGE) -> str:
    if not conversations:
        return f"### {_t(language, 'conversations_title')}\n\n{_t(language, 'no_conversations')}"
    lines = [f"### {_t(language, 'conversations_title')}"]
    for item in sort_conversations_by_time_desc(conversations):
        conversation_id = item.get("Id") or item.get("id") or ""
        name = item.get("Name") or item.get("name") or _t(language, "conversation_untitled")
        updated_at = _format_created_at(_conversation_time_raw(item))
        lines.append(f"- `{_short_id(conversation_id)}` `{updated_at}` {name}")
    if has_more:
        lines.append(f"\n{_t(language, 'more_conversations')}")
    return "\n".join(lines)


def format_agents(agents: list[dict], language: str = DEFAULT_LANGUAGE) -> str:
    if not agents:
        return f"### {_t(language, 'agents_title')}\n\n{_t(language, 'no_agents')}"
    lines = [f"### {_t(language, 'agents_title')}"]
    for item in agents:
        agent_id = item.get("Id") or item.get("id") or ""
        name = item.get("Name") or item.get("name") or _t(language, "agent_unnamed")
        tools = item.get("Tools") or item.get("tools") or []
        enable_tools = item.get("EnableTools")
        tool_state = "on" if enable_tools is True else "off"
        lines.append(f"- `{_short_id(agent_id)}` {name}")
        lines.append(f"  - {_t(language, 'tools')}: `{tool_state}`  count: `{len(tools)}`")
    return "\n".join(lines)


def format_agent_status(agent: dict, language: str = DEFAULT_LANGUAGE) -> str:
    if not agent:
        return str(_t(language, "agent_status_default"))
    agent_id = str(agent.get("id") or agent.get("Id") or "").strip()
    name = str(agent.get("name") or agent.get("Name") or "").strip() or str(_t(language, "agent_unnamed"))
    return str(_t(language, "agent_status_selected", name=name, agent_id=_short_id(agent_id)))


def format_skills(
    skills: list[dict],
    page_number: int = 1,
    total_count: Any = None,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    lines = [f"### {_t(language, 'skills_title', page=page_number)}"]
    if not skills:
        lines.append("")
        lines.append(str(_t(language, "no_skills")))
    for item in skills:
        name = item.get("Name") or item.get("name") or "unnamed-skill"
        skill_type = item.get("SkillType") or item.get("skill_type") or ""
        dbtypes = item.get("Dbtypes") or item.get("dbtypes") or []
        dbtype_text = ",".join(str(v) for v in dbtypes) if dbtypes else "-"
        desc = _truncate(item.get("Description") or item.get("description") or "", 100)
        meta = "  ".join(item for item in (skill_type, dbtype_text) if item)
        lines.append(f"- `{name}`" + (f"  {meta}" if meta else ""))
        if desc:
            lines.append(f"  - {desc}")
    if total_count not in (None, ""):
        lines.append(str(_t(language, "total", total=total_count)))
    lines.append(str(_t(language, "skill_hint")))
    return "\n".join(lines)


def format_language_options(current_language: str, language: str = DEFAULT_LANGUAGE) -> str:
    return "\n".join(
        [
            f"### {_t(language, 'language_title')}",
            "",
            f"- {_t(language, 'language_current', language=current_language or DEFAULT_LANGUAGE)}",
            f"- {_t(language, 'language_available', values=_code_join(SUPPORTED_LANGUAGE_VALUES))}",
            f"- {_t(language, 'language_hint')}",
        ]
    )


def format_timezone_status(current_timezone: str, language: str = DEFAULT_LANGUAGE) -> str:
    return "\n".join(
        [
            f"### {_t(language, 'timezone_title')}",
            "",
            f"- {_t(language, 'timezone_current', timezone=current_timezone or DEFAULT_TIMEZONE)}",
            f"- {_t(language, 'timezone_hint')}",
        ]
    )


def format_help(language: str = DEFAULT_LANGUAGE) -> str:
    lines = [f"### {_t(language, 'help_title')}", "", str(_t(language, "help_prefix_hint")), ""]
    for command, description in _t(language, "help_items"):
        lines.append(f"- `{command}` - {description}")
    return "\n".join(lines)


def format_invalid_command(command: str, language: str = DEFAULT_LANGUAGE) -> str:
    return "\n\n".join(
        [
            str(_t(language, "invalid_command_argument", command=command)),
            format_help(language),
        ]
    )


def format_still_working_message(
    elapsed_seconds: float,
    event_count: int,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    elapsed_minutes = int(elapsed_seconds // 60)
    return str(_t(language, "still_working", minutes=elapsed_minutes, events=event_count))


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def run_still_working_notifier(
    state: ActiveConversationState,
    send_callback: Callable[[str], Any],
    interval_seconds: float | None = None,
    language: str = DEFAULT_LANGUAGE,
):
    if interval_seconds is None:
        try:
            interval_seconds = float(os.getenv(STILL_WORKING_INTERVAL_ENV, "180"))
        except ValueError:
            interval_seconds = 180
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        snapshot = state.snapshot()
        if snapshot["done"] or snapshot["cancel_requested"]:
            return
        elapsed = time.time() - snapshot["started_at"]
        await maybe_await(send_callback(format_still_working_message(elapsed, snapshot["event_count"], language)))


def _get_copilot(rds_copilot: Any) -> Any:
    if rds_copilot is None:
        return RdsCopilot()
    if isinstance(rds_copilot, type):
        return rds_copilot()
    if callable(rds_copilot) and not any(
        hasattr(rds_copilot, attr)
        for attr in ("chat", "stop_task", "list_conversations", "list_custom_agents", "list_skills")
    ):
        return rds_copilot()
    return rds_copilot


def _get_session_checkout_token(text: str) -> str:
    parts = _normalize_command_prefix(text).split(maxsplit=1)
    if len(parts) == 2 and parts[0] == "/session" and _is_conversation_id_token(parts[1].strip()):
        return parts[1].strip()
    return ""


def _get_agent_name(text: str) -> str:
    parts = _normalize_command_prefix(text).split(maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    return ""


def _parse_skills_args(text: str) -> tuple[int, bool]:
    parts = _normalize_command_prefix(text).split(maxsplit=1)
    if len(parts) == 1:
        return 1, True
    arg = parts[1].strip()
    if arg.isdigit():
        return max(1, int(arg)), True
    return 1, False


def _is_single_argument_command(text: str, command: str) -> bool:
    parts = _normalize_command_prefix(text).split(maxsplit=2)
    return len(parts) == 2 and parts[0] == command and bool(parts[1].strip())


async def handle_control_command(
    text: str,
    context: BotContext,
    rds_copilot: Any = None,
    *,
    card_supported: bool = True,
) -> ControlCommandResult:
    command_text = (text or "").strip()
    normalized = _normalize_command_prefix(command_text)
    store = context.store
    platform = context.platform
    chat_id = context.chat_id
    sender_id = context.sender_id
    language = store.get_language(chat_id, sender_id, platform=platform)

    if normalized == "/help":
        return ControlCommandResult(True, format_help(language))

    if normalized == "/btw":
        state = context.registry.get(context)
        if not state:
            return ControlCommandResult(True, str(_t(language, "no_active_task")))
        snapshot = state.snapshot()
        content = snapshot["message_buffer"].strip()
        if not content:
            content = str(_t(language, "no_displayable_content"))
        return ControlCommandResult(True, content)

    if normalized == "/stop":
        state = context.registry.get(context)
        if not state:
            return ControlCommandResult(True, str(_t(language, "no_active_task_stop")))
        partial_content = state.snapshot()["message_buffer"].strip()
        state.request_cancel()
        should_stop, task_id = state.should_send_stop()
        if should_stop:
            _get_copilot(rds_copilot).stop_task(task_id)
        stopped_content = str(_t(language, "stopped_task"))
        response_contents = [partial_content, stopped_content] if partial_content else [stopped_content]
        return ControlCommandResult(True, stopped_content, response_contents)

    if normalized == "/language":
        return ControlCommandResult(True, format_language_options(language, language))

    if normalized.startswith("/language "):
        requested_language = normalized.split(maxsplit=1)[1].strip()
        normalized_language = _normalize_language(requested_language)
        if not normalized_language:
            return ControlCommandResult(
                True,
                "\n".join(
                    [
                        f"### {_t(language, 'language_title')}",
                        "",
                        f"- {_t(language, 'unsupported_language', language=requested_language)}",
                        f"- {_t(language, 'language_available', values=_code_join(SUPPORTED_LANGUAGE_VALUES))}",
                    ]
                )
                + "\n\n"
                + format_help(language),
            )
        store.set_language(chat_id, sender_id, normalized_language, platform=platform)
        return ControlCommandResult(True, f"**{_t(normalized_language, 'language_switched', language=normalized_language)}**")

    if normalized == "/tz":
        return ControlCommandResult(True, format_timezone_status(store.get_timezone(chat_id, sender_id, platform=platform), language))

    if normalized.startswith("/tz "):
        timezone = normalized.split(maxsplit=1)[1].strip()
        if not _is_valid_timezone(timezone):
            return ControlCommandResult(True, format_unsupported_timezone(timezone, language) + "\n\n" + format_help(language))
        store.set_timezone(chat_id, sender_id, timezone, platform=platform)
        return ControlCommandResult(True, f"**{_t(language, 'timezone_switched', timezone=timezone)}**")

    card_command = parse_card_command(normalized)
    if card_command:
        if not card_supported:
            return ControlCommandResult(True, str(_t(language, "card_unsupported")))
        if card_command == "status":
            status = "on" if store.is_card_enabled(chat_id, sender_id, platform=platform) else "off"
            return ControlCommandResult(True, str(_t(language, "card_status", status=status)))
        enabled = card_command == "on"
        store.set_card_enabled(chat_id, sender_id, enabled, platform=platform)
        return ControlCommandResult(
            True,
            str(_t(language, "card_enabled" if enabled else "card_disabled")),
        )
    if _is_single_argument_command(normalized, "/card"):
        return ControlCommandResult(True, format_invalid_command(command_text, language))

    session_command = parse_session_command(normalized)
    if session_command:
        if session_command == "on":
            store.set_session_enabled(chat_id, sender_id, True, platform=platform)
            return ControlCommandResult(True, str(_t(language, "session_enabled")))
        if session_command == "off":
            store.set_session_enabled(chat_id, sender_id, False, platform=platform)
            return ControlCommandResult(True, str(_t(language, "session_disabled")))
        if session_command == "status":
            enabled = store.is_session_enabled(chat_id, sender_id, platform=platform)
            conversation_id = store.get(chat_id, sender_id, platform=platform) if enabled else ""
            return ControlCommandResult(
                True,
                str(
                    _t(
                        language,
                        "session_status",
                        status="on" if enabled else "off",
                        conversation_id=conversation_id or _t(language, "none"),
                    )
                ),
            )
        if session_command == "ls":
            data = _get_copilot(rds_copilot).list_conversations(limit=10, sort_by="UpdatedAt")
            conversations = sort_conversations_by_time_desc(data.get("Data") or [])
            context.cache.set_conversations(platform, chat_id, sender_id, conversations)
            return ControlCommandResult(True, format_conversations(conversations, bool(data.get("HasMore")), language))
        if session_command == "checkout":
            token = _get_session_checkout_token(normalized)
            conversation_id = context.cache.resolve_conversation(platform, chat_id, sender_id, token)
            if not conversation_id:
                return ControlCommandResult(True, str(_t(language, "conversation_not_found")))
            store.set_conversation_id(chat_id, sender_id, conversation_id, platform=platform)
            store.set_session_enabled(chat_id, sender_id, True, platform=platform)
            return ControlCommandResult(True, str(_t(language, "checked_out", conversation_id=conversation_id)))
    if _is_single_argument_command(normalized, "/session"):
        return ControlCommandResult(True, format_invalid_command(command_text, language))

    if is_new_conversation_command(normalized):
        store.clear_conversation_id(chat_id, sender_id, platform=platform)
        return ControlCommandResult(True, str(_t(language, "new_conversation")))

    if normalized == "/agent ls":
        data = _get_copilot(rds_copilot).list_custom_agents(page_number=1, page_size=20)
        agents = data.get("Data") or []
        context.cache.set_agents(platform, chat_id, sender_id, agents)
        return ControlCommandResult(True, format_agents(agents, language))

    if normalized == "/agent":
        return ControlCommandResult(True, format_agent_status(store.get_agent(chat_id, sender_id, platform=platform), language))

    if normalized == "/agent default":
        store.clear_agent(chat_id, sender_id, platform=platform)
        return ControlCommandResult(True, str(_t(language, "agent_cleared")))

    if normalized.startswith("/agent "):
        agent_name = _get_agent_name(normalized)
        agent = context.cache.resolve_agent_by_name(platform, chat_id, sender_id, agent_name)
        if not agent:
            return ControlCommandResult(True, str(_t(language, "agent_not_found")))
        agent_id = agent.get("Id") or agent.get("id") or ""
        resolved_name = agent.get("Name") or agent.get("name") or agent_name
        store.set_agent(chat_id, sender_id, agent_id, resolved_name, platform=platform)
        return ControlCommandResult(True, str(_t(language, "agent_selected", name=resolved_name)))

    if normalized == "/skills" or normalized.startswith("/skills "):
        page_number, valid_skills_command = _parse_skills_args(normalized)
        if not valid_skills_command:
            return ControlCommandResult(True, format_invalid_command(command_text, language))
        data = _get_copilot(rds_copilot).list_skills(
            page_number=page_number,
            page_size=20,
            language=language,
        )
        return ControlCommandResult(
            True,
            format_skills(
                data.get("Data") or [],
                page_number=data.get("PageNumber") or page_number,
                total_count=data.get("TotalCount"),
                language=language,
            ),
        )

    return ControlCommandResult(False, "")


_chat_executor = ThreadPoolExecutor(
    max_workers=max(4, int(os.getenv("RDS_COPILOT_CHAT_WORKERS", "8"))),
    thread_name_prefix="rds_chat",
)


async def call_with_stream(
    request_content: str,
    update_callback: Callable[[dict], Any],
    rds_copilot: RdsCopilot,
    conversion_id: str = "",
    *,
    custom_agent_id: str = "",
    language: str = DEFAULT_LANGUAGE,
    timezone: str = DEFAULT_TIMEZONE,
    active_state: ActiveConversationState | None = None,
):
    full_content = ""
    preparations = []
    seen_tool_call_ids = set()
    event_queue = Queue()
    final_conversion_id = conversion_id

    def maybe_stop_if_requested() -> bool:
        if active_state is None:
            return False
        should_stop, task_id = active_state.should_send_stop()
        if should_stop:
            try:
                rds_copilot.stop_task(task_id)
            except Exception:
                logger.warning("Failed to stop RDS Copilot task %s", task_id, exc_info=True)
            return True
        return active_state.snapshot()["cancel_requested"]

    def accepts_kwarg(func: Callable, name: str) -> bool:
        try:
            parameters = inspect.signature(func).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == name
            for parameter in parameters
        )

    def chat_kwargs(func: Callable, include_progress_events: bool = False) -> dict:
        kwargs = {
            "custom_agent_id": custom_agent_id,
            "language": language,
            "timezone": timezone,
        }
        if include_progress_events and accepts_kwarg(func, "include_progress_events"):
            kwargs["include_progress_events"] = True
        return kwargs

    progress_events_seen = False

    async def handle_stream_event(event) -> None:
        nonlocal final_conversion_id, full_content, progress_events_seen
        rendered_event = isinstance(event, (MessageEvent, ToolCallStart, ToolCallPending, ToolCallEnd, DocumentEvent))
        progress_event = isinstance(event, StreamProgressEvent) or (
            not rendered_event and hasattr(event, "task_id") and hasattr(event, "conversion_id")
        )

        if active_state is not None:
            if progress_event:
                progress_events_seen = True
                active_state.record_event()
            elif not progress_events_seen:
                active_state.record_event()
            active_state.record_task_id(getattr(event, "task_id", ""))

        if getattr(event, "conversion_id", ""):
            final_conversion_id = event.conversion_id

        if progress_event:
            return

        if isinstance(event, MessageEvent) and event.text:
            full_content += event.text
            if active_state is not None:
                active_state.record_message(event.text)
            await maybe_await(update_callback({"content": full_content}))
        elif isinstance(event, (ToolCallStart, ToolCallPending, ToolCallEnd)):
            try:
                tool_call_data = json.loads(event.text)
                tool_call_name = tool_call_data.get("tool_call_name") or tool_call_data.get("ToolCallName", "")
                tool_call_id = tool_call_data.get("tool_call_id") or tool_call_data.get("ToolCallId", "")
                if active_state is not None:
                    active_state.record_tool(tool_call_name)
                if tool_call_id and tool_call_id in seen_tool_call_ids:
                    return
                if tool_call_id:
                    seen_tool_call_ids.add(tool_call_id)
                if tool_call_name and tool_call_name not in {p["name"] for p in preparations}:
                    preparations.append({"name": tool_call_name})
                    await maybe_await(update_callback({"preparations": preparations}))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse tool_call event: {e}")
        elif isinstance(event, DocumentEvent):
            logger.info(f"Received DocumentEvent: {event.title}")
        else:
            logger.info(f"Unhandled Copilot event type: {type(event).__name__}")

    chat_async = getattr(rds_copilot, "chat_async", None)
    if callable(chat_async):
        try:
            async for event in chat_async(
                request_content,
                conversion_id,
                **chat_kwargs(chat_async, include_progress_events=True),
            ):
                await handle_stream_event(event)
                if maybe_stop_if_requested():
                    break
        except Exception as e:
            logger.error(f"Copilot chat failed: {e}")
            raise
        finally:
            if active_state is not None:
                active_state.mark_done()

        return {
            "content": full_content,
            "preparations": preparations,
            "conversion_id": final_conversion_id,
            "cancelled": active_state.snapshot()["cancel_requested"] if active_state is not None else False,
        }

    def run_chat_in_thread():
        nonlocal final_conversion_id
        try:
            chat_func = rds_copilot.chat
            chat_gen = rds_copilot.chat(
                request_content,
                conversion_id,
                **chat_kwargs(chat_func, include_progress_events=True),
            )
            for event in chat_gen:
                task_id = getattr(event, "task_id", "")
                if active_state is not None:
                    active_state.record_event()
                    active_state.record_task_id(task_id)
                if getattr(event, "conversion_id", ""):
                    final_conversion_id = event.conversion_id
                event_queue.put(event)
                if maybe_stop_if_requested():
                    break
        except Exception as e:
            logger.error(f"Copilot chat failed: {e}")
            event_queue.put(e)
        finally:
            event_queue.put(None)

    loop = asyncio.get_running_loop()
    chat_task = loop.run_in_executor(_chat_executor, run_chat_in_thread)

    try:
        while True:
            event = await asyncio.to_thread(event_queue.get)
            if event is None:
                break
            if isinstance(event, Exception):
                raise event

            if active_state is not None:
                active_state.record_task_id(getattr(event, "task_id", ""))

            if isinstance(event, MessageEvent) and event.text:
                full_content += event.text
                if active_state is not None:
                    active_state.record_message(event.text)
                await maybe_await(update_callback({"content": full_content}))
            elif isinstance(event, (ToolCallStart, ToolCallPending, ToolCallEnd)):
                try:
                    tool_call_data = json.loads(event.text)
                    tool_call_name = tool_call_data.get("tool_call_name") or tool_call_data.get("ToolCallName", "")
                    tool_call_id = tool_call_data.get("tool_call_id") or tool_call_data.get("ToolCallId", "")
                    if active_state is not None:
                        active_state.record_tool(tool_call_name)
                    if tool_call_id and tool_call_id in seen_tool_call_ids:
                        continue
                    if tool_call_id:
                        seen_tool_call_ids.add(tool_call_id)
                    if tool_call_name and tool_call_name not in {p["name"] for p in preparations}:
                        preparations.append({"name": tool_call_name})
                        await maybe_await(update_callback({"preparations": preparations}))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse tool_call event: {e}")
            elif isinstance(event, DocumentEvent):
                logger.info(f"Received DocumentEvent: {event.title}")
            else:
                logger.info(f"Unhandled Copilot event type: {type(event).__name__}")

        await chat_task
    finally:
        if active_state is not None:
            active_state.mark_done()

    return {
        "content": full_content,
        "preparations": preparations,
        "conversion_id": final_conversion_id,
        "cancelled": active_state.snapshot()["cancel_requested"] if active_state is not None else False,
    }
