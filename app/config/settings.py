from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved relative to this file's own location, not the process's current working
# directory. A bare ".env" resolves against os.getcwd(), which is only the project root
# when something happens to launch this process from there -- an MCP client that spawns
# this project's server without an explicit "cwd" (e.g. Claude Desktop's cos-sales-agent
# entry historically had none) runs with whatever cwd it launches from, silently making
# both load_dotenv(".env") below and SettingsConfigDict's env_file=".env" find no file at
# all. That failure is silent by design (dotenv/pydantic-settings never raise for a
# missing env file), so every field not also passed as a real OS env var quietly falls
# back to its class-level default instead of the project's real .env value -- confirmed
# live: a Cowork-spawned process with no cwd resolved llm_provider="mock" even though the
# project's own .env says "claude". __file__ is this module's own path
# (app/config/settings.py); walking up three parents reaches the project/worktree root
# regardless of what directory the interpreter was started from.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

# pydantic-settings resolves a field by checking each configured SOURCE in priority
# order (OS environment, then the .env file, then field defaults), and WITHIN a source,
# tries AliasChoices in order -- it does NOT let a lower-priority source's alias beat a
# higher-priority source's alias. So if this machine has a system-wide OS environment
# variable named plain MONGODB_DATABASE (set by some other, unrelated application), that
# would always win over SALES_AGENT_MONGODB_DATABASE declared only in this project's .env
# file, even though AliasChoices lists the project-specific name first. Explicitly
# loading .env into the OS environment (without clobbering anything already set there)
# ensures SALES_AGENT_MONGODB_* is present in that same top-priority source, so its
# alias-order preference actually takes effect. override=False is essential: it must
# never clobber a real OS-level value (from this shell, or a test's monkeypatch) --
# only fill in names that aren't set anywhere else yet.
load_dotenv(_ENV_FILE, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"

    # SALES_AGENT_MONGODB_* takes priority over the generic MONGODB_* names. This machine
    # also runs another, unrelated system that sets MONGODB_URI/MONGODB_DATABASE as
    # system-wide OS environment variables (which always outrank this project's own .env
    # file) -- without the project-specific alias, this project would silently read that
    # other system's database. The generic names remain as a fallback for anyone who
    # hasn't hit that collision.
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("SALES_AGENT_MONGODB_URI", "MONGODB_URI"),
    )
    mongodb_database: str = Field(
        default="cos_sales",
        validation_alias=AliasChoices("SALES_AGENT_MONGODB_DATABASE", "MONGODB_DATABASE"),
    )

    email_limit: int = 50

    email_provider: str = "demo"
    calendar_provider: str = "mock"
    llm_provider: str = "mock"

    llm_api_key: str = ""
    llm_model: str = ""
    # Optional custom endpoint for the "openai" provider (app.providers.llm.openai.
    # OpenAIProvider) -- lets it target any OpenAI-compatible API (e.g. Groq) instead of
    # api.openai.com, without a separate provider branch. None preserves the OpenAI SDK's
    # own default endpoint exactly as before this field existed.
    llm_base_url: str | None = None

    # The authenticated user's own mailbox. Emails sent FROM this address (e.g. the sales
    # rep's own outbound messages in a two-sided thread) must never get a reply draft
    # generated for them -- see app/pipeline.py's run_pipeline. Defaults to Task 14's demo
    # sales rep address so the demo behaves correctly with zero configuration.
    agent_email: str = "ashok@oursalesagent-demo.example"

    mcp_email_enabled: bool = False
    mcp_calendar_enabled: bool = False

    simulation_mode: bool = True

    timezone: str = "Asia/Kolkata"

    log_level: str = "INFO"

    demo_seed: int = 42

    # Folder-watching scheduler (python -m app.scheduler). Polled every
    # ingestion_interval_minutes for new/changed *.json Gmail-export files (the same
    # shape --mode=file already ingests). Unrelated to email_provider/EMAIL_PROVIDER --
    # the scheduler never uses ProviderFactory.create_email_provider.
    ingestion_folder: str = "data/inbox"
    ingestion_interval_minutes: int = 5

    # Which Source the ingestion scheduler (python -m app.scheduler) polls: "folder"
    # (default, preserves existing local-file behavior) or "gmail" (LiveGmailSource --
    # currently blocked: no real MCP client exists to inject, see
    # app/providers/source/live_gmail.py; selecting it makes the scheduler fail loudly
    # and clearly on every poll rather than silently doing nothing).
    email_source: str = "folder"

    # Reminder scheduler (python -m app.reminders) -- deliberately separate from the
    # ingestion scheduler above: ingestion and reminders are different concerns with
    # independent polling needs, not one process wearing two hats.
    reminder_interval_minutes: int = 5

    # Knowledge projector (python -m app.knowledge_projector) -- MongoDB remains the
    # source of truth; this is a derived, read-only Markdown projection underneath it.
    knowledge_dir: str = "data/knowledge"


@lru_cache
def get_settings() -> Settings:
    return Settings()
