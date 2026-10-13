"""Pydantic shape validation for persist_context_delta's input. Structural
only (required fields, types, basic length bounds) -- enum-membership,
whitelist, evidence-in-text, and cross-reference checks are custom checks
in app/context.py (they need data this module doesn't have: the claimed
documents' text, participant list, etc). Kept as plain str fields here
(not pydantic Literal) so app/context_config.py's sets stay the single
source of truth for valid enum values -- no duplicated, driftable lists.
"""

from pydantic import BaseModel, Field


class ObservationInput(BaseModel):
    entity_type: str
    entity_key: str
    attribute: str
    value: str
    evidence: str
    source_message_id: str


class PersonOrgLinkInput(BaseModel):
    person_email: str
    org_domain: str
    role_title: str | None = None
    valid_from: str
    evidence: str
    source_message_id: str
    close_existing: bool = False


class OpenItemInput(BaseModel):
    text: str
    owner: str
    evidence: str
    source_message_id: str


class ThreadDeltaInput(BaseModel):
    status: str | None = None
    summary: str | None = None
    open_items: list[OpenItemInput] | None = None


class EmailInsightInput(BaseModel):
    message_id: str
    intent: str
    sentiment: str
    sender_type: str
    summary: str
    evidence: str


class ContextDelta(BaseModel):
    observations: list[ObservationInput] = Field(default_factory=list)
    person_org_links: list[PersonOrgLinkInput] = Field(default_factory=list)
    thread: ThreadDeltaInput | None = None
    email_insights: list[EmailInsightInput] = Field(default_factory=list)
