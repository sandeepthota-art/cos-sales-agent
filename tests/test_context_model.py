"""Phase 4 data model behavior -- profile rebuild, person_org_links,
out-of-order ingestion, thread history cap. Synthetic data only."""

from datetime import datetime, timezone

import mongomock
import pytest

from app import context, tools
from app.config import Settings
from app.context_db import ObservationRepository, PersonOrgLinkRepository
from app.db import initialize_indexes
from app.models import parse_email

BODY = "Can we discuss renewal pricing for next quarter? Jane Doe is the account manager at CustomerCo."


@pytest.fixture
def db():
    database = mongomock.MongoClient().db
    initialize_indexes(database)
    return database


def _settings(**overrides):
    defaults = {"agent_email": "agent@ourcompany.example"}
    defaults.update(overrides)
    return Settings(**defaults)


def _email_doc(message_id: str, **overrides):
    doc = {
        "message_id": message_id,
        "from": {"name": "Jane", "email": "jane@customerco.example"},
        "to": [{"name": "Agent", "email": "agent@ourcompany.example"}],
        "subject": "Renewal pricing",
        "body": BODY,
        "timestamp": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _ingest(db, settings, message_id: str, **overrides) -> dict:
    return tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id, **overrides)), settings)


def _observation(**overrides):
    obs = {
        "entity_type": "person",
        "entity_key": "jane@customerco.example",
        "attribute": "title",
        "value": "Analyst",
        "evidence": "e",
        "source_message_id": "MSG-1",
        "source_thread_id": "T-1",
        "observed_at": "2026-08-01T00:00:00+00:00",
        "recorded_at": "2026-08-01T00:00:00+00:00",
    }
    obs.update(overrides)
    return obs


def test_conflicting_facts_latest_by_observed_at_wins_and_history_kept(db):
    obs_repo = ObservationRepository(db)
    obs_repo.append(_observation(value="Analyst", observed_at="2026-08-01T00:00:00+00:00"))
    obs_repo.append(_observation(value="Manager", observed_at="2026-08-05T00:00:00+00:00", source_message_id="MSG-2"))

    person = context.rebuild_person(db, "jane@customerco.example")

    assert person["profile"]["title"] == "Manager"
    assert len(obs_repo.for_entity("person", "jane@customerco.example")) == 2


def test_out_of_order_observation_does_not_override_later_one(db):
    obs_repo = ObservationRepository(db)
    # Inserted/recorded FIRST but observed LATER -- should still win.
    obs_repo.append(
        _observation(value="Manager", observed_at="2026-08-05T00:00:00+00:00", recorded_at="2026-08-01T00:00:00+00:00")
    )
    # Inserted/recorded SECOND but observed EARLIER -- must not override.
    obs_repo.append(
        _observation(
            value="Analyst",
            observed_at="2026-08-01T00:00:00+00:00",
            recorded_at="2026-08-10T00:00:00+00:00",
            source_message_id="MSG-2",
        )
    )

    person = context.rebuild_person(db, "jane@customerco.example")

    assert person["profile"]["title"] == "Manager"


def test_rebuild_reproduces_incremental_profile_exactly(db):
    obs_repo = ObservationRepository(db)
    obs_repo.append(_observation(attribute="title", value="Analyst"))
    context.rebuild_person(db, "jane@customerco.example")
    obs_repo.append(_observation(attribute="department", value="Sales", source_message_id="MSG-2"))
    incremental = context.rebuild_person(db, "jane@customerco.example")

    fresh = context.compute_person_profile(db, "jane@customerco.example")
    assert incremental["profile"] == fresh["profile"]


def test_person_org_link_created_from_corporate_domain_with_valid_from(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    claimed = context.claim_next_thread_for_context(db)
    delta = {
        "person_org_links": [
            {
                "person_email": "jane@customerco.example",
                "org_domain": "customerco.example",
                "role_title": "Account Manager",
                "valid_from": "2026-08-01",
                "evidence": "Jane Doe is the account manager at CustomerCo.",
                "source_message_id": "MSG-1",
            }
        ]
    }

    result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)

    assert result["ok"] is True
    link = PersonOrgLinkRepository(db).find_by_id("jane@customerco.example|customerco.example|2026-08-01")
    assert link["valid_from"] == "2026-08-01"
    assert link["valid_to"] is None


def test_valid_to_set_only_with_evidenced_departure_not_by_domain_change_alone(db):
    settings = _settings()
    _ingest(
        db,
        settings,
        "MSG-1",
        thread_id="T-1",
        body=BODY + " She has since moved to NewCo at newco.example as of last month.",
    )
    claimed = context.claim_next_thread_for_context(db)
    delta = {
        "person_org_links": [
            {
                "person_email": "jane@customerco.example",
                "org_domain": "customerco.example",
                "valid_from": "2026-01-01",
                "evidence": "Jane Doe is the account manager at CustomerCo.",
                "source_message_id": "MSG-1",
            },
            {
                "person_email": "jane@customerco.example",
                "org_domain": "newco.example",
                "valid_from": "2026-08-01",
                "evidence": "She has since moved to NewCo at newco.example as of last month.",
                "source_message_id": "MSG-1",
            },
        ]
    }

    result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)

    assert result["ok"] is True
    old_link = PersonOrgLinkRepository(db).find_by_id("jane@customerco.example|customerco.example|2026-01-01")
    # A new org domain appearing does NOT auto-close the old link.
    assert old_link["valid_to"] is None
    new_link = PersonOrgLinkRepository(db).find_by_id("jane@customerco.example|newco.example|2026-08-01")
    assert new_link["valid_to"] is None


def test_close_existing_with_evidence_sets_valid_to(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", body=BODY + " Jane left CustomerCo last week.")
    claimed = context.claim_next_thread_for_context(db)
    delta = {
        "person_org_links": [
            {
                "person_email": "jane@customerco.example",
                "org_domain": "customerco.example",
                "valid_from": "2026-01-01",
                "evidence": "Jane left CustomerCo last week.",
                "source_message_id": "MSG-1",
                "close_existing": True,
            }
        ]
    }

    result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)

    assert result["ok"] is True
    link = PersonOrgLinkRepository(db).find_by_id("jane@customerco.example|customerco.example|2026-01-01")
    assert link["valid_to"] is not None


def test_out_of_order_ingestion_triggers_needs_rebuild_and_reprocessing(db):
    settings = _settings()
    _ingest(db, settings, "MSG-NEW", thread_id="T-1", timestamp=datetime(2026, 8, 10, tzinfo=timezone.utc))
    claimed = context.claim_next_thread_for_context(db)
    context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], {"thread": {"status": "active"}})
    assert db.raw_emails_dump.find_one({"message_id": "MSG-NEW"})["context_version"] == 1

    _ingest(db, settings, "MSG-OLD", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))

    thread_doc = db.threads.find_one({"_id": "T-1"})
    assert thread_doc["needs_rebuild"] is True
    assert db.raw_emails_dump.find_one({"message_id": "MSG-NEW"})["context_version"] == 0
    assert db.raw_emails_dump.find_one({"message_id": "MSG-OLD"})["context_version"] == 0


def test_thread_status_and_open_items_persist_and_history_capped_at_20(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    for i in range(25):
        claimed = context.claim_next_thread_for_context(db)
        delta = {
            "thread": {
                "status": "active",
                "summary": f"Update {i}",
                "open_items": [
                    {
                        "text": "Send contract",
                        "owner": "us",
                        "evidence": "Can we discuss renewal pricing for next quarter?",
                        "source_message_id": "MSG-1",
                    }
                ],
            }
        }
        result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)
        assert result["ok"] is True
        db.raw_emails_dump.update_many(
            {"message_id": "MSG-1"}, {"$set": {"context_version": 0, "context_claim_id": None, "context_claimed_at": ""}}
        )

    thread_doc = db.threads.find_one({"_id": "T-1"})
    assert thread_doc["status"] == "active"
    assert thread_doc["summary"] == "Update 24"
    assert thread_doc["open_items"][0]["text"] == "Send contract"

    history_doc = db.threads_history.find_one({"_id": "T-1"})
    assert len(history_doc["snapshots"]) == 20
