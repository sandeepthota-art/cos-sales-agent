"""Request bodies for the API's mutation endpoints only. List/detail GET
endpoints return plain dicts (matching app.mcp.tools' own
`dict[str, Any]` convention throughout this codebase) -- these are the only
places genuinely new validation is needed, since a request body is
untrusted input at a system boundary in a way an internal function call
never is.
"""
from pydantic import BaseModel


class LoginRequest(BaseModel):
    password: str


class ReplyEditRequest(BaseModel):
    subject: str
    body: str


class ProjectFieldsUpdate(BaseModel):
    """All optional -- mirrors app.mcp.tools.update_project_fields's own
    contract exactly: only fields actually supplied are changed."""
    status: str | None = None
    owner: str | None = None
    health: str | None = None
    next_milestone: str | None = None
    due: str | None = None


class OpportunityFieldsUpdate(BaseModel):
    """Mirrors app.mcp.tools.update_opportunity_fields's own contract."""
    stage: str | None = None
    owner: str | None = None
    value: float | None = None
    currency: str | None = None
    expected_close_date: str | None = None
    next_action: str | None = None
