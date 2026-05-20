import sys
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alibabacloud_rds_openapi_mcp_server import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    monkeypatch.setenv("ENABLE_WRITE_TOOLS", "true")

    server.main()

    assert captured["config"]["host"] == "0.0.0.0"


def test_public_http_default_write_tools_require_explicit_enable(monkeypatch):
    _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("API_KEY", "test-token")
    monkeypatch.delenv("ENABLE_WRITE_TOOLS", raising=False)
    monkeypatch.delenv("MCP_TOOLSETS", raising=False)

    with pytest.raises(ValueError, match="ENABLE_WRITE_TOOLS"):
        server.main()


def test_public_http_read_only_toolset_does_not_require_write_enable(monkeypatch):
    captured = _patch_server_run(monkeypatch)
    monkeypatch.setenv("SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("API_KEY", "test-token")
    monkeypatch.setenv("MCP_TOOLSETS", "rds_custom_read")
    monkeypatch.delenv("ENABLE_WRITE_TOOLS", raising=False)

    server.main()

    assert captured["config"]["host"] == "0.0.0.0"


async def _call_verify_header_middleware(monkeypatch, authorization: str | None):
    monkeypatch.setenv("API_KEY", "test-token")
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": headers})
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return Response("OK")

    middleware = server.VerifyHeaderMiddleware(app=lambda _scope, _receive, _send: None)
    response = await middleware.dispatch(request, call_next)
    return response, called


@pytest.mark.anyio
async def test_api_key_requires_bearer_authorization_scheme(monkeypatch):
    response, called = await _call_verify_header_middleware(monkeypatch, "Token test-token")

    assert response.status_code == 401
    assert called is False


@pytest.mark.anyio
async def test_api_key_accepts_bearer_authorization_scheme(monkeypatch):
    response, called = await _call_verify_header_middleware(monkeypatch, "Bearer test-token")

    assert response.status_code == 200
    assert called is True
