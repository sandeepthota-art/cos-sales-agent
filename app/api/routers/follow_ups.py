from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db, require_auth
from app.ui.data import list_follow_ups

router = APIRouter(prefix="/api/v1/follow-ups", tags=["follow_ups"], dependencies=[Depends(require_auth)])


@router.get("")
def list_follow_ups_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    """Enriched with `what`/`person_name`/`org_name` -- closes the
    Follow-up/Commitment product gap. Reuses app.ui.data.list_follow_ups
    exactly, no re-derivation here."""
    return list_follow_ups(db, limit=limit, offset=offset)
