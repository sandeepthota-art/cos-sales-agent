"""MongoDB -> Markdown knowledge projection. MongoDB remains the source of truth;
this module only ever READS from it and writes deterministic .md files under
KNOWLEDGE_DIR. It never writes to MongoDB, and is fully decoupled from
app.pipeline.run_pipeline and app.scheduler -- neither is imported or modified here.

Canonical IDs: reused as-is from the existing entities (PER-/PRJ-/CMT-/FUP-/MTG-/PSN-,
minted via app.entities.ids.next_id and never re-minted here) and from emails/threads'
own real identifiers (message_id/thread_id -- there is no separate MSG-/THR- prefix
system anywhere in this codebase).

Organization is NOT a stored MongoDB entity -- there is no collection, no counter
prefix, no resolution logic for it (only free-text Person.org / Project.entity
strings exist). Rather than inventing a persisted canonical entity for something the
application doesn't actually have, Organization nodes are DERIVED at projection time
only: a deterministic slug id (ORG-<slug>, e.g. ORG-the-614-group) computed by
exact-matching those free-text strings -- the same join logic app.mcp.tools'
get_company_summary already uses. Every organization .md file says so explicitly.

Relationships: most of this schema has no direct FK between entity types (confirmed
elsewhere in this project: Commitment.project_id, Project.owner/collaborators,
Person.reports_to are schema fields the live pipeline never populates;
Commitment.owed_by/owed_to and Meeting.attendees are free text, not ids). The one
rich, fully real relationship source is email["entities_referenced"] -- every
processed email already lists exactly which people/projects/commitments/follow_ups/
meetings/personal items it produced, together. Any two entity ids appearing in the
same email's entities_referenced get a co-occurrence edge, with that email's
message_id recorded as literal provenance -- this is reading data already stored, not
inference. Genuine direct FKs (FollowUp.commitment_id, Commitment.source_record, the
bolted-on thread_id on Commitment/Meeting, Person.open_threads, Email->Thread via
threads.message_ids, PersonalItem->Person via an exact sender_email==Person.email
match) are layered on top and marked as such.

Incremental projection: each entity's intended Markdown content is rendered in memory
first; a file is only written if that content differs from what's already on disk
(byte-for-byte). No "Generated at: ..." timestamps anywhere, so an unchanged MongoDB
state always re-renders identical bytes and triggers zero writes.

CLI usage:
    python -m app.knowledge_projector          # one-shot projection
    python -m app.knowledge_projector --once    # same (only mode supported today)
"""

import argparse
import itertools
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pymongo.database import Database

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.database.repositories import (
    CommitmentRepository,
    EmailRepository,
    FollowUpRepository,
    MeetingRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
    ThreadRepository,
)

logger = logging.getLogger(__name__)

_ENTITY_REF_KEYS = ("people", "projects", "commitments", "follow_ups", "meetings", "personal")
_TYPE_DIR = {
    "people": "people", "projects": "projects", "commitments": "commitments",
    "follow_ups": "followups", "meetings": "meetings", "personal": "personal_items",
}
_COOCCURRENCE_LABELS: dict[frozenset[str], str] = {
    frozenset({"people", "projects"}): "INVOLVED_IN",
    frozenset({"people", "commitments"}): "HAS_COMMITMENT",
    frozenset({"people", "follow_ups"}): "HAS_FOLLOWUP",
    frozenset({"people", "meetings"}): "HAS_MEETING",
    frozenset({"projects", "commitments"}): "HAS_COMMITMENT",
    frozenset({"projects", "follow_ups"}): "HAS_FOLLOWUP",
    frozenset({"projects", "meetings"}): "HAS_MEETING",
}
_DEFAULT_COOCCURRENCE_LABEL = "MENTIONED_WITH"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "unknown"


def organization_id(name: str) -> str:
    return f"ORG-{slugify(name)}"


# --- data loading -----------------------------------------------------------------


def _load_all(db: Database) -> dict[str, list[dict[str, Any]]]:
    return {
        "people": PersonRepository(db).find_many({}),
        "projects": ProjectRepository(db).find_many({}),
        "commitments": CommitmentRepository(db).find_many({}),
        "follow_ups": FollowUpRepository(db).find_many({}),
        "meetings": MeetingRepository(db).find_many({}),
        "personal": PersonalItemRepository(db).find_many({}),
        "emails": EmailRepository(db).find_many({}),
        "threads": ThreadRepository(db).find_many({}),
    }


# --- relationship building ----------------------------------------------------------


def _build_cooccurrence(emails: list[dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    """edges[entity_id][other_entity_id] = set of message_ids where both appeared
    together in that email's entities_referenced. Symmetric by construction
    (itertools.permutations yields both directions)."""
    edges: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    id_type: dict[str, str] = {}
    for email in emails:
        refs = email.get("entities_referenced") or {}
        message_id = email["message_id"]
        tagged: list[tuple[str, str]] = []
        for key in _ENTITY_REF_KEYS:
            for entity_id in refs.get(key) or []:
                tagged.append((key, entity_id))
                id_type[entity_id] = key
        for (_, a), (_, b) in itertools.permutations(tagged, 2):
            edges[a][b].add(message_id)
    return edges


def _cooccurrence_label(id_type: dict[str, str], a: str, b: str) -> str:
    type_a, type_b = id_type.get(a), id_type.get(b)
    if type_a and type_b:
        label = _COOCCURRENCE_LABELS.get(frozenset({type_a, type_b}))
        if label:
            return label
    return _DEFAULT_COOCCURRENCE_LABEL


class RelationshipGraph:
    """Read-only view over one snapshot of MongoDB data, answering "what edges does
    this entity id have" for the Markdown renderers below. Built once per projection
    run from _load_all()'s output -- never persisted, never written back to Mongo."""

    def __init__(self, data: dict[str, list[dict[str, Any]]]):
        self.data = data
        self.by_id: dict[str, dict[str, Any]] = {}
        self.id_type: dict[str, str] = {}
        for key in ("people", "projects", "commitments", "follow_ups", "meetings", "personal"):
            for doc in data[key]:
                entity_id = doc.get("id")
                if not entity_id:
                    # A malformed document missing its own id can't be an addressable
                    # graph node -- skip it here so the rest of the graph still builds;
                    # the per-entity render loop in project_once() independently
                    # catches and reports this same document as a projection error.
                    logger.warning("skipping malformed %s document with no id: %r", key, doc)
                    continue
                self.by_id[entity_id] = doc
                self.id_type[entity_id] = key

        self.threads_by_id = {t["thread_id"]: t for t in data["threads"]}
        self.thread_for_message: dict[str, str] = {}
        for thread in data["threads"]:
            for message_id in thread["message_ids"]:
                self.thread_for_message[message_id] = thread["thread_id"]

        self.emails_by_message_id = {e["message_id"]: e for e in data["emails"]}
        self.cooccurrence = _build_cooccurrence(data["emails"])

        # Person org / Project entity -> derived Organization membership.
        self.org_members: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: {"people": [], "projects": []}
        )
        for person in data["people"]:
            if person.get("org"):
                self.org_members[organization_id(person["org"])]["people"].append(person["id"])
        for project in data["projects"]:
            if project.get("entity"):
                self.org_members[organization_id(project["entity"])]["projects"].append(project["id"])

        # PersonalItem -> Person, exact sender_email == Person.email match only.
        self.person_by_email = {p["email"].lower(): p["id"] for p in data["people"] if p.get("email")}

    def cooccurring(self, entity_id: str) -> list[tuple[str, str, list[str]]]:
        """Returns [(label, other_id, sorted source message_ids)], sorted for
        deterministic output."""
        edges = self.cooccurrence.get(entity_id, {})
        results = []
        for other_id, message_ids in edges.items():
            label = _cooccurrence_label(self.id_type, entity_id, other_id)
            results.append((label, other_id, sorted(message_ids)))
        results.sort(key=lambda r: (r[0], r[1]))
        return results

    def organizations(self) -> list[str]:
        return sorted(self.org_members)


# --- markdown rendering helpers ------------------------------------------------------


def _render(title: str, sections: list[tuple[str, list[str]]]) -> str:
    lines = [f"# {title}", ""]
    for heading, body_lines in sections:
        lines.append(f"## {heading}")
        if body_lines:
            lines.extend(body_lines)
        else:
            lines.append("- (none)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _relationship_lines(
    direct: list[tuple[str, str]], cooccurring: list[tuple[str, str, list[str]]]
) -> list[str]:
    lines = [f"- {label} -> {target}" for label, target in sorted(direct)]
    for label, target, sources in cooccurring:
        lines.append(f"- {label} -> {target} (derived: co-occurred in {', '.join(sources)})")
    return lines


def render_person(person: dict[str, Any], graph: RelationshipGraph) -> str:
    pid = person["id"]
    direct: list[tuple[str, str]] = []
    org = None
    if person.get("org"):
        org = organization_id(person["org"])
        direct.append(("WORKS_AT", org))
    for thread_id in person.get("open_threads") or []:
        direct.append(("MEMBER_OF_THREAD", thread_id))

    entity_lines = [
        f"- ID: {pid}", "- Type: Person", f"- Name: {person.get('name')}",
        f"- Email: {person.get('email')}", f"- Organization: {org or '(none)'}",
    ]
    return _render(
        f"{pid} — {person.get('name')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(pid))),
            ("Provenance", [f"- Source: {person.get('source', 'unknown')}"]),
        ],
    )


def render_organization(org_id: str, name: str, graph: RelationshipGraph) -> str:
    members = graph.org_members[org_id]
    direct = [("HAS_MEMBER", pid) for pid in sorted(members["people"])]
    direct += [("HAS_PROJECT", prid) for prid in sorted(members["projects"])]
    return _render(
        f"{org_id} — {name}",
        [
            ("Entity", [f"- ID: {org_id}", "- Type: Organization (derived)", f"- Name: {name}"]),
            ("Relationships", _relationship_lines(direct, [])),
            (
                "Provenance",
                [
                    "- Derived from exact-matching Person.org / Project.entity strings.",
                    "- NOT a stored MongoDB entity -- no organizations collection exists.",
                ],
            ),
        ],
    )


def render_project(project: dict[str, Any], graph: RelationshipGraph) -> str:
    prid = project["id"]
    direct: list[tuple[str, str]] = []
    if project.get("entity"):
        direct.append(("BELONGS_TO", organization_id(project["entity"])))
    entity_lines = [
        f"- ID: {prid}", "- Type: Project", f"- Name: {project.get('project')}",
        f"- Entity: {project.get('entity')}", f"- Goal pillar: {project.get('goal_pillar')}",
        f"- Status: {project.get('status')}",
    ]
    return _render(
        f"{prid} — {project.get('project')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(prid))),
            ("Provenance", [f"- Source: {project.get('source', 'unknown')}"]),
        ],
    )


def render_commitment(commitment: dict[str, Any], graph: RelationshipGraph) -> str:
    cid = commitment["id"]
    direct: list[tuple[str, str]] = []
    if commitment.get("source_record"):
        direct.append(("DERIVED_FROM", commitment["source_record"]))
    if commitment.get("thread_id"):
        direct.append(("MEMBER_OF_THREAD", commitment["thread_id"]))
    if commitment.get("project_id"):
        direct.append(("BELONGS_TO", commitment["project_id"]))

    entity_lines = [
        f"- ID: {cid}", "- Type: Commitment", f"- What: {commitment.get('what')}",
        f"- Class: {commitment.get('class')}", f"- Owed by: {commitment.get('owed_by')}",
        f"- Owed to: {commitment.get('owed_to')}", f"- Status: {commitment.get('status')}",
        f"- Date type: {commitment.get('date_type')}",
    ]
    provenance = [
        f"- Source record (email): {commitment.get('source_record')}",
        f"- Made on: {commitment.get('made_on')}",
        f"- Committed date: {commitment.get('committed_date')}",
    ]
    return _render(
        f"{cid} — {commitment.get('what')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(cid))),
            ("Provenance", provenance),
        ],
    )


def render_follow_up(follow_up: dict[str, Any], graph: RelationshipGraph) -> str:
    fid = follow_up["id"]
    direct: list[tuple[str, str]] = []
    if follow_up.get("commitment_id"):
        direct.append(("DERIVED_FROM", follow_up["commitment_id"]))
    if follow_up.get("thread_id"):
        direct.append(("MEMBER_OF_THREAD", follow_up["thread_id"]))

    entity_lines = [
        f"- ID: {fid}", "- Type: FollowUp",
        f"- Commitment: {follow_up.get('commitment_id') or '(none)'}",
        f"- Thread: {follow_up.get('thread_id') or '(none)'}",
    ]
    return _render(
        f"{fid} — Follow-up",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(fid))),
            ("Provenance", ["- FollowUp has no independent date/status field in the current schema."]),
        ],
    )


def render_meeting(meeting: dict[str, Any], graph: RelationshipGraph) -> str:
    mid = meeting["id"]
    direct: list[tuple[str, str]] = []
    if meeting.get("thread_id"):
        direct.append(("MEMBER_OF_THREAD", meeting["thread_id"]))

    entity_lines = [
        f"- ID: {mid}", "- Type: Meeting", f"- Date: {meeting.get('date')}",
        f"- Actionable: {meeting.get('actionable')}",
        f"- Attendees (free text, not linked to Person ids): {', '.join(meeting.get('attendees') or []) or '(none)'}",
    ]
    return _render(
        f"{mid} — Meeting",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(mid))),
            ("Provenance", [f"- Thread: {meeting.get('thread_id')}"]),
        ],
    )


def render_personal_item(item: dict[str, Any], graph: RelationshipGraph) -> str:
    iid = item["id"]
    direct: list[tuple[str, str]] = []
    sender = (item.get("sender_email") or "").lower()
    person_id = graph.person_by_email.get(sender)
    if person_id:
        direct.append(("SUBMITTED_BY", person_id))

    entity_lines = [
        f"- ID: {iid}", "- Type: PersonalItem", f"- Item type: {item.get('type')}",
        f"- Description: {item.get('description')}", f"- Status: {item.get('status')}",
        f"- Date/deadline: {item.get('date_or_deadline')}",
    ]
    provenance = [f"- Sender email: {item.get('sender_email')}"]
    if person_id:
        provenance.append(f"- SUBMITTED_BY -> {person_id} (derived: exact sender_email match)")
    return _render(
        f"{iid} — {item.get('type')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, graph.cooccurring(iid))),
            ("Provenance", provenance),
        ],
    )


def render_email(email: dict[str, Any], graph: RelationshipGraph) -> str:
    message_id = email["message_id"]
    thread_id = graph.thread_for_message.get(message_id)
    direct: list[tuple[str, str]] = []
    if thread_id:
        direct.append(("MEMBER_OF_THREAD", thread_id))
    refs = email.get("entities_referenced") or {}
    for key in _ENTITY_REF_KEYS:
        for entity_id in refs.get(key) or []:
            direct.append(("MENTIONS", entity_id))

    entity_lines = [
        f"- Message ID: {message_id}", f"- Subject: {email.get('subject')}",
        f"- From: {(email.get('from') or {}).get('email')}",
        f"- To: {', '.join(a.get('email', '') for a in email.get('to') or [])}",
        f"- Timestamp: {email.get('timestamp')}",
        f"- Processing stage: {(email.get('processing_status') or {}).get('stage')}",
    ]
    return _render(
        f"{message_id} — {email.get('subject')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(sorted(set(direct)), [])),
            ("Provenance", ["- Generated from MongoDB emails collection."]),
        ],
    )


def render_thread(thread: dict[str, Any]) -> str:
    thread_id = thread["thread_id"]
    entity_lines = [
        f"- Thread ID: {thread_id}", f"- Subject: {thread.get('normalized_subject')}",
        f"- Participants: {', '.join(thread.get('participant_emails') or [])}",
        f"- Last message at: {thread.get('last_message_at')}",
    ]
    direct = [("CONTAINS", mid) for mid in sorted(thread.get("message_ids") or [])]
    return _render(
        f"{thread_id} — {thread.get('normalized_subject')}",
        [
            ("Entity", entity_lines),
            ("Relationships", _relationship_lines(direct, [])),
            ("Provenance", ["- Generated from MongoDB threads collection."]),
        ],
    )


_RENDERERS: dict[str, Any] = {
    "people": lambda doc, graph: render_person(doc, graph),
    "projects": lambda doc, graph: render_project(doc, graph),
    "commitments": lambda doc, graph: render_commitment(doc, graph),
    "follow_ups": lambda doc, graph: render_follow_up(doc, graph),
    "meetings": lambda doc, graph: render_meeting(doc, graph),
    "personal": lambda doc, graph: render_personal_item(doc, graph),
}


# --- index files ---------------------------------------------------------------------


def render_type_index(title: str, docs: list[dict[str, Any]], label_field: str) -> str:
    # A malformed document with no id was already reported as a projection error by
    # the per-entity render loop -- it's simply not addressable as an index entry.
    valid_docs = [doc for doc in docs if doc.get("id")]
    lines = [f"- {doc['id']} — {doc.get(label_field)}" for doc in sorted(valid_docs, key=lambda d: d["id"])]
    return _render(title, [("Entries", lines)])


def render_relationships_index(graph: RelationshipGraph) -> str:
    lines: list[str] = []
    for entity_id in sorted(graph.by_id):
        for label, other_id, _sources in graph.cooccurring(entity_id):
            lines.append(f"- {entity_id} --{label}--> {other_id}")
    for person in graph.data["people"]:
        if person.get("org"):
            lines.append(f"- {person['id']} --WORKS_AT--> {organization_id(person['org'])}")
    for project in graph.data["projects"]:
        if project.get("entity"):
            lines.append(f"- {project['id']} --BELONGS_TO--> {organization_id(project['entity'])}")
    return _render("Relationships", [("Edges", sorted(set(lines)))])


# --- incremental writer ----------------------------------------------------------------


class ProjectionReport:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[str] = []
        self.unchanged: list[str] = []
        self.errors: list[tuple[str, str]] = []

    def summary(self) -> str:
        return (
            f"created={len(self.created)} updated={len(self.updated)} "
            f"unchanged={len(self.unchanged)} errors={len(self.errors)}"
        )


def _write_if_changed(path: Path, content: str, report: ProjectionReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel = str(path)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        report.unchanged.append(rel)
        return
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    (report.updated if existed else report.created).append(rel)


# --- orchestrator ----------------------------------------------------------------------


class KnowledgeProjector:
    def __init__(self, db: Database, output_dir: str):
        self._db = db
        self._root = Path(output_dir)

    def project_once(self) -> ProjectionReport:
        report = ProjectionReport()
        data = _load_all(self._db)
        graph = RelationshipGraph(data)

        for key, renderer in _RENDERERS.items():
            dir_name = _TYPE_DIR[key]
            for doc in data[key]:
                try:
                    content = renderer(doc, graph)
                    self._write(dir_name, doc["id"], content, report)
                except Exception as exc:
                    logger.exception("failed to project %s %s", key, doc.get("id"))
                    report.errors.append((f"{key}:{doc.get('id')}", str(exc)))

        for email in data["emails"]:
            try:
                content = render_email(email, graph)
                self._write("emails", email["message_id"], content, report)
            except Exception as exc:
                logger.exception("failed to project email %s", email.get("message_id"))
                report.errors.append((f"email:{email.get('message_id')}", str(exc)))

        for thread in data["threads"]:
            try:
                content = render_thread(thread)
                self._write("threads", thread["thread_id"], content, report)
            except Exception as exc:
                logger.exception("failed to project thread %s", thread.get("thread_id"))
                report.errors.append((f"thread:{thread.get('thread_id')}", str(exc)))

        for org_id in graph.organizations():
            members = graph.org_members[org_id]
            source_person = next(
                (p for p in data["people"] if p.get("org") and organization_id(p["org"]) == org_id),
                None,
            )
            source_project = next(
                (p for p in data["projects"] if p.get("entity") and organization_id(p["entity"]) == org_id),
                None,
            )
            name = (source_person or {}).get("org") or (source_project or {}).get("entity") or org_id
            content = render_organization(org_id, name, graph)
            self._write("organizations", org_id, content, report)
            _ = members  # already folded into render_organization via graph

        self._write_index(data, graph, report)
        logger.info("projection complete: %s", report.summary())
        return report

    def _write(self, type_dir: str, entity_id: str, content: str, report: ProjectionReport) -> None:
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", entity_id)
        _write_if_changed(self._root / type_dir / f"{safe_id}.md", content, report)

    def _write_index(
        self, data: dict[str, list[dict[str, Any]]], graph: RelationshipGraph, report: ProjectionReport
    ) -> None:
        index_specs = [
            ("people", "people.md", data["people"], "name"),
            ("projects", "projects.md", data["projects"], "project"),
            ("commitments", "commitments.md", data["commitments"], "what"),
            ("follow_ups", "followups.md", data["follow_ups"], "id"),
            ("meetings", "meetings.md", data["meetings"], "date"),
        ]
        for title, filename, docs, label_field in index_specs:
            content = render_type_index(title.replace("_", " ").title(), docs, label_field)
            _write_if_changed(self._root / "index" / filename, content, report)

        org_lines = [f"- {oid} — {oid}" for oid in graph.organizations()]
        _write_if_changed(
            self._root / "index" / "organizations.md",
            _render("Organizations", [("Entries", org_lines)]),
            report,
        )
        _write_if_changed(
            self._root / "index" / "relationships.md",
            render_relationships_index(graph),
            report,
        )


# --- CLI ----------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent -- MongoDB to Markdown knowledge projector")
    parser.add_argument(
        "--once", action="store_true",
        help="Run one projection pass and exit (currently the only supported mode)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Override the output directory (defaults to settings.knowledge_dir)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    output_dir = args.output or settings.knowledge_dir
    projector = KnowledgeProjector(db, output_dir)
    report = projector.project_once()

    print("## KNOWLEDGE PROJECTION COMPLETE")
    print()
    print(f"Created:   {len(report.created)}")
    print(f"Updated:   {len(report.updated)}")
    print(f"Unchanged: {len(report.unchanged)}")
    print(f"Errors:    {len(report.errors)}")
    if report.errors:
        print()
        print("Errors:")
        for entity_ref, message in report.errors:
            print(f"  {entity_ref}: {message}")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
