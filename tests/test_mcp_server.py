# tests/test_mcp_server.py
import asyncio

from starlette.testclient import TestClient

from app.mcp.server import _BearerAuthMiddleware, _health, mcp


def test_process_email_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "process_email" in names


# --- HTTP transport auth boundary (app.mcp.server._run_http) -------------------------
# Exercises the actual Starlette app + middleware wiring _run_http assembles, without
# starting a real uvicorn server or requiring MCP_AUTH_TOKEN to be set in the
# environment -- the token is supplied directly to the middleware here instead.


def _http_app_for_test(token: str):
    app = mcp.streamable_http_app()
    app.add_route("/health", _health, methods=["GET"])
    app.add_middleware(_BearerAuthMiddleware, expected_token=token)
    return app


def test_health_endpoint_requires_no_bearer_token():
    client = TestClient(_http_app_for_test("secret-token"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_endpoint_rejects_requests_without_a_bearer_token():
    client = TestClient(_http_app_for_test("secret-token"))
    response = client.post("/mcp", json={})
    assert response.status_code == 401


def test_mcp_endpoint_rejects_an_incorrect_bearer_token():
    client = TestClient(_http_app_for_test("secret-token"))
    response = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_list_processed_emails_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_processed_emails" in names


def test_lookup_knowledge_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "lookup_knowledge" in names


def test_list_reply_drafts_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_reply_drafts" in names


def test_get_reply_draft_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "get_reply_draft" in names


# --- _wants_http_transport (Render "Application exited early" fix) --------------------
# On Render, MCP_TRANSPORT was never set, so main() fell into stdio mode with no
# client attached -- mcp.run() hit EOF on stdin and exited within milliseconds,
# surfacing as Render's generic "Application exited early" with zero diagnostics.
# $PORT is the signal every PaaS-style host injects for a web service and a stdio
# launcher never sets.


def test_wants_http_transport_when_mcp_transport_explicitly_set(monkeypatch):
    from app.mcp.server import _wants_http_transport

    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("PORT", raising=False)
    assert _wants_http_transport() is True


def test_wants_stdio_transport_when_mcp_transport_explicitly_set_to_stdio_even_with_port(monkeypatch):
    from app.mcp.server import _wants_http_transport

    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PORT", "8000")
    assert _wants_http_transport() is False


def test_wants_http_transport_inferred_from_port_when_mcp_transport_unset(monkeypatch):
    from app.mcp.server import _wants_http_transport

    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("PORT", "10000")
    assert _wants_http_transport() is True


def test_wants_stdio_transport_when_neither_mcp_transport_nor_port_is_set(monkeypatch):
    from app.mcp.server import _wants_http_transport

    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert _wants_http_transport() is False
