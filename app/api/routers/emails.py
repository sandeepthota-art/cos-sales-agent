"""Emails -- FROZEN schema (docs/DATA_DICTIONARY.md's emails section, the
earlier collection-by-collection cleanup pass). This router only reads;
it never writes to the emails collection, and never touches
EMAIL_COLUMN_ORDER's approved field set."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth
from app.database.repositories import EmailRepository
from app.ui.data import list_emails

router = APIRouter(prefix="/api/v1/emails", tags=["emails"], dependencies=[Depends(require_auth)])


@router.get("")
def list_emails_route(
    limit: int = 50, offset: int = 0, db=Depends(get_db)
) -> list[dict[str, Any]]:
    """Newest-first, reusing app.ui.data.list_emails exactly (same sort,
    same approved field set -- EMAIL_COLUMN_ORDER is the UI-layer's own
    display-order concern, not re-derived here)."""
    return list_emails(db, limit=limit, offset=offset)


@router.get("/{message_id}")
def get_email_route(message_id: str, db=Depends(get_db)) -> dict[str, Any]:
    email = EmailRepository(db).find_one({"message_id": message_id})
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no email found for message_id={message_id!r}")
    return email
