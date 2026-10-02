from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, get_settings_dependency, require_auth
from app.config.settings import Settings
from app.query.meetings import get_meeting_brief
from app.ui.data import list_meetings

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"], dependencies=[Depends(require_auth)])


@router.get("")
def list_meetings_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    """Enriched with a derived `title` -- closes the Meeting product gap.
    Reuses app.ui.data.list_meetings exactly."""
    return list_meetings(db, limit=limit, offset=offset)


@router.get("/{meeting_id}/brief")
def get_meeting_brief_route(
    meeting_id: str, db=Depends(get_db), settings: Settings = Depends(get_settings_dependency)
) -> dict[str, Any]:
    """Reuses app.query.meetings.get_meeting_brief exactly (Phase 23 Meeting
    Intelligence) -- the meeting-preparation view."""
    brief = get_meeting_brief(db, meeting_id, settings.agent_email)
    if brief is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no meeting found for id={meeting_id!r}")
    return brief.model_dump(mode="json")
