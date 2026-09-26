import json
from types import SimpleNamespace

import pytest

from app.email.models import parse_email
from app.providers.llm.claude import _ANALYSIS_INSTRUCTIONS, ClaudeProvider, _extract_json


def _fake_response(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


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


def test_extract_json_parses_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_parses_json_fenced_with_json_tag():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_parses_fence_without_language_tag():
    text = '```\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_strips_surrounding_whitespace():
    text = '  \n  {"a": 1}  \n  '
    assert _extract_json(text) == {"a": 1}


def test_extract_json_handles_uppercase_json_tag():
    text = '```JSON\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_handles_fence_with_no_internal_newlines():
    text = '```json{"a": 1}```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_handles_multiline_object_inside_fence():
    text = '```json\n{\n  "a": 1,\n  "b": [1, 2, 3]\n}\n```'
    assert _extract_json(text) == {"a": 1, "b": [1, 2, 3]}


def test_complete_json_strips_fence_from_real_anthropic_response_shape(monkeypatch):
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    monkeypatch.setattr(
        provider._client.messages,
        "create",
        lambda **kwargs: _fake_response('```json\n{"summary": "ok"}\n```'),
    )

    result = provider._complete_json("system", "user")

    assert result == {"summary": "ok"}


def test_complete_json_still_parses_unfenced_response(monkeypatch):
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    monkeypatch.setattr(
        provider._client.messages,
        "create",
        lambda **kwargs: _fake_response('{"summary": "ok"}'),
    )

    result = provider._complete_json("system", "user")

    assert result == {"summary": "ok"}


def test_complete_json_explicitly_disables_extended_thinking(monkeypatch):
    # Regression test: claude-sonnet-5 enables extended thinking by default even when
    # the caller never asks for it, and thinking tokens count against max_tokens --
    # this silently truncated/emptied the JSON answer on every real call (see the
    # comment in _complete_json). Thinking must be explicitly disabled on every
    # structured-extraction call, not left to the API's default.
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_response('{"summary": "ok"}')

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    provider._complete_json("system", "user")

    assert captured_kwargs["thinking"] == {"type": "disabled"}


def test_complete_json_uses_a_token_budget_with_headroom(monkeypatch):
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_response('{"summary": "ok"}')

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    provider._complete_json("system", "user")

    # 1024 was the original (too-small, pre-fix) budget -- assert real headroom above it
    # rather than a specific magic number, so this doesn't need editing on minor tuning.
    assert captured_kwargs["max_tokens"] > 1024


# --- Real-data-motivated robustness cases (100-email validation run) ---


def test_extract_json_tolerates_valid_json_followed_by_trailing_text():
    # The actual observed failure shape: a short, complete, valid JSON object followed
    # immediately by stray extra content Claude added despite "JSON only" instructions.
    # json.loads would reject this as "Extra data"; raw_decode takes the first value.
    text = '{"summary": "ok"}\nSome trailing commentary the model was told not to add.'
    assert _extract_json(text) == {"summary": "ok"}


def test_extract_json_takes_the_first_of_two_concatenated_json_objects():
    text = '{"summary": "first"}\n{"summary": "second"}'
    assert _extract_json(text) == {"summary": "first"}


def test_extract_json_still_raises_on_malformed_property_syntax():
    # A genuine JSON syntax defect (unquoted/trailing-comma key) inside the object itself
    # -- raw_decode's trailing-content tolerance must NOT extend to fixing broken JSON.
    text = '{"summary": "ok", }'
    with pytest.raises(json.JSONDecodeError):
        _extract_json(text)


def test_extract_json_still_raises_on_truncated_json():
    text = '```json\n{"summary": "ok", "requirements": ["100 sea'  # cut off mid-string, no closing fence
    with pytest.raises(json.JSONDecodeError):
        _extract_json(text)


def test_extract_json_raises_on_non_json_prose_response():
    text = "I'm sorry, I cannot help with that request."
    with pytest.raises(json.JSONDecodeError):
        _extract_json(text)


def test_extract_json_rejects_a_top_level_list():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('[{"summary": "ok"}]')


def test_extract_json_rejects_a_top_level_scalar():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('"just a string"')


def test_complete_json_ignores_non_text_content_blocks(monkeypatch):
    # Defensive regression: even with thinking disabled, a response shaped like
    # [ThinkingBlock(no .text attribute), TextBlock(text=...)] must still parse
    # correctly by only joining blocks that actually have a .text attribute.
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    thinking_block = SimpleNamespace(type="thinking", signature="abc")  # no .text attribute
    text_block = SimpleNamespace(type="text", text='{"summary": "ok"}')
    monkeypatch.setattr(
        provider._client.messages,
        "create",
        lambda **kwargs: SimpleNamespace(content=[thinking_block, text_block]),
    )

    result = provider._complete_json("system", "user")

    assert result == {"summary": "ok"}


# --- draft_reply: recipient_preferences injected into the system prompt ---------------


def test_draft_reply_system_prompt_unchanged_when_no_recipient_preferences(monkeypatch):
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs["system"]
        return _fake_response('{"subject": "Re: Enterprise pricing", "body": "ok"}')

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    provider.draft_reply({}, _email())

    assert "preferences" not in captured["system"].lower()


def test_draft_reply_injects_recipient_preferences_into_the_system_prompt(monkeypatch):
    provider = ClaudeProvider(api_key="fake-key", model="claude-sonnet-5")
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs["system"]
        return _fake_response('{"subject": "Re: Enterprise pricing", "body": "ok"}')

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    context = {"recipient_preferences": {"voice_signature": "short and concise", "remove_long_dash": True}}
    provider.draft_reply(context, _email())

    assert "short and concise" in captured["system"]
    assert "remove_long_dash" in captured["system"]


# --- Extraction prompt boundary: ignore meta-instructions / engineering specs --------
# Regression coverage for a real incident: an internal "Minutes of the meeting on COS
# Agent" email (engineering requirements about the AI system itself -- "labelling
# should be performed", "system should create only 1 draft email") got extracted into
# 23 knowledge_items as if they were business facts about a person/company. Fixed by
# adding a strict boundary to _ANALYSIS_INSTRUCTIONS. This is a prompt-content test, not
# a live-model behavioral test -- consistent with this suite's own convention of never
# making a real LLM API call (see test_complete_json_explicitly_disables_extended_thinking
# and the recipient_preferences tests above, which likewise assert on constructed
# prompt/request content rather than a live response). _ANALYSIS_INSTRUCTIONS is shared
# by both ClaudeProvider and OpenAIProvider (app/providers/llm/openai.py imports it
# directly), so this single test covers both real-LLM code paths.


def test_analysis_instructions_forbid_extracting_meta_instructions_about_the_agent_itself():
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "meta-instruction" in lowered
    assert "agent's own behavior" in lowered or "agent's own behaviour" in lowered
    assert "configuration" in lowered


def test_analysis_instructions_forbid_extracting_software_engineering_specs():
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "software engineering specs" in lowered
    assert "database structures" in lowered
    assert "schemas" in lowered


def test_analysis_instructions_scope_extraction_to_business_domain_facts():
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "business domain only" in lowered
    assert "project timelines" in lowered
    assert "deal terms" in lowered


def test_analysis_instructions_instruct_leaving_lists_empty_for_internal_engineering_emails():
    # The exact failure shape: a WHOLE email about the agent's own configuration, not
    # just one stray sentence -- the prompt must tell the model to leave the
    # business-fact lists empty rather than force-extracting something from it.
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "internal engineering discussion" in lowered
    assert "leave" in lowered and "empty" in lowered
