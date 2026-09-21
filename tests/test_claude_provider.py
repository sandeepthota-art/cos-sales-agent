import json
from types import SimpleNamespace

import pytest

from app.providers.llm.claude import ClaudeProvider, _extract_json


def _fake_response(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


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
