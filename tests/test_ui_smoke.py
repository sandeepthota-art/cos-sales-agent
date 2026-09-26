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


def test_dashboard_read_only_mode_hides_approval_buttons(monkeypatch):
    # Regression coverage for the public, friend-facing deployment: with
    # DASHBOARD_READ_ONLY=true, a real pending reply draft and a real pending
    # calendar action must still be visible, but every action button
    # (Approve/Reject/Save edit/Create on my calendar/Ignore) must be gone --
    # this dashboard is the ONLY place those actions can be triggered at all
    # (see app/ui/dashboard.py's module docstring), so read-only mode is the
    # entire safety guarantee for a viewer who isn't the operator.
    fake_client = mongomock.MongoClient()
    db = fake_client["cos_sales_test"]
    initialize_indexes(db)
    db.reply_drafts.insert_one(
        {
            "reply_id": "reply_msg_001",
            "thread_id": "t1",
            "source_email_id": "msg_001",
            "status": "awaiting_approval",
            "draft": {"subject": "Re: Hi", "body": "Body text"},
        }
    )
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
    # The records themselves are still genuinely visible, not just absent-with-no-trace.
    all_text = " ".join(md.value for md in at.markdown)
    assert "Re: Hi" in all_text
    assert "Kickoff call" in all_text


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
