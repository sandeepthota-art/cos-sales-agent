from datetime import datetime
from typing import Any, Literal

from email_validator import validate_email
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# BRD 6.1's six labels -- the same taxonomy cos-sales-agent's
# EmailAnalysis.label_applied uses, copied here as the one place this project
# defines it.
EmailLabel = Literal[
    "1. Needs reply: ASAP", "1. Needs reply", "1. Needs reply: mention", "1. Read only", "1. Delete", "1. Undecided"
]


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
    """message_id/thread_id are the caller-supplied identity (the raw Gmail id,
    on this project's raw-dump-only path -- there is no canonical EML-nnn
    reassignment here, unlike cos-sales-agent's analyzed pipeline).
    source_message_id/source_thread_id are auto-filled from them when a caller
    doesn't supply them separately (the pre-refactor contract every existing
    fixture still uses)."""

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
