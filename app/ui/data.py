"""Pure data-fetching helpers for the Streamlit dashboard.

These functions take a `db` handle and return plain dicts/lists so they can be
unit tested without spinning up Streamlit.
"""

from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OpportunityRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)


def dashboard_metrics(db) -> dict:
    failures = db.emails.count_documents({"processing_status.stage": "FAILED"})
    return {
        "emails_processed": db.emails.count_documents({}),
        "threads": db.threads.count_documents({}),
        "knowledge_items": db.knowledge_items.count_documents({}),
        "pending_replies": db.reply_drafts.count_documents({"status": "awaiting_approval"}),
        "pending_calendar_actions": db.calendar_actions.count_documents(
            {"status": {"$in": ["awaiting_approval", "needs_clarification"]}}
        ),
        "failures": failures,
    }


def _with_priority_keys_first(doc: dict, priority_keys: tuple[str, ...]) -> dict:
    """Reorders a dict so `priority_keys` (that exist in `doc`) come first, with
    every other key following in its original order. Streamlit's dataframe column
    order follows the underlying data's own key order (see column_descriptions.py's
    column_config_for docstring); for the Emails tab specifically, the dashboard's
    actual visible order/visibility is additionally controlled by
    EMAIL_COLUMN_ORDER (passed as `column_order` in `app/ui/dashboard.py`) -- this
    function's dict-level reordering just keeps a sane key order for any other,
    non-Streamlit consumer of `list_emails`.
    """
    ordered = {key: doc[key] for key in priority_keys if key in doc}
    ordered.update(doc)
    return ordered


def list_emails(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    # Newest first by actual send time -- never by `id`/`message_id`/insertion
    # order, which reflect processing order, not when the email was sent.
    # Sorted server-side (Mongo, via find_many's sort param) rather than in
    # Python, so limit/offset (React/FastAPI migration pagination) actually
    # avoid fetching the whole collection -- the prior Python-side `sorted()`
    # would have defeated that entirely. Streamlit's own call site (no
    # limit/offset) is completely unaffected: same documents, same order.
    docs = EmailRepository(db).find_many({}, sort=[("timestamp", -1)], skip=offset, limit=limit)
    return [
        _with_priority_keys_first(doc, ("message_id", "thread_id", "source_message_id", "source_thread_id"))
        for doc in docs
    ]


def list_threads(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return ThreadRepository(db).find_many({}, skip=offset, limit=limit)


def _thread_display_label(thread: dict) -> str:
    """`thread_id` (THR-nnn) is the primary, human-readable label; `source_thread_id`
    -- the genuine original Gmail/provider thread id -- stays visible alongside it
    for traceability when the source actually supplied one. Falls back to just
    `thread_id` when there is no source_thread_id (common: many threads are
    resolved by subject/participant/in_reply_to matching, not an explicit source
    thread id).

    Bug fix (threads-collection schema cleanup, second collection-by-collection
    pass): this used to combine `thread['id']` with `thread['thread_id']` --
    always-identical values (both THR-nnn, verified in app.pipeline._upsert_thread),
    so it rendered a misleading "THR-001 (THR-001)" rather than ever showing the
    genuinely distinct source_thread_id.
    """
    source_thread_id = thread.get("source_thread_id")
    return f"{thread['thread_id']} ({source_thread_id})" if source_thread_id else thread["thread_id"]


def thread_context_versions(db, thread_id: str) -> list[dict]:
    return ContextSnapshotRepository(db).all_for_thread(thread_id)


def list_knowledge(
    db, thread_id: str | None = None, *, limit: int | None = None, offset: int = 0
) -> list[dict]:
    repo = KnowledgeRepository(db)
    if thread_id:
        # all_for_thread is a small, already-bounded, thread-scoped read --
        # pagination isn't meaningful here the way it is for an unbounded
        # collection-wide list, so it's intentionally not threaded through.
        return repo.all_for_thread(thread_id)
    return repo.find_many({}, skip=offset, limit=limit)


def list_reply_drafts(
    db, status: str | None = None, *, limit: int | None = None, offset: int = 0
) -> list[dict]:
    repo = ReplyDraftRepository(db)
    query = {"status": status} if status else {}
    return repo.find_many(query, skip=offset, limit=limit)


def list_calendar_actions(
    db, status: str | None = None, *, limit: int | None = None, offset: int = 0
) -> list[dict]:
    repo = CalendarActionRepository(db)
    query = {"status": status} if status else {}
    return repo.find_many(query, skip=offset, limit=limit)


def list_people(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return PersonRepository(db).find_many({}, skip=offset, limit=limit)


def list_organizations(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return OrganizationRepository(db).find_many({}, skip=offset, limit=limit)


def person_label(db, person_id: str | None) -> str | None:
    """Promoted out of app.ui.dashboard (React/FastAPI migration,
    docs/REACT_MIGRATION_PLAN.md's shared-layer requirement) so both the
    Streamlit Reply/Calendar approval cards and the new API's equivalent
    endpoints resolve a person_id to a display name identically, from one
    single source, rather than two copies of the same lookup."""
    if not person_id:
        return None
    person = PersonRepository(db).find_one({"id": person_id})
    return person["name"] if person else person_id


def org_label(db, org_id: str | None) -> str | None:
    """Promoted out of app.ui.dashboard -- see person_label's docstring."""
    if not org_id:
        return None
    org = OrganizationRepository(db).find_one({"id": org_id})
    return org["name"] if org else org_id


def list_projects(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return ProjectRepository(db).find_many({}, skip=offset, limit=limit)


def list_opportunities(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return OpportunityRepository(db).find_many({}, skip=offset, limit=limit)


def list_commitments(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return CommitmentRepository(db).find_many({}, skip=offset, limit=limit)


def list_follow_ups(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Follow-up/Commitment product gap: FollowUp has no `what` text of its own
    -- it lives on the parent Commitment (`commitment_id`). Rather than
    duplicate that text into every FollowUp document, this derives `what`
    (and readable `person_name`/`org_name`, resolved the same way the Reply/
    Calendar approval cards already do) here, at the UI layer only -- never
    persisted back to MongoDB, recomputed fresh on every read. A thread-only
    follow-up (no commitment_id -- see FollowUp's own model validator) simply
    gets `what: None`, never a fabricated value.

    limit/offset apply to the base FollowUp query, before enrichment -- this
    also bounds the number of per-row Commitment/Person/Organization lookups
    below, not just the final result size.
    """
    follow_ups = FollowUpRepository(db).find_many({}, skip=offset, limit=limit)
    commitment_repo = CommitmentRepository(db)
    person_repo = PersonRepository(db)
    org_repo = OrganizationRepository(db)

    enriched = []
    for follow_up in follow_ups:
        what = None
        commitment_id = follow_up.get("commitment_id")
        if commitment_id:
            commitment = commitment_repo.find_one({"id": commitment_id})
            what = commitment["what"] if commitment else None

        person_id = follow_up.get("person_id")
        person = person_repo.find_one({"id": person_id}) if person_id else None
        org_id = follow_up.get("org_id")
        org = org_repo.find_one({"id": org_id}) if org_id else None

        enriched.append(
            {
                "what": what,
                "person_name": person["name"] if person else None,
                "org_name": org["name"] if org else None,
                **follow_up,
            }
        )
    return enriched


def list_meetings(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Meeting product gap: Meeting has no title/subject field of its own
    (confirmed in app.entities.models.Meeting) -- only date/attendees/thread_id.
    Rather than introduce a new stored field, this derives a display-only
    `title` here, at the UI layer, from the earliest email in the meeting's own
    thread (consistent with how Thread.normalized_subject is itself derived
    from the first email) -- never persisted back to MongoDB, recomputed fresh
    on every read. Falls back to the bare thread_id when no matching email
    exists (e.g. a meeting inserted directly in a test with no real thread).

    limit/offset apply to the base Meeting query, before title derivation.
    """
    meetings = MeetingRepository(db).find_many({}, skip=offset, limit=limit)
    email_repo = EmailRepository(db)
    title_by_thread_id: dict[str, str] = {}

    for meeting in meetings:
        thread_id = meeting.get("thread_id")
        if not thread_id or thread_id in title_by_thread_id:
            continue
        thread_emails = sorted(
            email_repo.find_many({"thread_id": thread_id}), key=lambda e: e.get("timestamp", "")
        )
        if thread_emails:
            title_by_thread_id[thread_id] = thread_emails[0]["subject"]

    return [
        {"title": title_by_thread_id.get(meeting.get("thread_id"), meeting.get("thread_id")), **meeting}
        for meeting in meetings
    ]


def list_personal_items(db, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    return PersonalItemRepository(db).find_many({}, skip=offset, limit=limit)
