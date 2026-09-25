from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ReplyDraftContent(BaseModel):
    subject: str
    body: str


class ReplyDraft(BaseModel):
    reply_id: str
    thread_id: str
    source_email_id: str
    status: Literal[
        "no_reply_required",
        "awaiting_approval",
        "approved",
        "edited",
        "rejected",
        "cancelled",
        "simulated_sent",
        "sent",
    ]
    draft: ReplyDraftContent
    # Canonical references, additive: the resolved Person/Organization of the email
    # this draft replies to (its sender) -- so "a reply draft to Ashok" stays
    # connected to PER-391 even if a later email calls him something else. None on any
    # draft persisted before this field existed, and never backfilled retroactively.
    person_id: str | None = None
    org_id: str | None = None
    created_by: str = "sales_agent"
    # Optional (not required, no auto-generated default) -- a draft persisted before
    # this field existed has no created_at in MongoDB at all, and
    # app.ui.dashboard._render_reply_approval_tab re-validates stored drafts through
    # this exact model on every read, so a required field would crash the UI on any
    # pre-existing production draft. New drafts always get a real value explicitly
    # (see app.pipeline's ReplyDraft construction) -- this is never silently
    # backfilled for an old record.
    created_at: datetime | None = None
    approved_by: str | None = None
    sent_at: datetime | None = None
