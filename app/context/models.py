from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProvenancedValue(BaseModel):
    value: str
    basis: Literal["stated", "inferred"]
    source_email_ids: list[str] = Field(default_factory=list)


class ThreadContext(BaseModel):
    summary: str = ""
    participants: list[str] = Field(default_factory=list)
    company: dict = Field(default_factory=dict)
    opportunity: dict = Field(default_factory=dict)
    requirements: list[ProvenancedValue] = Field(default_factory=list)
    pain_points: list[ProvenancedValue] = Field(default_factory=list)
    products_discussed: list[ProvenancedValue] = Field(default_factory=list)
    competitors: list[ProvenancedValue] = Field(default_factory=list)
    pricing: dict = Field(default_factory=dict)
    objections: list[ProvenancedValue] = Field(default_factory=list)
    buying_signals: list[ProvenancedValue] = Field(default_factory=list)
    decisions: list[ProvenancedValue] = Field(default_factory=list)
    commitments: list[ProvenancedValue] = Field(default_factory=list)
    open_questions: list[ProvenancedValue] = Field(default_factory=list)
    next_actions: list[ProvenancedValue] = Field(default_factory=list)
    meetings: list[ProvenancedValue] = Field(default_factory=list)


class ContextChange(BaseModel):
    type: Literal["ADDED", "REMOVED", "UPDATED"]
    field: str
    detail: str
    source_email_id: str


class ContextSnapshot(BaseModel):
    thread_id: str
    context_version: int
    triggering_email_id: str
    context: ThreadContext
    changes_from_previous_context: list[ContextChange] = Field(default_factory=list)
    created_at: datetime


LIST_FIELDS: tuple[str, ...] = (
    "requirements",
    "pain_points",
    "products_discussed",
    "competitors",
    "objections",
    "buying_signals",
    "decisions",
    "commitments",
    "open_questions",
    "next_actions",
    "meetings",
)

DICT_FIELDS: tuple[str, ...] = ("company", "opportunity", "pricing")


class DeltaItem(BaseModel):
    """One new value to add to a ProvenancedValue list field. Deliberately excludes
    source_email_ids -- the current email's message_id is always known to the
    deterministic merge code that applies the delta (app.context.engine.apply_context_delta),
    so asking the LLM to reproduce it would only add a way for the LLM to get it wrong,
    for no benefit."""

    value: str
    basis: Literal["stated", "inferred"] = "stated"


class ListFieldDelta(BaseModel):
    """Bounded change to one ProvenancedValue list field: values to add (each carrying
    its own basis) and prior values to invalidate/remove. Omitting a field entirely (or
    leaving both lists empty) means "no change to this field for this email" -- the
    deterministic merge leaves everything already in that field untouched, exactly as
    the previous full-context-echo contract did for a value the LLM repeated unchanged."""

    added: list[DeltaItem] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class ContextDelta(BaseModel):
    """What one email changes about a thread's ThreadContext, bounded to just that
    change -- never the full accumulated context. This is the shape LLMProvider.
    update_context must now return (see app.interfaces.llm_provider.LLMProvider).

    One ListFieldDelta per ThreadContext list field, keyed by the same field name (so
    app.context.engine.apply_context_delta can iterate LIST_FIELDS and getattr(delta,
    field) directly, in lockstep with getattr(thread_context, field)). `participants`
    (list[str], not list[ProvenancedValue]) and the three DICT_FIELDS use their own
    shapes below since their underlying types differ from the ProvenancedValue lists.
    """

    summary: str | None = None

    participants_added: list[str] = Field(default_factory=list)
    participants_removed: list[str] = Field(default_factory=list)

    # A DICT_FIELD update: an entry mapped to None means "remove this key"; any other
    # value means "set/overwrite this key" -- mirrors app.context.diff.diff_context's
    # existing per-key ADDED/REMOVED/UPDATED treatment of these three dict fields.
    company_updates: dict[str, Any] = Field(default_factory=dict)
    opportunity_updates: dict[str, Any] = Field(default_factory=dict)
    pricing_updates: dict[str, Any] = Field(default_factory=dict)

    requirements: ListFieldDelta = Field(default_factory=ListFieldDelta)
    pain_points: ListFieldDelta = Field(default_factory=ListFieldDelta)
    products_discussed: ListFieldDelta = Field(default_factory=ListFieldDelta)
    competitors: ListFieldDelta = Field(default_factory=ListFieldDelta)
    objections: ListFieldDelta = Field(default_factory=ListFieldDelta)
    buying_signals: ListFieldDelta = Field(default_factory=ListFieldDelta)
    decisions: ListFieldDelta = Field(default_factory=ListFieldDelta)
    commitments: ListFieldDelta = Field(default_factory=ListFieldDelta)
    open_questions: ListFieldDelta = Field(default_factory=ListFieldDelta)
    next_actions: ListFieldDelta = Field(default_factory=ListFieldDelta)
    meetings: ListFieldDelta = Field(default_factory=ListFieldDelta)


DICT_DELTA_FIELDS: dict[str, str] = {
    "company": "company_updates",
    "opportunity": "opportunity_updates",
    "pricing": "pricing_updates",
}
