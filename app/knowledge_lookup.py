"""Read-only relationship/query layer over the Markdown knowledge projection
(data/knowledge/, written by app.knowledge_projector). This module never touches
MongoDB, run_pipeline, or the scheduler -- it only parses the deterministic .md files
the projector already writes. MongoDB remains the system's source of truth; Markdown
is a derived projection; this class is a query layer over that projection.

The public interface (find_entity, find_people, find_projects, get_relationships,
get_related_entities, get_neighbors) is deliberately backing-store-agnostic: a future
swap to a real graph database only needs a new implementation behind this same
interface, not a change to every caller.

Search strategy, in order (no LLM, no vector search, no fuzzy matching): canonical
id -> exact email -> exact normalized name -> exact normalized project name -> safe
substring match as a last resort.
"""

import re
from pathlib import Path
from typing import Any

_TYPE_DIR = {
    "PER": "people", "PRJ": "projects", "CMT": "commitments",
    "FUP": "followups", "MTG": "meetings", "PSN": "personal_items", "ORG": "organizations",
}
_CANONICAL_ID_PATTERN = re.compile(r"^([A-Z]+)-")
_RELATIONSHIP_LINE = re.compile(
    r"^- (?P<label>[A-Z_]+) -> (?P<target>\S+)(?: \(derived: co-occurred in (?P<sources>.+)\))?$"
)
_ENTITY_LINE = re.compile(r"^- ([^:]+): (.*)$")
_MAX_DEPTH = 5


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _entity_type_from_id(entity_id: str) -> str | None:
    match = _CANONICAL_ID_PATTERN.match(entity_id)
    if match and match.group(1) in _TYPE_DIR:
        return _TYPE_DIR[match.group(1)]
    return None


def _parse_markdown(text: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line)

    entity: dict[str, str] = {}
    for line in sections.get("Entity", []):
        match = _ENTITY_LINE.match(line)
        if match:
            entity[match.group(1).strip()] = match.group(2).strip()

    relationships: list[dict[str, Any]] = []
    for line in sections.get("Relationships", []):
        if line.strip() == "- (none)":
            continue
        match = _RELATIONSHIP_LINE.match(line)
        if not match:
            continue
        sources_raw = match.group("sources")
        sources = [s.strip() for s in sources_raw.split(",")] if sources_raw else []
        relationships.append(
            {
                "relationship": match.group("label"),
                "target_id": match.group("target"),
                "basis": "derived" if sources else "direct",
                "co_occurrence_email_ids": sources,
            }
        )

    provenance_lines = [
        line[2:].strip() for line in sections.get("Provenance", []) if line.strip() != "- (none)"
    ]

    return {"entity": entity, "relationships": relationships, "provenance_lines": provenance_lines}


class KnowledgeLookup:
    def __init__(self, knowledge_dir: str):
        self._root = Path(knowledge_dir)
        self._cache: dict[str, dict[str, Any] | None] = {}

    # --- file resolution -------------------------------------------------------

    def _path_for(self, entity_id: str) -> tuple[Path, str] | None:
        type_dir = _entity_type_from_id(entity_id)
        if type_dir:
            candidate = self._root / type_dir / f"{entity_id}.md"
            return (candidate, type_dir) if candidate.exists() else None
        for type_dir in ("emails", "threads"):
            candidate = self._root / type_dir / f"{entity_id}.md"
            if candidate.exists():
                return candidate, type_dir
        return None

    def _parsed(self, entity_id: str) -> dict[str, Any] | None:
        """Cached per lookup-instance so a multi-hop traversal doesn't re-read and
        re-parse the same file repeatedly."""
        if entity_id in self._cache:
            return self._cache[entity_id]
        resolved = self._path_for(entity_id)
        if resolved is None:
            self._cache[entity_id] = None
            return None
        path, type_dir = resolved
        try:
            parsed = _parse_markdown(path.read_text(encoding="utf-8"))
        except OSError:
            self._cache[entity_id] = None
            return None
        parsed["type"] = type_dir
        self._cache[entity_id] = parsed
        return parsed

    # --- provenance resolution ---------------------------------------------------

    def _email_thread_ids(self, message_id: str) -> list[str]:
        email = self._parsed(message_id)
        if not email:
            return []
        return sorted(
            {
                r["target_id"] for r in email["relationships"]
                if r["relationship"] == "MEMBER_OF_THREAD"
            }
        )

    def _provenance_for(self, relationship: str, target_id: str, co_occurrence_email_ids: list[str]) -> dict[str, Any]:
        if co_occurrence_email_ids:
            thread_ids: set[str] = set()
            for message_id in co_occurrence_email_ids:
                thread_ids.update(self._email_thread_ids(message_id))
            return {
                "available": True,
                "source_email_ids": co_occurrence_email_ids,
                "source_thread_ids": sorted(thread_ids),
            }
        if relationship == "MEMBER_OF_THREAD":
            return {"available": True, "source_email_ids": [], "source_thread_ids": [target_id]}
        if relationship in ("DERIVED_FROM", "MENTIONS") and _entity_type_from_id(target_id) is None:
            # Target isn't a canonical entity id -- for these two labels that means
            # it's a message id (the projector only ever points DERIVED_FROM/MENTIONS
            # at a message_id or a canonical entity, never a thread_id directly).
            return {"available": True, "source_email_ids": [target_id], "source_thread_ids": []}
        return {"available": False, "source_email_ids": [], "source_thread_ids": []}

    def _enrich(self, relationship: dict[str, Any]) -> dict[str, Any]:
        target_id = relationship["target_id"]
        return {
            "relationship": relationship["relationship"],
            "target_id": target_id,
            "target_type": _entity_type_from_id(target_id) or self._infer_message_or_thread_type(target_id),
            "basis": relationship["basis"],
            "provenance": self._provenance_for(
                relationship["relationship"], target_id, relationship["co_occurrence_email_ids"]
            ),
        }

    def _infer_message_or_thread_type(self, entity_id: str) -> str | None:
        if (self._root / "emails" / f"{entity_id}.md").exists():
            return "emails"
        if (self._root / "threads" / f"{entity_id}.md").exists():
            return "threads"
        return None

    # --- entity lookup -------------------------------------------------------------

    def find_entity(self, entity_id: str) -> dict[str, Any] | None:
        parsed = self._parsed(entity_id)
        if parsed is None:
            return None
        return {
            "id": entity_id,
            "type": parsed["type"],
            "entity": parsed["entity"],
            "relationships": [self._enrich(r) for r in parsed["relationships"]],
            "provenance": parsed["provenance_lines"],
        }

    def _search(self, type_dir: str, query: str, name_field: str, email_field: str | None) -> list[dict[str, Any]]:
        # 1. canonical id (case-insensitive on input, but canonical ids are always
        # uppercase in this system -- normalize before the file lookup)
        candidate_id = query.strip().upper()
        if _entity_type_from_id(candidate_id) == type_dir:
            found = self.find_entity(candidate_id)
            if found:
                return [found]
        directory = self._root / type_dir
        if not directory.is_dir():
            return []
        candidates = []
        for path in sorted(directory.glob("*.md")):
            entity_id = path.stem
            parsed = self._parsed(entity_id)
            if not parsed:
                continue
            candidates.append((entity_id, parsed))

        normalized_query = _normalize(query)

        # 2. exact email
        if email_field:
            for entity_id, parsed in candidates:
                value = parsed["entity"].get(email_field)
                if value and _normalize(value) == normalized_query:
                    return [self.find_entity(entity_id)]

        # 3. exact normalized name
        for entity_id, parsed in candidates:
            value = parsed["entity"].get(name_field)
            if value and _normalize(value) == normalized_query:
                return [self.find_entity(entity_id)]

        # 4. safe substring match (deterministic, not fuzzy)
        matches = []
        for entity_id, parsed in candidates:
            value = parsed["entity"].get(name_field)
            if value and normalized_query in _normalize(value):
                matches.append(self.find_entity(entity_id))
        return matches

    def find_people(self, query: str) -> list[dict[str, Any]]:
        return self._search("people", query, name_field="Name", email_field="Email")

    def find_projects(self, query: str) -> list[dict[str, Any]]:
        return self._search("projects", query, name_field="Name", email_field=None)

    # --- relationship traversal -----------------------------------------------------

    def get_relationships(self, entity_id: str) -> list[dict[str, Any]]:
        found = self.find_entity(entity_id)
        return found["relationships"] if found else []

    def get_related_entities(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {
                "source_id": entity_id,
                "relationship": r["relationship"],
                "target_id": r["target_id"],
                "target_type": r["target_type"],
                "basis": r["basis"],
                "provenance": r["provenance"],
            }
            for r in self.get_relationships(entity_id)
        ]

    def get_neighbors(self, entity_id: str, depth: int = 1) -> dict[str, Any]:
        """Bounded BFS traversal. depth is clamped to [0, _MAX_DEPTH] regardless of
        what's requested -- unlimited recursion is never possible. A visited set
        ensures no node is expanded twice, so cycles terminate the walk rather than
        looping forever, while the edges that close a cycle are still reported once."""
        depth = max(0, min(depth, _MAX_DEPTH))
        root = self.find_entity(entity_id)
        if root is None:
            return {"entity_id": entity_id, "found": False, "hops": []}

        visited = {entity_id}
        hops: list[dict[str, Any]] = []
        frontier = [entity_id]
        for hop_number in range(1, depth + 1):
            next_frontier: list[str] = []
            hop_edges: list[dict[str, Any]] = []
            for node_id in frontier:
                for edge in self.get_related_entities(node_id):
                    hop_edges.append({**edge, "source_id": node_id})
                    if edge["target_id"] not in visited:
                        visited.add(edge["target_id"])
                        next_frontier.append(edge["target_id"])
            if not hop_edges:
                break
            hops.append({"hop": hop_number, "edges": hop_edges})
            frontier = next_frontier
        return {"entity_id": entity_id, "found": True, "hops": hops}
