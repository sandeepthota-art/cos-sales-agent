from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved relative to this file's own location, not the process's current working
# directory -- an MCP client spawning this server without an explicit "cwd" must
# still find the real .env (same reasoning as cos-sales-agent's own
# app/config/settings.py, which this file is a trimmed copy of).
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_FILE, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Same .env as cos-sales-agent, same alias priority: SALES_AGENT_MONGODB_*
    # outranks the generic MONGODB_* names.
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("SALES_AGENT_MONGODB_URI", "MONGODB_URI"),
    )
    mongodb_database: str = Field(
        default="cos_sales",
        validation_alias=AliasChoices("SALES_AGENT_MONGODB_DATABASE", "MONGODB_DATABASE"),
    )
    # Identifies "us" -- a new ingested email FROM this address, landing in a
    # thread that already has an open "Needs reply*" item, closes that item
    # out automatically (see app.tools.ingest_raw_email_only).
    agent_email: str = ""
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
