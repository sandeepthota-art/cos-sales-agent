from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth
from app.entities.context import get_person_context
from app.ui.data import list_people

router = APIRouter(prefix="/api/v1/people", tags=["people"], dependencies=[Depends(require_auth)])


@router.get("")
def list_people_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_people(db, limit=limit, offset=offset)


@router.get("/{person_id}")
def get_person_route(person_id: str, db=Depends(get_db)) -> dict[str, Any]:
    """360 view -- reuses app.entities.context.get_person_context exactly
    (org, recent activity, open threads, related commitments/meetings)."""
    context = get_person_context(db, person_id)
    if context is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no person found for id={person_id!r}")
    return context
