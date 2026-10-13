"""Finding #3 recovery (Milestone 2 audit): conservative, explicit, re-runnable
repair for the one gap app.entities.person_story's own inline reconciliation
(_reconcile_stale_pending) does not cover -- a crash AFTER a story version
reaches commit_status="committed" but BEFORE people.profile_summary is
updated to match it (see app/entities/person_story.py's module docstring for
why no transaction exists to prevent this window).

This module never calls persist_story_version and never creates a
person_story_versions document -- it only ever reads existing committed
history and, within narrowly verified limits, repairs `superseded` flags and
(only with explicit opt-in) `people.profile_summary` together with
`people.profile_source_email_ids` (final-verification-pass fix: the two
fields must be restored together, from the SAME selected committed version,
or a repair can "fix" the narrative while leaving its claimed evidence
pointing at the wrong version). It is intentionally separate from, and more
conservative than, the inline reconciliation that already runs automatically
inside every persist_story_version call.

Evidence restoration never invents data: it uses the selected version's own
stored `source_email_ids` list exactly as recorded, including when that list
is empty. When a version row predates the `source_email_ids` field entirely
(the key is absent, not merely `[]` -- only possible on data older than this
schema field), `profile_source_email_ids` is left untouched rather than
fabricated; `profile_summary` still repairs normally in that case, since it
does not depend on the evidence field being present. This module also never
touches `source_email_ids`/`source_snapshot_ids` on any `person_story_versions`
document -- only `people.profile_source_email_ids` is ever written here.

The repair write updates only the fields actually being repaired (never the
whole person document re-merged from its initial read), so a legitimate
concurrent change to an unrelated Person field between this function's
initial read and its repair write is not silently reverted.

Known, stated limitation (never silently assumed away): there is no
operation id or version pointer anywhere in this schema linking a specific
persist_person_profile call to the person_story_versions row it produced.
"profile_summary matches an EARLIER committed version's text" is therefore
evidence CONSISTENT WITH an interrupted write, never proof of one -- a
person's summary could theoretically have reached that same text through
some other legitimate path this codebase doesn't currently have. This is why
the matching classification is named `probable_interrupted_write`, not
"confirmed", and why repairing it requires an explicit opt-in even in
execute mode (see `repair_profile_summary` below) rather than happening by
default alongside the (unambiguous) superseded-flag repair.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import PersonRepository, PersonStoryVersionRepository


def _find_matching_version(summary: Any, committed: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Searches newest-to-oldest so that, in the degenerate case of two
    versions sharing byte-identical text, the most recent one is reported --
    the more conservative choice (closer to "consistent", less likely to be
    misclassified as an older match)."""
    for version in reversed(committed):
        if version["summary"] == summary:
            return version
    return None


def _find_superseded_drift(committed: list[dict[str, Any]], latest_version: int) -> list[int]:
    """Only version numbers strictly less than the latest committed version
    -- the latest committed version's own flag is never a candidate, and
    pending rows are structurally excluded already (all_committed_for_person
    only ever returns commit_status="committed" documents)."""
    return sorted(
        v["version"] for v in committed if v["version"] < latest_version and not v.get("superseded", False)
    )


def reconcile_person_story(
    db: Database, person_id: str, dry_run: bool = True, repair_profile_summary: bool = False
) -> dict[str, Any]:
    """Detects and, within the limits documented above, repairs an
    interrupted person-story write for one person. Never fabricates a
    version, never touches source_email_ids/source_snapshot_ids on any
    version, never calls persist_story_version.

    `dry_run=True` (the default): pure reporting, writes nothing at all,
    regardless of `repair_profile_summary`.

    `dry_run=False`: superseded-flag drift (an unambiguous, purely internal
    bookkeeping repair -- see `_find_superseded_drift`) is always repaired.
    `profile_summary` (together with `profile_source_email_ids`, restored
    from that SAME selected version's own stored evidence -- see module
    docstring) is repaired ONLY when `repair_profile_summary=True` is ALSO
    explicitly passed -- the manual-review default applies even in execute
    mode otherwise, per the stated causation limitation above. If the
    selected version has no source_email_ids recorded at all (predates that
    field), profile_source_email_ids is left unchanged rather than
    fabricated; `detail` names this limitation when it applies.
    `ambiguous_mismatch` is NEVER repaired automatically, under any
    combination of flags -- there is no classification path that writes
    profile_summary or profile_source_email_ids when the current text
    matches no committed version at all.

    Returns a dict with: person_id, classification
    ("not_found" | "no_history" | "consistent" | "probable_interrupted_write"
    | "ambiguous_mismatch"), action ("none" | "would_update_profile_summary"
    | "updated_profile_summary" | "manual_review_required"), detail (a
    concise, human-readable string -- NEVER the raw summary/email-body text
    of any version), matched_version, latest_version,
    superseded_drift_versions (candidates found), and
    superseded_repaired_versions (candidates actually repaired and verified
    this call -- always [] when dry_run=True).
    """
    person_repo = PersonRepository(db)
    version_repo = PersonStoryVersionRepository(db)

    # Read once, up front. The repair write below never re-merges this
    # snapshot wholesale -- only the specific repaired fields are $set -- so
    # a person update that lands between this read and that write is not
    # clobbered by resending a stale copy of fields this function never
    # intended to touch.
    person = person_repo.find_one({"id": person_id})
    if person is None:
        return {
            "person_id": person_id,
            "classification": "not_found",
            "action": "none",
            "detail": "no person found for this person_id",
            "matched_version": None,
            "latest_version": None,
            "superseded_drift_versions": [],
            "superseded_repaired_versions": [],
        }

    committed = version_repo.all_committed_for_person(person_id)  # oldest-first
    if not committed:
        return {
            "person_id": person_id,
            "classification": "no_history",
            "action": "none",
            "detail": "no committed story versions exist for this person -- a legacy or never-versioned profile, not an error",
            "matched_version": None,
            "latest_version": None,
            "superseded_drift_versions": [],
            "superseded_repaired_versions": [],
        }

    latest = committed[-1]
    current_summary = person.get("profile_summary")
    match = _find_matching_version(current_summary, committed)

    if match is None:
        classification = "ambiguous_mismatch"
        action = "manual_review_required"
        detail = (
            f"profile_summary matches no committed version (latest is version "
            f"{latest['version']}) -- never auto-repaired, review required"
        )
        matched_version = None
    elif match["version"] == latest["version"]:
        classification = "consistent"
        action = "none"
        detail = f"profile_summary matches the latest committed version ({latest['version']})"
        matched_version = latest["version"]
    else:
        classification = "probable_interrupted_write"
        # The target version's own stored evidence -- used exactly as
        # recorded, never recomputed or inferred. `.get` (not indexing)
        # because a version row older than the source_email_ids field may
        # lack the key entirely; that is a different, narrower case than the
        # key being present but empty (see module docstring).
        target_source_email_ids = latest.get("source_email_ids")
        evidence_available = target_source_email_ids is not None
        detail = (
            f"profile_summary matches version {match['version']}, not the latest committed "
            f"version ({latest['version']}) -- consistent with an interrupted write, but not "
            "proven: no operation id exists in this schema to establish causation"
        )
        if not evidence_available:
            detail += (
                "; version has no source_email_ids recorded (predates that field) -- "
                "profile_source_email_ids will be left unchanged on repair, not fabricated"
            )
        matched_version = match["version"]
        if not dry_run and repair_profile_summary:
            repair_fields: dict[str, Any] = {"profile_summary": latest["summary"]}
            if evidence_available:
                repair_fields["profile_source_email_ids"] = target_source_email_ids
            # Only the repaired fields are $set -- never the whole person
            # document re-merged from the initial read -- so a legitimate
            # concurrent change to any other Person field survives this call.
            person_repo.upsert_by_key({"id": person_id}, repair_fields)
            verify = person_repo.find_one({"id": person_id})
            verified = verify is not None and verify.get("profile_summary") == latest["summary"]
            if verified and evidence_available:
                verified = verify.get("profile_source_email_ids") == target_source_email_ids
            if not verified:
                raise RuntimeError(
                    f"profile_summary repair for person_id={person_id!r} did not verify after write"
                )
            action = "updated_profile_summary"
        else:
            action = "would_update_profile_summary"

    drift_versions = _find_superseded_drift(committed, latest["version"])
    repaired_versions: list[int] = []
    if not dry_run:
        for version in drift_versions:
            version_repo.mark_superseded(person_id, version)
        # Verify afterward, per the audit's explicit instruction -- a silent
        # no-op write (0 documents matched) must be surfaced, never reported
        # as a successful repair.
        refreshed = {v["version"]: v for v in version_repo.all_committed_for_person(person_id)}
        for version in drift_versions:
            row = refreshed.get(version)
            if row is None or not row.get("superseded"):
                raise RuntimeError(
                    f"superseded-flag repair for person_id={person_id!r} version={version} "
                    "did not verify after write"
                )
            repaired_versions.append(version)

    return {
        "person_id": person_id,
        "classification": classification,
        "action": action,
        "detail": detail,
        "matched_version": matched_version,
        "latest_version": latest["version"],
        "superseded_drift_versions": drift_versions,
        "superseded_repaired_versions": repaired_versions,
    }
