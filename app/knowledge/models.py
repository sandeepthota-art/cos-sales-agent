from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HistoryEntry(BaseModel):
    value: str
    source_email_id: str
    recorded_at: datetime


class KnowledgeItem(BaseModel):
    knowledge_id: str
    thread_id: str
    subject_key: str
    predicate: str
    fact_key: str
    current_value: str
    # Canonical references, additive: set only when subject_key could be matched,
    # unambiguously, against a Person or Organization already resolved for this
    # thread (see app.pipeline._link_knowledge_to_entities). Not every knowledge item
    # is about a specific person or company -- a thread-level fact (e.g. from
    # requirements/pain_points) is neither and both stay None, which is correct, not
    # a gap to force-fill.
    person_id: str | None = None
    org_id: str | None = None
    history: list[HistoryEntry] = Field(default_factory=list)
    source_emails: list[str] = Field(default_factory=list)
    basis: Literal["stated", "inferred"]
    first_seen_at: datetime
    last_confirmed_at: datetime
    confidence: float
    status: Literal["active", "contradicted", "retracted"] = "active"
