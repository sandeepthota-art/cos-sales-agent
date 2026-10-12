"""Fallback thread resolution -- adapted from cos-sales-agent's
app/email/threading.py (IMPORT AND CHANGE, see docs/CARRYOVER.md).

Only runs when Gmail's own threadId is missing on a NEW email (rare --
Gmail almost always provides one). Without this, such a message is
permanently isolated: ingest_raw_email_only's close/reopen logic and Mode
B's thread_context both key off thread_id, so a None here means the
message never groups with the rest of its conversation.

Adapted for this project's schema, not copied verbatim:
- No canonical THR-nnn ids exist here (this project deliberately never
  reassigns ids, see app/models.py). A resolved match returns the
  EXISTING raw Gmail thread_id to reuse, never a synthesized one -- same
  principle as the original ("never synthesizes a thread identifier
  itself"), just with a different id space.
- Candidates are built on the fly from raw_emails_dump documents (no
  separate threads collection exists here) via build_thread_candidates.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db import JsonDoc
from app.models import Email

_REPLY_FORWARD_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:\s*", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _normalize_subject(subject: str) -> str:
    text = subject.strip()
    while True:
        stripped = _REPLY_FORWARD_PREFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return _WHITESPACE.sub(" ", text).strip()


def _participants(email: Email) -> set[str]:
    return {email.from_.email, *(a.email for a in email.to), *(a.email for a in email.cc)}


@dataclass(frozen=True)
class ThreadCandidate:
    thread_id: str
    normalized_subject: str
    participant_emails: frozenset[str]
    source_message_ids: frozenset[str]
    last_message_at: datetime


def build_thread_candidates(docs: list[JsonDoc]) -> list[ThreadCandidate]:
    """Groups existing raw_emails_dump documents by thread_id into one
    ThreadCandidate per thread -- documents with no thread_id are excluded
    (nothing to match against). ponytail: scans the whole collection every
    call; fine at this project's current scale, upgrade to an indexed
    per-thread lookup if volume ever makes this slow."""
    groups: dict[str, dict] = {}
    for doc in docs:
        thread_id = doc.get("thread_id")
        if not thread_id:
            continue
        timestamp = datetime.fromisoformat(doc["timestamp"]) if isinstance(doc["timestamp"], str) else doc["timestamp"]
        group = groups.setdefault(
            thread_id,
            {"subject": doc.get("subject", ""), "participants": set(), "message_ids": set(), "last_at": timestamp},
        )
        group["participants"].update(
            e["email"] for e in [doc.get("from", {})] + doc.get("to", []) + doc.get("cc", []) if e.get("email")
        )
        group["message_ids"].add(doc["message_id"])
        if timestamp > group["last_at"]:
            group["last_at"] = timestamp
    return [
        ThreadCandidate(
            thread_id=thread_id,
            normalized_subject=_normalize_subject(g["subject"]),
            participant_emails=frozenset(g["participants"]),
            source_message_ids=frozenset(g["message_ids"]),
            last_message_at=g["last_at"],
        )
        for thread_id, g in groups.items()
    ]


def resolve_thread_id_fallback(
    email: Email, candidates: list[ThreadCandidate], window_days: int = 14
) -> str | None:
    """Returns an EXISTING thread_id this email likely belongs to, or None
    if nothing matches by any signal -- callers never synthesize a new
    thread_id here, same principle as the original. Only call this when
    email.thread_id is already None (Gmail gave nothing to go on)."""
    if email.in_reply_to:
        for candidate in candidates:
            if email.in_reply_to in candidate.source_message_ids:
                return candidate.thread_id

    if email.references:
        for candidate in candidates:
            if candidate.source_message_ids.intersection(email.references):
                return candidate.thread_id

    normalized = _normalize_subject(email.subject)
    participants = _participants(email)

    for candidate in candidates:
        if candidate.normalized_subject == normalized and candidate.participant_emails.intersection(participants):
            return candidate.thread_id

    window = timedelta(days=window_days)
    matching = [
        c
        for c in candidates
        if c.participant_emails.intersection(participants) and email.timestamp - c.last_message_at <= window
    ]
    if matching:
        matching.sort(key=lambda c: c.last_message_at, reverse=True)
        return matching[0].thread_id

    return None
