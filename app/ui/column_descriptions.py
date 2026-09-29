"""Column/field descriptions shown as info-icon tooltips on the dashboard.

Single source of truth for what each dashboard-displayed field means, kept in
sync with `docs/DATA_DICTIONARY.md` (that document is authoritative; this
module restates it concisely for on-screen display, it does not redefine
anything). Keyed by collection first, then field name, because several field
names (`status`, `stage`, `owner`, `goal_pillar`, `confidence`, ...) mean
different things in different collections -- see DATA_DICTIONARY.md's
"Distinguishing `goal_pillar` and `label_applied`" section and its
per-collection tables for the full explanation each string here summarizes.

Only fields actually rendered somewhere in `app/ui/dashboard.py` need an
entry here. A field declared in a model but never observed to be set by any
code path still gets an entry (the dashboard shows the column either way) --
its description says so explicitly, matching DATA_DICTIONARY.md's own
"Not observed to be set by any current code path" language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import streamlit as st

_NOT_SET = "Declared on the model; not currently set by any current code path."

# Raw dataframe-dump tabs (`st.dataframe(list_X(db))`) -- one entry per column
# that appears in the resulting table, i.e. one entry per persisted field on
# that collection's documents.
COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "emails": {
        "id": "Internal human-readable email identifier (EML-nnn). Additive only -- never used for deduplication; message_id remains the canonical dedup key.",
        "message_id": "Original source/Gmail message identifier. Used for deduplication and source traceability (MongoDB dedup key).",
        "thread_id": "Existing resolved thread identifier this email belongs to. May be a source Gmail thread id, an inherited resolved id, or the application's synthetic fallback -- see the threads collection's own thread_id tooltip.",
        "from": "Sender name and email address.",
        "to": "Recipient name(s) and email address(es).",
        "cc": "CC'd recipient name(s) and email address(es).",
        "subject": "Email subject line.",
        "body": "Full email body text.",
        "timestamp": "When the email was sent.",
        "in_reply_to": "Threading header: the message id this email replies to.",
        "references": "Threading header: chain of prior message ids in this thread.",
        "attachments": "Attachment filenames.",
        "labels": "Gmail label ids, plus the applied triage label (label_applied) once analysis completes.",
        "processing_status": "Current pipeline stage (e.g. COMPLETED, FAILED), plus any error.",
        "record_id": "Bookkeeping id; currently always identical to message_id.",
        "date": "The email's timestamp, date component only (YYYY-MM-DD).",
        "entities_referenced": "IDs of every Person/Project/Commitment/Meeting/etc. this email produced.",
        "goal_pillar": (
            'Business goal category identified during email analysis. Only "Sales" has '
            "defined downstream behavior (it gates Opportunity creation); other values are "
            "stored but not otherwise acted on. Independent of label_applied."
        ),
        "label_applied": (
            'Six-way reply-urgency triage label (e.g. "Needs reply", "Read only"). '
            "Independent of goal_pillar -- an email can be Sales-classified and Read-only "
            "at the same time."
        ),
    },
    "people": {
        "id": "Canonical person id (PER-###).",
        "name": "Display name.",
        "email": "Email address, lowercased. Omitted entirely (not just null) when unknown.",
        "aliases": "Alternate names proven to belong to this person by an actual name/org match.",
        "org": "Free-text company name, as mentioned in email.",
        "org_id": "Canonical Organization this person belongs to, resolved by email domain.",
        "type": 'Set to "operator" for the configured mailbox owner; otherwise unset.',
        "goal_pillar": _NOT_SET,
        "last_inbound": "Most recent email known received FROM this person (only ever moves forward in time).",
        "last_outbound": "Most recent email known sent TO this person (only ever moves forward in time).",
        "open_threads": "Every conversation thread this person has been part of.",
        "note_link": _NOT_SET,
        "source": 'Where this record originated -- currently always "gmail".',
        "status": '"active", or "merged" if retired into another canonical person by an admin duplicate-consolidation run.',
        "merged_into": 'If status is "merged", the canonical person id this record now points to.',
    },
    "organizations": {
        "id": "Canonical organization id (ORG-###).",
        "name": "Display name -- may be just the email domain until a real name is supplied.",
        "domain": "Email domain -- the sole identity key used to match organizations.",
        "aliases": _NOT_SET,
        "source": 'Where this record originated -- currently always "gmail".',
    },
    "projects": {
        "id": "Canonical project id (PRJ-###).",
        "project": "Project/deal name, as mentioned in email.",
        "cluster": _NOT_SET,
        "entity": "Free-text company name associated with this project.",
        "goal_pillar": (
            "Copied from the triggering email's goal_pillar. Combined with a resolved "
            'project mention, "Sales" is what creates an Opportunity record on the '
            "Opportunities tab (a separate collection)."
        ),
        "objective": _NOT_SET,
        "target": _NOT_SET,
        "status": _NOT_SET,
        "owner": _NOT_SET + " (Different field from the human-managed opportunities.owner on the Opportunities tab.)",
        "collaborators": _NOT_SET,
        "person_ids": "People already resolved for the same email whose company matches this project's entity.",
        "org_id": "Organization matched to this project.",
        "next_milestone": _NOT_SET,
        "due": _NOT_SET,
        "health": _NOT_SET,
        "last_movement": _NOT_SET,
        "note_link": _NOT_SET,
        "source": 'Where this record originated -- currently always "gmail".',
    },
    "opportunities": {
        "id": "Canonical opportunity id (OPP-###).",
        "name": "Deal name, copied from the resolving project mention.",
        "entity": "Free-text company name, mirrors the linked project's entity.",
        "org_id": "Canonical Organization for this deal.",
        "description": _NOT_SET,
        "status": (
            '"open", "won", or "lost". Only "open" is ever set today -- there is currently '
            "no way to set it to \"won\"/\"lost\" (update_opportunity_fields doesn't accept "
            "a status parameter)."
        ),
        "stage": "Free-text sales-pipeline stage (e.g. \"Discovery\"). Human-managed only -- never inferred from email content.",
        "owner": "Free-text CRM owner. Human-managed only.",
        "value": "Deal value. Human-managed only.",
        "currency": "Human-managed only.",
        "expected_close_date": "Human-managed only.",
        "next_action": "Human-managed only.",
        "source_email_ids": "Every email that has touched this deal -- append-only.",
        "project_ids": (
            "The Project(s) this deal is linked to -- also the sole dedup key: a second "
            "Sales email resolving to the same project is always the same Opportunity."
        ),
        "meeting_ids": "Meetings detected alongside this deal -- append-only.",
        "person_ids": "People resolved alongside this deal -- append-only.",
        "buying_signals": "Plain-text buying-signal strings copied from analysis -- append-only.",
        "last_activity_at": "Bumped automatically every time a new email/meeting touches this deal.",
        "created_at": "When this Opportunity was first created.",
        "updated_at": "When this Opportunity was last changed, automatically or by a human.",
    },
    "commitments": {
        "id": "Canonical commitment id (CMT-###).",
        "what": "The commitment text.",
        "class": (
            '"mine", "owed_to_me", "theirs", or "recap". Only "mine"/"owed_to_me" are ever '
            "chased (produce a Follow-up)."
        ),
        "importance": "Free-text importance hint from analysis.",
        "owed_by": "Free-text name of who owes this commitment.",
        "owed_to": "Free-text name of who this commitment is owed to.",
        "person_id": "Canonical person matched to owed_by/owed_to, when unambiguous.",
        "org_id": "That matched person's organization.",
        "source_record": "The message_id this commitment was extracted from.",
        "made_on": "Timestamp of the email this commitment was made in.",
        "committed_date": "Resolved due date, if a date phrase was recognized.",
        "date_type": 'How committed_date was derived: "stated", "inferred", or "window".',
        "status": 'Declared as "open" by default; no code path currently changes it.',
        "goal_pillar": "Copied from the triggering email's goal_pillar.",
        "project_id": "Linked project, only when unambiguous (exactly one project shares the counterparty's organization).",
        "thread_id": "Conversation thread this commitment belongs to.",
    },
    "follow_ups": {
        "id": "Canonical follow-up id (FUP-###).",
        "commitment_id": "The commitment this follow-up chases.",
        "thread_id": "Conversation thread this follow-up belongs to.",
        "person_id": "Inherited directly from the parent commitment.",
        "org_id": "Inherited directly from the parent commitment.",
        "escalation_level": (
            "BRD escalation ladder (1-4). No scheduler currently advances this -- every "
            "follow-up stays at level 1 today."
        ),
        "surfaced": "Whether this item has been surfaced to the user. No code path currently sets this true.",
        "status": '"active", "resolved", or "dropped". No code path currently changes it from "active".',
        "audience": (
            "Who this follow-up concerns: internal, a client with a fixed date, or a client "
            "with an open time window."
        ),
        "follow_up_earliest_at": "Start of the recommended follow-up window.",
        "follow_up_latest_at": "End of the recommended follow-up window.",
    },
    "meetings": {
        "id": "Canonical meeting id (MTG-###).",
        "date": "Resolved meeting date, if a date phrase was recognized.",
        "attendees": "Free-text attendee names, as mentioned in email.",
        "person_ids": "Attendees matched against canonical people already resolved for this email.",
        "org_id": "An attendee's organization.",
        "project_or_pillar": (
            "Copied from the email's goal_pillar; used to categorize the meeting "
            "(e.g. Sales, Finance)."
        ),
        "minutes_record": _NOT_SET,
        "actions_raised": "Free-text action items raised in the meeting mention.",
        "next_meeting_date": _NOT_SET,
        "agenda_target": _NOT_SET,
        "actionable": "True for a future-oriented meeting mention; false only when explicitly flagged as already past.",
        "agenda_written": _NOT_SET,
        "thread_id": "Conversation thread this meeting belongs to.",
    },
    "personal_items": {
        "id": "Canonical personal-item id (PSN-###).",
        "type": 'Free-text item type, e.g. "reminder".',
        "description": "Item description.",
        "date_or_deadline": "Resolved deadline, if a date phrase was recognized.",
        "status": '"open", or "reminded" once a separate reminder scheduler has triggered on it.',
        "sender_email": "Sender address this item was extracted from.",
    },
    # Thread Explorer / Context Evolution (custom widgets, not a raw dataframe
    # dump -- see the inline `help=`/`st.caption(..., help=...)` calls in
    # `app/ui/dashboard.py` that use these same strings).
    "context_snapshots": {
        "thread_id": "Which conversation thread this snapshot belongs to.",
        "context_version": "1-based version number of this thread's accumulated context.",
        "triggering_email_id": "The email that produced this version.",
        "context": (
            "The full accumulated deal/conversation context as of this version "
            "(requirements, pain points, pricing, buying signals, etc.), shown raw."
        ),
        "changes_from_previous_context": "What this version added, removed, or updated versus the previous one.",
    },
    "knowledge_items": {
        "subject_key": "Who or what this fact is about.",
        "predicate": 'The relation, e.g. "requires", "has_pain_point".',
        "current_value": "The fact's current value.",
        "basis": 'Whether the fact was directly stated ("STATED") or inferred by the model ("AI INFERENCE").',
        "confidence": (
            "Starts at 0.75 and increases by 0.01 (capped at 0.99) each time this fact is "
            "re-confirmed by a later email."
        ),
        "history": "Every prior value this fact has had, oldest first.",
    },
    "reply_drafts": {
        "draft.subject": "The drafted reply's subject line.",
        "draft.body": "The drafted reply's body text.",
    },
    "calendar_actions": {
        "event.title": "The proposed meeting's title.",
        "event.time": "The proposed meeting's start and end time.",
        "reason": "Why this proposal needs clarification (shown only when it does).",
    },
}

# The 6 Dashboard-tab metric cards (`app/ui/data.py:dashboard_metrics`) -- not
# a table, but `st.metric` natively supports `help=`, so these get the same
# tooltip treatment for consistency.
DASHBOARD_METRIC_DESCRIPTIONS: dict[str, str] = {
    "emails_processed": "Total email documents ever written, at any processing stage.",
    "threads": "Total distinct conversation threads created.",
    "knowledge_items": "Total extracted facts stored.",
    "pending_replies": 'Reply drafts currently awaiting a human decision (status = "awaiting_approval").',
    "pending_calendar_actions": (
        'Detected meetings awaiting a human decision, including ones needing '
        "clarification."
    ),
    "failures": "Emails whose processing pipeline stopped with an error.",
}


def field_help(collection: str, field: str) -> str:
    """The tooltip text for one field, or an explicit "undocumented" notice --
    never a guessed/invented description. Callers pass this straight into a
    Streamlit `help=` parameter or a `column_config` entry.
    """
    return COLUMN_DESCRIPTIONS.get(collection, {}).get(
        field, f"Not yet documented in docs/DATA_DICTIONARY.md ({collection}.{field})."
    )


def column_config_for(collection: str) -> dict[str, "st.column_config.Column"]:
    """Builds a `column_config` dict for `st.dataframe(..., column_config=...)`.
    Only sets `help` -- column order, labels, formatting, sorting, and the
    download/search toolbar are all left exactly as Streamlit's defaults
    already render them.
    """
    import streamlit as st  # local import: keeps this module importable/testable without Streamlit installed

    return {
        field: st.column_config.Column(help=description)
        for field, description in COLUMN_DESCRIPTIONS.get(collection, {}).items()
    }
