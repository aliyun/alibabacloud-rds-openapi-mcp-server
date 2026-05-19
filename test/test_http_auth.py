import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alibabacloud_rds_openapi_mcp_server import server


class FakeApp:
    def add_middleware(self, *_args, **_kwargs):
        pass


class FakeServer:
    def __init__(self, config):
        self.config = config

    async def serve(self):
        pass


def _patch_server_run(monkeypatch):
    captured = {}

    def fake_config(app, **kwargs):
        captured["app"] = app
        captured["config"] = kwargs
        return kwargs

    monkeypatch.setattr(server.mcp, "activate", lambda enabled_groups: None)
    monkeypatch.setattr(server.mcp, "sse_app", lambda: FakeApp())
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: FakeApp())
    monkeypatch.setattr(server.uvicorn, "Config", fake_config)
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server.anyio, "run", lambda _serve: None)
    return captured


def test_http_transport_defaults_to_loopback_without_api_key(monkeypatch):
    captured = _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "sse")
    monkeypatch.delenv("SERVER_HOST", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    server.main()

    assert captured["config"]["host"] == "127.0.0.1"


def test_sse_transport_requires_api_key_for_public_host(monkeypatch):
    _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="API_KEY is required"):
        server.main()


def test_streamable_http_transport_requires_api_key_for_public_host(monkeypatch):
    _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "streamable_http")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="API_KEY is required"):
        server.main()


def test_public_host_rejects_blank_api_key(monkeypatch):
    _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("API_KEY", "   ")

    with pytest.raises(ValueError, match="API_KEY is required"):
        server.main()


def test_public_host_with_api_key_is_allowed(monkeypatch):
    captured = _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "streamable-http")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("API_KEY", "test-token")

    server.main()

    assert captured["config"]["host"] == "0.0.0.0"
