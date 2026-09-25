import json

import pytest

from app.email.models import parse_email
from app.providers.email.file import FileEmailProvider


def _write(tmp_path, payload):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _message(**overrides):
    base = {
        "id": "18f348ce69f386be",
        "threadId": "18f348ce69f386be",
        "subject": "sales/capabilities materials",
        "sender": "john.t@614group.com",
        "toRecipients": ["ashok@databeat.io"],
        "ccRecipients": None,
        "date": "2024-05-01T14:26:28Z",
        "labelIds": ["IMPORTANT", "INBOX"],
        "snippet": "Hi Ashok...",
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


def test_parses_json_and_converts_into_canonical_email_model(tmp_path):
    path = _write(tmp_path, {"label": "BD", "labelId": "L1", "threadCount": 1, "messageCount": 1, "messages": [_message()]})

    provider = FileEmailProvider(path=path)
    raw_emails = provider.fetch_emails(limit=10)

    assert len(raw_emails) == 1
    email = parse_email(raw_emails[0])
    assert email.message_id == "18f348ce69f386be"
    assert email.subject == "sales/capabilities materials"
    assert email.from_.email == "john.t@614group.com"
    assert email.to[0].email == "ashok@databeat.io"
    assert email.body == "Hi Ashok, good to see you at Possible in Miami."
    assert email.timestamp.year == 2024


def test_thread_id_is_preserved_from_source(tmp_path):
    path = _write(
        tmp_path,
        {"messages": [_message(id="msg_a", threadId="thread_xyz"), _message(id="msg_b", threadId="thread_xyz")]},
    )

    provider = FileEmailProvider(path=path)
    emails = [parse_email(raw) for raw in provider.fetch_emails(limit=10)]

    assert {e.thread_id for e in emails} == {"thread_xyz"}


def test_missing_optional_fields_are_handled_safely(tmp_path):
    message = _message()
    del message["toRecipients"]  # key absent entirely, not just null
    message["ccRecipients"] = None
    path = _write(tmp_path, {"messages": [message]})

    provider = FileEmailProvider(path=path)
    email = parse_email(provider.fetch_emails(limit=10)[0])

    assert email.to == []
    assert email.cc == []


def test_message_missing_a_required_field_is_skipped_not_raised(tmp_path):
    good = _message(id="good_id")
    bad = _message(id="bad_id")
    del bad["subject"]
    path = _write(tmp_path, {"messages": [good, bad]})

    provider = FileEmailProvider(path=path)

    assert provider.total_found == 2
    assert provider.skipped_malformed == 1
    assert len(provider.fetch_emails(limit=10)) == 1


def test_invalid_top_level_structure_raises_value_error(tmp_path):
    path = _write(tmp_path, {"not_messages": []})

    with pytest.raises(ValueError, match="messages"):
        FileEmailProvider(path=path)


def test_fetch_emails_respects_limit(tmp_path):
    path = _write(
        tmp_path,
        {"messages": [_message(id=f"id_{i}", threadId=f"thread_{i}") for i in range(5)]},
    )

    provider = FileEmailProvider(path=path)

    assert len(provider.fetch_emails(limit=2)) == 2


def test_send_email_only_simulates(tmp_path, capsys):
    path = _write(tmp_path, {"messages": [_message()]})
    provider = FileEmailProvider(path=path)

    provider.send_email(to="a@example.com", subject="s", body="b")

    assert "[SIMULATED EMAIL SEND]" in capsys.readouterr().out
