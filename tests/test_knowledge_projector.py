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
from app.knowledge_projector import KnowledgeProjector, organization_id


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


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


def _seed_full_graph(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project())
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, _commitment())
    FollowUpRepository(db).upsert_by_key({"id": "FU-001"}, _follow_up())
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting())
    PersonalItemRepository(db).upsert_by_key({"id": "PSN-001"}, _personal_item())
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, _email())
    ThreadRepository(db).upsert_by_key({"thread_id": "thread_1"}, _thread())


# --- creation -----------------------------------------------------------------------


def test_person_creates_markdown_file(db, tmp_path):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    report = KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "people" / "PER-001.md").exists()
    assert any("PER-001.md" in f for f in report.created)


def test_project_creates_markdown_file(db, tmp_path):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project())
    KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "projects" / "PRJ-001.md").exists()


def test_commitment_creates_markdown_file(db, tmp_path):
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, _commitment())
    KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "commitments" / "COM-001.md").exists()


def test_follow_up_creates_markdown_file(db, tmp_path):
    FollowUpRepository(db).upsert_by_key({"id": "FU-001"}, _follow_up())
    KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "followups" / "FU-001.md").exists()


def test_meeting_creates_markdown_file(db, tmp_path):
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting())
    KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "meetings" / "MTG-001.md").exists()


def test_email_and_thread_are_projected(db, tmp_path):
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, _email())
    ThreadRepository(db).upsert_by_key({"thread_id": "thread_1"}, _thread())
    KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "emails" / "msg_001.md").exists()
    assert (tmp_path / "threads" / "thread_1.md").exists()


def test_organization_is_derived_not_a_stored_entity(db, tmp_path):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(org="Acme Corporation"))
    KnowledgeProjector(db, str(tmp_path)).project_once()

    org_path = tmp_path / "organizations" / f"{organization_id('Acme Corporation')}.md"
    assert org_path.exists()
    content = org_path.read_text()
    assert "derived" in content.lower()
    assert "not a stored mongodb entity" in content.lower()


# --- relationships & provenance -------------------------------------------------------


def test_relationships_use_canonical_ids(db, tmp_path):
    _seed_full_graph(db)
    KnowledgeProjector(db, str(tmp_path)).project_once()

    content = (tmp_path / "people" / "PER-001.md").read_text()
    assert "HAS_COMMITMENT -> COM-001" in content
    assert "HAS_FOLLOWUP -> FU-001" in content
    assert "HAS_MEETING -> MTG-001" in content
    assert "INVOLVED_IN -> PRJ-001" in content
    assert "WORKS_AT -> ORG-acme-corporation" in content
    assert "MEMBER_OF_THREAD -> thread_1" in content


def test_provenance_preserves_source_email_and_thread_ids(db, tmp_path):
    _seed_full_graph(db)
    KnowledgeProjector(db, str(tmp_path)).project_once()

    commitment_content = (tmp_path / "commitments" / "COM-001.md").read_text()
    assert "msg_001" in commitment_content  # source_record
    assert "DERIVED_FROM -> msg_001" in commitment_content
    assert "MEMBER_OF_THREAD -> thread_1" in commitment_content

    person_content = (tmp_path / "people" / "PER-001.md").read_text()
    assert "co-occurred in msg_001" in person_content


def test_co_occurrence_relationships_cite_source_email(db, tmp_path):
    _seed_full_graph(db)
    KnowledgeProjector(db, str(tmp_path)).project_once()

    relationships = (tmp_path / "index" / "relationships.md").read_text()
    assert "PER-001 --HAS_COMMITMENT--> COM-001" in relationships


# --- incremental behavior ------------------------------------------------------------


def test_first_run_creates_then_second_identical_run_creates_nothing(db, tmp_path):
    _seed_full_graph(db)
    projector = KnowledgeProjector(db, str(tmp_path))

    first = projector.project_once()
    assert len(first.created) == 16  # 6 entity types + email + thread + org + 6 index files
    assert first.updated == []
    assert first.unchanged == []

    second = projector.project_once()
    assert second.created == []
    assert second.updated == []
    assert len(second.unchanged) == 16


def test_modifying_one_entity_only_updates_that_files_output(db, tmp_path):
    _seed_full_graph(db)
    projector = KnowledgeProjector(db, str(tmp_path))
    projector.project_once()

    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(name="John A. Smith"))
    third = projector.project_once()

    # PER-001.md itself, plus index/people.md (whose rendered label includes the
    # person's name) both legitimately change -- nothing else does.
    assert len(third.updated) == 2
    assert any("PER-001.md" in f for f in third.updated)
    assert any("index" in f and "people.md" in f for f in third.updated)
    assert len(third.unchanged) == 14
    assert third.created == []


def test_unchanged_file_bytes_are_not_rewritten_on_disk(db, tmp_path):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    projector = KnowledgeProjector(db, str(tmp_path))
    projector.project_once()

    path = tmp_path / "people" / "PER-001.md"
    mtime_before = path.stat().st_mtime_ns
    projector.project_once()

    assert path.stat().st_mtime_ns == mtime_before


# --- stable IDs -----------------------------------------------------------------------


def test_same_entity_always_maps_to_the_same_filename_across_runs(db, tmp_path):
    _seed_full_graph(db)
    projector = KnowledgeProjector(db, str(tmp_path))

    projector.project_once()
    files_first = {p.name for p in (tmp_path / "people").glob("*.md")}
    projector.project_once()
    files_second = {p.name for p in (tmp_path / "people").glob("*.md")}

    assert files_first == files_second == {"PER-001.md"}


def test_organization_slug_id_is_deterministic(db, tmp_path):
    assert organization_id("Acme Corporation") == organization_id("Acme Corporation")
    assert organization_id("Acme Corporation") == "ORG-acme-corporation"


# --- no fabricated relationships ------------------------------------------------------


def test_commitment_without_project_id_has_no_belongs_to_edge(db, tmp_path):
    # Matches production reality: Commitment.project_id is never populated by the live
    # pipeline. No BELONGS_TO edge must be fabricated when it's None.
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, _commitment(project_id=None))
    KnowledgeProjector(db, str(tmp_path)).project_once()

    content = (tmp_path / "commitments" / "COM-001.md").read_text()
    assert "BELONGS_TO" not in content


def test_meeting_attendees_do_not_create_a_person_edge(db, tmp_path):
    # Meeting.attendees is free text, not Person ids -- must never be turned into a
    # fabricated graph edge to a Person.
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting(attendees=["Someone Not In People"]))
    KnowledgeProjector(db, str(tmp_path)).project_once()

    content = (tmp_path / "meetings" / "MTG-001.md").read_text()
    assert "-> PER-" not in content
    assert "Someone Not In People" in content  # still shown as plain text info


def test_person_with_no_org_has_no_works_at_edge(db, tmp_path):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(org=None))
    KnowledgeProjector(db, str(tmp_path)).project_once()

    content = (tmp_path / "people" / "PER-001.md").read_text()
    assert "WORKS_AT" not in content


# --- restart safety -------------------------------------------------------------------


def test_running_the_cli_twice_creates_no_duplicate_files_or_relationship_entries(
    monkeypatch, tmp_path
):
    import mongomock as _mongomock

    import app.knowledge_projector as projector_module
    from app.config.settings import Settings

    fake_client = _mongomock.MongoClient()
    settings = Settings()
    fake_db = fake_client[settings.mongodb_database]
    initialize_indexes(fake_db)
    _seed_full_graph(fake_db)
    monkeypatch.setattr(projector_module, "get_client", lambda uri: fake_client)
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path))

    projector_module.main(["--once"])
    files_first = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.md"))
    projector_module.main(["--once"])
    files_second = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.md"))

    assert files_first == files_second
    relationships = (tmp_path / "index" / "relationships.md").read_text()
    edge_lines = [line for line in relationships.splitlines() if line.startswith("- ")]
    assert len(edge_lines) == len(set(edge_lines))  # no duplicate edges


# --- failure isolation ------------------------------------------------------------------


def test_one_malformed_entity_does_not_block_others_from_projecting(db, tmp_path):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    db.people.insert_one({"name": "Missing an id field entirely"})  # bypasses the model

    report = KnowledgeProjector(db, str(tmp_path)).project_once()

    assert (tmp_path / "people" / "PER-001.md").exists()
    assert len(report.errors) == 1
    assert report.errors[0][0].startswith("people:")
