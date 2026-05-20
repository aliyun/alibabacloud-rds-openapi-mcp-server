import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alibabacloud_rds_openapi_mcp_server import utils


def test_get_aksk_ignores_request_header_credentials_by_default(monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "env-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "env-sk")
    monkeypatch.delenv("ALIBABA_CLOUD_SECURITY_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_HEADER_CREDENTIALS", raising=False)
    token = utils.current_request_headers.set(
        {"ak": "header-ak", "sk": "header-sk", "sts": "header-sts"}
    )

    try:
        assert utils.get_aksk() == ("env-ak", "env-sk", None)
    finally:
        utils.current_request_headers.reset(token)


def test_get_aksk_allows_request_header_credentials_when_enabled(monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "env-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "env-sk")
    monkeypatch.setenv("ALLOW_HEADER_CREDENTIALS", "true")
    token = utils.current_request_headers.set(
        {"ak": "header-ak", "sk": "header-sk", "sts": "header-sts"}
    )

    try:
        assert utils.get_aksk() == ("header-ak", "header-sk", "header-sts")
    finally:
        utils.current_request_headers.reset(token)


def test_get_rds_account_ignores_request_header_credentials_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_HEADER_CREDENTIALS", raising=False)
    token = utils.current_request_headers.set(
        {"rds_user": "header-user", "rds_passwd": "header-password"}
    )

    try:
        assert utils.get_rds_account() == (None, None)
    finally:
        utils.current_request_headers.reset(token)


def test_get_rds_account_allows_request_header_credentials_when_enabled(monkeypatch):
    monkeypatch.setenv("ALLOW_HEADER_CREDENTIALS", "true")
    token = utils.current_request_headers.set(
        {"rds_user": "header-user", "rds_passwd": "header-password"}
    )

    try:
        assert utils.get_rds_account() == ("header-user", "header-password")
    finally:
        utils.current_request_headers.reset(token)
