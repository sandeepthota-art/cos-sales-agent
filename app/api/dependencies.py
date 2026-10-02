"""Shared FastAPI dependencies: database access, settings, and auth gating.

`get_db` mirrors app.mcp.server._get_db exactly (same @lru_cache singleton
pattern, same get_client/initialize_database call) -- the API reaches MongoDB
through the identical, already-tested connection path the MCP server and
Streamlit dashboard both use. No new database-access code, no duplicated
connection logic.
"""
from functools import lru_cache

from fastapi import Cookie, HTTPException, status

from app.api.auth import verify_session_token
from app.config.settings import Settings, get_settings
from app.database.mongodb import get_client, initialize_database

SESSION_COOKIE_NAME = "cos_session"


@lru_cache
def get_db():
    settings = get_settings()
    return initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)


def get_settings_dependency() -> Settings:
    return get_settings()


def require_auth(cos_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)) -> None:
    """Gates every route except /api/v1/auth/login and /health.

    Unset `api_password_hash` bypasses auth entirely -- mirrors
    `dashboard_password`'s own zero-configuration-for-local-dev contract
    (see app.config.settings.Settings.api_password_hash's own docstring).
    When auth IS configured, a missing or invalid/expired session cookie is
    always a 401 -- never a silent pass-through, and never a response body
    that distinguishes "missing cookie" from "invalid cookie" (nothing for
    an attacker to learn from the error shape).
    """
    settings = get_settings()
    if not settings.api_password_hash:
        return
    if not settings.api_secret_key:
        # Misconfiguration (password hash set, but no signing key) -- refuse
        # to silently accept every request; this must never happen in a real
        # deployment (see Settings' own docstring), so failing loudly here is
        # correct, not an oversight.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "API auth misconfigured")
    if not cos_session or not verify_session_token(cos_session, settings.api_secret_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


def require_write_enabled() -> None:
    """Server-side enforcement of dashboard_read_only -- the same safety
    boundary app.ui.dashboard's render functions already check client-side
    (hiding buttons), now also enforced on every mutation route regardless
    of what the React client sends, so a read-only deployment can never be
    bypassed by calling the API directly."""
    settings = get_settings()
    if settings.dashboard_read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This deployment is read-only")
