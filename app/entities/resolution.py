# app/entities/resolution.py
import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from pymongo.database import Database

from app.database.repositories import (
    CommitmentRepository,
    FollowUpRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
)
from app.entities.ids import next_id
from app.entities.lifecycle import is_person_active, resolve_canonical_person_id
from app.entities.models import Commitment, FollowUp, Meeting, Organization, Person, PersonalItem, Project
from app.knowledge.normalize import normalize_text

# Serializes a datetime exactly the way Person.model_dump(mode="json") would (e.g. a
# tz-aware UTC value as "...Z", not datetime.isoformat()'s "...+00:00"), without assuming
# `now` is guaranteed UTC-aware -- so the update path below matches the creation path's
# format regardless of what tzinfo (or lack of one) `now` actually carries.
_DATETIME_JSON = TypeAdapter(datetime)


def _parse_iso(value: str | None) -> datetime | None:
    # pydantic's model_dump(mode="json") serializes a tz-aware UTC datetime with a
    # trailing "Z" (e.g. "...T00:00:00Z"), while Python's own datetime.isoformat()
    # produces "...+00:00" for the same instant. Comparing those two string forms
    # directly (as the original spec's code did) breaks the "exact match" dedup rule
    # for any non-null date, since two representations of the identical instant would
    # never compare equal as strings. Parsing back to a datetime for comparison makes
    # the equality check instant-based rather than string-format-based, which is what
    # "exact natural-key match" actually requires.
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _add_thread_id(open_threads: list[str], thread_id: str) -> list[str]:
    # Append-only, no duplicates -- an existing Person's open_threads is never rewritten
    # wholesale, only ever grown by at most one new entry.
    if thread_id in open_threads:
        return open_threads
    return [*open_threads, thread_id]


def _forward_only_timestamp_update(existing_value: str | None, new_value: datetime) -> str | None:
    """Shared merge rule for last_inbound/last_outbound: both fields must only ever
    move FORWARD, regardless of processing order (emails are not guaranteed to
    arrive/process in timestamp order -- a delayed message, a re-run over an
    out-of-order batch). Returns the newly serialized value only when it should
    overwrite existing_value (existing missing, or new_value is strictly newer);
    returns None when existing_value should be left untouched (new_value is older
    than or equal to what's already stored) -- the caller only assigns into `update`
    when this returns non-None, so an unchanged field is never rewritten.
    """
    existing_dt = _parse_iso(existing_value)
    if existing_dt is None or new_value > existing_dt:
        return _DATETIME_JSON.dump_python(new_value, mode="json")
    return None


def resolve_organization(db: Database, email: str | None, name_hint: str | None = None) -> str | None:
    """Canonical Organization resolution (Part 4 of the global-identity design) --
    mirrors resolve_person's own hierarchy: the email DOMAIN is the strongest, safest
    signal, exactly analogous to a full email address for a Person. Returns None
    whenever no domain is available -- a company NAME alone is never sufficient to
    establish or look up a canonical organization; text-similarity-only merging
    ("DataBeat" vs "DataBeat Inc." vs "databeat") is deliberately out of scope here,
    since two different real companies can share a very similar display name.
    """
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if not domain:
        return None

    repo = OrganizationRepository(db)
    existing = repo.find_one({"domain": domain})
    if existing:
        # Enrichment, not migration: the org may have been created earlier with no real
        # display name (e.g. the envelope-resolution loop never has a name hint, so the
        # raw domain was used as a placeholder name) -- a later call that DOES have one
        # fills it in, but only while the stored name is still just the domain itself;
        # never overwrites a real name that's already been recorded.
        if name_hint and existing.get("name") == domain:
            repo.upsert_by_key({"id": existing["id"]}, {**existing, "name": name_hint})
        return existing["id"]

    org_id = next_id(db, "ORG-")
    org = Organization(id=org_id, name=name_hint or domain, domain=domain)
    repo.upsert_by_key({"id": org_id}, org.model_dump(mode="json"))
    return org_id


def _name_tokens(name: str) -> set[str]:
    return set(normalize_text(name).split())


def _reuse_target(db: Database, repo: PersonRepository, found: dict[str, Any]) -> dict[str, Any]:
    """Phase 19.1: given a Person document some lookup in resolve_person just
    found, returns the document that should actually be enriched and returned --
    `found` itself if it's ACTIVE, or its canonical replacement's own document if
    `found` turns out to be MERGED. This is the single place resolve_person
    redirects a match away from a retired historical record, so an incoming
    mention that matches a since-consolidated Person transparently attaches to its
    live replacement instead of resurrecting or duplicating the retired identity --
    it never creates a new Person just because the first match was merged.

    Propagates CanonicalResolutionError untouched if the merged_into chain is
    broken -- resolve_person surfaces that as a hard failure rather than silently
    inventing a replacement or repairing the chain itself.
    """
    if is_person_active(found):
        return found
    canonical_id = resolve_canonical_person_id(db, found["id"])
    canonical = repo.find_one({"id": canonical_id})
    return canonical


def resolve_canonical_person_for_email(db: Database, email: str | None) -> dict[str, Any] | None:
    """Phase 20.1: the one safe way for a downstream object (ReplyDraft,
    CalendarAction, a historical-backfill migration proposal, ...) to turn a
    sender's email address into the Person it should reference. This is exact-email
    identity tier 1 -- the same lookup resolve_person's own email branch performs --
    with the SAME lifecycle redirect (_reuse_target) applied to a merged match, so
    it is never a second, competing identity-resolution implementation.

    Returns None when no Person has this email at all -- a genuinely new/unknown
    identity is NOT created here; that stays resolve_person's job during entity
    resolution, which already ran earlier for this same email. Never guesses:
    exact-email lookup has no ambiguity tier (email is unique), and this function
    implements only that tier, nothing broader.

    Propagates CanonicalResolutionError untouched (never swallowed) if the matched
    Person's merged_into chain is broken -- callers must not attach a downstream
    object to a retired identity just because its canonical chain is corrupt.
    """
    if not email:
        return None
    repo = PersonRepository(db)
    found = repo.find_one({"email": email.strip().lower()})
    if found is None:
        return None
    return _reuse_target(db, repo, found)


def match_resolved_person_by_name(
    resolved_people: list[dict[str, Any]], name: str | None
) -> dict[str, Any] | None:
    """Matches a free-text name (a commitment's owed_by/owed_to, a meeting attendee, a
    knowledge fact's subject, ...) against exactly one of the People ALREADY resolved
    from this same email (envelope + LLM people_mentioned) -- never a fresh, wider scan
    of the whole `people` collection. This is deliberately narrower than resolve_person's
    own tier 2: the candidate pool here is only what this specific email already
    established as real, canonical people, so there is no new ambiguity surface beyond
    what resolve_person itself already resolved for this email.

    Returns None (never guesses) when the name is empty, or when zero or more than one
    resolved person matches.
    """
    if not name:
        return None
    name_tokens = _name_tokens(name)
    if not name_tokens:
        return None

    # resolved_people routinely contains the SAME real person more than once (e.g. the
    # sender resolved once via the envelope loop and again via the LLM's own
    # people_mentioned) -- ambiguity must be judged by distinct person id, never by how
    # many list entries happen to reference the same one.
    matches_by_id = {
        p["id"]: p
        for p in resolved_people
        if name_tokens <= _name_tokens(p["name"]) or _name_tokens(p["name"]) <= name_tokens
    }
    if len(matches_by_id) == 1:
        return next(iter(matches_by_id.values()))
    return None


def _find_email_anchored_candidate(repo: PersonRepository, name: str, org: str | None) -> dict[str, Any] | None:
    """Tier 2 of the person-resolution hierarchy (Part 1 of the global-identity
    design): a no-email mention with BOTH a name and a company can reuse an existing,
    email-anchored Person -- but only when the evidence is strong on both axes:

      - org: the mention's normalized org must exactly match the candidate's
        normalized org. No org on the mention at all means this tier can never fire
        (a name alone, even a full "Ashok Ganapam", is not enough -- see Part 1's
        tier 4: "Name alone must NOT establish identity").
      - name: every word in the SHORTER of the two normalized names must appear as a
        whole word in the longer one (a subset check, not substring/fuzzy matching).
        "ashok" ⊆ {"ashok", "ganapam"} matches; "ash" does NOT (it is not a whole word
        in either direction) -- a bare nickname like "Ash" is intentionally NOT strong
        enough evidence on its own (Part 1's TEST 2), only a real alias entry or a
        fuller name match is.

    More than one equally-good candidate is treated exactly like no match: never
    guess which real person a mention belongs to.
    """
    org_norm = normalize_text(org or "")
    if not org_norm:
        return None
    mention_tokens = _name_tokens(name)
    if not mention_tokens:
        return None

    candidates = []
    for p in repo.find_many({"email": {"$exists": True}}):
        if normalize_text(p.get("org") or "") != org_norm:
            continue
        candidate_tokens = _name_tokens(p["name"])
        alias_tokens = {tok for alias in p.get("aliases", []) for tok in _name_tokens(alias)}
        if (
            mention_tokens <= candidate_tokens
            or candidate_tokens <= mention_tokens
            or mention_tokens <= alias_tokens
        ):
            candidates.append(p)

    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_person(
    db: Database, mention: dict[str, Any], is_sender: bool | None, now: datetime, thread_id: str
) -> str:
    repo = PersonRepository(db)
    email = (mention.get("email") or "").strip().lower() or None

    if email:
        existing = repo.find_one({"email": email})
        if existing:
            # Phase 19.1: a mention's email can exact-match a Person that has since
            # been consolidated into another one (its own email field is left
            # untouched by consolidation -- see app.duplicate_consolidation). Redirect
            # to the canonical record BEFORE any enrichment below, so a merged
            # record is never re-enriched or re-returned as if it were still live.
            existing = _reuse_target(db, repo, existing)
            # {**existing, **update}: existing's own fields (email included) always win
            # unless update explicitly overrides them -- update only ever sets
            # last_inbound/last_outbound/open_threads/org_id, so an existing email,
            # name, org, etc. can never be clobbered by a later, less-complete mention.
            update: dict[str, Any] = {}
            if is_sender is True:
                # max(existing, new) -- see _forward_only_timestamp_update. last_inbound
                # must always reflect the latest inbound email known for this person,
                # regardless of processing order.
                new_last_inbound = _forward_only_timestamp_update(existing.get("last_inbound"), now)
                if new_last_inbound is not None:
                    update["last_inbound"] = new_last_inbound
            elif is_sender is False:
                # Same forward-only rule as last_inbound above, applied symmetrically to
                # last_outbound.
                new_last_outbound = _forward_only_timestamp_update(existing.get("last_outbound"), now)
                if new_last_outbound is not None:
                    update["last_outbound"] = new_last_outbound
            new_open_threads = _add_thread_id(existing.get("open_threads", []), thread_id)
            if new_open_threads != existing.get("open_threads", []):
                update["open_threads"] = new_open_threads
            # Always call resolve_organization on reuse, not just when org_id is
            # missing: it's an idempotent, cheap lookup by domain, and its own
            # enrichment (backfilling the Organization's display name once a mention
            # finally supplies one -- see resolve_organization) needs to run on every
            # call, not just the first. Only ever assigned into `update` when it
            # actually changes anything on the Person record itself.
            backfilled_org_id = resolve_organization(db, email, mention.get("org") or existing.get("org"))
            if backfilled_org_id and not existing.get("org_id"):
                update["org_id"] = backfilled_org_id
            # Same enrichment for the free-text `org` display field: a Person resolved
            # first via the envelope loop (which never has an org hint -- see
            # app.pipeline._process_entities) has org=None until a LATER mention (LLM
            # people_mentioned, which does carry org) fills it in. Only ever fills a
            # missing value, never overwrites an existing one.
            if not existing.get("org") and mention.get("org"):
                update["org"] = mention.get("org")
            if update:
                repo.upsert_by_key({"id": existing["id"]}, {**existing, **update})
            return existing["id"]

        person_id = next_id(db, "PER-")
        person = Person(
            id=person_id,
            name=mention.get("name") or email,
            email=email,
            org=mention.get("org"),
            org_id=resolve_organization(db, email, mention.get("org")),
            review_flag=False,
            last_inbound=now if is_sender is True else None,
            last_outbound=now if is_sender is False else None,
            open_threads=[thread_id],
        )
        repo.upsert_by_key({"id": person_id}, person.model_dump(mode="json"))
        return person_id

    # No email present.
    name = mention.get("name") or "Unknown"
    org = mention.get("org")
    normalized_name = normalize_text(name)

    # Tier 2 (Part 1 of the global-identity design): before falling back to the
    # existing thread-scoped-only reuse below, check for a strong, cross-thread match
    # against an existing EMAIL-ANCHORED Person -- the important new resolution tier
    # that lets "Ashok" (mentioned with no email, org "DataBeat") reuse the canonical
    # PER-xxx that already has ashok@databeat.io, without ever merging on name alone.
    anchored = _find_email_anchored_candidate(repo, name, org)
    if anchored is not None:
        # Phase 19.1: same redirect as the exact-email-match branch above -- an
        # email-anchored match can itself have since been merged into another
        # canonical Person (e.g. a future anchor-anchor consolidation).
        anchored = _reuse_target(db, repo, anchored)
        update: dict[str, Any] = {}
        new_open_threads = _add_thread_id(anchored.get("open_threads", []), thread_id)
        if new_open_threads != anchored.get("open_threads", []):
            update["open_threads"] = new_open_threads
        if is_sender is True:
            new_last_inbound = _forward_only_timestamp_update(anchored.get("last_inbound"), now)
            if new_last_inbound is not None:
                update["last_inbound"] = new_last_inbound
        elif is_sender is False:
            new_last_outbound = _forward_only_timestamp_update(anchored.get("last_outbound"), now)
            if new_last_outbound is not None:
                update["last_outbound"] = new_last_outbound
        # Evidence-gated alias recording (Part 6): this exact resolution just proved
        # `name` belongs to the canonical person -- record it as a known alternate form,
        # but only now, never speculatively, and never as a basis for a FUTURE merge on
        # its own (matching remains name+org, or a full name/alias-token match, above).
        existing_aliases = anchored.get("aliases", [])
        if name != anchored["name"] and name not in existing_aliases:
            update["aliases"] = [*existing_aliases, name]
        if update:
            repo.upsert_by_key({"id": anchored["id"]}, {**anchored, **update})
        return anchored["id"]

    # Never merge on name alone GLOBALLY -- but a name that exactly matches an existing
    # no-email Person already linked to THIS SAME THREAD is strong enough evidence to
    # reuse that record (same conversation, same name, no email ever given) without
    # risking merging two different real people who happen to share a name across
    # unrelated threads. Ambiguous (>1 candidate) is treated exactly like no match at
    # all: never guess, always create a new flagged record.
    candidates = [
        p
        for p in repo.find_many({"open_threads": thread_id})
        if not p.get("email") and normalize_text(p["name"]) == normalized_name
    ]
    if len(candidates) == 1:
        # Phase 19.1: this thread-scoped record itself may since have been merged
        # into a canonical Person (exactly the historical Ashok/John-Toth-style
        # fragments this tier originally created) -- redirect rather than reusing
        # or re-enriching the retired record.
        return _reuse_target(db, repo, candidates[0])["id"]

    person_id = next_id(db, "PER-")
    person = Person(
        id=person_id,
        name=name,
        email=None,
        org=org,
        review_flag=True,
        open_threads=[thread_id],
    )
    # exclude={"email"}: MongoDB's sparse unique index on people.email (Task 3) only
    # excludes a document where the field is entirely MISSING -- a document with
    # email explicitly set to null still gets indexed with key null, and a second such
    # document would collide on the unique constraint. Omitting the key entirely (rather
    # than storing "email": null) is what actually makes two no-email People coexist on
    # real MongoDB, not just under mongomock's more lenient interpretation of sparse+null.
    repo.upsert_by_key({"id": person_id}, person.model_dump(mode="json", exclude={"email"}))
    return person_id


def resolve_operator_person(
    db: Database, agent_email: str, display_name: str, is_sender: bool | None, now: datetime, thread_id: str
) -> str:
    """Resolves the operator's OWN dedicated Person profile -- a single, real,
    tracked record for the system's own user, deliberately distinct from an
    external lead. Reused forever after by exact email match, identical in
    contract to resolve_person's own email branch: name/type are set only at
    creation and never overwritten by a later call (only
    last_inbound/last_outbound/open_threads ever change on reuse), so a later
    mention's own wording (a signature, a calendar invite's attendee list) can never
    clobber the operator's distinct display name or its type="operator" marker.

    Called from app.pipeline._process_entities whenever an envelope address or a
    people_mentioned entry is recognized as the operator (by agent_email or
    agent_name) -- never creates a second, generic Person for the same address.
    """
    repo = PersonRepository(db)
    email = agent_email.strip().lower()
    existing = repo.find_one({"email": email})
    if existing:
        # Same merged-record redirect as resolve_person's own email branch -- an
        # operator profile is not expected to ever be a duplicate-consolidation
        # target, but this keeps the two code paths' invariants identical rather
        # than assuming it can never happen.
        existing = _reuse_target(db, repo, existing)
        update: dict[str, Any] = {}
        if is_sender is True:
            new_last_inbound = _forward_only_timestamp_update(existing.get("last_inbound"), now)
            if new_last_inbound is not None:
                update["last_inbound"] = new_last_inbound
        elif is_sender is False:
            new_last_outbound = _forward_only_timestamp_update(existing.get("last_outbound"), now)
            if new_last_outbound is not None:
                update["last_outbound"] = new_last_outbound
        new_open_threads = _add_thread_id(existing.get("open_threads", []), thread_id)
        if new_open_threads != existing.get("open_threads", []):
            update["open_threads"] = new_open_threads
        if update:
            repo.upsert_by_key({"id": existing["id"]}, {**existing, **update})
        return existing["id"]

    person_id = next_id(db, "PER-")
    person = Person(
        id=person_id,
        name=display_name,
        email=email,
        org=None,
        org_id=None,
        type="operator",
        review_flag=False,
        last_inbound=now if is_sender is True else None,
        last_outbound=now if is_sender is False else None,
        open_threads=[thread_id],
    )
    repo.upsert_by_key({"id": person_id}, person.model_dump(mode="json"))
    return person_id


# Punctuation that functions as a separator between words in a project/entity name and
# should be treated as equivalent to a space before general normalization -- NOT applied
# to app.knowledge.normalize.normalize_text globally, since that function is shared by
# Commitment/PersonalItem/Knowledge-fact dedup, which have no equivalent problem and
# their own established tests; this is a project-identity-only concern.
_JOINER_PATTERN = re.compile(r"[+&/-]")


def _normalize_project_name(text: str) -> str:
    # NFKD + strip combining marks folds accented characters to their base form (e.g.
    # "Condé" -> "Conde"), so a project mentioned once with the accent and once without
    # (the real observed case) compares equal.
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    # Replace joiners with a space (not delete them) BEFORE normalize_text's own
    # punctuation-stripping: normalize_text alone deletes "-" outright, so "DataBeat-X"
    # would become "databeatx" while "DataBeat + X" becomes "databeat x" -- different
    # strings despite being the same name with a different separator style. Turning the
    # joiner into a space first makes both collapse to "databeat x".
    with_joiners_as_spaces = _JOINER_PATTERN.sub(" ", without_accents)
    return normalize_text(with_joiners_as_spaces)


def resolve_project(
    db: Database,
    mention: dict[str, Any],
    goal_pillar: str,
    person_ids: list[str] | None = None,
    org_id: str | None = None,
) -> str:
    repo = ProjectRepository(db)
    name_normalized = _normalize_project_name(mention["name"])
    entity = mention.get("org")
    entity_normalized = _normalize_project_name(entity) if entity else None

    # entity is compared normalized too (not just the project name) -- the real observed
    # duplicate was actually split at the entity level ("Condé Nast" vs "Conde Nast" as
    # two different `entity` values), so an exact Mongo-level {"entity": entity} filter
    # would never even bring the accented and unaccented records into the same candidate
    # pool. This can no longer be a single indexed-equality query once entity itself
    # needs normalized comparison; scans the full projects collection, mirroring how
    # Commitment/Meeting resolution already do their matching in Python, not Mongo.
    for candidate in repo.find_many({}):
        candidate_entity = candidate.get("entity")
        candidate_entity_normalized = _normalize_project_name(candidate_entity) if candidate_entity else None
        if (
            candidate_entity_normalized == entity_normalized
            and _normalize_project_name(candidate["project"]) == name_normalized
            and candidate.get("goal_pillar") == goal_pillar
        ):
            # Accumulate newly-resolved people/org onto the existing project, the same
            # append-only pattern resolve_person already uses for open_threads -- never
            # overwrites, only grows.
            update: dict[str, Any] = {}
            existing_person_ids = candidate.get("person_ids", [])
            new_person_ids = list(dict.fromkeys([*existing_person_ids, *(person_ids or [])]))
            if new_person_ids != existing_person_ids:
                update["person_ids"] = new_person_ids
            if org_id and not candidate.get("org_id"):
                update["org_id"] = org_id
            if update:
                repo.upsert_by_key({"id": candidate["id"]}, {**candidate, **update})
            return candidate["id"]

    project_id = next_id(db, "PRJ-")
    project = Project(
        id=project_id,
        project=mention["name"],
        entity=entity,
        goal_pillar=goal_pillar,
        person_ids=person_ids or [],
        org_id=org_id,
    )
    repo.upsert_by_key({"id": project_id}, project.model_dump(mode="json"))
    return project_id


def _commitment_dates_match(
    candidate_date: datetime | None, resolved_date: datetime | None, date_type: str | None
) -> bool:
    if date_type == "inferred" and candidate_date is not None and resolved_date is not None:
        # Same-calendar-day identity for inferred/relative dates only: a relative phrase
        # like "10am" resolves against each email's OWN send time (see
        # app/entities/dates.py), so the same real commitment restated in two emails a
        # few hours apart on the same day previously resolved to two different exact
        # instants and never merged (the real observed COM-129/COM-132 case). Comparing
        # calendar day instead fixes that, deliberately never crossing a day boundary
        # (2025-02-07 vs 2025-02-08 stays separate) and deliberately not applied to a
        # "stated" (explicit) date, which keeps exact-instant matching unchanged there.
        return candidate_date.date() == resolved_date.date()
    return candidate_date == resolved_date


def resolve_commitment(
    db: Database,
    thread_id: str,
    raw: dict[str, Any],
    message_id: str,
    made_on: datetime,
    resolved_date: datetime | None,
    date_type: str | None,
    goal_pillar: str,
    project_id: str | None,
    person_id: str | None = None,
    org_id: str | None = None,
) -> str:
    repo = CommitmentRepository(db)
    what_normalized = normalize_text(raw["what"])

    for candidate in repo.all_for_thread(thread_id):
        if (
            normalize_text(candidate["what"]) == what_normalized
            and candidate["class"] == raw["class"]
            and candidate.get("date_type") == date_type
            and _commitment_dates_match(_parse_iso(candidate.get("committed_date")), resolved_date, date_type)
        ):
            # Backfill the canonical reference onto an existing commitment discovered
            # again in a later email, exactly like Person's own org_id backfill above --
            # ordinary incremental enrichment, never a bulk historical pass. project_id
            # follows the same only-fill-a-missing-value rule (app.pipeline computes it
            # from evidence resolved in that later email, never guessed here).
            update: dict[str, Any] = {}
            if person_id and not candidate.get("person_id"):
                update["person_id"] = person_id
            if org_id and not candidate.get("org_id"):
                update["org_id"] = org_id
            if project_id and not candidate.get("project_id"):
                update["project_id"] = project_id
            if update:
                repo.upsert_by_key({"id": candidate["id"]}, {**candidate, **update})
            return candidate["id"]

    commitment_id = next_id(db, "CMT-")
    commitment = Commitment(
        id=commitment_id,
        what=raw["what"],
        commitment_class=raw["class"],
        importance=raw.get("importance_hint"),
        owed_by=raw.get("owed_by"),
        owed_to=raw.get("owed_to"),
        person_id=person_id,
        org_id=org_id,
        source_record=message_id,
        made_on=made_on,
        committed_date=resolved_date,
        date_type=date_type,
        goal_pillar=goal_pillar,
        project_id=project_id,
        thread_id=thread_id,
    )
    repo.upsert_by_key({"id": commitment_id}, commitment.model_dump(mode="json", by_alias=True))
    return commitment_id


def resolve_meeting(
    db: Database,
    thread_id: str,
    date: datetime | None,
    raw: dict[str, Any],
    actionable: bool,
    person_ids: list[str] | None = None,
    org_id: str | None = None,
) -> str:
    repo = MeetingRepository(db)

    for candidate in repo.all_for_thread(thread_id):
        if _parse_iso(candidate.get("date")) == date:
            existing_person_ids = candidate.get("person_ids", [])
            new_person_ids = list(dict.fromkeys([*existing_person_ids, *(person_ids or [])]))
            update: dict[str, Any] = {}
            if new_person_ids != existing_person_ids:
                update["person_ids"] = new_person_ids
            if org_id and not candidate.get("org_id"):
                update["org_id"] = org_id
            if update:
                repo.upsert_by_key({"id": candidate["id"]}, {**candidate, **update})
            return candidate["id"]

    meeting_id = next_id(db, "MTG-")
    meeting = Meeting(
        id=meeting_id,
        date=date,
        attendees=raw.get("attendees", []),
        person_ids=person_ids or [],
        org_id=org_id,
        actions_raised=raw.get("actions_raised", []),
        actionable=actionable,
        thread_id=thread_id,
    )
    repo.upsert_by_key({"id": meeting_id}, meeting.model_dump(mode="json"))
    return meeting_id


def resolve_personal_item(
    db: Database, sender_email: str, raw: dict[str, Any], resolved_date: datetime | None
) -> str:
    repo = PersonalItemRepository(db)
    sender_normalized = sender_email.strip().lower()
    description_normalized = normalize_text(raw["description"])

    for candidate in repo.find_many({"sender_email": sender_normalized}):
        if normalize_text(candidate["description"]) == description_normalized:
            return candidate["id"]

    item_id = next_id(db, "PSN-")
    item = PersonalItem(
        id=item_id,
        type=raw["item_type"],
        description=raw["description"],
        date_or_deadline=resolved_date,
        sender_email=sender_normalized,
    )
    repo.upsert_by_key({"id": item_id}, item.model_dump(mode="json"))
    return item_id


def derive_follow_up(
    db: Database,
    commitment_id: str | None,
    thread_id: str | None,
    person_id: str | None = None,
    org_id: str | None = None,
    audience: str | None = None,
    follow_up_earliest_at: datetime | None = None,
    follow_up_latest_at: datetime | None = None,
) -> str:
    repo = FollowUpRepository(db)

    if commitment_id is not None:
        existing = repo.find_one({"commitment_id": commitment_id})
        if existing:
            return existing["id"]
        follow_up_id = next_id(db, "FUP-")
        # thread_id is carried alongside commitment_id (previously dropped here even
        # when the caller passed one) so a FollowUp is directly queryable by thread_id
        # without following commitment_id -> Commitment -> thread_id indirection.
        # person_id/org_id are inherited directly from the parent Commitment (the
        # caller passes exactly what that commitment already resolved) -- never
        # independently inferred here from a name. audience/timing are likewise
        # computed by the caller (app.pipeline, via
        # app.entities.dates.classify_follow_up_timing) -- this function only persists
        # what it's given, never classifies anything itself.
        follow_up = FollowUp(
            id=follow_up_id, commitment_id=commitment_id, thread_id=thread_id, person_id=person_id, org_id=org_id,
            audience=audience, follow_up_earliest_at=follow_up_earliest_at, follow_up_latest_at=follow_up_latest_at,
        )
        repo.upsert_by_key({"id": follow_up_id}, follow_up.model_dump(mode="json"))
        return follow_up_id

    existing = repo.find_one({"thread_id": thread_id})
    if existing:
        return existing["id"]
    follow_up_id = next_id(db, "FUP-")
    follow_up = FollowUp(id=follow_up_id, thread_id=thread_id)
    repo.upsert_by_key({"id": follow_up_id}, follow_up.model_dump(mode="json"))
    return follow_up_id
