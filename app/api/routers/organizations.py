from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth
from app.database.repositories import OrganizationRepository
from app.mcp.tools import get_company_summary
from app.ui.data import list_organizations

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"], dependencies=[Depends(require_auth)])


@router.get("")
def list_organizations_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    return list_organizations(db, limit=limit, offset=offset)


@router.get("/{org_id}")
def get_organization_route(org_id: str, db=Depends(get_db)) -> dict[str, Any]:
    """360 view -- reuses app.mcp.tools.get_company_summary directly by org_id,
    the same id this route itself takes (get_company_summary used to require
    the organization's free-text name instead; that round-trip is gone now
    that the tool joins on org_id directly)."""
    org = OrganizationRepository(db).find_one({"id": org_id})
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no organization found for id={org_id!r}")
    return {"organization": org, "summary": get_company_summary(db, org_id)}
