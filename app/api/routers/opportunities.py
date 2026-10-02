from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth, require_write_enabled
from app.api.schemas import OpportunityFieldsUpdate
from app.database.repositories import OpportunityRepository
from app.mcp.tools import update_opportunity_fields
from app.ui.data import list_opportunities

router = APIRouter(prefix="/api/v1/opportunities", tags=["opportunities"], dependencies=[Depends(require_auth)])


@router.get("")
def list_opportunities_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_opportunities(db, limit=limit, offset=offset)


@router.get("/{opportunity_id}")
def get_opportunity_route(opportunity_id: str, db=Depends(get_db)) -> dict[str, Any]:
    opportunity = OpportunityRepository(db).find_one({"id": opportunity_id})
    if opportunity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no opportunity found for id={opportunity_id!r}")
    return opportunity


@router.patch("/{opportunity_id}", dependencies=[Depends(require_write_enabled)])
def update_opportunity_route(
    opportunity_id: str, body: OpportunityFieldsUpdate, db=Depends(get_db)
) -> dict[str, Any]:
    """Reuses app.mcp.tools.update_opportunity_fields exactly, the same tool
    the MCP server exposes as `update_opportunity_fields`."""
    try:
        return update_opportunity_fields(
            db, opportunity_id, body.stage, body.owner, body.value,
            body.currency, body.expected_close_date, body.next_action,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
