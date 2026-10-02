from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db, require_auth
from app.ui.data import list_commitments

router = APIRouter(prefix="/api/v1/commitments", tags=["commitments"], dependencies=[Depends(require_auth)])


@router.get("")
def list_commitments_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_commitments(db, limit=limit, offset=offset)
