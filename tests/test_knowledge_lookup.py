import mongomock
import pytest

from app.database.indexes import initialize_indexes
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
from app.knowledge_lookup import KnowledgeLookup
from app.knowledge_projector import KnowledgeProjector


def _person(**overrides):
    doc = {
        "id": "PER-001", "name": "John Smith", "email": "john@example.com", "aliases": [],
        "org": "Acme Corporation", "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": ["thread_1"], "note_link": None,
        "review_flag": False, "source": "gmail",
    }
    doc.update(overrides)
    return doc


def _project(**overrides):
    doc = {
        "id": "PRJ-001", "project": "Enterprise CRM", "cluster": None, "entity": "Acme Corporation",
        "goal_pillar": "Sales", "objective": None, "target": None, "status": None, "owner": None,
        "collaborators": [], "next_milestone": None, "due": None, "health": None,
        "last_movement": None, "note_link": None, "source": "gmail",
    }
    doc.update(overrides)
    return doc


def _commitment(**overrides):
    doc = {
        "id": "COM-001", "what": "send the proposal", "class": "mine", "importance": None,
        "owed_by": None, "owed_to": None, "source_record": "msg_001", "made_on": "2026-09-13T10:30:00",
        "committed_date": None, "date_type": None, "status": "open", "goal_pillar": None,
        "project_id": None, "thread_id": "thread_1",
    }
    doc.update(overrides)
    return doc


def _follow_up(**overrides):
    doc = {"id": "FU-001", "commitment_id": "COM-001", "thread_id": None}
    doc.update(overrides)
    return doc


def _meeting(**overrides):
    doc = {
        "id": "MTG-001", "date": "2026-09-20T10:00:00", "attendees": ["John Smith"],
        "project_or_pillar": None, "minutes_record": None, "actions_raised": [],
        "next_meeting_date": None, "agenda_target": None, "actionable": True,
        "agenda_written": False, "thread_id": "thread_1",
    }
    doc.update(overrides)
    return doc


def _personal_item(**overrides):
    doc = {
        "id": "PSN-001", "type": "reminder", "description": "check in", "date_or_deadline": None,
        "status": "open", "sender_email": "john@example.com",
    }
    doc.update(overrides)
    return doc


def _email(**overrides):
    doc = {
        "message_id": "msg_001", "thread_id": "thread_1",
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}], "cc": [],
        "subject": "Enterprise CRM Proposal", "body": "I will send the proposal.",
        "timestamp": "2026-09-13T10:30:00Z", "labels": [],
        "processing_status": {"stage": "COMPLETED", "error": None},
        "entities_referenced": {
            "people": ["PER-001"], "projects": ["PRJ-001"], "commitments": ["COM-001"],
            "follow_ups": ["FU-001"], "meetings": ["MTG-001"], "personal": ["PSN-001"],
        },
    }
    doc.update(overrides)
    return doc


def _thread(**overrides):
    doc = {
        "thread_id": "thread_1", "normalized_subject": "enterprise crm proposal",
        "participant_emails": ["john@example.com", "ashok@example.com"],
        "message_ids": ["msg_001"], "last_message_at": "2026-09-13T10:30:00",
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def knowledge_dir(tmp_path):
    """Seeds MongoDB (mongomock), projects it to Markdown via the real
    KnowledgeProjector, and returns the resulting knowledge directory -- so
    KnowledgeLookup is tested against real projector output, not hand-crafted
    fixtures that might drift from the actual Markdown format."""
    client = mongomock.MongoClient()
    db = client["cos_sales_test"]
    initialize_indexes(db)

    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project())
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, _commitment())
    FollowUpRepository(db).upsert_by_key({"id": "FU-001"}, _follow_up())
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting())
    PersonalItemRepository(db).upsert_by_key({"id": "PSN-001"}, _personal_item())
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, _email())
    ThreadRepository(db).upsert_by_key({"thread_id": "thread_1"}, _thread())

    KnowledgeProjector(db, str(tmp_path)).project_once()
    return tmp_path


@pytest.fixture
def lookup(knowledge_dir):
    return KnowledgeLookup(str(knowledge_dir))


# --- entity lookup --------------------------------------------------------------------


def test_find_entity_by_canonical_id(lookup):
    result = lookup.find_entity("PER-001")
    assert result is not None
    assert result["id"] == "PER-001"
    assert result["type"] == "people"
    assert result["entity"]["Name"] == "John Smith"


def test_find_people_by_exact_name(lookup):
    results = lookup.find_people("John Smith")
    assert [r["id"] for r in results] == ["PER-001"]


def test_find_people_by_exact_email(lookup):
    results = lookup.find_people("john@example.com")
    assert [r["id"] for r in results] == ["PER-001"]


def test_find_people_by_safe_substring(lookup):
    results = lookup.find_people("John")
    assert [r["id"] for r in results] == ["PER-001"]


def test_find_projects_by_exact_name(lookup):
    results = lookup.find_projects("Enterprise CRM")
    assert [r["id"] for r in results] == ["PRJ-001"]


def test_find_projects_by_substring(lookup):
    results = lookup.find_projects("CRM")
    assert [r["id"] for r in results] == ["PRJ-001"]


def test_find_entity_by_message_id(lookup):
    result = lookup.find_entity("msg_001")
    assert result is not None
    assert result["type"] == "emails"


def test_find_entity_by_thread_id(lookup):
    result = lookup.find_entity("thread_1")
    assert result is not None
    assert result["type"] == "threads"


# --- relationships ---------------------------------------------------------------------


def test_direct_relationship_is_labeled_direct(lookup):
    relationships = lookup.get_relationships("PER-001")
    member_of_thread = next(r for r in relationships if r["relationship"] == "MEMBER_OF_THREAD")
    assert member_of_thread["basis"] == "direct"
    assert member_of_thread["target_id"] == "thread_1"


def test_co_occurrence_relationship_is_labeled_derived(lookup):
    relationships = lookup.get_relationships("PER-001")
    has_commitment = next(r for r in relationships if r["relationship"] == "HAS_COMMITMENT")
    assert has_commitment["basis"] == "derived"
    assert has_commitment["target_id"] == "COM-001"


def test_canonical_ids_are_preserved_in_relationships(lookup):
    relationships = lookup.get_relationships("PER-001")
    target_ids = {r["target_id"] for r in relationships}
    assert "COM-001" in target_ids
    assert "FU-001" in target_ids
    assert "MTG-001" in target_ids
    assert "PRJ-001" in target_ids


def test_relationship_type_is_preserved_not_generalized(lookup):
    relationships = lookup.get_relationships("PER-001")
    labels = {r["relationship"] for r in relationships}
    assert labels == {
        "MEMBER_OF_THREAD", "WORKS_AT", "HAS_COMMITMENT", "HAS_FOLLOWUP",
        "HAS_MEETING", "INVOLVED_IN", "MENTIONED_WITH",
    }


def test_get_related_entities_matches_get_relationships_content(lookup):
    related = lookup.get_related_entities("PER-001")
    assert all(r["source_id"] == "PER-001" for r in related)
    assert {r["relationship"] for r in related} == {
        r["relationship"] for r in lookup.get_relationships("PER-001")
    }


# --- traversal -----------------------------------------------------------------------


def test_get_neighbors_depth_1_returns_only_direct_hop(lookup):
    result = lookup.get_neighbors("PER-001", depth=1)
    assert result["found"] is True
    assert len(result["hops"]) == 1
    assert result["hops"][0]["hop"] == 1
    first_hop_targets = {e["target_id"] for e in result["hops"][0]["edges"]}
    assert "COM-001" in first_hop_targets


def test_get_neighbors_depth_2_reaches_second_hop(lookup):
    result = lookup.get_neighbors("PER-001", depth=2)
    assert len(result["hops"]) >= 1
    all_targets = {e["target_id"] for hop in result["hops"] for e in hop["edges"]}
    # COM-001's own relationships (e.g. DERIVED_FROM -> msg_001) should be reachable
    # by hop 2, since PER-001 -> COM-001 is hop 1.
    assert "msg_001" in all_targets or any(
        "msg_001" in {e["target_id"] for e in hop["edges"]} for hop in result["hops"]
    )


def test_cycle_protection_does_not_loop_forever(lookup):
    # PER-001 <-> COM-001 co-occur, which is a real 2-node cycle by construction.
    # A generous depth must still terminate.
    result = lookup.get_neighbors("PER-001", depth=5)
    assert result["found"] is True
    assert len(result["hops"]) <= 5


def test_visited_nodes_are_not_re_expanded(lookup):
    result = lookup.get_neighbors("PER-001", depth=3)
    # PER-001 itself must never appear as a *newly expanded* frontier node producing
    # its own outgoing edges a second time at a later hop.
    expansions_of_origin = [
        edge for hop in result["hops"] for edge in hop["edges"] if edge["source_id"] == "PER-001"
    ]
    assert len(expansions_of_origin) == len(lookup.get_relationships("PER-001"))


def test_depth_is_bounded_even_when_a_larger_value_is_requested(lookup):
    result = lookup.get_neighbors("PER-001", depth=999)
    assert len(result["hops"]) <= 5  # _MAX_DEPTH


def test_depth_zero_returns_no_hops(lookup):
    result = lookup.get_neighbors("PER-001", depth=0)
    assert result["found"] is True
    assert result["hops"] == []


# --- provenance ------------------------------------------------------------------------


def test_co_occurrence_provenance_includes_source_email(lookup):
    relationships = lookup.get_relationships("PER-001")
    has_commitment = next(r for r in relationships if r["relationship"] == "HAS_COMMITMENT")
    assert has_commitment["provenance"]["available"] is True
    assert has_commitment["provenance"]["source_email_ids"] == ["msg_001"]


def test_co_occurrence_provenance_resolves_source_thread(lookup):
    relationships = lookup.get_relationships("PER-001")
    has_commitment = next(r for r in relationships if r["relationship"] == "HAS_COMMITMENT")
    assert has_commitment["provenance"]["source_thread_ids"] == ["thread_1"]


def test_member_of_thread_provenance_is_the_thread_itself(lookup):
    relationships = lookup.get_relationships("PER-001")
    member = next(r for r in relationships if r["relationship"] == "MEMBER_OF_THREAD")
    assert member["provenance"]["available"] is True
    assert member["provenance"]["source_thread_ids"] == ["thread_1"]


def test_unavailable_provenance_is_explicitly_marked_not_fabricated(lookup):
    relationships = lookup.get_relationships("PER-001")
    works_at = next(r for r in relationships if r["relationship"] == "WORKS_AT")
    assert works_at["provenance"]["available"] is False
    assert works_at["provenance"]["source_email_ids"] == []
    assert works_at["provenance"]["source_thread_ids"] == []


# --- negative cases --------------------------------------------------------------------


def test_unknown_entity_returns_none(lookup):
    assert lookup.find_entity("PER-999") is None


def test_unknown_entity_neighbors_reports_not_found(lookup):
    result = lookup.get_neighbors("PER-999", depth=2)
    assert result["found"] is False
    assert result["hops"] == []


def test_find_people_with_no_match_returns_empty_list(lookup):
    assert lookup.find_people("Nobody Named This") == []


def test_malformed_markdown_does_not_crash_lookup(knowledge_dir):
    # A file that doesn't follow the projector's format at all.
    (knowledge_dir / "people" / "PER-999.md").write_text("not valid markdown at all", encoding="utf-8")
    lookup = KnowledgeLookup(str(knowledge_dir))

    result = lookup.find_entity("PER-999")

    assert result is not None
    assert result["relationships"] == []  # no ## Relationships section to parse


def test_missing_relationship_section_yields_empty_relationships(knowledge_dir):
    (knowledge_dir / "people" / "PER-998.md").write_text(
        "# PER-998 — Ghost\n\n## Entity\n- ID: PER-998\n- Name: Ghost\n", encoding="utf-8"
    )
    lookup = KnowledgeLookup(str(knowledge_dir))

    result = lookup.find_entity("PER-998")

    assert result["entity"]["Name"] == "Ghost"
    assert result["relationships"] == []


def test_dangling_relationship_target_has_no_target_type_but_does_not_crash(knowledge_dir):
    (knowledge_dir / "people" / "PER-997.md").write_text(
        "# PER-997 — Dangling\n\n## Entity\n- ID: PER-997\n- Name: Dangling\n\n"
        "## Relationships\n- HAS_COMMITMENT -> CMT-DOES-NOT-EXIST\n",
        encoding="utf-8",
    )
    lookup = KnowledgeLookup(str(knowledge_dir))

    result = lookup.find_entity("PER-997")

    assert len(result["relationships"]) == 1
    edge = result["relationships"][0]
    assert edge["target_id"] == "CMT-DOES-NOT-EXIST"
    assert edge["target_type"] == "commitments"  # inferred from the CMT- prefix even though the file is missing


def test_relationship_with_missing_provenance_source_email_returns_unavailable(knowledge_dir):
    (knowledge_dir / "people" / "PER-996.md").write_text(
        "# PER-996 — NoProv\n\n## Entity\n- ID: PER-996\n- Name: NoProv\n\n"
        "## Relationships\n- BELONGS_TO -> ORG-somewhere\n",
        encoding="utf-8",
    )
    lookup = KnowledgeLookup(str(knowledge_dir))

    edge = lookup.get_relationships("PER-996")[0]
    assert edge["provenance"]["available"] is False


# --- production safety (this file never touches MongoDB) --------------------------------


def test_lookup_never_imports_or_touches_mongodb():
    import inspect

    import app.knowledge_lookup as module

    source = inspect.getsource(module)
    assert "pymongo" not in source
    assert "Database" not in source
