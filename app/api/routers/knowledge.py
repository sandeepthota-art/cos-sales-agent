from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db, require_auth
from app.ui.data import list_knowledge

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"], dependencies=[Depends(require_auth)])


@router.get("")
def list_knowledge_route(
    thread_id: str | None = None, limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_knowledge(db, thread_id, limit=limit, offset=offset)
