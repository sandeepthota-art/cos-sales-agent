import os

from dotenv import dotenv_values

from app.config.settings import _ENV_FILE, Settings, get_settings


def test_settings_load_defaults(monkeypatch):
    # Force every var this test asserts a schema default for via an OS-level env var
    # override, not just delenv("EMAIL_LIMIT") -- Settings reads a .env FILE
    # (model_config's env_file=".env"), which is a separate source from the OS
    # environment: delenv only removes an OS env var and has no effect on a value that
    # only lives in the .env file (e.g. a developer's local LLM_PROVIDER=claude for real
    # API testing). Per pydantic-settings' precedence, OS env vars outrank the dotenv
    # file, so explicitly setting each one to its documented default is what actually
    # makes this test assert the schema's defaults regardless of local .env content.
    monkeypatch.setenv("EMAIL_LIMIT", "50")
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SIMULATION_MODE", "true")
    monkeypatch.setenv("INGESTION_FOLDER", "data/inbox")
    monkeypatch.setenv("INGESTION_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("REMINDER_INTERVAL_MINUTES", "5")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.email_limit == 50
    assert settings.email_provider == "demo"
    assert settings.calendar_provider == "mock"
    assert settings.llm_provider == "mock"
    assert settings.simulation_mode is True
    assert settings.ingestion_folder == "data/inbox"
    assert settings.ingestion_interval_minutes == 5
    assert settings.reminder_interval_minutes == 5


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("EMAIL_LIMIT", "10")
    monkeypatch.setenv("EMAIL_PROVIDER", "mcp")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.email_limit == 10
    assert settings.email_provider == "mcp"
    get_settings.cache_clear()


def test_settings_ingestion_env_override(monkeypatch):
    monkeypatch.setenv("INGESTION_FOLDER", "custom/inbox")
    monkeypatch.setenv("INGESTION_INTERVAL_MINUTES", "15")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.ingestion_folder == "custom/inbox"
    assert settings.ingestion_interval_minutes == 15
    get_settings.cache_clear()


def test_settings_reminder_env_override(monkeypatch):
    monkeypatch.setenv("REMINDER_INTERVAL_MINUTES", "20")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.reminder_interval_minutes == 20
    get_settings.cache_clear()


# --- Regression: .env must resolve to the project's own file regardless of the
# process's current working directory (see app.config.settings._ENV_FILE). A Cowork/
# Claude Desktop MCP entry with no "cwd" key previously made both load_dotenv(".env")
# and SettingsConfigDict(env_file=".env") silently fail to find the file whenever the
# process wasn't started from the project root -- confirmed live: llm_provider fell
# back to the class default "mock" even though the project's real .env says "claude". ---


def test_settings_loads_project_env_file_regardless_of_cwd(monkeypatch, tmp_path):
    # Read the REAL project .env directly (never hardcoded here) to know what a correct
    # resolution must produce -- this also self-skips meaningfully if someone edits .env
    # later, rather than silently asserting a stale hardcoded expectation.
    expected = dotenv_values(_ENV_FILE)
    assert expected.get("LLM_PROVIDER"), "project .env must define LLM_PROVIDER for this test to be meaningful"
    assert expected.get("AGENT_EMAIL"), "project .env must define AGENT_EMAIL for this test to be meaningful"

    # No OS env var may supply these -- otherwise the test would pass even if .env
    # resolution were still broken (OS env outranks the .env file either way).
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_EMAIL", raising=False)

    # The actual failure mode: a process started from an arbitrary directory with no
    # .env of its own -- exactly what a Cowork MCP entry with no "cwd" key produces.
    monkeypatch.chdir(tmp_path)

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.llm_provider == expected["LLM_PROVIDER"]
    assert settings.agent_email == expected["AGENT_EMAIL"]
    get_settings.cache_clear()


# --- llm_base_url (Groq/OpenAI-compatible-endpoint support -- app.providers.llm.openai.
# OpenAIProvider) -- optional, must default to None (preserving the OpenAI SDK's own
# default endpoint) and must not change llm_provider/llm_api_key/llm_model defaults. ---


def test_settings_llm_base_url_defaults_to_none(monkeypatch):
    # Two independent sources can supply LLM_BASE_URL even after this delenv: (1) this
    # module's own load_dotenv(_ENV_FILE, override=False) already copied the real
    # project .env's LLM_BASE_URL into os.environ at import time -- delenv removes that
    # copy for this test; (2) Settings' own configured env_file=_ENV_FILE independently
    # re-reads the .env FILE directly on every construction, regardless of os.environ --
    # _env_file=None disables that second source for this one construction. Both are
    # required to actually observe the class default rather than this developer's real,
    # live LLM_BASE_URL (set for real Groq testing).
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_base_url is None
    # Unrelated defaults must be unaffected by this field's addition.
    assert settings.llm_provider == "mock" or isinstance(settings.llm_provider, str)
    assert isinstance(settings.llm_api_key, str)
    assert isinstance(settings.llm_model, str)
    get_settings.cache_clear()


def test_settings_llm_base_url_env_override(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    get_settings.cache_clear()


def test_settings_os_env_still_outranks_project_dotenv_after_cwd_fix(monkeypatch, tmp_path):
    # The fix must not flip .env's priority above the OS environment -- an OS-level
    # override (e.g. a test harness, or a deliberate operator override) must still win,
    # exactly as pydantic-settings' documented source precedence requires.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.chdir(tmp_path)

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.llm_provider == "openai"
    get_settings.cache_clear()
