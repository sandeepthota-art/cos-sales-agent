"""Phase 4 business logic: the 4 context tools, their validation pipeline,
and the observations-to-profile rebuild shared with scripts/rebuild_profiles.py.
Same boundary as app/tools.py: this module knows what fields MEAN; app/db.py
and app/context_db.py only know how to read/write Mongo.

Email content is DATA, never instructions -- nothing here parses or obeys
free-form text; every stored fact must be independently evidence-checked
against the claimed message it's attributed to before it's ever written.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from email_validator import validate_email
from pydantic import ValidationError
from pymongo.database import Database

from app import context_config as cfg
from app.context_db import (
    EmailInsightRepository,
    ObservationRepository,
    OrganizationRepository,
    PersonOrgLinkRepository,
    PersonRepository,
    ThreadHistoryRepository,
    ThreadRepository,
)
from app.context_models import ContextDelta, ThreadDeltaInput
from app.db import JsonDoc, RawEmailDumpRepository
from app.text_clean import strip_quotes_and_signature

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_control_chars(value: str) -> str:
    return _CONTROL_CHARS_RE.sub("", value)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def _local_part_of(email: str) -> str:
    return email.split("@", 1)[0].strip().lower()


def _is_role_address(email: str) -> bool:
    return _local_part_of(email) in cfg.ROLE_LOCAL_PARTS


def _is_free_mail(domain: str) -> bool:
    return domain.strip().lower() in cfg.FREE_MAIL_DOMAINS


def _is_valid_email_syntax(email: str) -> bool:
    try:
        validate_email(email, check_deliverability=False)
        return True
    except Exception:
        return False


def _participant_emails(docs: list[JsonDoc]) -> set[str]:
    emails: set[str] = set()
    for doc in docs:
        for addr in [doc.get("from")] + list(doc.get("to") or []) + list(doc.get("cc") or []):
            if addr and addr.get("email"):
                emails.add(addr["email"].strip().lower())
    return emails


def _derive_display_name(docs: list[JsonDoc], email: str) -> str | None:
    for doc in docs:
        for addr in [doc.get("from")] + list(doc.get("to") or []) + list(doc.get("cc") or []):
            if addr and (addr.get("email") or "").strip().lower() == email and addr.get("name"):
                return addr["name"]
    return None


def _message_view(doc: JsonDoc) -> JsonDoc:
    body_clean = strip_quotes_and_signature(doc.get("body") or "")
    truncated = len(body_clean) > cfg.MAX_BODY_CHARS_PER_MESSAGE
    return {
        "message_id": doc["message_id"],
        "from": doc.get("from"),
        "to": doc.get("to"),
        "cc": doc.get("cc"),
        "sent_at": doc.get("sort_key"),
        "subject": doc.get("subject"),
        "body_clean": body_clean[: cfg.MAX_BODY_CHARS_PER_MESSAGE],
        "truncated": truncated,
    }


def _participant_context(db: Database, emails: list[str]) -> list[JsonDoc]:
    person_repo = PersonRepository(db)
    link_repo = PersonOrgLinkRepository(db)
    org_repo = OrganizationRepository(db)
    out = []
    for email in emails:
        links = link_repo.current_links_for_person(email)
        orgs = {link["org_domain"]: org_repo.find(link["org_domain"]) for link in links}
        out.append(
            {
                "email": email,
                "person": person_repo.find(email),
                "current_links": links,
                "organizations": [o for o in orgs.values() if o],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Profile rebuild -- shared by persist_context_delta's apply step and
# scripts/rebuild_profiles.py, so the two can never drift on what a profile
# IS: always a pure function of observations (latest observed_at wins, tie
# by recorded_at), never an incrementally-mutated field.
# ---------------------------------------------------------------------------


def latest_value_per_attribute(observations: list[JsonDoc]) -> dict[str, JsonDoc]:
    latest: dict[str, JsonDoc] = {}
    for obs in observations:
        attr = obs["attribute"]
        key = (obs["observed_at"], obs["recorded_at"])
        current = latest.get(attr)
        if current is None or key >= (current["observed_at"], current["recorded_at"]):
            latest[attr] = obs
    return latest


def _participation_entries(claimed_docs: list[JsonDoc], email: str) -> list[tuple[str, str]]:
    """(message_id, sort_key) for every claimed message this email appears
    on as a participant (from/to/cc) -- the Phase 4B source of truth for
    "a person appeared on a message" independent of whether any fact was
    ever extracted about them."""
    return [(doc["message_id"], doc["sort_key"]) for doc in claimed_docs if email in _participant_emails([doc])]


def _participation_entries_for_domain(claimed_docs: list[JsonDoc], domain: str) -> list[tuple[str, str]]:
    return [
        (doc["message_id"], doc["sort_key"])
        for doc in claimed_docs
        if any(_domain_of(e) == domain for e in _participant_emails([doc]))
    ]


def _merge_seen_at_and_count(
    existing: JsonDoc, base_message_ids: set[str], seen_values: list[str]
) -> tuple[str, str, int, list[str]]:
    """Union-merge, never increment -- re-merging the SAME claimed_docs
    twice (e.g. a crash-then-reprocess, or a straight re-persist) leaves
    participant_message_ids, and therefore message_count, unchanged."""
    participant_ids = set(existing.get("participant_message_ids") or []) | base_message_ids
    existing_first = existing.get("first_seen_at")
    existing_last = existing.get("last_seen_at")
    first_candidates = ([existing_first] if existing_first else []) + seen_values
    last_candidates = ([existing_last] if existing_last else []) + seen_values
    first_seen_at = min(first_candidates) if first_candidates else ""
    last_seen_at = max(last_candidates) if last_candidates else ""
    return first_seen_at, last_seen_at, len(participant_ids), sorted(participant_ids)


def _organization_names(observations: list[JsonDoc], existing_names: list[str], cap: int) -> list[str]:
    names = list(existing_names)
    for obs in observations:
        if obs["attribute"] == "name" and obs["value"] not in names:
            names.append(obs["value"])
    return names[:cap]


def compute_person_profile(db: Database, email: str, claimed_docs: list[JsonDoc] | None = None) -> JsonDoc:
    """Pure computation, no write -- what rebuild_person WOULD set.
    scripts/rebuild_profiles.py's dry-run diffs this against the current
    stored document without touching it. claimed_docs (optional) merges in
    this call's message participation on top of observations -- a person
    with zero extracted facts still gets counted/dated from appearing on a
    claimed message (Phase 4B)."""
    observations = ObservationRepository(db).for_entity("person", email)
    latest = latest_value_per_attribute(observations)
    profile = {attr: obs["value"] for attr, obs in latest.items()}
    existing = PersonRepository(db).find(email) or {}

    base_message_ids = {obs["source_message_id"] for obs in observations}
    seen_values = [obs["observed_at"] for obs in observations]
    if claimed_docs:
        for message_id, sort_key in _participation_entries(claimed_docs, email):
            base_message_ids.add(message_id)
            seen_values.append(sort_key)

    first_seen_at, last_seen_at, message_count, participant_message_ids = _merge_seen_at_and_count(
        existing, base_message_ids, seen_values
    )
    return {
        "profile": profile,
        "is_role_address": _is_role_address(email),
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "message_count": message_count,
        "participant_message_ids": participant_message_ids,
    }


def compute_organization_profile(db: Database, domain: str, claimed_docs: list[JsonDoc] | None = None) -> JsonDoc:
    observations = ObservationRepository(db).for_entity("organization", domain)
    latest = latest_value_per_attribute(observations)
    profile = {attr: obs["value"] for attr, obs in latest.items() if attr != "name"}
    existing = OrganizationRepository(db).find(domain) or {}

    base_message_ids = {obs["source_message_id"] for obs in observations}
    seen_values = [obs["observed_at"] for obs in observations]
    if claimed_docs:
        for message_id, sort_key in _participation_entries_for_domain(claimed_docs, domain):
            base_message_ids.add(message_id)
            seen_values.append(sort_key)

    first_seen_at, last_seen_at, message_count, participant_message_ids = _merge_seen_at_and_count(
        existing, base_message_ids, seen_values
    )
    names = _organization_names(observations, existing.get("names") or [], cfg.ORGANIZATION_NAMES_MAX_COUNT)
    return {
        "profile": profile,
        "names": names,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "message_count": message_count,
        "participant_message_ids": participant_message_ids,
    }


def rebuild_person(
    db: Database, email: str, display_name_hint: str | None = None, claimed_docs: list[JsonDoc] | None = None
) -> JsonDoc:
    computed = compute_person_profile(db, email, claimed_docs)
    existing = PersonRepository(db).find(email) or {}
    display_names = set(existing.get("display_names") or [])
    if display_name_hint:
        display_names.add(display_name_hint)
    PersonRepository(db).replace_computed_fields(
        email,
        sorted(display_names),
        computed["is_role_address"],
        computed["profile"],
        computed["first_seen_at"],
        computed["last_seen_at"],
        computed["message_count"],
        computed["participant_message_ids"],
    )
    return PersonRepository(db).find(email)


def rebuild_organization(db: Database, domain: str, claimed_docs: list[JsonDoc] | None = None) -> JsonDoc:
    computed = compute_organization_profile(db, domain, claimed_docs)
    OrganizationRepository(db).replace_computed_fields(
        domain,
        computed["names"],
        computed["profile"],
        computed["first_seen_at"],
        computed["last_seen_at"],
        computed["message_count"],
        computed["participant_message_ids"],
    )
    return OrganizationRepository(db).find(domain)


# ---------------------------------------------------------------------------
# Tool 1: claim_next_thread_for_context
# ---------------------------------------------------------------------------


def claim_next_thread_for_context(db: Database) -> JsonDoc:
    repo = RawEmailDumpRepository(db)
    now = datetime.now(timezone.utc)
    claim_cutoff_iso = (now - timedelta(minutes=cfg.CONTEXT_CLAIM_TTL_MINUTES)).isoformat()
    claim_id = uuid.uuid4().hex
    claimed = repo.claim_thread_for_context(
        claim_cutoff_iso=claim_cutoff_iso,
        max_attempts=cfg.MAX_CONTEXT_ATTEMPTS,
        claim_id=claim_id,
        now_iso=now.isoformat(),
        max_messages=cfg.MAX_MESSAGES_PER_THREAD_CLAIM,
    )
    if not claimed:
        return {"empty": True}

    thread_id = claimed[0].get("thread_id")
    thread_key = thread_id or claimed[0]["message_id"]
    claimed_ids = {d["message_id"] for d in claimed}

    participants = _participant_emails(claimed)
    previously_processed: list[JsonDoc] = []
    if thread_id:
        siblings = repo.find_many({"thread_id": thread_id})
        done_ids = [s["message_id"] for s in siblings if s.get("context_version") == 1 and s["message_id"] not in claimed_ids]
        if done_ids:
            previously_processed = EmailInsightRepository(db).find_many(done_ids)
        participants |= _participant_emails(siblings)

    return {
        "empty": False,
        "claim_id": claim_id,
        "thread_id": thread_key,
        "messages": [_message_view(d) for d in claimed],
        "previously_processed": previously_processed,
        "thread": ThreadRepository(db).find(thread_key),
        "participants": sorted(participants),
        "participant_context": _participant_context(db, sorted(participants)),
    }


# ---------------------------------------------------------------------------
# Tool 2: persist_context_delta
# ---------------------------------------------------------------------------


def _validate_delta(delta: ContextDelta, claimed_docs: list[JsonDoc]) -> list[str]:
    errors: list[str] = []
    claimed_ids = {d["message_id"] for d in claimed_docs}
    text_by_message = {
        d["message_id"]: _normalize_for_match((d.get("subject") or "") + " " + strip_quotes_and_signature(d.get("body") or ""))
        for d in claimed_docs
    }
    thread_text = " ".join(text_by_message.values())
    participants = _participant_emails(claimed_docs)
    participant_domains = {_domain_of(e) for e in participants}

    total_entities = len(delta.observations) + len(delta.person_org_links)
    if total_entities > cfg.MAX_ENTITIES_PER_DELTA:
        errors.append(f"too many entities in delta: {total_entities} > {cfg.MAX_ENTITIES_PER_DELTA}")

    for i, obs in enumerate(delta.observations):
        prefix = f"observations[{i}]"
        if obs.entity_type not in cfg.ENTITY_TYPES:
            errors.append(f"{prefix}: invalid entity_type {obs.entity_type!r}")
            continue
        if obs.source_message_id not in claimed_ids:
            errors.append(f"{prefix}: unknown message_id {obs.source_message_id!r}")
            continue
        whitelist = cfg.PERSON_ATTRIBUTE_WHITELIST if obs.entity_type == "person" else cfg.ORGANIZATION_ATTRIBUTE_WHITELIST
        if obs.attribute not in whitelist:
            errors.append(f"{prefix}: attribute {obs.attribute!r} not in whitelist for {obs.entity_type}")
            continue
        if not obs.evidence.strip() or len(obs.evidence) > cfg.EVIDENCE_MAX_LENGTH:
            errors.append(f"{prefix}: evidence invalid (empty or too long)")
            continue
        if _normalize_for_match(obs.evidence) not in text_by_message.get(obs.source_message_id, ""):
            errors.append(f"{prefix}: evidence not found in source message")
            continue
        if len(obs.value) > cfg.VALUE_MAX_LENGTH:
            errors.append(f"{prefix}: value too long")
            continue
        if obs.entity_type == "person":
            email = obs.entity_key.strip().lower()
            if not _is_valid_email_syntax(email):
                errors.append(f"{prefix}: invalid person email {obs.entity_key!r}")
                continue
            if email not in participants and _normalize_for_match(email) not in thread_text:
                errors.append(f"{prefix}: person email not in participants or evidence")
                continue
            if _is_role_address(email):
                errors.append(f"{prefix}: role address cannot receive person attributes")
                continue
        else:
            domain = obs.entity_key.strip().lower()
            if _is_free_mail(domain):
                errors.append(f"{prefix}: free-mail domain cannot be an organization")
                continue
            if domain not in participant_domains and _normalize_for_match(domain) not in thread_text:
                errors.append(f"{prefix}: organization domain not in participants or evidence")
                continue

    for i, link in enumerate(delta.person_org_links):
        prefix = f"person_org_links[{i}]"
        if link.source_message_id not in claimed_ids:
            errors.append(f"{prefix}: unknown message_id {link.source_message_id!r}")
            continue
        if not link.evidence.strip() or len(link.evidence) > cfg.EVIDENCE_MAX_LENGTH:
            errors.append(f"{prefix}: evidence invalid (empty or too long)")
            continue
        if _normalize_for_match(link.evidence) not in text_by_message.get(link.source_message_id, ""):
            errors.append(f"{prefix}: evidence not found in source message")
            continue
        email = link.person_email.strip().lower()
        if not _is_valid_email_syntax(email):
            errors.append(f"{prefix}: invalid person email {link.person_email!r}")
            continue
        if email not in participants and _normalize_for_match(email) not in thread_text:
            errors.append(f"{prefix}: person email not in participants or evidence")
            continue
        if _is_role_address(email):
            errors.append(f"{prefix}: role address cannot receive person attributes")
            continue
        domain = link.org_domain.strip().lower()
        if _is_free_mail(domain):
            errors.append(f"{prefix}: free-mail domain cannot be an organization")
            continue
        if domain not in participant_domains and _normalize_for_match(domain) not in thread_text:
            errors.append(f"{prefix}: organization domain not in participants or evidence")
            continue
        if not link.valid_from.strip():
            errors.append(f"{prefix}: valid_from is required")
            continue
        if link.role_title and len(link.role_title) > cfg.VALUE_MAX_LENGTH:
            errors.append(f"{prefix}: role_title too long")
            continue

    if delta.thread is not None:
        if delta.thread.status is not None and delta.thread.status not in cfg.THREAD_STATUSES:
            errors.append(f"thread.status: invalid status {delta.thread.status!r}")
        if delta.thread.summary is not None and len(delta.thread.summary) > cfg.THREAD_SUMMARY_MAX_LENGTH:
            errors.append("thread.summary: too long")
        for i, item in enumerate(delta.thread.open_items or []):
            prefix = f"thread.open_items[{i}]"
            if item.owner not in cfg.OPEN_ITEM_OWNERS:
                errors.append(f"{prefix}: invalid owner {item.owner!r}")
                continue
            if item.source_message_id not in claimed_ids:
                errors.append(f"{prefix}: unknown message_id {item.source_message_id!r}")
                continue
            if not item.evidence.strip() or len(item.evidence) > cfg.EVIDENCE_MAX_LENGTH:
                errors.append(f"{prefix}: evidence invalid (empty or too long)")
                continue
            if _normalize_for_match(item.evidence) not in thread_text:
                errors.append(f"{prefix}: evidence not found in thread")
                continue
            if len(item.text) > cfg.VALUE_MAX_LENGTH:
                errors.append(f"{prefix}: text too long")

    for i, ei in enumerate(delta.email_insights):
        prefix = f"email_insights[{i}]"
        if ei.message_id not in claimed_ids:
            errors.append(f"{prefix}: unknown message_id {ei.message_id!r}")
            continue
        if ei.intent not in cfg.INTENTS:
            errors.append(f"{prefix}: invalid intent {ei.intent!r}")
        if ei.sentiment not in cfg.SENTIMENTS:
            errors.append(f"{prefix}: invalid sentiment {ei.sentiment!r}")
        if ei.sender_type not in cfg.SENDER_TYPES:
            errors.append(f"{prefix}: invalid sender_type {ei.sender_type!r}")
        if len(ei.summary) > cfg.EMAIL_INSIGHT_SUMMARY_MAX_LENGTH:
            errors.append(f"{prefix}: summary too long")
        if not ei.evidence.strip() or len(ei.evidence) > cfg.EVIDENCE_MAX_LENGTH:
            errors.append(f"{prefix}: evidence invalid (empty or too long)")
        elif _normalize_for_match(ei.evidence) not in text_by_message.get(ei.message_id, ""):
            errors.append(f"{prefix}: evidence not found in source message")

    return errors


def _apply_thread_delta(db: Database, thread_key: str, thread_input: ThreadDeltaInput | None, claimed_docs: list[JsonDoc]) -> None:
    repo = ThreadRepository(db)
    existing = repo.find(thread_key) or {}
    participants = sorted(_participant_emails(claimed_docs) | set(existing.get("participants") or []))
    message_count = len(RawEmailDumpRepository(db).find_many({"thread_id": thread_key})) or 1
    fields: JsonDoc = {
        "participants": participants,
        "last_message_sort_key": max(d["sort_key"] for d in claimed_docs),
        "message_count": message_count,
        "summary_basis_through": max(d["sort_key"] for d in claimed_docs),
        "needs_rebuild": False,
        "context_version": existing.get("context_version", 0) + 1,
    }
    if thread_input is not None:
        if thread_input.status is not None:
            fields["status"] = thread_input.status
        if thread_input.summary is not None:
            fields["summary"] = _strip_control_chars(thread_input.summary)
        if thread_input.open_items is not None:
            fields["open_items"] = [
                {
                    "text": _strip_control_chars(oi.text),
                    "owner": oi.owner,
                    "evidence": oi.evidence,
                    "source_message_id": oi.source_message_id,
                }
                for oi in thread_input.open_items
            ]
    repo.upsert(thread_key, fields)


def persist_context_delta(db: Database, claim_id: str, thread_id: str | None, delta: dict[str, Any]) -> JsonDoc:
    repo = RawEmailDumpRepository(db)
    claimed_docs = repo.find_by_context_claim_id(claim_id)
    if not claimed_docs:
        return {"ok": False, "reason": "claim_lost"}

    try:
        parsed = ContextDelta.model_validate(delta)
    except ValidationError as exc:
        return {"ok": False, "reason": "validation_failed", "errors": [str(e) for e in exc.errors()]}

    errors = _validate_delta(parsed, claimed_docs)
    if errors:
        return {"ok": False, "reason": "validation_failed", "errors": errors}

    now_iso = _now_iso()
    sort_key_by_message = {d["message_id"]: d["sort_key"] for d in claimed_docs}
    claimed_ids = {d["message_id"] for d in claimed_docs}

    # (a) append observations -- idempotent via the unique index.
    obs_repo = ObservationRepository(db)
    touched_people: set[str] = set()
    touched_orgs: set[str] = set()
    for obs in parsed.observations:
        observed_at = sort_key_by_message[obs.source_message_id]
        entity_key = obs.entity_key.strip().lower()
        obs_repo.append(
            {
                "entity_type": obs.entity_type,
                "entity_key": entity_key,
                "attribute": obs.attribute,
                "value": _strip_control_chars(obs.value),
                "evidence": obs.evidence,
                "source_message_id": obs.source_message_id,
                "source_thread_id": thread_id,
                "observed_at": observed_at,
                "recorded_at": now_iso,
            }
        )
        (touched_people if obs.entity_type == "person" else touched_orgs).add(entity_key)

    # (b) upsert people/organizations/links/threads/email_insights.
    link_repo = PersonOrgLinkRepository(db)
    for link in parsed.person_org_links:
        observed_at = sort_key_by_message[link.source_message_id]
        person_email = link.person_email.strip().lower()
        org_domain = link.org_domain.strip().lower()
        link_id = f"{person_email}|{org_domain}|{link.valid_from}"
        link_repo.upsert_link(
            link_id,
            {
                "person_email": person_email,
                "org_domain": org_domain,
                "role_title": _strip_control_chars(link.role_title) if link.role_title else None,
                "valid_from": link.valid_from,
                "valid_to": observed_at if link.close_existing else None,
                "last_seen_at": observed_at,
                "evidence": link.evidence,
                "source_message_id": link.source_message_id,
            },
        )
        touched_people.add(person_email)
        touched_orgs.add(org_domain)

    # Phase 4B: every participant of a claimed message gets/updates a
    # people doc even with zero extracted facts (role addresses flagged
    # is_role_address, still no attributes -- validation already rejects
    # any observation naming one). Same for organizations, scoped to
    # non-free-mail participant domains only.
    all_participants = _participant_emails(claimed_docs)
    touched_people |= all_participants
    touched_orgs |= {_domain_of(e) for e in all_participants if not _is_free_mail(_domain_of(e))}

    for email in touched_people:
        rebuild_person(db, email, _derive_display_name(claimed_docs, email), claimed_docs=claimed_docs)
    for domain in touched_orgs:
        rebuild_organization(db, domain, claimed_docs=claimed_docs)

    thread_key = thread_id or claimed_docs[0]["message_id"]
    _apply_thread_delta(db, thread_key, parsed.thread, claimed_docs)

    for ei in parsed.email_insights:
        EmailInsightRepository(db).upsert(
            ei.message_id,
            {
                "intent": ei.intent,
                "sentiment": ei.sentiment,
                "sender_type": ei.sender_type,
                "summary": _strip_control_chars(ei.summary),
                "evidence": ei.evidence,
            },
        )

    # (c) append a threads_history snapshot.
    thread_doc = ThreadRepository(db).find(thread_key) or {}
    ThreadHistoryRepository(db).append_snapshot(
        thread_key,
        {
            "snapshot_at": now_iso,
            "status": thread_doc.get("status"),
            "summary": thread_doc.get("summary"),
            "open_items": thread_doc.get("open_items"),
            "context_version": thread_doc.get("context_version"),
        },
        cfg.THREAD_HISTORY_MAX_SNAPSHOTS,
    )

    # (d) LAST: release the claim on every claimed message. If a crash
    # happened before this point, (a)-(c) already ran safely/idempotently --
    # the lease expires and the thread is reprocessed. If the claim was lost
    # (lease expired and reclaimed between the read above and here), this
    # still reports claim_lost even though (a)-(c) already applied, matching
    # the spec's own stated crash-safety invariant.
    released_ok = True
    for message_id in claimed_ids:
        updated = repo.update_if_claim_matches(
            message_id,
            claim_id,
            {
                "context_version": 1,
                "context_claim_id": None,
                "context_claimed_at": "",
                "context_attempts": 0,
                "context_last_error": None,
            },
            claim_field="context_claim_id",
        )
        if updated is None:
            released_ok = False
    if not released_ok:
        return {"ok": False, "reason": "claim_lost"}
    return {"ok": True, "thread_id": thread_key, "messages_processed": sorted(claimed_ids)}


# ---------------------------------------------------------------------------
# Tool 3: mark_context_failed
# ---------------------------------------------------------------------------


def mark_context_failed(db: Database, claim_id: str, thread_id: str | None, reason: str) -> JsonDoc:
    repo = RawEmailDumpRepository(db)
    claimed_docs = repo.find_by_context_claim_id(claim_id)
    if not claimed_docs:
        return {"ok": False, "reason": "claim_lost"}

    truncated_reason = reason[: cfg.CONTEXT_LAST_ERROR_MAX_LENGTH]
    ok = True
    for doc in claimed_docs:
        updated = repo.update_if_claim_matches(
            doc["message_id"],
            claim_id,
            {
                "context_attempts": doc.get("context_attempts", 0) + 1,
                "context_last_error": truncated_reason,
                "context_claim_id": None,
                "context_claimed_at": "",
            },
            claim_field="context_claim_id",
        )
        if updated is None:
            ok = False
    if not ok:
        return {"ok": False, "reason": "claim_lost"}
    return {"ok": True, "thread_id": thread_id}


# ---------------------------------------------------------------------------
# Tool 4: get_stuck_context
# ---------------------------------------------------------------------------


def get_stuck_context(db: Database, limit: int = 20) -> list[JsonDoc]:
    repo = RawEmailDumpRepository(db)
    return repo.find_many({"context_attempts": {"$gte": cfg.MAX_CONTEXT_ATTEMPTS}}, limit=limit, sort=[("message_id", 1)])
