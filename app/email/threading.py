from datetime import datetime, timedelta

from pydantic import BaseModel

from app.email.models import Email
from app.email.normalizer import normalize_subject


class ThreadCandidate(BaseModel):
    thread_id: str  # canonical THR-nnn
    source_thread_id: str | None = None  # raw Gmail/provider thread id, if any
    normalized_subject: str
    participant_emails: set[str]
    # Raw Gmail/provider message ids of every message already in this thread --
    # NEVER canonical EML- ids. in_reply_to/references are themselves always raw
    # Gmail header values (see app.email.models.Email), so matching them against
    # anything else would silently and permanently break reply-threading the
    # moment message_id became canonical.
    source_message_ids: set[str]
    last_message_at: datetime


def _participants(email: Email) -> set[str]:
    return {email.from_.email, *(a.email for a in email.to), *(a.email for a in email.cc)}


def resolve_thread_id(
    email: Email,
    candidates: list[ThreadCandidate],
    window_days: int = 14,
) -> str | None:
    """Returns the existing canonical thread_id (THR-nnn) this email belongs to,
    or None when no existing thread matches by any signal -- the caller
    (app.pipeline._upsert_thread) is responsible for allocating a brand-new
    THR-nnn in that case via the same atomic next_id counter every other
    internal id uses. This function never synthesizes a thread identifier
    itself (the old `f"thread_{message_id}"` fallback is gone) -- a
    synthesized string was never a real Gmail-traceable identity, and
    fabricating one here is exactly the kind of independent identity
    generation the canonical-id refactor eliminates.
    """
    if email.source_thread_id:
        for candidate in candidates:
            if candidate.source_thread_id == email.source_thread_id:
                return candidate.thread_id
        return None  # first email of a Gmail thread never seen before

    if email.in_reply_to:
        for candidate in candidates:
            if email.in_reply_to in candidate.source_message_ids:
                return candidate.thread_id

    if email.references:
        for candidate in candidates:
            if candidate.source_message_ids.intersection(email.references):
                return candidate.thread_id

    normalized = normalize_subject(email.subject)
    participants = _participants(email)

    for candidate in candidates:
        if candidate.normalized_subject == normalized and candidate.participant_emails.intersection(participants):
            return candidate.thread_id

    window = timedelta(days=window_days)
    matching = [
        c
        for c in candidates
        if c.participant_emails.intersection(participants)
        and email.timestamp - c.last_message_at <= window
    ]
    if matching:
        matching.sort(key=lambda c: c.last_message_at, reverse=True)
        return matching[0].thread_id

    return None
