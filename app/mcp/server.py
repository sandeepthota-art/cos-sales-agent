# app/mcp/server.py
import os
import secrets
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.email.models import Email
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.llm_provider import LLMProvider
from app.mcp import tools
from app.providers.factory import ProviderFactory

# Render (and any other HTTP host) assigns the port to listen on via $PORT at runtime --
# irrelevant for the default stdio transport (a local subprocess Claude Desktop spawns
# has no port), so defaulting to 8000 when unset is harmless for local/stdio use.
_HTTP_PORT = int(os.environ.get("PORT", "8000"))


@lru_cache
def _get_db():
    settings = get_settings()
    return initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)


@lru_cache
def _get_llm_provider() -> LLMProvider:
    return ProviderFactory.create_llm_provider(get_settings())


@lru_cache
def _get_calendar_provider() -> CalendarProvider:
    return ProviderFactory.create_calendar_provider(get_settings())


mcp = FastMCP("cos-sales-agent", host="0.0.0.0", port=_HTTP_PORT)


@mcp.tool()
def process_email(email: Email) -> dict[str, Any]:
    """Run one Gmail message through the sales-agent pipeline: normalization, thread
    resolution, LLM analysis, cumulative context, knowledge extraction/deduplication,
    meeting detection, and reply drafting. Persists everything to MongoDB. Returns
    structured results -- including a proposed reply draft and/or meeting proposal for
    you to review and act on via your Gmail connector. Never sends email or creates
    calendar events itself.

    Map Gmail fields into the input shape as follows:
    - message_id: Gmail message id (or Message-ID header)
    - thread_id: Gmail thread id, if available (omit if unknown -- the pipeline will
      infer one)
    - from/to/cc: {"name": ..., "email": ...} objects
    - timestamp: ISO-8601 datetime string
    - in_reply_to / references: Message-ID header values, if available
    """
    return tools.process_email(
        _get_db(), email, _get_llm_provider(), _get_calendar_provider(), get_settings()
    )


@mcp.tool()
def list_processed_emails(limit: int = 50) -> list[dict[str, Any]]:
    """List emails already ingested via process_email, most recent first.

    Only shows emails that have already been processed by this tool -- it never reads
    Gmail directly. Each entry includes message_id, thread_id, from/to/cc, subject,
    timestamp, processing_status, the thread's current summary, and a body_preview
    truncated to about 150 characters. The full email body is never returned.

    Each entry also includes record_id, source_type, source_link, date, goal_pillar,
    label_applied, confidence, and entities_referenced (a dict of people/projects/
    commitments/follow_ups/meetings/personal id lists) -- these are populated once the
    email reaches the ENTITIES_PROCESSED stage, and are None (or empty lists, for
    entities_referenced) for an email that hasn't gotten there yet.
    """
    return tools.list_processed_emails(_get_db(), limit)


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request without a valid bearer token.

    Only used for the streamable-http transport. Unlike stdio (a local subprocess only
    Claude Desktop itself can spawn and talk to), an HTTP deployment is reachable by
    anyone who has the URL -- without this, they could call process_email using your
    MongoDB credentials and burn your LLM API key with no credentials of their own.
    """

    def __init__(self, app, expected_token: str) -> None:
        super().__init__(app)
        self._expected_token = expected_token

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        provided = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not provided or not secrets.compare_digest(provided, self._expected_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def _run_http() -> None:
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "")
    if not auth_token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN must be set when MCP_TRANSPORT=streamable-http -- running this "
            "server on the public internet without it would let anyone call its tools using "
            "your MongoDB credentials and LLM API key, with no authentication at all."
        )

    import uvicorn

    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(_BearerAuthMiddleware, expected_token=auth_token)
    uvicorn.run(
        starlette_app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") == "streamable-http":
        _run_http()
    else:
        mcp.run()


if __name__ == "__main__":
    main()
