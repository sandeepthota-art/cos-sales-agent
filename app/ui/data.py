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


def list_emails(db) -> list[dict]:
    return EmailRepository(db).find_many({})


def list_threads(db) -> list[dict]:
    return ThreadRepository(db).find_many({})


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


def list_commitments(db) -> list[dict]:
    return CommitmentRepository(db).find_many({})


def list_follow_ups(db) -> list[dict]:
    return FollowUpRepository(db).find_many({})


def list_meetings(db) -> list[dict]:
    return MeetingRepository(db).find_many({})


def list_personal_items(db) -> list[dict]:
    return PersonalItemRepository(db).find_many({})
