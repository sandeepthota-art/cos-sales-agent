from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class MentionedPerson(BaseModel):
    name: str
    email: str | None = None
    org: str | None = None
    role_hint: str | None = None


class MentionedProject(BaseModel):
    name: str
    org: str | None = None
    objective_hint: str | None = None


class RawCommitment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    what: str
    commitment_class: Literal["mine", "owed_to_me", "theirs", "recap"] = Field(alias="class")
    owed_by: str | None = None
    owed_to: str | None = None
    date_phrase: str | None = None
    importance_hint: str | None = None


class RawMeeting(BaseModel):
    date_phrase: str | None = None
    attendees: list[str] = Field(default_factory=list)
    is_past: bool = False
    actions_raised: list[str] = Field(default_factory=list)


class RawPersonalItem(BaseModel):
    item_type: str
    description: str
    date_phrase: str | None = None


class Fact(BaseModel):
    subject: str
    predicate: str
    object: str


class EmailAnalysis(BaseModel):
    email_id: str
    summary: str
    intent: str
    entities: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    buying_signals: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    pricing_mentions: list[str] = Field(default_factory=list)
    commitments: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    meetings: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    people_mentioned: list[MentionedPerson] = Field(default_factory=list)
    projects_mentioned: list[MentionedProject] = Field(default_factory=list)
    commitments_mentioned: list[RawCommitment] = Field(default_factory=list)
    meetings_mentioned: list[RawMeeting] = Field(default_factory=list)
    personal_items_mentioned: list[RawPersonalItem] = Field(default_factory=list)
    goal_pillar: str = ""
    # BRD 6.1's six labels. "Needs reply: Soon" (this repo's original name for the
    # BRD's plain "Needs reply") is retired in favor of the BRD's own wording; "Needs
    # reply: mention" is new -- see app.providers.llm.claude._ANALYSIS_INSTRUCTIONS for
    # the per-label semantics drawn from the BRD.
    label_applied: Literal[
        "Needs reply: ASAP", "Needs reply", "Needs reply: mention", "Read only", "Delete", "Undecided"
    ] = "Undecided"
    # BRD 6.1: "each message is tagged P1 or P2 for business priority." The BRD gives
    # no finer-grained criteria than that (Section 12's own open-items list even names
    # "whether P1/P2 replaces the six-label scheme or sits alongside it" as unresolved)
    # -- no more specific rule is invented here. Defaults to "P2" (the lower-urgency
    # value) rather than "P1", so an unclassified/failed-analysis email fails safe by
    # under-flagging rather than over-flagging, matching "Undecided"'s own safe-default
    # role for label_applied.
    priority: Literal["P1", "P2"] = "P2"
    confidence: float = 0.0
