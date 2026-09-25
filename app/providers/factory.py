from app.config.settings import Settings
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.email_provider import EmailProvider
from app.interfaces.llm_provider import LLMProvider


class ProviderFactory:
    @staticmethod
    def create_email_provider(settings: Settings) -> EmailProvider:
        provider = settings.email_provider.lower()
        if provider == "demo":
            from app.providers.email.demo import DemoEmailProvider

            return DemoEmailProvider(seed=settings.demo_seed)
        if provider == "mock":
            from app.providers.email.mock import MockEmailProvider

            return MockEmailProvider(payloads=[])
        if provider == "mcp":
            if not settings.mcp_email_enabled:
                raise ValueError("EMAIL_PROVIDER=mcp requires MCP_EMAIL_ENABLED=true")
            from app.providers.email.mcp import MCPEmailProvider

            return MCPEmailProvider()
        raise ValueError(f"Unknown EMAIL_PROVIDER: {settings.email_provider!r}")

    @staticmethod
    def create_calendar_provider(settings: Settings) -> CalendarProvider:
        provider = settings.calendar_provider.lower()
        if provider == "mock":
            from app.providers.calendar.mock import MockCalendarProvider

            return MockCalendarProvider()
        if provider == "mcp":
            if not settings.mcp_calendar_enabled:
                raise ValueError("CALENDAR_PROVIDER=mcp requires MCP_CALENDAR_ENABLED=true")
            from app.providers.calendar.mcp import MCPCalendarProvider

            return MCPCalendarProvider()
        raise ValueError(f"Unknown CALENDAR_PROVIDER: {settings.calendar_provider!r}")

    @staticmethod
    def create_llm_provider(settings: Settings) -> LLMProvider:
        provider = settings.llm_provider.lower()
        if provider == "mock":
            from app.providers.llm.mock import MockLLMProvider

            return MockLLMProvider()
        if provider == "claude":
            from app.providers.llm.claude import ClaudeProvider

            return ClaudeProvider(api_key=settings.llm_api_key, model=settings.llm_model or "claude-sonnet-5")
        if provider == "openai":
            from app.providers.llm.openai import OpenAIProvider

            return OpenAIProvider(
                api_key=settings.llm_api_key,
                model=settings.llm_model or "gpt-4o",
                base_url=settings.llm_base_url,
            )
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
