from datetime import datetime
from typing import Any

from email_validator import validate_email
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class EmailAddress(BaseModel):
    name: str | None = None
    email: str

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        try:
            validate_email(v, check_deliverability=False)
        except Exception as e:
            raise ValueError(f"Invalid email address: {e}")
        return v


class Email(BaseModel):
    """Canonical ID refactor (see the ID Architecture Audit / Phase 0 trace):
    `message_id`/`thread_id` are the CANONICAL, in-flight identity fields
    (`EML-nnn`/`THR-nnn` once `app.pipeline.ingest_raw_email`/
    `resolve_and_persist_thread` have run) -- every downstream stage (entity
    resolution, knowledge, context, reply drafts, calendar, MCP) keeps reading
    these two fields exactly as before; only the VALUE they hold changes, at
    one boundary, not their name or role.

    `source_message_id`/`source_thread_id` are the true, permanent Gmail/
    provider-origin identifiers -- never reassigned, never used as a
    dedup/foreign key by anything downstream of ingestion. They exist only to
    map external Gmail identity to canonical identity, and for
    `in_reply_to`/`references` matching (see `app.email.threading`), since
    those two fields are themselves always raw Gmail header values and can
    never be compared against a canonicalized id.

    Transitional shim: a raw dict that only supplies `message_id`/`thread_id`
    (the pre-refactor contract every existing test fixture and
    `convert_gmail_message` caller still uses) has `source_message_id`/
    `source_thread_id` auto-filled from them at parse time -- this is exactly
    correct for a message that has not yet been through the ingestion
    boundary, since at that point the two are still identical. A caller that
    explicitly supplies `source_message_id`/`source_thread_id` (the new,
    preferred provider contract) is never overridden by the shim.
    """

    model_config = ConfigDict(populate_by_name=True)

    message_id: str
    source_message_id: str | None = None
    thread_id: str | None = None
    source_thread_id: str | None = None
    from_: EmailAddress = Field(alias="from")
    to: list[EmailAddress]
    cc: list[EmailAddress] = Field(default_factory=list)
    subject: str
    body: str
    timestamp: datetime
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shim_source_ids(self) -> "Email":
        if self.source_message_id is None:
            self.source_message_id = self.message_id
        if self.source_thread_id is None and self.thread_id is not None:
            self.source_thread_id = self.thread_id
        return self


def parse_email(raw: dict[str, Any]) -> Email:
    return Email.model_validate(raw)
