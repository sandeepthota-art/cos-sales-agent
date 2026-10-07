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


class PersonFactMention(BaseModel):
    """A qualitative fact EXPLICITLY attributed to one named person -- never a
    thread/organization-level signal (those stay in requirements/pain_points/
    objections/competitors/buying_signals, unchanged). person_name/person_email
    identify WHO this is about; the pipeline resolves that to a canonical
    person_id via app.entities.resolution.match_resolved_person_by_name against
    people ALREADY resolved for this same email (see app.pipeline._process_entities)
    -- never a fresh, riskier free-text scan of the whole people collection, and
    never guessed when the name is ambiguous (an unresolved mention is simply
    dropped, not attached to the wrong person).
    """

    person_name: str
    person_email: str | None = None
    category: Literal[
        "role", "responsibility", "preference", "goal", "interest",
        "concern", "pain_point", "objection", "buying_signal", "other",
    ]
    value: str
    basis: Literal["stated", "inferred"] = "stated"


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
    person_facts_mentioned: list[PersonFactMention] = Field(default_factory=list)
    goal_pillar: str = ""
    # BRD 6.1's six labels. "Needs reply: Soon" (this repo's original name for the
    # BRD's plain "Needs reply") is retired in favor of the BRD's own wording; "Needs
    # reply: mention" is new -- see app.providers.llm.claude._ANALYSIS_INSTRUCTIONS for
    # the per-label semantics drawn from the BRD. Every value additionally carries a
    # "1. " prefix (e.g. "1. Needs reply") -- a user-requested display-order marker,
    # not a BRD naming change.
    label_applied: Literal[
        "1. Needs reply: ASAP", "1. Needs reply", "1. Needs reply: mention", "1. Read only", "1. Delete", "1. Undecided"
    ] = "1. Undecided"
