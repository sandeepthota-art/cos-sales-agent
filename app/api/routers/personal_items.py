from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db, require_auth
from app.ui.data import list_personal_items

router = APIRouter(prefix="/api/v1/personal-items", tags=["personal_items"], dependencies=[Depends(require_auth)])


@router.get("")
def list_personal_items_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_personal_items(db, limit=limit, offset=offset)
