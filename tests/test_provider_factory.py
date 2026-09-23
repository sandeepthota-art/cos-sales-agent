from unittest.mock import patch

import pytest

from app.config.settings import Settings
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.factory import ProviderFactory
from app.providers.llm.mock import MockLLMProvider


def test_factory_creates_demo_email_provider_by_default():
    from app.providers.email.demo import DemoEmailProvider

    settings = Settings(email_provider="demo")
    provider = ProviderFactory.create_email_provider(settings)
    assert isinstance(provider, DemoEmailProvider)


def test_factory_creates_mock_calendar_provider():
    settings = Settings(calendar_provider="mock")
    provider = ProviderFactory.create_calendar_provider(settings)
    assert isinstance(provider, MockCalendarProvider)


def test_factory_creates_mock_llm_provider():
    settings = Settings(llm_provider="mock")
    provider = ProviderFactory.create_llm_provider(settings)
    assert isinstance(provider, MockLLMProvider)


def test_factory_raises_on_unknown_provider_name():
    settings = Settings(llm_provider="not_a_real_provider")
    with pytest.raises(ValueError):
        ProviderFactory.create_llm_provider(settings)


def test_factory_raises_when_mcp_email_selected_but_not_enabled():
    settings = Settings(email_provider="mcp", mcp_email_enabled=False)
    with pytest.raises(ValueError, match="MCP_EMAIL_ENABLED"):
        ProviderFactory.create_email_provider(settings)


def test_factory_raises_when_mcp_calendar_selected_but_not_enabled():
    settings = Settings(calendar_provider="mcp", mcp_calendar_enabled=False)
    with pytest.raises(ValueError, match="MCP_CALENDAR_ENABLED"):
        ProviderFactory.create_calendar_provider(settings)


def test_factory_creates_mcp_email_provider_when_enabled():
    from app.providers.email.mcp import MCPEmailProvider

    settings = Settings(email_provider="mcp", mcp_email_enabled=True)
    provider = ProviderFactory.create_email_provider(settings)
    assert isinstance(provider, MCPEmailProvider)


def test_factory_creates_mcp_calendar_provider_when_enabled():
    from app.providers.calendar.mcp import MCPCalendarProvider

    settings = Settings(calendar_provider="mcp", mcp_calendar_enabled=True)
    provider = ProviderFactory.create_calendar_provider(settings)
    assert isinstance(provider, MCPCalendarProvider)


# --- llm_base_url passthrough (Groq/OpenAI-compatible-endpoint support) --------------
# The OpenAI SDK client is mocked in every test below -- no real API request (Groq or
# otherwise) is ever made by these tests.


def test_factory_creates_claude_provider_and_leaves_it_unmodified():
    from app.providers.llm.claude import ClaudeProvider

    settings = Settings(llm_provider="claude", llm_api_key="test-key")
    provider = ProviderFactory.create_llm_provider(settings)
    assert isinstance(provider, ClaudeProvider)


def test_factory_creates_openai_provider_with_no_base_url_by_default(monkeypatch):
    from app.providers.llm.openai import OpenAIProvider

    # Isolate from this developer's real project .env, which may configure a real
    # LLM_BASE_URL (e.g. for Groq) for live testing: app.config.settings' own
    # load_dotenv(override=False) copies it into os.environ at import time (removed by
    # delenv), and Settings' configured env_file independently re-reads the .env FILE on
    # every construction regardless of os.environ (disabled here via _env_file=None).
    # Without both, this test would silently assert against whatever this machine's
    # real .env happens to contain instead of the documented "no base_url" default.
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with patch("app.providers.llm.openai.OpenAI") as mock_openai_cls:
        settings = Settings(llm_provider="openai", llm_api_key="test-key", _env_file=None)
        provider = ProviderFactory.create_llm_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    mock_openai_cls.assert_called_once_with(api_key="test-key", base_url=None)


def test_factory_passes_llm_base_url_through_to_openai_provider():
    from app.providers.llm.openai import OpenAIProvider

    with patch("app.providers.llm.openai.OpenAI") as mock_openai_cls:
        settings = Settings(
            llm_provider="openai",
            llm_api_key="test-key",
            llm_model="Llama 3.1 8B Instant",
            llm_base_url="https://api.groq.com/openai/v1",
        )
        provider = ProviderFactory.create_llm_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    mock_openai_cls.assert_called_once_with(
        api_key="test-key", base_url="https://api.groq.com/openai/v1"
    )
