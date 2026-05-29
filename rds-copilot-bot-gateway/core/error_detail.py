import contextlib
import json
import re
from typing import Any


SENSITIVE_KEY_PARTS = ("secret", "token", "authorization", "password", "access_key", "accesskey")


def compact_text(value: Any, *, max_length: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
                redacted[key_text] = "***"
            else:
                redacted[key_text] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def payload_detail(payload: Any, *, max_length: int = 500) -> str:
    with contextlib.suppress(Exception):
        return compact_text(
            json.dumps(_redact_sensitive(payload), ensure_ascii=False, sort_keys=True),
            max_length=max_length,
        )
    return compact_text(payload, max_length=max_length)


def http_error_detail(error: BaseException, *, max_length: int = 500) -> str:
    response = getattr(error, "response", None)
    status_code = (
        getattr(response, "status_code", None)
        or getattr(error, "code", None)
        or getattr(error, "status", None)
    )
    reason = (
        getattr(response, "reason_phrase", None)
        or getattr(error, "reason", None)
        or getattr(error, "msg", None)
    )
    body = ""
    if response is not None:
        with contextlib.suppress(Exception):
            body = str(getattr(response, "text", "") or "")
    if not body and hasattr(error, "read"):
        with contextlib.suppress(Exception):
            raw_body = error.read()
            body = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else str(raw_body or "")

    parts = []
    if status_code:
        parts.append(f"HTTP {status_code}")
    if reason:
        parts.append(f"原因：{compact_text(reason, max_length=max_length)}")
    if body:
        parts.append(f"响应：{compact_text(body, max_length=max_length)}")
    return "；".join(parts)


def exception_detail(error: BaseException, *, max_length: int = 500) -> str:
    http_detail = http_error_detail(error, max_length=max_length)
    basic = compact_text(f"{error.__class__.__name__}: {error}", max_length=max_length)
    if http_detail:
        return f"{basic}；{http_detail}"
    return basic
