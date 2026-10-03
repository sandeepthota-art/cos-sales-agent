"""Streamlit entrypoint for the CoS Staff EA Agent human-in-the-loop dashboard.

User-facing branding only: "CoS Staff EA Agent" (page title, headers, nav).
Backend module/package names, MongoDB collection names, and internal
identifiers are deliberately left unchanged -- renaming those would be a
pure-risk refactor with no user-facing benefit.

Run with: `streamlit run app/ui/dashboard.py`

"create calendar event" actions are triggered from this module exclusively
through `_render_calendar_approval_tab`'s explicit Approve-button handler
(via `approve_calendar_action`). Reply approval/sending has no tab in this
Streamlit dashboard (removed per explicit request) -- it remains reachable
via the React ReplyApprovalsPage and the reply-approval MCP tools.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

# Resolved relative to this file's own location, not the process's current working
# directory or how `streamlit run` happened to be invoked -- same class of problem,
# same fix, as app/config/settings.py's _ENV_FILE (see its own comment): `streamlit
# run app/ui/dashboard.py` does not reliably put the repo root on sys.path in every
# deployment context (confirmed failing on Render's Docker service -- "No module
# named 'app'" -- despite working from a plain local shell), because unlike `python
# -m app.mcp.server`, streamlit's own script runner does not treat the CWD the same
# way `-m` does. __file__ always resolves correctly regardless of invocation style.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.calendar.actions import approve_calendar_action, reject_calendar_action
from app.calendar.models import CalendarAction
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    FollowUpRepository,
    MeetingRepository,
    OpportunityRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
)
from app.providers.factory import ProviderFactory
from app.ui.column_descriptions import (
    COMMITMENTS_COLUMN_ORDER,
    DASHBOARD_METRIC_DESCRIPTIONS,
    EMAIL_COLUMN_ORDER,
    FOLLOW_UPS_COLUMN_ORDER,
    MEETINGS_COLUMN_ORDER,
    OPPORTUNITIES_COLUMN_ORDER,
    ORGANIZATIONS_COLUMN_ORDER,
    PEOPLE_COLUMN_ORDER,
    PROJECTS_COLUMN_ORDER,
    column_config_for,
    field_help,
)
from app.ui.data import (
    _thread_display_label,
    dashboard_metrics,
    list_calendar_actions,
    list_commitments,
    list_emails,
    list_follow_ups,
    list_knowledge,
    list_meetings,
    list_opportunities,
    list_organizations,
    list_people,
    list_projects,
    list_threads,
    org_label,
    person_label,
    thread_context_versions,
)

st.set_page_config(page_title="CoS Staff EA Agent", layout="wide")


@st.cache_resource
def _get_db():
    settings = get_settings()
    client = get_client(settings.mongodb_uri)
    return initialize_database(client, settings.mongodb_database)


def _render_dashboard_tab(db) -> None:
    metrics = dashboard_metrics(db)
    cols = st.columns(6)
    labels = [
        ("Emails processed", "emails_processed"),
        ("Threads", "threads"),
        ("Knowledge items", "knowledge_items"),
        ("Pending replies", "pending_replies"),
        ("Pending calendar actions", "pending_calendar_actions"),
        ("Failures", "failures"),
    ]
    for col, (label, key) in zip(cols, labels):
        col.metric(label, metrics[key], help=DASHBOARD_METRIC_DESCRIPTIONS.get(key))

    st.divider()
    _render_email_lookup_box(db)


_IST = ZoneInfo("Asia/Kolkata")
_IST_FORMAT = "%d %b %Y, %I:%M %p IST"


def _to_ist_string(iso_value: str | None) -> str | None:
    """Converts a stored UTC isoformat string to an IST display string. Returns
    None (never a guessed/invented value) if `iso_value` is absent or malformed.
    """
    if not iso_value:
        return None
    try:
        return datetime.fromisoformat(iso_value).astimezone(_IST).strftime(_IST_FORMAT)
    except ValueError:
        return None


def _format_processing_status(status: dict | None) -> str:
    """Display-only simplification: processing_status.stage has 11 real pipeline
    values (see app.processing.models.ProcessingStage), but this table only ever
    needs to distinguish done/failed/still-running. FAILED is kept distinct from
    "In Progress" rather than folded into it -- silently hiding a real failure
    would be misleading. updated_at (always a UTC isoformat string -- see
    EmailRepository.set_stage) is converted to IST for display, per explicit
    request; the stored value itself is untouched.
    """
    if not status:
        return "—"
    stage = status.get("stage")
    label = {"COMPLETED": "Completed", "FAILED": "Failed"}.get(stage, "In Progress")
    ist_time = _to_ist_string(status.get("updated_at"))
    return f"{label} · {ist_time}" if ist_time else label


def _format_entities_referenced(entities: dict | None) -> str:
    """Display-only: entities_referenced is a real {category: [ids]} dict on
    every Email document (e.g. {"people": [...], "projects": [...], "meetings":
    [], ...}) -- most categories are empty for any given email. Rather than show
    the raw dict (braces, empty-list categories included), this renders only the
    categories that actually have ids, as plain "category: id, id" text.
    """
    if not entities:
        return "—"
    parts = [f"{category}: {', '.join(ids)}" for category, ids in entities.items() if ids]
    return " · ".join(parts) if parts else "—"


def _format_participant(participant: dict | None) -> str:
    if not participant:
        return "—"
    return participant.get("email") or participant.get("name") or "—"


def _format_participants(participants: list[dict] | None) -> str:
    if not participants:
        return "—"
    return ", ".join(_format_participant(p) for p in participants) or "—"


def _render_email_lookup_box(db) -> None:
    """A CTO-facing "look up one email" box: pick a message from a dropdown, see
    every real, related piece of information about it in one place -- the email
    itself plus everything the pipeline actually derived from it (people,
    projects, commitments, follow-ups, meetings, opportunities, any reply draft,
    any calendar action). Every section is resolved from entities_referenced's
    real ids (never guessed), and a category with nothing in it is simply
    omitted, never shown as an empty table.
    """
    st.subheader("Look up an email")
    # A malformed/partially-written email document (e.g. a retry that left
    # only a processing_status stub behind) has none of the fields this box
    # displays -- it's excluded here rather than shown with fabricated
    # placeholder text, consistent with list_processed_emails's own handling
    # of the same kind of stub document.
    _REQUIRED_FIELDS = ("message_id", "subject", "from", "thread_id", "timestamp", "body")
    emails = sorted(
        (e for e in list_emails(db) if all(field in e for field in _REQUIRED_FIELDS)),
        key=lambda e: e.get("timestamp") or "",
        reverse=True,
    )
    if not emails:
        st.info("No emails yet.")
        return

    options = {f"{e['message_id']} — {e['subject']}": e for e in emails}
    choice = st.selectbox("Select a message", list(options.keys()), key="email_lookup_select")
    email = options[choice]

    st.markdown(f"#### {email['subject']}")
    col1, col2, col3, col4 = st.columns(4)
    col1.markdown(f"**From**  \n{_format_participant(email.get('from'))}")
    col2.markdown(f"**To**  \n{_format_participants(email.get('to'))}")
    col3.markdown(f"**Received**  \n{_to_ist_string(email.get('timestamp')) or '—'}")
    col4.markdown(f"**Status**  \n{_format_processing_status(email.get('processing_status'))}")

    st.caption(
        f"Thread: {email['thread_id']} · Label: {email.get('label_applied') or '—'} "
        f"· Goal pillar: {email.get('goal_pillar') or '—'}"
    )

    if email.get("body_summary"):
        st.markdown("**Summary**")
        st.markdown(email["body_summary"])
    else:
        st.caption("No summary yet -- ask Claude to summarize this email.")

    with st.expander("Full email body"):
        st.text(email.get("body") or "")

    entities = email.get("entities_referenced") or {}

    people_ids = entities.get("people") or []
    if people_ids:
        people = [p for p in (PersonRepository(db).find_one({"id": pid}) for pid in people_ids) if p]
        st.markdown("**Related People**")
        st.dataframe(
            [{"Name": p.get("name"), "Email": p.get("email"), "Role": p.get("role")} for p in people],
            hide_index=True,
        )

    project_ids = entities.get("projects") or []
    if project_ids:
        projects = [p for p in (ProjectRepository(db).find_one({"id": pid}) for pid in project_ids) if p]
        st.markdown("**Related Projects**")
        st.dataframe(
            [
                {"Project": p.get("project"), "Company": p.get("entity"), "Status": p.get("status")}
                for p in projects
            ],
            hide_index=True,
        )

    opportunity_ids = entities.get("opportunities") or []
    if opportunity_ids:
        opportunities = [
            o for o in (OpportunityRepository(db).find_one({"id": oid}) for oid in opportunity_ids) if o
        ]
        st.markdown("**Related Opportunities**")
        st.dataframe(
            [
                {
                    "Deal name": o.get("name"), "Stage": o.get("stage"), "Status": o.get("status"),
                    "Value": o.get("value"), "Currency": o.get("currency"),
                }
                for o in opportunities
            ],
            hide_index=True,
        )

    commitment_ids = entities.get("commitments") or []
    if commitment_ids:
        commitments = [
            c for c in (CommitmentRepository(db).find_one({"id": cid}) for cid in commitment_ids) if c
        ]
        st.markdown("**Related Commitments**")
        st.dataframe(
            [
                {
                    "Commitment": c.get("what"), "Class": c.get("class"),
                    "Owed by": c.get("owed_by"), "Owed to": c.get("owed_to"), "Status": c.get("status"),
                }
                for c in commitments
            ],
            hide_index=True,
        )

    follow_up_ids = entities.get("follow_ups") or []
    if follow_up_ids:
        follow_ups = [
            f for f in (FollowUpRepository(db).find_one({"id": fid}) for fid in follow_up_ids) if f
        ]
        rows = []
        for f in follow_ups:
            commitment = (
                CommitmentRepository(db).find_one({"id": f["commitment_id"]}) if f.get("commitment_id") else None
            )
            rows.append(
                {
                    "Commitment": commitment["what"] if commitment else None,
                    "Status": f.get("status"), "Audience": f.get("audience"),
                }
            )
        st.markdown("**Related Follow-ups**")
        st.dataframe(rows, hide_index=True)

    meeting_ids = entities.get("meetings") or []
    if meeting_ids:
        meetings = [m for m in (MeetingRepository(db).find_one({"id": mid}) for mid in meeting_ids) if m]
        st.markdown("**Related Meetings**")
        st.dataframe(
            [
                {
                    "Date": m.get("date"), "Attendees": ", ".join(m.get("attendees") or []),
                    "Actionable": m.get("actionable"),
                }
                for m in meetings
            ],
            hide_index=True,
        )

    draft = ReplyDraftRepository(db).find_one({"source_email_id": email["message_id"]})
    if draft:
        st.markdown("**Reply Draft**")
        gmail_status = "Synced to Gmail" if draft.get("gmail_draft_id") else "Not yet synced to Gmail"
        st.caption(f"Status: {draft.get('status')} · {gmail_status}")
        st.markdown(f"**{draft['draft']['subject']}**")
        st.text(draft["draft"]["body"])
    elif email.get("reply_withheld_reason"):
        st.markdown("**Reply Draft**")
        st.warning(f"No draft was created. Reason: {email['reply_withheld_reason']}")

    calendar_action = CalendarActionRepository(db).find_one({"thread_id": email["thread_id"]})
    if calendar_action:
        st.markdown("**Calendar Action**")
        event = calendar_action.get("event") or {}
        st.caption(
            f"Status: {calendar_action.get('status')} · {event.get('title')} · "
            f"{_to_ist_string(event.get('start')) or event.get('start')} – "
            f"{_to_ist_string(event.get('end')) or event.get('end')}"
        )


def _render_emails_tab(db) -> None:
    # Display-only flattening: `from` is a real {name, email} dict on every
    # Email document (frozen schema, untouched) -- the FastAPI/React path
    # still receives that full dict unchanged (app.ui.data.list_emails is not
    # touched here). This dashboard table alone shows just the plain email
    # address instead of the dict's raw repr, per explicit request.
    emails = list_emails(db)

    # in_reply_to is a raw Gmail threading header (a message id), not an address --
    # resolve it to the sender of the email it actually refers to. Indexed by both
    # source_message_id (the real Gmail id a threading header would reference) and
    # message_id (for data that never went through the canonical-id split), sender
    # address wins on either key, never fabricated if nothing matches.
    reply_sender_by_id: dict[str, str] = {}
    for email in emails:
        sender_address = _format_participant(email.get("from"))
        if email.get("source_message_id"):
            reply_sender_by_id[email["source_message_id"]] = sender_address
        if email.get("message_id"):
            reply_sender_by_id.setdefault(email["message_id"], sender_address)

    for email in emails:
        sender = email.get("from") or {}
        email["from"] = sender.get("email") or sender.get("name")
        email["processing_status"] = _format_processing_status(email.get("processing_status"))
        email["entities_referenced"] = _format_entities_referenced(email.get("entities_referenced"))
        # Shows the Claude-authored summary (set_email_summary) when one exists, so the
        # table is scannable at a glance -- falls back to the raw body (real data, never
        # blank) for an email that hasn't been summarized yet.
        email["body"] = email.get("body_summary") or email.get("body")
        in_reply_to = email.get("in_reply_to")
        # Falls back to the raw header value (real data, not fabricated) when no
        # matching email is stored -- e.g. a reply to a message outside this pipeline.
        email["in_reply_to"] = reply_sender_by_id.get(in_reply_to, in_reply_to) if in_reply_to else None

    st.dataframe(
        emails,
        column_config=column_config_for("emails"),
        column_order=EMAIL_COLUMN_ORDER,
    )


def _render_people_tab(db) -> None:
    st.dataframe(
        list_people(db),
        column_config=column_config_for("people"),
        column_order=PEOPLE_COLUMN_ORDER,
    )


def _render_organizations_tab(db) -> None:
    st.dataframe(
        list_organizations(db),
        column_config=column_config_for("organizations"),
        column_order=ORGANIZATIONS_COLUMN_ORDER,
    )


def _render_projects_tab(db) -> None:
    st.dataframe(
        list_projects(db),
        column_config=column_config_for("projects"),
        column_order=PROJECTS_COLUMN_ORDER,
    )


def _render_opportunities_tab(db) -> None:
    st.dataframe(
        list_opportunities(db),
        column_config=column_config_for("opportunities"),
        column_order=OPPORTUNITIES_COLUMN_ORDER,
    )


def _render_commitments_tab(db) -> None:
    st.dataframe(
        list_commitments(db),
        column_config=column_config_for("commitments"),
        column_order=COMMITMENTS_COLUMN_ORDER,
    )


def _render_follow_ups_tab(db) -> None:
    st.dataframe(
        list_follow_ups(db),
        column_config=column_config_for("follow_ups"),
        column_order=FOLLOW_UPS_COLUMN_ORDER,
    )


def _render_meetings_tab(db) -> None:
    st.dataframe(
        list_meetings(db),
        column_config=column_config_for("meetings"),
        column_order=MEETINGS_COLUMN_ORDER,
    )


def _render_thread_explorer_tab(db) -> None:
    threads = list_threads(db)
    if not threads:
        st.info("No threads yet.")
        return
    label_to_thread_id = {_thread_display_label(t): t["thread_id"] for t in threads}
    selected_label = st.selectbox(
        "Thread", list(label_to_thread_id.keys()), help=field_help("context_snapshots", "thread_id")
    )
    selected = label_to_thread_id[selected_label]
    for snapshot in thread_context_versions(db, selected):
        with st.expander(f"Context V{snapshot['context_version']} (triggered by {snapshot['triggering_email_id']})"):
            st.caption("Context", help=field_help("context_snapshots", "context"))
            st.json(snapshot["context"])
            st.caption("Changes", help=field_help("context_snapshots", "changes_from_previous_context"))
            for change in snapshot.get("changes_from_previous_context", []):
                st.write(f"{change['type']}: {change['field']} -> {change['detail']}")


def _render_context_evolution_tab(db) -> None:
    threads = list_threads(db)
    if not threads:
        st.info("No threads yet.")
        return
    label_to_thread_id = {_thread_display_label(t): t["thread_id"] for t in threads}
    selected_label = st.selectbox(
        "Thread ", list(label_to_thread_id.keys()), key="context_evolution_thread",
        help=field_help("context_snapshots", "thread_id"),
    )
    selected = label_to_thread_id[selected_label]
    versions = thread_context_versions(db, selected)
    st.markdown(
        " -> ".join(f"V{v['context_version']}" for v in versions),
        help=field_help("context_snapshots", "context_version"),
    )
    st.caption("Latest changes", help=field_help("context_snapshots", "changes_from_previous_context"))
    for change in versions[-1]["changes_from_previous_context"] if versions else []:
        st.write(f"{change['type']}: {change['field']} -> {change['detail']}")


def _render_knowledge_tab(db) -> None:
    for item in list_knowledge(db):
        basis_label = "STATED" if item["basis"] == "stated" else "AI INFERENCE"
        fact_help = f"{field_help('knowledge_items', 'basis')} {field_help('knowledge_items', 'confidence')}"
        st.markdown(
            f"**{item['subject_key']} {item['predicate']} = {item['current_value']}** "
            f"[{basis_label}] (confidence {item['confidence']:.2f})",
            help=fact_help,
        )
        with st.expander("History"):
            st.caption("History", help=field_help("knowledge_items", "history"))
            for entry in item["history"]:
                st.write(f"{entry['recorded_at']}: {entry['value']} (source {entry['source_email_id']})")


def _render_calendar_approval_tab(db, settings) -> None:
    repo = CalendarActionRepository(db)
    calendar_provider = None if settings.dashboard_read_only else ProviderFactory.create_calendar_provider(settings)

    for doc in list_calendar_actions(db, status="awaiting_approval") + list_calendar_actions(
        db, status="needs_clarification"
    ):
        action = CalendarAction.model_validate(doc)
        # Same CTO-facing context addition as the Reply Approval card above -- the
        # proposed meeting concerns a specific person/org, but that was previously
        # invisible here (only the underlying thread_id was used, in button keys,
        # never shown).
        person = person_label(db, action.person_id)
        org = org_label(db, action.org_id)
        meta = " · ".join(
            part for part in (
                f"Re: {person}" if person else None,
                org,
                f"Thread: {action.thread_id}",
            ) if part
        )
        if meta:
            st.caption(
                meta,
                help=f"{field_help('calendar_actions', 'person_id')} {field_help('calendar_actions', 'org_id')}",
            )
        st.markdown(f"**{action.event.title}**", help=field_help("calendar_actions", "event.title"))
        st.markdown(
            f"{action.event.start} - {action.event.end} ({action.event.timezone})",
            help=field_help("calendar_actions", "event.time"),
        )
        st.caption(
            "Attendees: Authenticated user only",
            help=(
                "UI-only text, not read from any database field. Real attendees "
                "(event.attendees) are always empty by design -- external attendees "
                "are never permitted."
            ),
        )
        if action.status == "needs_clarification":
            st.warning(f"Needs clarification: {action.reason}")
            continue
        if settings.dashboard_read_only:
            st.caption("Read-only view -- approval actions disabled.")
            continue
        col1, col2 = st.columns(2)
        if col1.button("Create on my calendar", key=f"create_{action.meeting_fingerprint}"):
            result = approve_calendar_action(action, calendar_provider)
            repo.upsert_by_key(
                {"thread_id": result.thread_id, "meeting_fingerprint": result.meeting_fingerprint},
                result.model_dump(mode="json"),
            )
            st.rerun()
        if col2.button("Ignore", key=f"ignore_{action.meeting_fingerprint}"):
            result = reject_calendar_action(action)
            repo.upsert_by_key(
                {"thread_id": result.thread_id, "meeting_fingerprint": result.meeting_fingerprint},
                result.model_dump(mode="json"),
            )
            st.rerun()


def _check_password_gate(settings) -> bool:
    """Unset DASHBOARD_PASSWORD bypasses the gate entirely (zero configuration for
    local development). When set, blocks every other render call until the exact
    password is entered -- st.session_state persists the unlocked flag across
    reruns within the same browser session so the prompt isn't re-shown on every
    interaction."""
    if not settings.dashboard_password:
        return True
    if st.session_state.get("dashboard_unlocked"):
        return True

    st.title("CoS Staff EA Agent")
    st.caption("Chief of Staff • Executive Assistant")
    entered = st.text_input("Password", type="password")
    if entered and entered == settings.dashboard_password:
        st.session_state["dashboard_unlocked"] = True
        st.rerun()
    elif entered:
        st.error("Incorrect password.")
    return False


def main() -> None:
    settings = get_settings()

    if not _check_password_gate(settings):
        return

    db = _get_db()

    st.title("CoS Staff EA Agent")
    st.caption("Chief of Staff • Executive Assistant")
    tabs = st.tabs(
        [
            "Dashboard",
            "Emails",
            "People",
            "Organizations",
            "Projects",
            "Opportunities",
            "Commitments",
            "Follow-ups",
            "Meetings",
            "Thread Explorer",
            "Context Evolution",
            "Knowledge",
            "Calendar Approval",
        ]
    )
    with tabs[0]:
        _render_dashboard_tab(db)
    with tabs[1]:
        _render_emails_tab(db)
    with tabs[2]:
        _render_people_tab(db)
    with tabs[3]:
        _render_organizations_tab(db)
    with tabs[4]:
        _render_projects_tab(db)
    with tabs[5]:
        _render_opportunities_tab(db)
    with tabs[6]:
        _render_commitments_tab(db)
    with tabs[7]:
        _render_follow_ups_tab(db)
    with tabs[8]:
        _render_meetings_tab(db)
    with tabs[9]:
        _render_thread_explorer_tab(db)
    with tabs[10]:
        _render_context_evolution_tab(db)
    with tabs[11]:
        _render_knowledge_tab(db)
    with tabs[12]:
        _render_calendar_approval_tab(db, settings)


main()
