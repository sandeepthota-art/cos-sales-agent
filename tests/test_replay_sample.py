import json

import mongomock
import pytest

from app import replay_sample
from app.config.settings import Settings
from app.interfaces.llm_provider import LLMProvider


def _settings(**overrides):
    # Constructs Settings directly rather than round-tripping through os.environ:
    # pydantic-settings 2.15.0 has a real, pre-existing quirk where an AliasChoices
    # field (mongodb_database/mongodb_uri use SALES_AGENT_MONGODB_* | MONGODB_*) does
    # not pick up a changed env var on a second Settings() construction within the
    # same process -- invisible in production (Settings is built once per process),
    # but it would make monkeypatch.setenv-based tests in this file silently see a
    # PRIOR test's value. Direct construction sidesteps that entirely.
    defaults = {
        "mongodb_uri": "mongodb://localhost:27017", "mongodb_database": "cos_sales_dryrun",
        "llm_provider": "mock", "calendar_provider": "mock", "email_provider": "mock",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(replay_sample, "get_client", lambda uri: fake_client)
    yield fake_client


def _email_doc(message_id="MSG-1", **overrides):
    doc = {
        "message_id": message_id,
        "from": {"name": "Jane", "email": "jane@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@ourcompany.example"}],
        "cc": [],
        "subject": "Renewal pricing",
        "body": "Can we discuss renewal pricing for next quarter?",
        "timestamp": "2026-08-01T09:00:00Z",
    }
    doc.update(overrides)
    return doc


def _write_sample(tmp_path, docs):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(docs), encoding="utf-8")
    return str(path)


def test_unwrap_extended_json_strips_oid_date_and_underscore_id():
    raw = {
        "_id": {"$oid": "abc123"},
        "message_id": "MSG-1",
        "timestamp": {"$date": "2026-08-01T09:00:00Z"},
        "nested": {"_id": {"$oid": "def456"}, "value": 1},
    }

    result = replay_sample._unwrap_extended_json(raw)

    assert "_id" not in result
    assert result["message_id"] == "MSG-1"
    assert result["timestamp"] == "2026-08-01T09:00:00Z"
    assert "_id" not in result["nested"]
    assert result["nested"]["value"] == 1


def test_load_sample_emails_requires_a_top_level_array(tmp_path):
    path = tmp_path / "not_a_list.json"
    path.write_text(json.dumps({"messages": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="expected a top-level JSON array"):
        replay_sample.load_sample_emails(str(path))


def test_refuses_to_run_against_a_database_name_containing_production(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(replay_sample, "get_settings", lambda: _settings(mongodb_database="cos_sales_production_v1"))
    path = _write_sample(tmp_path, [_email_doc()])

    def _explode(uri):
        raise AssertionError("get_client must never be called when the production guard trips")

    monkeypatch.setattr(replay_sample, "get_client", _explode)

    exit_code = replay_sample.main(["--input", path, "--yes"])

    assert exit_code == 1
    assert "looks like a production database" in capsys.readouterr().out


def test_refuses_without_yes_flag(monkeypatch, tmp_path, capsys, _patch_mongo_client):
    settings = _settings()
    monkeypatch.setattr(replay_sample, "get_settings", lambda: settings)
    path = _write_sample(tmp_path, [_email_doc()])

    exit_code = replay_sample.main(["--input", path])

    assert exit_code == 1
    assert "Re-run with --yes" in capsys.readouterr().out
    assert _patch_mongo_client[settings.mongodb_database].emails.count_documents({}) == 0


def test_missing_input_file_is_reported_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(replay_sample, "get_settings", lambda: _settings())

    exit_code = replay_sample.main(["--input", "does/not/exist.json", "--yes"])

    assert exit_code == 1
    assert "Could not load" in capsys.readouterr().out


class _SalesProjectLLM(LLMProvider):
    """Simulates what a real, correctly-prompted LLM returns for a genuine sales-deal
    email -- MockLLMProvider always returns projects_mentioned=[] regardless of input,
    so it can't exercise this path; this double stands in for it, the same pattern
    tests/test_end_to_end_validation.py already uses."""

    def analyze_email(self, email):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "buying_signal",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": ["pricing request"], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [],
            "companies": [], "products": [],
            "people_mentioned": [
                {"name": "Jane", "email": "jane@customerco.example", "org": "CustomerCo", "role_hint": None}
            ],
            "projects_mentioned": [{"name": "CustomerCo Renewal", "org": "CustomerCo", "objective_hint": "Q4 renewal"}],
            "commitments_mentioned": [], "meetings_mentioned": [], "personal_items_mentioned": [],
            "goal_pillar": "Sales", "label_applied": "Needs reply", "priority": "P1", "confidence": 0.9,
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


def test_replay_runs_the_real_pipeline_and_reports_created_projects(monkeypatch, tmp_path, capsys):
    settings = _settings()
    monkeypatch.setattr(replay_sample, "get_settings", lambda: settings)
    monkeypatch.setattr(
        replay_sample.ProviderFactory, "create_llm_provider", staticmethod(lambda settings: _SalesProjectLLM())
    )
    path = _write_sample(tmp_path, [_email_doc()])

    exit_code = replay_sample.main(["--input", path, "--yes"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "completed=1" in output
    assert "goal_pillar='Sales'" in output

    db = replay_sample.get_client(settings.mongodb_uri)[settings.mongodb_database]
    projects = list(db.projects.find({}))
    assert len(projects) == 1
    assert projects[0]["goal_pillar"] == "Sales"
