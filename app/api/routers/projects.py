from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth, require_write_enabled
from app.api.schemas import ProjectFieldsUpdate
from app.mcp.tools import get_project_summary, update_project_fields
from app.ui.data import list_projects

router = APIRouter(prefix="/api/v1/projects", tags=["projects"], dependencies=[Depends(require_auth)])


@router.get("")
def list_projects_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_projects(db, limit=limit, offset=offset)


@router.get("/{project_id}")
def get_project_route(project_id: str, db=Depends(get_db)) -> dict[str, Any]:
    """Reuses app.mcp.tools.get_project_summary exactly (commitments/
    follow-ups cross-collection summary)."""
    summary = get_project_summary(db, project_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no project found for id={project_id!r}")
    return summary


@router.patch("/{project_id}", dependencies=[Depends(require_write_enabled)])
def update_project_route(project_id: str, body: ProjectFieldsUpdate, db=Depends(get_db)) -> dict[str, Any]:
    """Closes the "Project product gap" -- reuses
    app.mcp.tools.update_project_fields exactly, the same tool
    `update_project_fields` the MCP server exposes. Only fields actually
    supplied (non-None in the request body) are changed."""
    try:
        return update_project_fields(
            db, project_id, body.status, body.owner, body.health, body.next_milestone, body.due
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
