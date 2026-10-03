from pathlib import Path

import mongomock
import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.database.indexes import initialize_indexes  # noqa: E402

# AppTest.from_file() resolves a relative path against the directory of the
# file that *calls* it (this test file's directory), not the process cwd, so
# a bare "app/ui/dashboard.py" does not resolve to the repo's app/ui/dashboard.py.
# Build an absolute path instead.
DASHBOARD_SCRIPT = Path(__file__).resolve().parent.parent / "app" / "ui" / "dashboard.py"


def _set_common_env(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    # SALES_AGENT_MONGODB_DATABASE, not MONGODB_DATABASE: Settings prefers the former
    # (app/config/settings.py) so this project isn't hijacked by an unrelated system's
    # same-named MONGODB_DATABASE env var elsewhere on the machine -- monkeypatching the
    # generic name alone would no longer take effect here.
    monkeypatch.setenv("SALES_AGENT_MONGODB_DATABASE", "cos_sales_test")


def _patch_db(monkeypatch, fake_client):
    # dashboard.py's _get_db() is @st.cache_resource-decorated -- that cache is a
    # process-global singleton that otherwise persists across separate AppTest runs
    # within the same pytest process (unlike the module-level get_client patch
    # above, which IS re-resolved fresh each run). Without clearing it, the second
    # test in this file to run would silently get back the FIRST test's already-
    # cached (and differently-seeded) db instance instead of its own.
    import streamlit as st

    st.cache_resource.clear()
    # Patch get_client on its defining module rather than importing
    # app.ui.dashboard directly: the dashboard module calls main() at import
    # time (required so `streamlit run app/ui/dashboard.py` works), so a bare
    # `import app.ui.dashboard` here would execute the real app against a
    # real MongoDB connection before we get a chance to patch anything.
    # AppTest.from_file() execs the script fresh from source into its own
    # module namespace, re-resolving `from app.database.mongodb import
    # get_client` against the (already-imported, now-patched) mongodb
    # module, so patching it there is what actually takes effect during the
    # AppTest run.
    import app.database.mongodb as mongodb_module

    monkeypatch.setattr(mongodb_module, "get_client", lambda uri: fake_client)


def test_dashboard_app_runs_without_exceptions(monkeypatch):
    fake_client = mongomock.MongoClient()
    initialize_indexes(fake_client["cos_sales_test"])
    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception


def test_dashboard_email_lookup_box_resolves_related_entities(monkeypatch):
    # The CTO-facing "look up an email" box on the Dashboard tab: selecting a
    # message from the dropdown must show real, resolved related-entity info
    # (a person's actual name, a project's actual name) -- not just the raw
    # PER-.../PRJ-... ids from entities_referenced.
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.people.insert_one({"id": "PER-001", "name": "Ashok Ganapam", "email": "ashok@databeat.io"})
    db.projects.insert_one({"id": "PRJ-001", "project": "Acme Rollout", "entity": "Acme", "status": "open"})
    db.emails.insert_one(
        {
            "message_id": "EML-001",
            "thread_id": "THR-001",
            "source_message_id": "src-1",
            "source_thread_id": "src-thread-1",
            "from": {"name": None, "email": "ashok@databeat.io"},
            "to": [{"name": None, "email": "vijender@alumnx.com"}],
            "cc": [],
            "subject": "Acme Rollout kickoff",
            "body": "Let's get started on the Acme rollout.",
            "timestamp": "2026-09-13T10:00:00Z",
            "in_reply_to": None,
            "references": [],
            "attachments": [],
            "labels": [],
            "processing_status": {"stage": "COMPLETED", "error": None, "failed_stage": None,
                                   "updated_at": "2026-09-13T10:05:00+00:00"},
            "entities_referenced": {"people": ["PER-001"], "projects": ["PRJ-001"], "commitments": [],
                                     "follow_ups": [], "meetings": [], "personal": [], "opportunities": []},
            "goal_pillar": "Sales",
            "label_applied": "Needs reply",
        }
    )

    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception

    select_boxes = [sb for sb in at.selectbox if sb.key == "email_lookup_select"]
    assert len(select_boxes) == 1
    target = next(o for o in select_boxes[0].options if "EML-001" in o)
    select_boxes[0].set_value(target).run()

    assert not at.exception
    # >= 1, not == 1: the People/Projects tabs (which render on every rerun,
    # same as every other Streamlit tab) legitimately also list this same
    # person/project -- this assertion only needs to prove the lookup box
    # itself resolved and displayed them, not that it's the only place they
    # appear.
    people_frames = [df.value for df in at.dataframe if "Ashok Ganapam" in df.value.to_string()]
    project_frames = [df.value for df in at.dataframe if "Acme Rollout" in df.value.to_string()]
    assert len(people_frames) >= 1
    assert len(project_frames) >= 1


def test_dashboard_email_lookup_box_shows_reply_withheld_reason_when_no_draft_exists(monkeypatch):
    # An email labeled "Needs reply" with no reply_drafts record but a
    # reply_withheld_reason set (e.g. a sensitive-data request, withheld on
    # purpose) must show that reason -- never silently look like an
    # unexplained gap.
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.emails.insert_one(
        {
            "message_id": "EML-018",
            "thread_id": "THR-018",
            "source_message_id": "src-18",
            "source_thread_id": "src-thread-18",
            "from": {"name": "Mohammed Fahad", "email": "fahad.mohammed@databeat.io"},
            "to": [{"name": None, "email": "sandeep.thota@databeat.io"}],
            "cc": [],
            "subject": "DOCUMENTS",
            "body": "Please send me your bank account details.",
            "timestamp": "2026-09-13T10:00:00Z",
            "in_reply_to": None,
            "references": [],
            "attachments": [],
            "labels": [],
            "processing_status": {"stage": "COMPLETED", "error": None, "failed_stage": None,
                                   "updated_at": "2026-09-13T10:05:00+00:00"},
            "entities_referenced": {"people": [], "projects": [], "commitments": [],
                                     "follow_ups": [], "meetings": [], "personal": [], "opportunities": []},
            "goal_pillar": "",
            "label_applied": "Needs reply",
            "reply_withheld_reason": "Requests bank account details -- withheld, sensitive data.",
        }
    )

    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception

    select_boxes = [sb for sb in at.selectbox if sb.key == "email_lookup_select"]
    target = next(o for o in select_boxes[0].options if "EML-018" in o)
    select_boxes[0].set_value(target).run()

    assert not at.exception
    warnings = [w.value for w in at.warning]
    assert any("bank account details" in w for w in warnings)


def test_dashboard_emails_tab_resolves_in_reply_to_to_the_original_senders_address(monkeypatch):
    # in_reply_to is a raw Gmail threading header (a message id), never an address --
    # the Emails tab must resolve it to the sender of the email it actually refers
    # to, not display the raw id.
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.emails.insert_one(
        {
            "message_id": "EML-001", "thread_id": "THR-001", "source_message_id": "gmail-src-1",
            "source_thread_id": "gmail-thread-1",
            "from": {"name": None, "email": "alice@example.com"}, "to": [], "cc": [],
            "subject": "Kickoff", "body": "Let's get started.", "timestamp": "2026-09-13T10:00:00Z",
            "in_reply_to": None, "references": [], "attachments": [], "labels": [],
            "processing_status": {"stage": "COMPLETED", "error": None, "failed_stage": None,
                                   "updated_at": "2026-09-13T10:05:00+00:00"},
            "entities_referenced": {}, "goal_pillar": "", "label_applied": "Read only",
        }
    )
    db.emails.insert_one(
        {
            "message_id": "EML-002", "thread_id": "THR-001", "source_message_id": "gmail-src-2",
            "source_thread_id": "gmail-thread-1",
            "from": {"name": None, "email": "bob@example.com"}, "to": [], "cc": [],
            "subject": "Re: Kickoff", "body": "Sounds good.", "timestamp": "2026-09-13T11:00:00Z",
            "in_reply_to": "gmail-src-1", "references": [], "attachments": [], "labels": [],
            "processing_status": {"stage": "COMPLETED", "error": None, "failed_stage": None,
                                   "updated_at": "2026-09-13T11:05:00+00:00"},
            "entities_referenced": {}, "goal_pillar": "", "label_applied": "Read only",
        }
    )

    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception

    email_frames = [df.value for df in at.dataframe if "Re: Kickoff" in df.value.to_string()]
    assert len(email_frames) == 1
    reply_row = email_frames[0][email_frames[0]["subject"] == "Re: Kickoff"].iloc[0]
    assert reply_row["in_reply_to"] == "alice@example.com"


def test_dashboard_shows_opportunities_tab_with_real_data(monkeypatch):
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.opportunities.insert_one(
        {
            "id": "OPP-001", "name": "DataBeat Q3 Rollout", "entity": "DataBeat", "org_id": "ORG-001",
            "description": None, "status": "open", "stage": None, "owner": None, "value": None,
            "currency": None, "expected_close_date": None, "next_action": None,
            "source_email_ids": [], "project_ids": ["PRJ-001"], "meeting_ids": [], "person_ids": [],
            "buying_signals": [], "last_activity_at": "2026-09-13T10:00:00Z",
            "created_at": "2026-09-13T10:00:00Z", "updated_at": "2026-09-13T10:00:00Z",
        }
    )

    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()

    assert not at.exception
    opportunity_frames = [df.value for df in at.dataframe if "DataBeat Q3 Rollout" in df.value.to_string()]
    assert len(opportunity_frames) == 1


def test_dashboard_read_only_mode_hides_approval_buttons(monkeypatch):
    # Regression coverage for the public, friend-facing deployment: with
    # DASHBOARD_READ_ONLY=true, a real pending calendar action must still be
    # visible, but every action button (Create on my calendar/Ignore) must be
    # gone -- this dashboard is the ONLY place those actions can be triggered
    # at all (see app/ui/dashboard.py's module docstring), so read-only mode
    # is the entire safety guarantee for a viewer who isn't the operator.
    # (Reply Approval has no tab in this dashboard -- removed per explicit
    # request; see app/ui/dashboard.py's module docstring.)
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.calendar_actions.insert_one(
        {
            "thread_id": "t1",
            "meeting_fingerprint": "fp1",
            "status": "awaiting_approval",
            "event": {
                "title": "Kickoff call",
                "start": "2026-09-13T10:00:00Z",
                "end": "2026-09-13T10:30:00Z",
                "timezone": "Asia/Kolkata",
                "description": "",
                "attendees": [],
            },
        }
    )

    _set_common_env(monkeypatch)
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "true")
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()

    assert not at.exception
    assert len(at.button) == 0
    # The record itself is still genuinely visible, not just absent-with-no-trace.
    all_text = " ".join(md.value for md in at.markdown)
    assert "Kickoff call" in all_text


def test_dashboard_shows_rebranded_product_name(monkeypatch):
    # User-facing rebrand: "CoS Sales Agent" -> "CoS Staff EA Agent", everywhere
    # visible in the UI. Backend module/package/collection names are deliberately
    # unchanged -- this only proves the UI-facing title text.
    fake_client = mongomock.MongoClient()
    initialize_indexes(fake_client["cos_sales_test"])
    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()

    assert not at.exception
    all_titles = " ".join(t.value for t in at.title)
    assert "CoS Staff EA Agent" in all_titles
    assert "CoS Sales Agent" not in all_titles


def test_dashboard_password_gate_blocks_until_correct_password(monkeypatch):
    fake_client = mongomock.MongoClient()
    initialize_indexes(fake_client["cos_sales_test"])
    _set_common_env(monkeypatch)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "letmein123")
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception
    # Locked: the gate's own password prompt is there, but none of the real
    # dashboard content (tabs) has rendered yet.
    assert len(at.text_input) == 1
    assert len(at.tabs) == 0

    at.text_input[0].set_value("wrong-password").run()
    assert not at.exception
    assert len(at.tabs) == 0
    assert any("Incorrect password" in e.value for e in at.error)

    at.text_input[0].set_value("letmein123").run()
    assert not at.exception
    assert len(at.tabs) > 0


def test_dashboard_password_gate_bypassed_when_unset(monkeypatch):
    fake_client = mongomock.MongoClient()
    initialize_indexes(fake_client["cos_sales_test"])
    _set_common_env(monkeypatch)
    _patch_db(monkeypatch, fake_client)

    at = AppTest.from_file(str(DASHBOARD_SCRIPT))
    at.run()
    assert not at.exception
    assert len(at.text_input) == 0
    assert len(at.tabs) > 0
