"""FastAPI backend tests (React/API migration, Phase 1-3 of the approved
docs/REACT_MIGRATION_PLAN.md). Uses FastAPI's own dependency_overrides to
swap in a mongomock db (the same pattern every other test file in this repo
uses, just via FastAPI's own override mechanism instead of a direct
monkeypatch of app.database.mongodb.get_client) -- and the same
monkeypatch.setenv + get_settings.cache_clear() pattern tests/test_settings.py
already established for every auth-related setting, so this file follows
existing conventions rather than inventing a new one.
"""
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password
from app.api.dependencies import get_db
from app.api.main import app
from app.config.settings import get_settings
from app.database.indexes import initialize_indexes


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture(autouse=True)
def _override_db(db):
    app.dependency_overrides[get_db] = lambda: db
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_auth_by_default(monkeypatch):
    # Default state for every test unless a test explicitly configures auth:
    # unset api_password_hash bypasses the login requirement entirely, same
    # as dashboard_password's own zero-config contract.
    monkeypatch.delenv("API_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("API_SECRET_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_READ_ONLY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    # base_url="https://testserver": the login route sets the session cookie
    # with secure=True (correct, production-required behavior) -- httpx's
    # cookie jar only echoes a Secure cookie back over https, so testing it
    # at all requires an https base_url here, not a weakening of the real
    # cookie flag in app/api/routers/auth.py.
    return TestClient(app, base_url="https://testserver")


# --- health (unauthenticated) ---


def test_health_requires_no_auth(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- auth ---


def test_login_succeeds_with_no_password_configured(client):
    response = client.post("/api/v1/auth/login", json={"password": "anything"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_login_succeeds_with_correct_password_and_sets_cookie(client, monkeypatch):
    monkeypatch.setenv("API_PASSWORD_HASH", hash_password("correct-horse"))
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    get_settings.cache_clear()

    response = client.post("/api/v1/auth/login", json={"password": "correct-horse"})

    assert response.status_code == 200
    assert "cos_session" in response.cookies


def test_login_rejects_incorrect_password(client, monkeypatch):
    monkeypatch.setenv("API_PASSWORD_HASH", hash_password("correct-horse"))
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    get_settings.cache_clear()

    response = client.post("/api/v1/auth/login", json={"password": "wrong"})

    assert response.status_code == 401
    assert "cos_session" not in response.cookies


def test_protected_route_returns_401_without_a_session_when_auth_is_configured(client, monkeypatch):
    monkeypatch.setenv("API_PASSWORD_HASH", hash_password("correct-horse"))
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    get_settings.cache_clear()

    response = client.get("/api/v1/people")

    assert response.status_code == 401


def test_protected_route_succeeds_after_login_when_auth_is_configured(client, monkeypatch):
    monkeypatch.setenv("API_PASSWORD_HASH", hash_password("correct-horse"))
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    get_settings.cache_clear()

    login_response = client.post("/api/v1/auth/login", json={"password": "correct-horse"})
    assert login_response.status_code == 200

    response = client.get("/api/v1/people")
    assert response.status_code == 200


def test_protected_route_succeeds_with_no_auth_configured_at_all(client):
    # Default state -- no API_PASSWORD_HASH -- every route is reachable with
    # no login at all, mirroring dashboard_password's own unset-bypasses-gate
    # contract.
    response = client.get("/api/v1/people")
    assert response.status_code == 200


def test_logout_clears_the_session_cookie(client, monkeypatch):
    monkeypatch.setenv("API_PASSWORD_HASH", hash_password("correct-horse"))
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    get_settings.cache_clear()
    client.post("/api/v1/auth/login", json={"password": "correct-horse"})

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert client.get("/api/v1/people").status_code == 401


# --- emails ---


def test_list_emails_route_returns_newest_first(client, db):
    db.emails.insert_one({"message_id": "EML-001", "timestamp": "2026-01-01T00:00:00Z", "subject": "Old"})
    db.emails.insert_one({"message_id": "EML-002", "timestamp": "2026-02-01T00:00:00Z", "subject": "New"})

    response = client.get("/api/v1/emails")

    assert response.status_code == 200
    subjects = [e["subject"] for e in response.json()]
    assert subjects == ["New", "Old"]


def test_get_email_route_returns_404_for_unknown_message_id(client):
    response = client.get("/api/v1/emails/EML-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_get_email_route_returns_the_email(client, db):
    db.emails.insert_one({"message_id": "EML-001", "subject": "Hi", "timestamp": "2026-01-01T00:00:00Z"})

    response = client.get("/api/v1/emails/EML-001")

    assert response.status_code == 200
    assert response.json()["subject"] == "Hi"


# --- threads ---


def test_list_threads_route(client, db):
    db.threads.insert_one({"thread_id": "THR-001", "last_message_at": "2026-01-01T00:00:00Z"})

    response = client.get("/api/v1/threads")

    assert response.status_code == 200
    assert [t["thread_id"] for t in response.json()] == ["THR-001"]


def test_get_thread_route_returns_404_for_unknown_thread(client):
    response = client.get("/api/v1/threads/THR-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_get_thread_route_returns_messages(client, db):
    db.threads.insert_one(
        {"thread_id": "THR-001", "message_ids": ["EML-001"], "normalized_subject": "hi",
         "participant_emails": [], "last_message_at": "2026-01-01T00:00:00Z"}
    )
    db.emails.insert_one(
        {"message_id": "EML-001", "subject": "Hi", "body": "Hello",
         "from": {"name": "A", "email": "a@example.com"}, "to": [], "cc": [],
         "timestamp": "2026-01-01T00:00:00Z"}
    )

    response = client.get("/api/v1/threads/THR-001")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "THR-001"
    assert len(response.json()["messages"]) == 1


def test_get_thread_context_versions_route(client, db):
    db.context_snapshots.insert_one(
        {"thread_id": "THR-001", "triggering_email_id": "EML-001", "context_version": 1,
         "context": {}, "changes_from_previous_context": []}
    )

    response = client.get("/api/v1/threads/THR-001/context-versions")

    assert response.status_code == 200
    assert len(response.json()) == 1


# --- people / organizations ---


def test_list_people_route(client, db):
    db.people.insert_one({"id": "PER-001", "name": "Ashok"})
    response = client.get("/api/v1/people")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Ashok"


def test_get_person_route_returns_404_for_unknown_person(client):
    response = client.get("/api/v1/people/PER-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_list_organizations_route(client, db):
    db.organizations.insert_one({"id": "ORG-001", "name": "DataBeat"})
    response = client.get("/api/v1/organizations")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "DataBeat"


def test_get_organization_route_returns_404_for_unknown_org(client):
    response = client.get("/api/v1/organizations/ORG-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_get_organization_route_returns_summary(client, db):
    db.organizations.insert_one({"id": "ORG-001", "name": "DataBeat"})
    db.people.insert_one({"id": "PER-001", "name": "Ashok", "org": "DataBeat"})

    response = client.get("/api/v1/organizations/ORG-001")

    assert response.status_code == 200
    body = response.json()
    assert body["organization"]["name"] == "DataBeat"
    assert len(body["summary"]["people"]) == 1


# --- projects (incl. update_project_fields mutation) ---


def test_list_projects_route(client, db):
    db.projects.insert_one({"id": "PRJ-001", "project": "Rollout"})
    response = client.get("/api/v1/projects")
    assert response.status_code == 200
    assert response.json()[0]["project"] == "Rollout"


def test_get_project_route_returns_404_for_unknown_project(client):
    response = client.get("/api/v1/projects/PRJ-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_update_project_route_sets_supplied_fields_only(client, db):
    db.projects.insert_one({"id": "PRJ-001", "project": "Rollout", "status": None, "owner": None})

    response = client.patch("/api/v1/projects/PRJ-001", json={"status": "on_track", "owner": "Sandeep"})

    assert response.status_code == 200
    assert response.json()["status"] == "on_track"
    assert response.json()["owner"] == "Sandeep"


def test_update_project_route_returns_404_for_unknown_project(client):
    response = client.patch("/api/v1/projects/PRJ-DOES-NOT-EXIST", json={"status": "on_track"})
    assert response.status_code == 404


def test_update_project_route_is_blocked_in_read_only_mode(client, db, monkeypatch):
    db.projects.insert_one({"id": "PRJ-001", "project": "Rollout"})
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "true")
    get_settings.cache_clear()

    response = client.patch("/api/v1/projects/PRJ-001", json={"status": "on_track"})

    assert response.status_code == 403


# --- opportunities (incl. update_opportunity_fields mutation) ---


def test_list_opportunities_route(client, db):
    db.opportunities.insert_one({"id": "OPP-001", "name": "Deal"})
    response = client.get("/api/v1/opportunities")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Deal"


def test_update_opportunity_route_sets_supplied_fields_only(client, db):
    db.opportunities.insert_one({"id": "OPP-001", "name": "Deal", "stage": None, "project_ids": ["PRJ-001"]})

    response = client.patch("/api/v1/opportunities/OPP-001", json={"stage": "negotiation"})

    assert response.status_code == 200
    assert response.json()["stage"] == "negotiation"
    assert response.json()["project_ids"] == ["PRJ-001"]  # pipeline-derived field untouched


def test_update_opportunity_route_is_blocked_in_read_only_mode(client, db, monkeypatch):
    db.opportunities.insert_one({"id": "OPP-001", "name": "Deal"})
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "true")
    get_settings.cache_clear()

    response = client.patch("/api/v1/opportunities/OPP-001", json={"stage": "negotiation"})

    assert response.status_code == 403


# --- commitments / follow-ups / meetings / personal-items / knowledge ---


def test_list_commitments_route(client, db):
    db.commitments.insert_one({"id": "CMT-001", "what": "Send pricing"})
    response = client.get("/api/v1/commitments")
    assert response.status_code == 200
    assert response.json()[0]["what"] == "Send pricing"


def test_list_follow_ups_route_is_enriched(client, db):
    db.commitments.insert_one({"id": "CMT-001", "what": "Send pricing"})
    db.follow_ups.insert_one({"id": "FUP-001", "commitment_id": "CMT-001"})

    response = client.get("/api/v1/follow-ups")

    assert response.status_code == 200
    assert response.json()[0]["what"] == "Send pricing"


def test_list_meetings_route_is_enriched_with_title(client, db):
    db.emails.insert_one({"message_id": "EML-001", "thread_id": "THR-001", "subject": "Pricing call", "timestamp": "2026-01-01T00:00:00Z"})
    db.meetings.insert_one({"id": "MTG-001", "thread_id": "THR-001"})

    response = client.get("/api/v1/meetings")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Pricing call"


def test_get_meeting_brief_route_returns_404_for_unknown_meeting(client):
    response = client.get("/api/v1/meetings/MTG-DOES-NOT-EXIST/brief")
    assert response.status_code == 404


def test_list_personal_items_route(client, db):
    db.personal_items.insert_one({"id": "PSN-001", "type": "reminder", "description": "Call back"})
    response = client.get("/api/v1/personal-items")
    assert response.status_code == 200
    assert response.json()[0]["description"] == "Call back"


def test_list_knowledge_route(client, db):
    db.knowledge_items.insert_one(
        {"knowledge_id": "k1", "thread_id": "THR-001", "subject_key": "x", "predicate": "y", "current_value": "z"}
    )
    response = client.get("/api/v1/knowledge")
    assert response.status_code == 200
    assert response.json()[0]["current_value"] == "z"


# --- reply approval workflow ---


def _reply_draft(reply_id="reply_EML-001", **overrides):
    doc = {
        "reply_id": reply_id,
        "thread_id": "THR-001",
        "source_email_id": "EML-001",
        "status": "awaiting_approval",
        "draft": {"subject": "Re: Hi", "body": "Thanks"},
        "person_id": None,
        "org_id": None,
        "created_by": "sales_agent",
        "created_at": None,
        "approved_by": None,
        "sent_at": None,
    }
    doc.update(overrides)
    return doc


def test_list_reply_drafts_route_is_enriched(client, db):
    db.people.insert_one({"id": "PER-001", "name": "Ashok"})
    db.reply_drafts.insert_one(_reply_draft(person_id="PER-001"))

    response = client.get("/api/v1/reply-drafts")

    assert response.status_code == 200
    assert response.json()[0]["recipient"] == "Ashok"


def test_approve_reply_draft_route(client, db):
    db.reply_drafts.insert_one(_reply_draft())

    response = client.post("/api/v1/reply-drafts/reply_EML-001/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "simulated_sent"
    assert response.json()["sent_at"] is not None


def test_reject_reply_draft_route(client, db):
    db.reply_drafts.insert_one(_reply_draft())

    response = client.post("/api/v1/reply-drafts/reply_EML-001/reject")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_edit_reply_draft_route(client, db):
    db.reply_drafts.insert_one(_reply_draft())

    response = client.post(
        "/api/v1/reply-drafts/reply_EML-001/edit", json={"subject": "Re: Hi (edited)", "body": "New body"}
    )

    assert response.status_code == 200
    assert response.json()["draft"]["body"] == "New body"
    assert response.json()["status"] == "awaiting_approval"


def test_approve_reply_draft_route_returns_404_for_unknown_reply(client):
    response = client.post("/api/v1/reply-drafts/reply_DOES-NOT-EXIST/approve")
    assert response.status_code == 404


def test_reply_draft_mutations_are_blocked_in_read_only_mode(client, db, monkeypatch):
    db.reply_drafts.insert_one(_reply_draft())
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "true")
    get_settings.cache_clear()

    response = client.post("/api/v1/reply-drafts/reply_EML-001/approve")

    assert response.status_code == 403


# --- calendar approval workflow ---


def _calendar_action(**overrides):
    doc = {
        "thread_id": "THR-001",
        "meeting_fingerprint": "fp1",
        "status": "awaiting_approval",
        "event": {
            "title": "Kickoff call", "start": "2026-09-13T10:00:00Z", "end": "2026-09-13T10:30:00Z",
            "timezone": "Asia/Kolkata", "description": "", "attendees": [],
        },
        "actor_type": "authenticated_user",
        "reason": None,
        "person_id": None,
        "org_id": None,
        "meeting_id": None,
    }
    doc.update(overrides)
    return doc


def test_list_calendar_actions_route_is_enriched(client, db):
    db.organizations.insert_one({"id": "ORG-001", "name": "DataBeat"})
    db.calendar_actions.insert_one(_calendar_action(org_id="ORG-001"))

    response = client.get("/api/v1/calendar-actions")

    assert response.status_code == 200
    assert response.json()[0]["org_name"] == "DataBeat"


def test_ignore_calendar_action_route(client, db):
    db.calendar_actions.insert_one(_calendar_action())

    response = client.post("/api/v1/calendar-actions/THR-001/fp1/ignore")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_calendar_action_mutations_are_blocked_in_read_only_mode(client, db, monkeypatch):
    db.calendar_actions.insert_one(_calendar_action())
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "true")
    get_settings.cache_clear()

    response = client.post("/api/v1/calendar-actions/THR-001/fp1/ignore")

    assert response.status_code == 403


def test_calendar_action_route_returns_404_for_unknown_action(client):
    response = client.post("/api/v1/calendar-actions/THR-DOES-NOT-EXIST/fp-none/ignore")
    assert response.status_code == 404


# --- overview ---


def test_overview_route_returns_metrics_and_attention(client, db):
    db.emails.insert_one({"message_id": "EML-001"})

    response = client.get("/api/v1/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["emails_processed"] == 1
    assert "attention" in body
