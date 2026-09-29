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
    column_config_for docstring) -- this is how the dashboard's Emails tab shows the
    human-readable `id` as the primary, leftmost identifier while still displaying
    every other field unchanged, rather than hiding anything via column_order.
    """
    ordered = {key: doc[key] for key in priority_keys if key in doc}
    ordered.update(doc)
    return ordered


def list_emails(db) -> list[dict]:
    return [
        _with_priority_keys_first(doc, ("id", "message_id", "thread_id"))
        for doc in EmailRepository(db).find_many({})
    ]


def list_threads(db) -> list[dict]:
    return ThreadRepository(db).find_many({})


def _thread_display_label(thread: dict) -> str:
    """THR-xxx is the primary, human-readable label; the raw thread_id (a source
    Gmail thread id, an inherited resolved id, or the application's synthetic
    fallback -- see docs/DATA_DICTIONARY.md) stays visible alongside it for
    traceability, never removed. Falls back to the raw id alone for a thread that
    predates this feature and hasn't been backfilled with an `id` yet."""
    return f"{thread['id']} ({thread['thread_id']})" if thread.get("id") else thread["thread_id"]


def thread_context_versions(db, thread_id: str) -> list[dict]:
    return ContextSnapshotRepository(db).all_for_thread(thread_id)


def list_knowledge(db, thread_id: str | None = None) -> list[dict]:
    repo = KnowledgeRepository(db)
    if thread_id:
        return repo.all_for_thread(thread_id)
    return repo.find_many({})


def list_reply_drafts(db, status: str | None = None) -> list[dict]:
    repo = ReplyDraftRepository(db)
    if status:
        return repo.find_many({"status": status})
    return repo.find_many({})


def list_calendar_actions(db, status: str | None = None) -> list[dict]:
    repo = CalendarActionRepository(db)
    if status:
        return repo.find_many({"status": status})
    return repo.find_many({})


def list_people(db) -> list[dict]:
    return PersonRepository(db).find_many({})


def list_organizations(db) -> list[dict]:
    return OrganizationRepository(db).find_many({})


def list_projects(db) -> list[dict]:
    return ProjectRepository(db).find_many({})


def list_opportunities(db) -> list[dict]:
    return OpportunityRepository(db).find_many({})


def list_commitments(db) -> list[dict]:
    return CommitmentRepository(db).find_many({})


def list_follow_ups(db) -> list[dict]:
    return FollowUpRepository(db).find_many({})


def list_meetings(db) -> list[dict]:
    return MeetingRepository(db).find_many({})


def list_personal_items(db) -> list[dict]:
    return PersonalItemRepository(db).find_many({})
