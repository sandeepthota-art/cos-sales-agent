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

Finding #4 fix: when insert_one raises DuplicateKeyError (another writer won
`next_version` first), the retry re-reads the actual latest committed
version and re-runs the meaningful-change decision against it (see
_evaluate_update) before trying again -- it never merely increments the
local version counter and retries the original, now-potentially-stale
decision. If the conflicting writer's commit means this update is no longer
meaningful, the retry returns the existing no-new-version result instead of
forcing a duplicate version into existence. Retries remain bounded
(_MAX_VERSION_CREATE_ATTEMPTS); persistent contention still raises
DuplicateKeyError explicitly rather than looping forever.

Finding #5 fix: summary-text equality is not proof that a pending row's
write is what produced people.profile_summary's current value -- a
coincidental match, a later writer's unrelated commit, or a stale read could
make a text-only check promote or delete the wrong row. Every version-create
attempt now carries a unique `attempt_id`, persisted on the pending/
committed row and mirrored onto people.profile_story_attempt_id by
app.mcp.tools.persist_person_profile (under the exact same version_created
gating as profile_summary/profile_source_email_ids). _reconcile_stale_pending
promotes a pending row ONLY when its attempt_id exactly matches the
Person's recorded attempt_id -- proof of causation, never a text comparison.
A pending row that isn't provably linked is deleted only once it is stale
(see _is_pending_stale) -- never merely because its text differs, since a
very-recently-created pending row could still belong to another writer
actively completing its own write sequence right now. A row that is neither
provably linked nor yet stale is left untouched for the next call to
reconsider. Legacy rows predating this field (no attempt_id at all) can
never be promoted -- there is no causal proof to check, and none is
invented -- but are still eventually reclaimed once stale, so a dead legacy
row doesn't block a version slot forever.

Finding #7 fix: several mutations here (set_commit_status, mark_superseded)
previously discarded their write result entirely, so a 0-match write (the
targeted row having vanished for some reason) would go unnoticed and the
caller would proceed as though it had succeeded. Each is now verified via
_find_version, a direct lookup of the exact (person_id, version) row, with a
severity matched to what actually breaks if it silently failed:
  - The MAIN success path's own set_commit_status (committing the version
    this very call just inserted) is load-bearing -- if it didn't verify,
    this function would otherwise return version_created=True for a version
    that was never actually committed. This one RAISES RuntimeError.
  - The MAIN success path's own mark_superseded (flagging the prior
    version) is cosmetic bookkeeping with an existing, dedicated repair
    tool (app.entities.person_story_reconciliation's superseded-drift
    detection) -- raising here would abort an otherwise-successful version
    commit over a flag on a DIFFERENT document. This one only logs a
    warning and still returns success.
  - _reconcile_stale_pending's promotion of a HISTORICAL (not this call's
    own) pending row is similarly non-fatal to this call -- it only logs a
    warning and leaves the row for a later reconciliation attempt; this
    call's own independent version evaluation proceeds regardless.
delete_pending's 0-match case is NOT a Finding #7 gap: its
commit_status="pending" filter clause makes a 0-match result correct by
construction (the row was promoted by someone else in the interim), not a
silent failure -- it is intentionally left unverified.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.database.repositories import (
    PersonContextSnapshotRepository,
    PersonRepository,
    PersonStoryVersionRepository,
)
from app.entities.ids import next_id

logger = logging.getLogger(__name__)

_MAX_VERSION_CREATE_ATTEMPTS = 3

# Finding #5: how long a pending row may sit uncommitted before
# _reconcile_stale_pending will consider it abandoned enough to delete. This
# codebase has no lease/heartbeat mechanism to know for certain whether
# another writer is still actively working on a given row, so age is used as
# a conservative proxy -- a real write sequence (insert_pending ->
# set_commit_status -> mark_superseded, three small round-trips) completes in
# well under a second in normal operation.
_STALE_PENDING_AGE = timedelta(minutes=5)


def _is_pending_stale(pending: dict[str, Any]) -> bool:
    """True only once a pending row is old enough to be confidently treated
    as abandoned rather than possibly still in-flight. A row missing
    created_at (should not happen -- every row this module writes always
    sets it) is treated as not-yet-stale rather than guessed about, so it is
    left alone instead of being deleted on an assumption."""
    created_at_raw = pending.get("created_at")
    if not created_at_raw:
        return False
    try:
        created_at = datetime.fromisoformat(created_at_raw)
    except ValueError:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > _STALE_PENDING_AGE


def _find_version(db: Database, person_id: str, version: int) -> dict[str, Any] | None:
    """Finding #7: a direct, single-document lookup for an exact
    (person_id, version) pair, used to verify a mutation actually took
    effect. Deliberately NOT latest_committed_for_person -- if a different,
    concurrent writer committed a newer version in between, that method
    would return THEIR row instead of ours, producing a false verification
    failure for a write of ours that actually succeeded. The unique
    (person_id, version) index guarantees at most one match."""
    docs = PersonStoryVersionRepository(db).find_many({"person_id": person_id, "version": version})
    return docs[0] if docs else None


def _reconcile_stale_pending(db: Database, person_id: str, person: dict[str, Any]) -> None:
    """Cleans up pending rows left by prior write attempts, before a new
    attempt begins. Finding #5 fix: promotion requires PROVEN causation --
    the pending row's own attempt_id must exactly match
    person["profile_story_attempt_id"] (set together, under the same gating
    as profile_summary, by app.mcp.tools.persist_person_profile once a
    version is actually used) -- summary text is never compared here at all
    any more. A pending row that isn't provably linked is deleted only once
    it is stale (_is_pending_stale) -- never merely because its text differs
    from the current profile_summary, since a fresh, unmatched pending row
    could still belong to another writer actively completing the same write
    sequence right now. A row that is neither provably linked nor yet stale
    is left untouched; the next call reconsiders it."""
    repo = PersonStoryVersionRepository(db)
    person_attempt_id = person.get("profile_story_attempt_id")
    for pending in repo.pending_for_person(person_id):
        pending_attempt_id = pending.get("attempt_id")
        if pending_attempt_id is not None and pending_attempt_id == person_attempt_id:
            repo.set_commit_status(person_id, pending["version"], "committed")
            # Finding #7 (Priority C): verify the promotion actually took
            # effect. A 0-match write here means this historical row (not
            # the current call's own new version) vanished between the read
            # above and this write -- e.g. a concurrent reconciliation pass
            # deleted it first. That is never fatal to THIS call: log and
            # move on, so the current call's own independent version
            # evaluation below still proceeds normally, and a later call's
            # own reconciliation pass reconsiders this row (or it is simply
            # gone, in which case there is nothing left to reconcile).
            verified = _find_version(db, person_id, pending["version"])
            if verified is None or verified.get("commit_status") != "committed":
                logger.warning(
                    "person_story reconciliation: promotion did not verify for "
                    "person_id=%s version=%s attempt_id=%s -- left for a later "
                    "reconciliation attempt",
                    person_id, pending["version"], pending_attempt_id,
                )
        elif _is_pending_stale(pending):
            repo.delete_pending(person_id, pending["version"])
        # else: neither provably linked nor yet stale -- leave untouched.


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


def _evaluate_update(
    db: Database, person_id: str, summary: str, new_email_ids: list[str]
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """Reads the latest committed version fresh and decides, against THAT
    state, whether (summary, new_email_ids) is a meaningful update -- the
    same rule persist_story_version always applied, factored out so a
    DuplicateKeyError retry (Finding #4 fix) can re-run it against freshly
    read state instead of trusting a decision made before a conflicting
    writer's commit landed.

    Returns (current, meaningful, change_reason); current is exactly what
    latest_committed_for_person returns (None or the latest committed row).
    """
    version_repo = PersonStoryVersionRepository(db)
    current = version_repo.latest_committed_for_person(person_id)

    if current is None:
        change_reason = (
            f"First version ({len(new_email_ids)} source email(s))." if new_email_ids
            else "First version."
        )
        return current, True, change_reason

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
    return current, meaningful, change_reason


def persist_story_version(
    db: Database, person_id: str, summary: str, source_email_ids: list[str] | None
) -> dict[str, Any]:
    """Core of Milestone 2. Called from app.mcp.tools.persist_person_profile
    after that function has already validated the person exists and composed
    `summary` -- this function never generates narrative text itself.

    Returns {version_created: bool, version: int | None, change_reason: str | None,
    attempt_id: str | None}. version is None only when no committed version
    exists yet AND this call's evidence doesn't justify creating one either
    (should not normally happen for a real call, since persist_person_profile
    always supplies a summary, but is handled rather than assumed away).
    attempt_id is this call's own unique identifier (Finding #5) when a
    version was created, else None -- app.mcp.tools.persist_person_profile
    mirrors it onto people.profile_story_attempt_id under the exact same
    gating it already applies to profile_summary.
    """
    person_repo = PersonRepository(db)
    version_repo = PersonStoryVersionRepository(db)

    person = person_repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    _reconcile_stale_pending(db, person_id, person)

    new_email_ids = _dedup(source_email_ids)
    current, meaningful, change_reason = _evaluate_update(db, person_id, summary, new_email_ids)

    if not meaningful:
        return {
            "version_created": False,
            "version": current["version"] if current else None,
            "change_reason": None,
            "attempt_id": None,
        }

    source_snapshot_ids = _derive_source_snapshot_ids(db, person_id, new_email_ids)
    next_version = (current["version"] if current else 0) + 1
    # Finding #5: one identifier for this whole call (including any retries
    # below) -- it names "this operation," not any single version-number
    # attempt, so a DuplicateKeyError retry still carries the same identity.
    attempt_id = next_id(db, "ATT-")

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
            "attempt_id": attempt_id,
        }
        try:
            version_repo.insert_pending(document)
            break
        except DuplicateKeyError:
            if attempt == _MAX_VERSION_CREATE_ATTEMPTS - 1:
                # Bounded retries exhausted under persistent contention --
                # surfaced explicitly rather than retried forever or silently
                # reported as success/no-op for a write that never landed.
                raise
            # Finding #4 fix: another writer won `next_version` first. Never
            # just bump the local counter and retry the stale decision --
            # re-read the actual persisted state and re-decide from scratch,
            # since the conflicting writer's commit may have changed whether
            # this update is still meaningful at all.
            current, meaningful, change_reason = _evaluate_update(db, person_id, summary, new_email_ids)
            if not meaningful:
                return {
                    "version_created": False,
                    "version": current["version"] if current else None,
                    "change_reason": None,
                    "attempt_id": None,
                }
            next_version = (current["version"] if current else 0) + 1
    else:  # pragma: no cover -- unreachable, loop always breaks, returns, or raises
        raise RuntimeError("unreachable")

    version_repo.set_commit_status(person_id, next_version, "committed")
    # Finding #7 (Priority A): this is the version row THIS call just
    # inserted -- if the commit-status write didn't actually take effect
    # (the pending row vanished for some reason between insert_pending and
    # here), there is no committed row to report. Returning
    # version_created=True in that case would be a false success claim the
    # caller (persist_person_profile) would propagate straight into
    # people.profile_summary/profile_source_email_ids/profile_story_attempt_id,
    # pointing them at a version that doesn't exist. This is load-bearing,
    # so it raises rather than logs.
    committed_row = _find_version(db, person_id, next_version)
    if committed_row is None or committed_row.get("commit_status") != "committed":
        raise RuntimeError(
            f"commit-status transition did not verify for person_id={person_id!r} "
            f"version={next_version!r} -- set_commit_status matched no document; "
            "refusing to report a version as created when no committed row exists"
        )

    if current is not None:
        version_repo.mark_superseded(person_id, current["version"])
        # Finding #7 (Priority B): cosmetic bookkeeping on a DIFFERENT
        # (older) document than the version this call just committed above.
        # app.entities.person_story_reconciliation already detects and
        # safely repairs exactly this drift, so a verification failure here
        # only logs -- raising would abort an otherwise-successful version
        # commit over a flag on an unrelated row.
        superseded_row = _find_version(db, person_id, current["version"])
        if superseded_row is None or not superseded_row.get("superseded"):
            logger.warning(
                "person_story: mark_superseded did not verify for person_id=%s "
                "version=%s (new version %s committed successfully; superseded-flag "
                "drift is repairable via "
                "app.entities.person_story_reconciliation.reconcile_person_story)",
                person_id, current["version"], next_version,
            )

    return {"version_created": True, "version": next_version, "change_reason": change_reason, "attempt_id": attempt_id}
