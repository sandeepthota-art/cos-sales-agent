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

Phase 14 (static React build serving): `frontend/dist` (the Vite production
build -- `cd frontend && npm run build`) is served from this same FastAPI
app/origin when present, so Render only needs one Web Service for both the
API and the dashboard. If `frontend/dist` doesn't exist (e.g. a backend-only
checkout, or before the frontend is ever built), the static/catch-all routes
below are simply never registered -- every existing API route and test is
unaffected either way.
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
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


FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_react_app(full_path: str) -> FileResponse:
        """Serves the built React SPA. Any real built file (JS/CSS bundle,
        favicon, etc.) is returned by its exact path; everything else falls
        back to index.html so React Router's client-side routes resolve on a
        hard refresh or a direct link. An unmatched /api/* path is a genuine
        404, not the SPA shell -- this route only ever runs for a path that
        didn't match one of the real API routes registered above."""
        if full_path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
