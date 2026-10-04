# tests/test_mcp_server.py
import asyncio

import mongomock
import pytest
from starlette.testclient import TestClient

from app.database.indexes import initialize_indexes
from app.database.repositories import MeetingRepository
from app.mcp import server as mcp_server
from app.mcp.server import _BearerAuthMiddleware, _health, mcp


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


def test_mcp_endpoint_accepts_a_correct_bearer_token():
    # The lifespan context (`with ... as client`) is required here -- without it,
    # FastMCP's own session manager raises "Task group is not initialized" before
    # the auth middleware's own pass/fail decision is ever reached. A correct token
    # must clear _BearerAuthMiddleware and reach the real MCP JSON-RPC handler --
    # proven by a 400 (an empty {} body is not a valid JSON-RPC request) rather
    # than a 401 (which would mean the token was rejected).
    with TestClient(_http_app_for_test("secret-token")) as client:
        response = client.post(
            "/mcp", json={},
            headers={"Authorization": "Bearer secret-token", "Accept": "application/json, text/event-stream"},
        )
    assert response.status_code != 401


# --- X-Auth-Token as a second, equal-secret auth header (Option 1) -------------------
# Same middleware, same MCP_AUTH_TOKEN -- a client that can't set a custom Authorization
# header can send X-Auth-Token instead. Authorization takes strict precedence whenever
# it's present at all: an invalid Authorization value is rejected outright and never
# falls back to X-Auth-Token, even when X-Auth-Token alone would have been valid.
#
# These exercise _BearerAuthMiddleware directly against a minimal dummy inner app,
# rather than the real mcp.streamable_http_app() -- that session manager can only run
# its lifespan once per process (see test_mcp_endpoint_accepts_a_correct_bearer_token
# above, the one test in this file allowed to use it), so a second or third test
# entering that same lifespan raises "StreamableHTTPSessionManager .run() can only be
# called once per instance." The middleware's own pass/fail decision -- which is all
# these tests are about -- doesn't need the real MCP stack at all.


def _isolated_auth_app(token: str):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", _ok, methods=["POST"]), Route("/health", _health, methods=["GET"])])
    app.add_middleware(_BearerAuthMiddleware, expected_token=token)
    return app


def test_auth_valid_authorization_header_is_allowed():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post("/mcp", json={}, headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_invalid_authorization_header_is_rejected():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_auth_valid_x_auth_token_is_allowed():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post("/mcp", json={}, headers={"X-Auth-Token": "secret-token"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_invalid_x_auth_token_is_rejected():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post("/mcp", json={}, headers={"X-Auth-Token": "wrong-token"})
    assert response.status_code == 401


def test_auth_no_credentials_at_all_is_rejected():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post("/mcp", json={})
    assert response.status_code == 401


def test_auth_both_headers_present_valid_authorization_is_allowed():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post(
        "/mcp", json={},
        headers={"Authorization": "Bearer secret-token", "X-Auth-Token": "irrelevant-does-not-matter"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_both_headers_present_invalid_authorization_valid_x_auth_token_is_rejected():
    # Precedence test: Authorization is present (even though wrong), so it is the only
    # header consulted -- a valid X-Auth-Token alongside it must NOT be used as a
    # fallback. This is the behavior that actually defines "prefer Authorization."
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.post(
        "/mcp", json={},
        headers={"Authorization": "Bearer wrong-token", "X-Auth-Token": "secret-token"},
    )
    assert response.status_code == 401


def test_auth_health_endpoint_remains_unauthenticated_with_x_auth_token_option_present():
    client = TestClient(_isolated_auth_app("secret-token"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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


def test_list_opportunities_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_opportunities" in names


def test_update_opportunity_fields_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "update_opportunity_fields" in names


def test_update_project_fields_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "update_project_fields" in names


def test_ask_question_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "ask_question" in names


def test_set_reply_draft_gmail_id_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "set_reply_draft_gmail_id" in names


def test_set_reply_withheld_reason_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "set_reply_withheld_reason" in names


def test_merge_person_records_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "merge_person_records" in names


def test_set_email_summary_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "set_email_summary" in names


def test_whats_on_my_table_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "whats_on_my_table" in names


def test_get_reply_draft_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "get_reply_draft" in names


# --- list_meetings category filter, genuinely exercised through the MCP tool
# dispatch layer (mcp.call_tool), not just tools.list_meetings directly -- proves
# the BRD gap-analysis FR-04 integration gap is actually closed at the MCP boundary,
# not merely at the underlying Python function. --------------------------------------


def _mtg(meeting_id: str, **overrides) -> dict:
    doc = {
        "id": meeting_id, "thread_id": meeting_id, "date": None, "attendees": [],
        "person_ids": [], "org_id": None, "project_or_pillar": None,
        "minutes_record": None, "actions_raised": [], "next_meeting_date": None,
        "agenda_target": None, "actionable": False, "agenda_written": False,
    }
    doc.update(overrides)
    return doc


def test_list_meetings_category_filter_is_reachable_through_the_mcp_tool_layer(monkeypatch):
    client = mongomock.MongoClient()
    db = client["mcp_reachability_test"]
    initialize_indexes(db)
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _mtg("MTG-SALES", project_or_pillar="Sales")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-FINANCE"}, _mtg("MTG-FINANCE", project_or_pillar="Finance")
    )
    monkeypatch.setattr(mcp_server, "_get_db", lambda: db)

    _, structured = asyncio.run(mcp_server.mcp.call_tool("list_meetings", {"category": "SALES"}))

    assert [m["id"] for m in structured["result"]] == ["MTG-SALES"]


def test_list_meetings_invalid_category_is_rejected_through_the_mcp_tool_layer(monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    client = mongomock.MongoClient()
    db = client["mcp_reachability_test_invalid"]
    initialize_indexes(db)
    monkeypatch.setattr(mcp_server, "_get_db", lambda: db)

    with pytest.raises(ToolError, match="prospect"):
        asyncio.run(mcp_server.mcp.call_tool("list_meetings", {"category": "prospect"}))


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


def test_persist_organization_research_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "persist_organization_research" in names


def test_list_unresearched_organizations_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_unresearched_organizations" in names


def test_preview_duplicate_organization_candidates_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "preview_duplicate_organization_candidates" in names


def test_merge_organization_records_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "merge_organization_records" in names


def test_list_organizations_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_organizations" in names
