from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth
from app.mcp.tools import get_thread
from app.ui.data import list_threads, thread_context_versions

router = APIRouter(prefix="/api/v1/threads", tags=["threads"], dependencies=[Depends(require_auth)])


@router.get("")
def list_threads_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    """Backs both the Thread Explorer tab and the Email conversation view's
    thread picker -- same underlying app.ui.data.list_threads."""
    return list_threads(db, limit=limit, offset=offset)


@router.get("/{thread_id}")
def get_thread_route(thread_id: str, db=Depends(get_db)) -> dict[str, Any]:
    """Full thread detail (all messages + latest context + event trail) --
    reuses app.mcp.tools.get_thread exactly, the same function the MCP
    `get_thread` tool already calls. Used by both the Email conversation
    view (frozen Emails schema, read-only) and the Thread Explorer."""
    result = get_thread(db, thread_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread found for thread_id={thread_id!r}")
    return result


@router.get("/{thread_id}/context-versions")
def get_thread_context_versions_route(thread_id: str, db=Depends(get_db)) -> list[dict[str, Any]]:
    """Backs the Context Evolution tab -- reuses
    app.ui.data.thread_context_versions exactly."""
    return thread_context_versions(db, thread_id)
