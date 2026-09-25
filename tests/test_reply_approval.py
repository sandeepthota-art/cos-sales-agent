from datetime import datetime, timezone

import pytest

from app.replies.approval import approve, edit, reject, simulate_send
from app.replies.models import ReplyDraft, ReplyDraftContent


def _draft(status="awaiting_approval"):
    return ReplyDraft(
        reply_id="reply_001",
        thread_id="thread_001",
        source_email_id="msg_004",
        status=status,
        draft=ReplyDraftContent(subject="Re: Enterprise pricing", body="Hi John..."),
        created_at=datetime.now(timezone.utc),
    )


def test_approve_transitions_to_approved():
    draft = approve(_draft(), approved_by="ashok@example.com")
    assert draft.status == "approved"
    assert draft.approved_by == "ashok@example.com"


def test_edit_returns_new_awaiting_approval_draft():
    original = _draft()
    edited = edit(original, new_subject="Re: Updated pricing", new_body="New body")
    assert edited.status == "awaiting_approval"
    assert edited.draft.body == "New body"
    assert original.draft.body == "Hi John..."  # original untouched


def test_reject_transitions_to_rejected():
    draft = reject(_draft())
    assert draft.status == "rejected"


def test_simulate_send_requires_approved_status():
    with pytest.raises(ValueError):
        simulate_send(_draft(status="awaiting_approval"), now=datetime.now(timezone.utc))


def test_reply_draft_validates_a_legacy_document_with_no_created_at():
    # Regression: app.ui.dashboard._render_reply_approval_tab re-validates every stored
    # reply_drafts document through ReplyDraft.model_validate() on every read. A draft
    # persisted before created_at existed has no such key in MongoDB at all -- this
    # must not raise, and must not fabricate a value.
    legacy_doc = {
        "reply_id": "reply_001",
        "thread_id": "thread_001",
        "source_email_id": "msg_004",
        "status": "awaiting_approval",
        "draft": {"subject": "Re: Enterprise pricing", "body": "Hi John..."},
    }
    draft = ReplyDraft.model_validate(legacy_doc)
    assert draft.created_at is None


def test_simulate_send_from_approved_sets_simulated_sent(capsys):
    approved = approve(_draft(), approved_by="ashok@example.com")
    sent = simulate_send(approved, now=datetime(2026, 9, 14, tzinfo=timezone.utc))
    assert sent.status == "simulated_sent"
    assert sent.sent_at is not None
    captured = capsys.readouterr()
    assert "[SIMULATED EMAIL SEND]" in captured.out
