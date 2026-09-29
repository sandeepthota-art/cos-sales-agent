import inspect
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.email.models import parse_email
from app.providers.llm.openai import OpenAIProvider


def _fake_chat_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def _email():
    return parse_email(
        {
            "message_id": "msg_001",
            "from": {"name": "John", "email": "john@example.com"},
            "to": [{"name": "Ashok", "email": "ashok@example.com"}],
            "subject": "Enterprise pricing",
            "body": "Can you send pricing?",
            "timestamp": "2026-09-13T10:30:00Z",
        }
    )


def test_openai_provider_base_url_parameter_defaults_to_none():
    sig = inspect.signature(OpenAIProvider.__init__)
    assert sig.parameters["base_url"].default is None


def test_openai_provider_without_base_url_preserves_existing_behavior():
    # base_url=None is the OpenAI SDK's own default endpoint (api.openai.com) -- passing
    # it explicitly is identical to omitting it, so this proves existing behavior (no
    # custom endpoint) is unchanged by the new parameter.
    with patch("app.providers.llm.openai.OpenAI") as mock_openai_cls:
        OpenAIProvider(api_key="test-key", model="gpt-4o")

    mock_openai_cls.assert_called_once_with(api_key="test-key", base_url=None)


def test_openai_provider_with_base_url_passes_it_to_the_client():
    with patch("app.providers.llm.openai.OpenAI") as mock_openai_cls:
        OpenAIProvider(
            api_key="test-key",
            model="Llama 3.1 8B Instant",
            base_url="https://api.groq.com/openai/v1",
        )

    mock_openai_cls.assert_called_once_with(
        api_key="test-key", base_url="https://api.groq.com/openai/v1"
    )


def test_analyze_email_with_no_thread_history_omits_prior_messages_block():
    provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_chat_response({"summary": "ok"})

    provider._client.chat.completions.create = fake_create

    provider.analyze_email(_email())

    user_content = captured_kwargs["messages"][1]["content"]
    assert "Prior messages" not in user_content
    assert user_content.startswith("Subject: Enterprise pricing")


def test_analyze_email_with_thread_history_prepends_it_before_subject_body():
    provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_chat_response({"summary": "ok"})

    provider._client.chat.completions.create = fake_create

    thread_history = [
        {"from": {"name": "Jane"}, "timestamp": "2026-09-12T10:00:00Z", "subject": "Enterprise pricing", "body": "We currently use Salesforce."}
    ]
    provider.analyze_email(_email(), thread_history=thread_history)

    user_content = captured_kwargs["messages"][1]["content"]
    assert "Prior messages in this thread" in user_content
    assert "We currently use Salesforce." in user_content
    assert user_content.index("We currently use Salesforce.") < user_content.index("Subject: Enterprise pricing")
