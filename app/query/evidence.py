"""Phase 22A: converts raw retrieved MongoDB records into typed EvidenceItems.

Two provenance axes are kept deliberately separate, never conflated (Section L):
  - IdentityBasis: how confidently a record's person_id/org_id LINK was established
    (canonical vs. legacy thread-scoped name matching vs. unresolved).
  - KnowledgeItem's own stored `basis` field ("stated"/"inferred"): whether the FACT
    itself is a direct statement or an inference -- preserved verbatim in
    summary_fields, never rewritten or upgraded.
"""

from datetime import datetime
from typing import Any

from app.query.schemas import EvidenceItem, IdentityBasis

_CONTEXT_BASIS_MAP = {
    "canonical_person_id": IdentityBasis.CANONICAL,
    "canonical_org_id": IdentityBasis.CANONICAL,
    "canonical_id": IdentityBasis.CANONICAL,
    "legacy_thread_scoped_name_match": IdentityBasis.LEGACY_THREAD_SCOPED,
    "thread_scoped_only": IdentityBasis.LEGACY_THREAD_SCOPED,
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_evidence_items(
    collection: str, records: list[dict[str, Any]], record_id_field: str, summary_fields: list[str],
    timestamp_field: str | None = None, basis: IdentityBasis = IdentityBasis.CANONICAL,
) -> list[EvidenceItem]:
    """For records retrieved by a DIRECT canonical filter (person_id=X in the Mongo
    query itself, as every app.query.retrieval function except the Person/Org-360
    wrappers does) -- every returned record already matched an exact canonical id,
    so `basis` defaults to CANONICAL. Never called with legacy-fallback results;
    build_evidence_from_context_items is the one that handles those.
    """
    items = []
    for record in records:
        record_id = str(record.get(record_id_field, ""))
        items.append(
            EvidenceItem(
                collection=collection, record_id=record_id, basis=basis,
                thread_id=record.get("thread_id"),
                person_id=record.get("person_id") or _first_or_none(record.get("person_ids")),
                org_id=record.get("org_id") or _first_or_none(record.get("org_ids")),
                project_id=record.get("project_id"),
                source_message_id=record.get("source_email_id") or record.get("source_record"),
                timestamp=_parse_iso(record.get(timestamp_field)) if timestamp_field else None,
                summary_fields={k: record.get(k) for k in summary_fields if k in record},
            )
        )
    return items


def build_evidence_from_context_items(
    collection: str, tagged_items: list[dict[str, Any]], record_id_field: str, summary_fields: list[str],
    timestamp_field: str | None = None,
) -> list[EvidenceItem]:
    """For items already tagged by app.entities.context.get_person_context/
    get_organization_context with their own `"basis"` string -- translated into
    IdentityBasis, never re-derived independently of what context.py already
    determined.
    """
    items = []
    for record in tagged_items:
        basis = _CONTEXT_BASIS_MAP.get(record.get("basis", ""), IdentityBasis.UNRESOLVED)
        record_id = str(record.get(record_id_field, ""))
        items.append(
            EvidenceItem(
                collection=collection, record_id=record_id, basis=basis,
                thread_id=record.get("thread_id"),
                person_id=record.get("person_id") or _first_or_none(record.get("person_ids")),
                org_id=record.get("org_id") or _first_or_none(record.get("org_ids")),
                project_id=record.get("project_id"),
                source_message_id=record.get("source_email_id") or record.get("source_record"),
                timestamp=_parse_iso(record.get(timestamp_field)) if timestamp_field else None,
                summary_fields={k: record.get(k) for k in summary_fields if k in record},
            )
        )
    return items


def _first_or_none(values: list[Any] | None) -> Any | None:
    return values[0] if values else None
