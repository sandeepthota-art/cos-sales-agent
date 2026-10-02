"""Reply Approval -- the exact mutation orchestration app.ui.dashboard's
_render_reply_approval_tab already performs (fetch -> validate ->
approve/reject/edit via app.replies.approval's pure functions -> persist),
replicated here as HTTP routes rather than Streamlit buttons. No business
logic is reimplemented: `approve`/`reject`/`edit`/`simulate_send` are the
exact same functions dashboard.py calls.
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_db, require_auth, require_write_enabled
from app.api.schemas import ReplyEditRequest
from app.database.repositories import EmailRepository, ReplyDraftRepository
from app.replies.approval import approve, edit, reject, simulate_send
from app.replies.models import ReplyDraft
from app.ui.data import list_reply_drafts, org_label, person_label

router = APIRouter(prefix="/api/v1/reply-drafts", tags=["reply_drafts"], dependencies=[Depends(require_auth)])


def _enrich(db, doc: dict[str, Any]) -> dict[str, Any]:
    draft = ReplyDraft.model_validate(doc)
    recipient = person_label(db, draft.person_id)
    if recipient is None:
        source_email = EmailRepository(db).find_one({"message_id": draft.source_email_id})
        if source_email:
            recipient = source_email["from"].get("name") or source_email["from"].get("email")
    return {
        **doc,
        "recipient": recipient,
        "org_name": org_label(db, draft.org_id),
    }


def _get_draft_or_404(db, reply_id: str) -> ReplyDraft:
    doc = ReplyDraftRepository(db).find_one({"reply_id": reply_id})
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no reply draft found for reply_id={reply_id!r}")
    return ReplyDraft.model_validate(doc)


@router.get("")
def list_reply_drafts_route(
    status_filter: str = "awaiting_approval", db=Depends(get_db)
) -> list[dict[str, Any]]:
    """Enriched with recipient/org_name -- same resolution the Streamlit
    Reply Approval card already does (identical fallback: person_id lookup,
    then the source email's own `from` field)."""
    return [_enrich(db, doc) for doc in list_reply_drafts(db, status_filter)]


@router.post("/{reply_id}/approve", dependencies=[Depends(require_write_enabled)])
def approve_reply_draft_route(reply_id: str, db=Depends(get_db)) -> dict[str, Any]:
    draft = _get_draft_or_404(db, reply_id)
    approved = approve(draft, approved_by="api_user")
    sent = simulate_send(approved, now=datetime.now(timezone.utc))
    repo = ReplyDraftRepository(db)
    repo.upsert_by_key({"source_email_id": sent.source_email_id}, sent.model_dump(mode="json"))
    return repo.find_one({"reply_id": reply_id})


@router.post("/{reply_id}/reject", dependencies=[Depends(require_write_enabled)])
def reject_reply_draft_route(reply_id: str, db=Depends(get_db)) -> dict[str, Any]:
    draft = _get_draft_or_404(db, reply_id)
    rejected = reject(draft)
    repo = ReplyDraftRepository(db)
    repo.upsert_by_key({"source_email_id": rejected.source_email_id}, rejected.model_dump(mode="json"))
    return repo.find_one({"reply_id": reply_id})


@router.post("/{reply_id}/edit", dependencies=[Depends(require_write_enabled)])
def edit_reply_draft_route(reply_id: str, body: ReplyEditRequest, db=Depends(get_db)) -> dict[str, Any]:
    draft = _get_draft_or_404(db, reply_id)
    edited = edit(draft, new_subject=body.subject, new_body=body.body)
    repo = ReplyDraftRepository(db)
    repo.upsert_by_key({"source_email_id": edited.source_email_id}, edited.model_dump(mode="json"))
    return repo.find_one({"reply_id": reply_id})
