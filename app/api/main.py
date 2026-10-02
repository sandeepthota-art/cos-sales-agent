"""FastAPI app factory (React/API migration, docs/REACT_MIGRATION_PLAN.md).

Reads MongoDB through the exact same app.database.mongodb.get_client /
app.database.repositories path the Streamlit dashboard and MCP server already
use -- no new database-access code. Business logic is never duplicated here:
every route in app/api/routers/* is a thin wrapper around an already-existing
function in app/ui/data.py, app/query/*, app/mcp/tools.py,
app/replies/approval.py, or app/calendar/actions.py.

Local dev:
    uvicorn app.api.main:app --reload --port 8001
Production (Render, matching the existing $PORT convention the MCP server
and Streamlit dashboard already use):
    uvicorn app.api.main:app --host 0.0.0.0 --port $PORT

Static React build serving (Phase 14 of the approved plan) is intentionally
NOT wired up yet -- no React build exists at this point in the implementation
sequence; this file covers Phase 1-3 (backend API + auth + tests) only.
"""
from fastapi import FastAPI
from starlette.responses import JSONResponse

from app.api.routers import (
    auth,
    calendar_actions,
    commitments,
    emails,
    follow_ups,
    knowledge,
    meetings,
    opportunities,
    organizations,
    overview,
    people,
    personal_items,
    projects,
    reply_drafts,
    threads,
)

app = FastAPI(title="CoS Staff EA Agent API", version="1.0.0")

# /health is deliberately unauthenticated -- a platform liveness probe has no
# way to supply a session cookie, same rationale as app.mcp.server's own
# unauthenticated /health endpoint.
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


for router in (
    auth.router,
    overview.router,
    emails.router,
    threads.router,
    people.router,
    organizations.router,
    projects.router,
    opportunities.router,
    commitments.router,
    follow_ups.router,
    meetings.router,
    personal_items.router,
    knowledge.router,
    reply_drafts.router,
    calendar_actions.router,
):
    app.include_router(router)
