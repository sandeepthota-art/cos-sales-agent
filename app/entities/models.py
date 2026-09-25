from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Organization(BaseModel):
    id: str
    name: str
    domain: str | None = None
    aliases: list[str] = Field(default_factory=list)
    source: str = "gmail"


class Person(BaseModel):
    id: str
    name: str
    email: str | None = None
    aliases: list[str] = Field(default_factory=list)
    org: str | None = None
    # Canonical organization reference (app.entities.models.Organization), resolved via
    # email domain only (app.entities.resolution.resolve_organization) -- additive
    # alongside the pre-existing free-text `org` field, which is left untouched so no
    # historical document or existing behavior changes. None on any Person resolved
    # before this field existed, or with no email to derive a domain from.
    org_id: str | None = None
    type: str | None = None
    goal_pillar: str | None = None
    role_in_pillar: str | None = None
    tier: str | None = None
    voice_register: str | None = None
    last_inbound: datetime | None = None
    last_outbound: datetime | None = None
    reports_to: str | None = None
    open_threads: list[str] = Field(default_factory=list)
    note_link: str | None = None
    review_flag: bool = False
    source: str = "gmail"
    # Free-form, per-person drafting preferences (e.g. voice_signature,
    # remove_long_dash) -- set manually today (no extraction path writes this yet).
    # Read by app.replies.drafter.draft_reply and injected into the reply-drafting
    # system prompt (app.providers.llm.claude.ClaudeProvider.draft_reply) whenever
    # non-empty. Never populated from knowledge_items -- system/process instructions
    # like these belong here, not floating in the knowledge graph as extracted facts.
    preferences: dict[str, Any] = Field(default_factory=dict)
    # Phase 18 (app.duplicate_consolidation): a person retired via approved duplicate
    # consolidation is never physically deleted -- it's marked "merged" and points at
    # its canonical replacement, preserving historical identity. "active" (the
    # default) covers every person today, including one that predates this field
    # entirely (absent in Mongo, defaults to "active" on read). merged_into is only
    # ever set together with status == "merged", and only by the (not-yet-run)
    # consolidation executor -- nothing in this codebase sets it as of this change.
    status: str = "active"
    merged_into: str | None = None


class Project(BaseModel):
    id: str
    project: str
    cluster: str | None = None
    entity: str | None = None
    goal_pillar: str | None = None
    objective: str | None = None
    target: str | None = None
    status: str | None = None
    owner: str | None = None
    collaborators: list[str] = Field(default_factory=list)
    # Canonical references, additive alongside the pre-existing free-text `entity`/
    # `collaborators` (which are left untouched for display/backward compatibility).
    # Populated from Persons already resolved elsewhere in the same email whose org
    # matches this project's `entity` -- never from `entity` text alone (see
    # resolve_organization: company name similarity is never a merge/link signal).
    person_ids: list[str] = Field(default_factory=list)
    org_id: str | None = None
    next_milestone: str | None = None
    due: datetime | None = None
    health: str | None = None
    last_movement: datetime | None = None
    note_link: str | None = None
    source: str = "gmail"


class Commitment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    what: str
    commitment_class: Literal["mine", "owed_to_me", "theirs", "recap"] = Field(alias="class")
    importance: str | None = None
    owed_by: str | None = None
    owed_to: str | None = None
    # Canonical references, additive alongside owed_by/owed_to (kept unchanged for
    # display/backward compatibility). Set only when owed_by or owed_to could be
    # matched, unambiguously, against a Person already resolved from this same email
    # (see app.entities.resolution.match_resolved_person_by_name) -- never guessed
    # from the free-text name alone. A commitment involving "Ashok" stays connected to
    # PER-391 even when a later email calls him "Ash" or "Ashok Ganapam", because the
    # match happens against the canonical Person, not against this stored string.
    person_id: str | None = None
    org_id: str | None = None
    source_record: str
    made_on: datetime
    committed_date: datetime | None = None
    date_type: Literal["stated", "inferred", "window"] | None = None
    status: str = "open"
    goal_pillar: str | None = None
    project_id: str | None = None
    # The email's resolved thread -- always a real value on the live pipeline's own
    # creation path (app.entities.resolution.resolve_commitment always receives one),
    # but kept optional (not required) here so existing direct-construction call sites
    # that never cared about threading are unaffected.
    thread_id: str | None = None


class FollowUp(BaseModel):
    id: str
    commitment_id: str | None = None
    thread_id: str | None = None
    # Inherited directly from the parent Commitment when derived from one (never
    # independently inferred from a name) -- see app.pipeline._process_entities.
    person_id: str | None = None
    org_id: str | None = None
    # BRD 6.4's escalation ladder: "the item is surfaced in a brief [level 1]; then it
    # moves to the top of the queue with an explicit line that it needs a decision
    # [level 2]; then it gets its own email and a 15-minute hold on his calendar
    # [level 3]; then it stops when he acts or says to drop it [level 4]." These are
    # structural fields only -- nothing in this codebase transitions them yet (no
    # scheduler exists to decide *when* to advance a level or mark one surfaced; see
    # app.pipeline._process_entities, which always creates a FollowUp at level 1,
    # unsurfaced, active).
    escalation_level: Literal[1, 2, 3, 4] = 1
    surfaced: bool = False
    # "stops when he acts or says to drop it" names two distinct terminal reasons --
    # kept distinguishable rather than collapsed into one, since the BRD names both.
    status: Literal["active", "resolved", "dropped"] = "active"
    # BRD 6.4's four audience categories, used to pick the follow-up timing rule below.
    # None whenever the FollowUp doesn't come from a chased ("owed_to_me") commitment --
    # see app.entities.dates.classify_follow_up_timing, the sole place this is set.
    # "his_own_question" is a valid value in principle but is never actually produced
    # today -- nothing in the current extraction pipeline distinguishes an unanswered
    # question of his from any other commitment (see that function's own docstring).
    audience: Literal["internal", "client_fixed_date", "client_open_window", "his_own_question"] | None = None
    # The BRD states each timing rule as a RANGE ("one to two days", "two to three
    # business days"), never a single number -- these two fields preserve that range
    # rather than collapsing it into one invented point. Both None when no anchor date
    # exists to compute from (client_open_window) or no audience was classified.
    follow_up_earliest_at: datetime | None = None
    follow_up_latest_at: datetime | None = None

    @model_validator(mode="after")
    def _at_least_one_link(self) -> "FollowUp":
        # Was "exactly one" (commitment_id XOR thread_id). A FollowUp derived from a
        # Commitment now carries both: commitment_id for the existing link, plus
        # thread_id (copied from the commitment's own thread_id) so a FollowUp is
        # directly queryable by thread without following commitment_id -> Commitment
        # -> thread_id indirection. A thread-only FollowUp (no commitment) still sets
        # only thread_id. Only "neither set" remains invalid.
        if self.commitment_id is None and self.thread_id is None:
            raise ValueError("at least one of commitment_id or thread_id must be set")
        return self


class Meeting(BaseModel):
    id: str
    date: datetime | None = None
    attendees: list[str] = Field(default_factory=list)
    # Canonical references, additive alongside the free-text `attendees` (kept
    # unchanged for display/backward compatibility). Populated by matching each
    # attendee name against Persons already resolved from this same email -- never
    # from name text alone.
    person_ids: list[str] = Field(default_factory=list)
    org_id: str | None = None
    project_or_pillar: str | None = None
    minutes_record: str | None = None
    actions_raised: list[str] = Field(default_factory=list)
    next_meeting_date: datetime | None = None
    agenda_target: str | None = None
    actionable: bool = False
    agenda_written: bool = False
    # Same rationale as Commitment.thread_id above -- always real on the live creation
    # path (app.entities.resolution.resolve_meeting), optional here for the same
    # backward-compatibility reason.
    thread_id: str | None = None


class PersonalItem(BaseModel):
    id: str
    type: str
    description: str
    date_or_deadline: datetime | None = None
    status: str = "open"
    # The normalized sender address this item was extracted from -- also the
    # deduplication key app.entities.resolution.resolve_personal_item matches on.
    # Optional (not required) for the same backward-compatibility reason as
    # Commitment.thread_id/Meeting.thread_id above.
    sender_email: str | None = None
