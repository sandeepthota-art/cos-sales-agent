"""Pydantic models + loaders for Phase 5's golden eval files
(eval/golden/labeling_cases.jsonl, eval/golden/context_cases.jsonl).
Synthetic data only -- every email address here must be on a reserved
domain (example.com / *.example / *.test / *.invalid), enforced by a
validator so a real address can never slip into a golden file
undetected. Shared by scripts/load_golden.py, scripts/score_eval.py, and
tests/test_eval_golden_sets.py so the three can never drift on what a
valid golden case looks like.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

LabelValue = Literal[
    "1. Needs reply: ASAP",
    "1. Needs reply",
    "1. Needs reply: mention",
    "1. Read only",
    "1. Delete",
    "1. Undecided",
]

ALL_LABELS: set[str] = {
    "1. Needs reply: ASAP",
    "1. Needs reply",
    "1. Needs reply: mention",
    "1. Read only",
    "1. Delete",
    "1. Undecided",
}

# The 16 categories the Phase 5 spec requires at least one labeling case
# for -- checked by tests/test_eval_golden_sets.py, not enforced here (a
# single case's category is just a free-form tag; completeness is a
# property of the WHOLE file, not one record).
REQUIRED_LABELING_CATEGORIES: set[str] = {
    "direct_question_to_owner",
    "deadline_asap_request",
    "please_review_mention_only_in_cc",
    "owner_only_cc_fyi",
    "newsletter",
    "marketing_cold_outreach",
    "automated_notification",
    "receipt_confirmation",
    "owner_already_replied_no_reply_needed",
    "resolved_thank_you",
    "relabel_on_new_message",
    "ambiguous_undecided",
    "prompt_injection_attempt",
    "long_quoted_history_question_in_quote",
    "free_mail_sender_real_question",
    "short_email_needs_reply",
}


def is_reserved_domain(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    return domain == "example.com" or domain.endswith(".example") or domain.endswith(".test") or domain.endswith(".invalid")


def _validate_reserved(email: str) -> str:
    if not is_reserved_domain(email):
        raise ValueError(f"email must use a reserved domain (example.com/.example/.test/.invalid): {email!r}")
    return email


class GoldenAddress(BaseModel):
    name: str | None = None
    email: str

    @field_validator("email")
    @classmethod
    def _reserved(cls, v: str) -> str:
        return _validate_reserved(v)


class GoldenMessage(BaseModel):
    model_config = {"populate_by_name": True}

    message_id: str
    thread_id: str
    from_: GoldenAddress = Field(alias="from")
    to: list[GoldenAddress]
    cc: list[GoldenAddress] = Field(default_factory=list)
    subject: str
    body: str
    sent_at: str


def to_email_doc(message: GoldenMessage) -> dict:
    """Shape load_golden.py feeds to app.models.parse_email -- the SAME
    Email schema the real pipeline validates against, so ingestion
    hygiene (thread resolution, default field creation) is exercised
    identically for golden data."""
    return {
        "message_id": message.message_id,
        "thread_id": message.thread_id,
        "from": message.from_.model_dump(),
        "to": [a.model_dump() for a in message.to],
        "cc": [a.model_dump() for a in message.cc],
        "subject": message.subject,
        "body": message.body,
        "timestamp": message.sent_at,
    }


class LabelingCase(BaseModel):
    case_id: str
    category: str
    owner_email: str
    target_message_id: str
    expected_label: LabelValue
    rationale: str
    messages: list[GoldenMessage] = Field(min_length=1)

    @field_validator("owner_email")
    @classmethod
    def _owner_reserved(cls, v: str) -> str:
        return _validate_reserved(v)

    @model_validator(mode="after")
    def _target_and_uniqueness(self) -> "LabelingCase":
        ids = [m.message_id for m in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError(f"case {self.case_id!r}: duplicate message_id within case")
        if self.target_message_id not in ids:
            raise ValueError(f"case {self.case_id!r}: target_message_id {self.target_message_id!r} not among messages")
        return self


class RequiredFact(BaseModel):
    entity_type: Literal["person", "organization"]
    entity_key: str
    attribute: str
    value: str
    evidence: str


class ForbiddenFact(BaseModel):
    entity_type: Literal["person", "organization"]
    entity_key: str
    attribute: str
    reason: str


class ContextCase(BaseModel):
    case_id: str
    owner_email: str
    rationale: str
    messages: list[GoldenMessage] = Field(min_length=1)
    required_facts: list[RequiredFact] = Field(default_factory=list)
    forbidden_facts: list[ForbiddenFact] = Field(default_factory=list)

    @field_validator("owner_email")
    @classmethod
    def _owner_reserved(cls, v: str) -> str:
        return _validate_reserved(v)

    @model_validator(mode="after")
    def _unique_message_ids(self) -> "ContextCase":
        ids = [m.message_id for m in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError(f"case {self.case_id!r}: duplicate message_id within case")
        return self


def _load_jsonl(path: str | Path, model: type[BaseModel]) -> list[BaseModel]:
    cases = []
    seen_ids: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            case = model.model_validate(raw)
            if case.case_id in seen_ids:
                raise ValueError(f"{path}:{line_no}: duplicate case_id {case.case_id!r}")
            seen_ids.add(case.case_id)
            cases.append(case)
    return cases


def load_labeling_cases(path: str | Path) -> list[LabelingCase]:
    return _load_jsonl(path, LabelingCase)


def load_context_cases(path: str | Path) -> list[ContextCase]:
    return _load_jsonl(path, ContextCase)
