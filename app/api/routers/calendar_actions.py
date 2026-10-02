"""Calendar Approval -- the exact mutation orchestration app.ui.dashboard's
_render_calendar_approval_tab already performs, replicated as HTTP routes.
`approve_calendar_action`/`reject_calendar_action` are the exact same pure
functions dashboard.py calls; `require_write_enabled` (a FastAPI dependency)
replaces dashboard.py's inline `settings.dashboard_read_only` check -- same
safety boundary, enforced server-side regardless of what the client sends.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, get_settings_dependency, require_auth, require_write_enabled
from app.calendar.actions import approve_calendar_action, reject_calendar_action
from app.calendar.models import CalendarAction
from app.config.settings import Settings
from app.database.repositories import CalendarActionRepository
from app.providers.factory import ProviderFactory
from app.ui.data import list_calendar_actions, org_label, person_label

router = APIRouter(prefix="/api/v1/calendar-actions", tags=["calendar_actions"], dependencies=[Depends(require_auth)])


def _enrich(db, doc: dict[str, Any]) -> dict[str, Any]:
    action = CalendarAction.model_validate(doc)
    return {
        **doc,
        "person_name": person_label(db, action.person_id),
        "org_name": org_label(db, action.org_id),
    }


def _get_action_or_404(db, thread_id: str, meeting_fingerprint: str) -> CalendarAction:
    doc = CalendarActionRepository(db).find_one(
        {"thread_id": thread_id, "meeting_fingerprint": meeting_fingerprint}
    )
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no calendar action found for that thread/fingerprint")
    return CalendarAction.model_validate(doc)


@router.get("")
def list_calendar_actions_route(db=Depends(get_db)) -> list[dict[str, Any]]:
    """Pending + needs-clarification -- same two statuses the Streamlit
    Calendar Approval tab shows together."""
    docs = list_calendar_actions(db, "awaiting_approval") + list_calendar_actions(db, "needs_clarification")
    return [_enrich(db, doc) for doc in docs]


@router.post("/{thread_id}/{meeting_fingerprint}/approve", dependencies=[Depends(require_write_enabled)])
def approve_calendar_action_route(
    thread_id: str,
    meeting_fingerprint: str,
    db=Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict[str, Any]:
    action = _get_action_or_404(db, thread_id, meeting_fingerprint)
    calendar_provider = ProviderFactory.create_calendar_provider(settings)
    result = approve_calendar_action(action, calendar_provider)
    repo = CalendarActionRepository(db)
    repo.upsert_by_key(
        {"thread_id": result.thread_id, "meeting_fingerprint": result.meeting_fingerprint},
        result.model_dump(mode="json"),
    )
    return repo.find_one({"thread_id": thread_id, "meeting_fingerprint": meeting_fingerprint})


@router.post("/{thread_id}/{meeting_fingerprint}/ignore", dependencies=[Depends(require_write_enabled)])
def ignore_calendar_action_route(thread_id: str, meeting_fingerprint: str, db=Depends(get_db)) -> dict[str, Any]:
    action = _get_action_or_404(db, thread_id, meeting_fingerprint)
    result = reject_calendar_action(action)
    repo = CalendarActionRepository(db)
    repo.upsert_by_key(
        {"thread_id": result.thread_id, "meeting_fingerprint": result.meeting_fingerprint},
        result.model_dump(mode="json"),
    )
    return repo.find_one({"thread_id": thread_id, "meeting_fingerprint": meeting_fingerprint})
