from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CalendarEvent(BaseModel):
    title: str
    start: datetime
    end: datetime
    timezone: str
    description: str
    attendees: list[str] = Field(default_factory=list)

    @field_validator("attendees")
    @classmethod
    def validate_no_external_attendees(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError("External attendees are not permitted")
        return value


class CalendarAction(BaseModel):
    thread_id: str
    meeting_fingerprint: str
    status: Literal[
        "pending", "awaiting_approval", "approved", "scheduled", "failed", "rejected", "needs_clarification"
    ]
    event: CalendarEvent
    actor_type: Literal["authenticated_user"] = "authenticated_user"
    reason: str | None = None
    # Canonical references -- METADATA about who/what this action concerns, entirely
    # separate from event.attendees (which must, and still does, always stay empty:
    # CalendarEvent's own validator is untouched by this addition). Lets "the calendar
    # action about Ashok" stay connected to PER-391 without ever adding him as an
    # external attendee on the real calendar event.
    person_id: str | None = None
    org_id: str | None = None
    meeting_id: str | None = None
