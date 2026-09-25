import inspect
from unittest.mock import patch

from app.providers.llm.openai import OpenAIProvider


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
