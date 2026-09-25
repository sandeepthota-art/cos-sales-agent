import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository, PersonRepository, ThreadRepository
from app.knowledge_projector import KnowledgeProjector
from app.mcp import tools


@pytest.fixture
def knowledge_dir(tmp_path):
    client = mongomock.MongoClient()
    db = client["cos_sales_test"]
    initialize_indexes(db)

    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"},
        {
            "id": "PER-001", "name": "John Smith", "email": "john@example.com", "aliases": [],
            "org": None, "type": None, "goal_pillar": None, "role_in_pillar": None, "tier": None,
            "voice_register": None, "last_inbound": None, "last_outbound": None, "reports_to": None,
            "open_threads": ["thread_1"], "note_link": None, "review_flag": False, "source": "gmail",
        },
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_001"},
        {
            "message_id": "msg_001", "thread_id": "thread_1",
            "from": {"name": "John", "email": "john@example.com"},
            "to": [], "cc": [], "subject": "Hi", "body": "Hi", "timestamp": "2026-09-13T10:30:00Z",
            "labels": [], "processing_status": {"stage": "COMPLETED", "error": None},
            "entities_referenced": {"people": ["PER-001"], "projects": [], "commitments": [], "follow_ups": [], "meetings": [], "personal": []},
        },
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "thread_1"},
        {
            "thread_id": "thread_1", "normalized_subject": "hi",
            "participant_emails": ["john@example.com"], "message_ids": ["msg_001"],
            "last_message_at": "2026-09-13T10:30:00",
        },
    )

    KnowledgeProjector(db, str(tmp_path)).project_once()
    return str(tmp_path)


def test_lookup_knowledge_valid_entity_id(knowledge_dir):
    result = tools.lookup_knowledge(knowledge_dir, entity_id="PER-001")

    assert result["found"] is True
    assert result["entity"]["id"] == "PER-001"
    assert result["neighbors"]["found"] is True


def test_lookup_knowledge_valid_query(knowledge_dir):
    result = tools.lookup_knowledge(knowledge_dir, query="John")

    assert result["found"] is True
    assert result["primary_match"] == "PER-001"
    assert result["matches"][0]["id"] == "PER-001"


def test_lookup_knowledge_invalid_entity_returns_found_false(knowledge_dir):
    result = tools.lookup_knowledge(knowledge_dir, entity_id="PER-999")

    assert result["found"] is False
    assert result["entity"] is None


def test_lookup_knowledge_unmatched_query_returns_found_false(knowledge_dir):
    result = tools.lookup_knowledge(knowledge_dir, query="Nobody Like This")

    assert result["found"] is False
    assert result["matches"] == []


def test_lookup_knowledge_requires_entity_id_or_query(knowledge_dir):
    with pytest.raises(ValueError):
        tools.lookup_knowledge(knowledge_dir)


def test_lookup_knowledge_depth_is_bounded_regardless_of_input(knowledge_dir):
    result = tools.lookup_knowledge(knowledge_dir, entity_id="PER-001", depth=9999)

    assert len(result["neighbors"]["hops"]) <= 5


def test_lookup_knowledge_result_is_json_serializable(knowledge_dir):
    import json

    result = tools.lookup_knowledge(knowledge_dir, entity_id="PER-001", depth=2)

    json.dumps(result)


def test_lookup_knowledge_never_writes_to_mongodb(knowledge_dir):
    client = mongomock.MongoClient()
    db = client["cos_sales_test"]
    initialize_indexes(db)
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, {"id": "PER-001", "name": "John", "email": None, "aliases": [], "org": None, "type": None, "goal_pillar": None, "role_in_pillar": None, "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None, "reports_to": None, "open_threads": [], "note_link": None, "review_flag": False, "source": "gmail"})
    before = list(db.people.find({}))

    tools.lookup_knowledge(knowledge_dir, entity_id="PER-001", depth=3)

    after = list(db.people.find({}))
    assert before == after
