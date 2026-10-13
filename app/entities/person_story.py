"""Milestone 2: versioned person-story history (app.mcp.tools.
persist_person_profile's new integration point).

No transactions: this project's real MongoDB deployment (Atlas) IS a
replica set and could support them, but mongomock -- used by this entire
test suite -- raises NotImplementedError the moment a session is started,
and nothing anywhere in this codebase uses a session today. Introducing the
first transaction dependency here would make this one feature untestable
under this codebase's existing conventions. Consistency is instead achieved
by: an explicit pending/committed commit_status per version row, a write
order that always touches people.profile_summary LAST (so it can never
point at an uncommitted version), a true insert_one (not upsert) for a new
version row so the unique (person_id, version) index is the actual
race-breaker for concurrent writers (not just documentation), and a
reconciliation pass that cleans up any stale pending row from a prior
interrupted attempt before a new one begins.
"""

from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.database.repositories import (
    PersonContextSnapshotRepository,
    PersonRepository,
    PersonStoryVersionRepository,
)
from app.entities.ids import next_id

_MAX_VERSION_CREATE_ATTEMPTS = 3


def _reconcile_stale_pending(db: Database, person_id: str, person: dict[str, Any]) -> None:
    """Cleans up any pending row left by a prior interrupted write, before a
    new attempt begins. A pending row whose text matches the Person's
    CURRENT profile_summary means the crash happened after step 6
    (profile_summary already updated) but before step 4 (the commit flip) --
    finish committing it. Any other pending row never got far enough to
    matter -- delete it so next_version computation stays clean."""
    repo = PersonStoryVersionRepository(db)
    for pending in repo.pending_for_person(person_id):
        if person.get("profile_summary") == pending["summary"]:
            repo.set_commit_status(person_id, pending["version"], "committed")
        else:
            repo.delete_pending(person_id, pending["version"])


def _dedup(ids: list[str] | None) -> list[str]:
    return sorted(set(ids or []))


def _derive_source_snapshot_ids(db: Database, person_id: str, source_email_ids: list[str]) -> list[str]:
    """Looks up each email id's real PersonContextSnapshot and takes its
    actual `id` -- never invents one, never assumes email ids and snapshot
    ids are interchangeable. An email id with no matching snapshot is
    silently excluded, not fabricated or treated as an error."""
    snapshot_repo = PersonContextSnapshotRepository(db)
    snapshot_ids: list[str] = []
    for email_id in source_email_ids:
        snapshot = snapshot_repo.find_for_email(person_id, email_id)
        if snapshot is not None:
            snapshot_ids.append(snapshot["id"])
    return sorted(set(snapshot_ids))


def persist_story_version(
    db: Database, person_id: str, summary: str, source_email_ids: list[str] | None
) -> dict[str, Any]:
    """Core of Milestone 2. Called from app.mcp.tools.persist_person_profile
    after that function has already validated the person exists and composed
    `summary` -- this function never generates narrative text itself.

    Returns {version_created: bool, version: int | None, change_reason: str | None}.
    version is None only when no committed version exists yet AND this call's
    evidence doesn't justify creating one either (should not normally happen
    for a real call, since persist_person_profile always supplies a summary,
    but is handled rather than assumed away).
    """
    person_repo = PersonRepository(db)
    version_repo = PersonStoryVersionRepository(db)

    person = person_repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    _reconcile_stale_pending(db, person_id, person)

    new_email_ids = _dedup(source_email_ids)
    current = version_repo.latest_committed_for_person(person_id)

    if current is None:
        meaningful = True
        change_reason = (
            f"First version ({len(new_email_ids)} source email(s))." if new_email_ids
            else "First version."
        )
    else:
        prior_email_ids: set[str] = set()
        for v in version_repo.all_committed_for_person(person_id):
            prior_email_ids.update(v.get("source_email_ids", []))
        genuinely_new_emails = sorted(set(new_email_ids) - prior_email_ids)
        text_changed = summary != current["summary"]
        meaningful = bool(genuinely_new_emails) and text_changed
        change_reason = (
            f"New evidence: {len(genuinely_new_emails)} additional email(s) "
            f"({', '.join(genuinely_new_emails)}) since version {current['version']}."
            if meaningful else None
        )

    if not meaningful:
        return {
            "version_created": False,
            "version": current["version"] if current else None,
            "change_reason": None,
        }

    source_snapshot_ids = _derive_source_snapshot_ids(db, person_id, new_email_ids)
    next_version = (current["version"] if current else 0) + 1

    for attempt in range(_MAX_VERSION_CREATE_ATTEMPTS):
        document = {
            "id": next_id(db, "PSV-"),
            "person_id": person_id,
            "version": next_version,
            "summary": summary,
            "source_email_ids": new_email_ids,
            "source_snapshot_ids": source_snapshot_ids,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": change_reason,
            "superseded": False,
            "commit_status": "pending",
        }
        try:
            version_repo.insert_pending(document)
            break
        except DuplicateKeyError:
            if attempt == _MAX_VERSION_CREATE_ATTEMPTS - 1:
                raise
            next_version += 1
    else:  # pragma: no cover -- unreachable, loop always breaks or raises
        raise RuntimeError("unreachable")

    version_repo.set_commit_status(person_id, next_version, "committed")
    if current is not None:
        version_repo.mark_superseded(person_id, current["version"])

    return {"version_created": True, "version": next_version, "change_reason": change_reason}
