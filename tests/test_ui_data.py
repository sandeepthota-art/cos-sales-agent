from datetime import datetime, timezone

import mongomock

from app.database.indexes import initialize_indexes
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
    list_personal_items,
    list_projects,
    list_reply_drafts,
    list_threads,
    thread_context_versions,
)


def _db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def test_dashboard_metrics_counts_each_collection():
    db = _db()
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001"})
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1"})
    KnowledgeRepository(db).upsert_by_key(
        {"thread_id": "t1", "subject_key": "abc", "predicate": "requires", "fact_key": "seat_count"},
        {"thread_id": "t1", "subject_key": "abc", "predicate": "requires", "fact_key": "seat_count"},
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "msg_001"}, {"source_email_id": "msg_001", "status": "awaiting_approval"}
    )
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "status": "awaiting_approval"},
    )

    metrics = dashboard_metrics(db)
    assert metrics["emails_processed"] == 1
    assert metrics["threads"] == 1
    assert metrics["knowledge_items"] == 1
    assert metrics["pending_replies"] == 1
    assert metrics["pending_calendar_actions"] == 1


def test_list_emails_and_threads_return_stored_documents():
    db = _db()
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "subject": "Hi"})
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1"})

    assert list_emails(db)[0]["subject"] == "Hi"
    assert list_threads(db)[0]["thread_id"] == "t1"


def test_list_emails_puts_identifier_fields_first_for_display():
    # Priority order is message_id -> thread_id -> source_message_id ->
    # source_thread_id. `emails.id` was removed from the schema entirely (a
    # true duplicate of message_id -- see
    # scripts/remove_email_id_record_id_date_fields.py); source_message_id/
    # source_thread_id hold the permanent, original Gmail ids -- the
    # genuinely distinct information, kept visible alongside the canonical
    # message_id/thread_id.
    db = _db()
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        {
            "message_id": "EML-001",
            "subject": "Hi",
            "thread_id": "THR-001",
            "source_message_id": "gmail_msg_1",
            "source_thread_id": "gmail_thread_1",
        },
    )

    keys = list(list_emails(db)[0].keys())
    assert keys[:4] == ["message_id", "thread_id", "source_message_id", "source_thread_id"]
    assert "subject" in keys  # every other field still present, just not first


def test_list_emails_sorts_newest_first_by_timestamp_not_insertion_order():
    db = _db()
    # Inserted oldest-first, so a pass here proves the sort is timestamp-based,
    # not insertion/id order (which would return them in this same order).
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        {"message_id": "EML-001", "subject": "Oldest", "timestamp": "2026-01-01T00:00:00+00:00"},
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-002"},
        {"message_id": "EML-002", "subject": "Newest", "timestamp": "2026-03-01T00:00:00+00:00"},
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-003"},
        {"message_id": "EML-003", "subject": "Middle", "timestamp": "2026-02-01T00:00:00+00:00"},
    )

    subjects = [email["subject"] for email in list_emails(db)]
    assert subjects == ["Newest", "Middle", "Oldest"]


def test_list_emails_handles_a_document_with_no_thread_or_source_ids_set():
    # `id` is no longer a schema field for emails at all (removed entirely --
    # see scripts/remove_email_id_record_id_date_fields.py), so a document
    # missing every priority key except message_id must still render sanely.
    db = _db()
    EmailRepository(db).upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "subject": "Hi"})

    email = list_emails(db)[0]
    assert "id" not in email
    assert list(email.keys())[0] == "message_id"


def test_thread_display_label_shows_canonical_id_with_source_thread_id_alongside():
    # Bug fix (threads-collection schema cleanup): this used to combine
    # thread['id'] with thread['thread_id'] -- always-identical values, so it
    # rendered a misleading "THR-001 (THR-001)". The genuinely distinct field
    # is source_thread_id (the real original Gmail thread id).
    assert _thread_display_label(
        {"thread_id": "THR-001", "source_thread_id": "gmail_thread_xyz"}
    ) == "THR-001 (gmail_thread_xyz)"


def test_thread_display_label_falls_back_to_just_thread_id_when_no_source_thread_id():
    assert _thread_display_label({"thread_id": "THR-002"}) == "THR-002"
    assert _thread_display_label({"thread_id": "THR-003", "source_thread_id": None}) == "THR-003"


def test_thread_context_versions_ordered_ascending():
    db = _db()
    repo = ContextSnapshotRepository(db)
    repo.upsert_by_key(
        {"thread_id": "t1", "triggering_email_id": "msg_002"},
        {"thread_id": "t1", "triggering_email_id": "msg_002", "context_version": 2},
    )
    repo.upsert_by_key(
        {"thread_id": "t1", "triggering_email_id": "msg_001"},
        {"thread_id": "t1", "triggering_email_id": "msg_001", "context_version": 1},
    )
    versions = [snap["context_version"] for snap in thread_context_versions(db, "t1")]
    assert versions == [1, 2]


def test_list_knowledge_filters_by_thread():
    db = _db()
    repo = KnowledgeRepository(db)
    repo.upsert_by_key(
        {"thread_id": "t1", "subject_key": "abc", "predicate": "requires", "fact_key": "seat_count"},
        {"thread_id": "t1", "subject_key": "abc", "predicate": "requires", "fact_key": "seat_count"},
    )
    repo.upsert_by_key(
        {"thread_id": "t2", "subject_key": "xyz", "predicate": "requires", "fact_key": "seat_count"},
        {"thread_id": "t2", "subject_key": "xyz", "predicate": "requires", "fact_key": "seat_count"},
    )
    assert len(list_knowledge(db, thread_id="t1")) == 1
    assert len(list_knowledge(db)) == 2


def test_list_reply_drafts_and_calendar_actions_filter_by_status():
    db = _db()
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "msg_001"}, {"source_email_id": "msg_001", "status": "awaiting_approval"}
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "msg_002"}, {"source_email_id": "msg_002", "status": "simulated_sent"}
    )
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "status": "needs_clarification"},
    )

    assert len(list_reply_drafts(db, status="awaiting_approval")) == 1
    assert len(list_reply_drafts(db)) == 2
    assert len(list_calendar_actions(db, status="needs_clarification")) == 1


def test_list_people_returns_stored_documents():
    db = _db()
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, {"id": "PER-001", "name": "Ashok Ganapam"})
    assert list_people(db)[0]["name"] == "Ashok Ganapam"


def test_list_organizations_returns_stored_documents():
    db = _db()
    OrganizationRepository(db).upsert_by_key({"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat"})
    assert list_organizations(db)[0]["name"] == "DataBeat"


def test_list_projects_returns_stored_documents():
    db = _db()
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, {"id": "PRJ-001", "project": "Renewal Q4"})
    assert list_projects(db)[0]["project"] == "Renewal Q4"


def test_list_opportunities_returns_stored_documents():
    db = _db()
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, {"id": "OPP-001", "name": "Renewal Q4 deal"})
    assert list_opportunities(db)[0]["name"] == "Renewal Q4 deal"


def test_list_commitments_returns_stored_documents():
    db = _db()
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, {"id": "COM-001", "what": "send pricing"})
    assert list_commitments(db)[0]["what"] == "send pricing"


def test_list_follow_ups_returns_stored_documents():
    db = _db()
    FollowUpRepository(db).upsert_by_key({"id": "FUP-001"}, {"id": "FUP-001", "commitment_id": "COM-001"})
    assert list_follow_ups(db)[0]["commitment_id"] == "COM-001"


def test_list_follow_ups_resolves_what_person_name_and_org_name():
    # Follow-up/Commitment product gap: the raw FollowUp document has no `what`
    # text of its own -- it must be resolved from the parent Commitment, and
    # person_id/org_id resolved to readable names, all at the UI layer only.
    db = _db()
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, {"id": "PER-001", "name": "Ashok Ganapam"})
    OrganizationRepository(db).upsert_by_key({"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat"})
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, {"id": "COM-001", "what": "Send pricing sheet"}
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FUP-001"},
        {"id": "FUP-001", "commitment_id": "COM-001", "person_id": "PER-001", "org_id": "ORG-001"},
    )

    result = list_follow_ups(db)[0]

    assert result["what"] == "Send pricing sheet"
    assert result["person_name"] == "Ashok Ganapam"
    assert result["org_name"] == "DataBeat"
    # Underlying ids still present, not replaced.
    assert result["commitment_id"] == "COM-001"
    assert result["person_id"] == "PER-001"
    assert result["org_id"] == "ORG-001"


def test_list_follow_ups_handles_a_thread_only_follow_up_with_no_commitment():
    # FollowUp's own model validator allows thread_id-only (no commitment_id) --
    # must never fabricate a `what` value in that case.
    db = _db()
    FollowUpRepository(db).upsert_by_key({"id": "FUP-001"}, {"id": "FUP-001", "thread_id": "THR-001"})

    result = list_follow_ups(db)[0]

    assert result["what"] is None
    assert result["person_name"] is None
    assert result["org_name"] is None


def test_list_meetings_returns_stored_documents():
    db = _db()
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, {"id": "MTG-001", "title": "Kickoff"})
    assert list_meetings(db)[0]["title"] == "Kickoff"


def test_list_meetings_derives_title_from_the_threads_earliest_email_subject():
    # Meeting product gap: Meeting has no stored title field of its own (a real
    # pipeline-created Meeting document, unlike the fixture above, never has
    # one) -- list_meetings must derive a display-only title from the thread's
    # earliest email, never from insertion order.
    db = _db()
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-002"},
        {"message_id": "EML-002", "thread_id": "THR-001", "subject": "Re: Pricing call", "timestamp": "2026-09-14T10:00:00Z"},
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        {"message_id": "EML-001", "thread_id": "THR-001", "subject": "Pricing call", "timestamp": "2026-09-13T10:00:00Z"},
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"}, {"id": "MTG-001", "thread_id": "THR-001"}
    )

    assert list_meetings(db)[0]["title"] == "Pricing call"


def test_list_meetings_falls_back_to_thread_id_when_no_matching_email_exists():
    db = _db()
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"}, {"id": "MTG-001", "thread_id": "THR-999-no-emails"}
    )

    assert list_meetings(db)[0]["title"] == "THR-999-no-emails"


def test_list_personal_items_returns_stored_documents():
    db = _db()
    PersonalItemRepository(db).upsert_by_key({"id": "PSN-001"}, {"id": "PSN-001", "description": "Renew passport"})
    assert list_personal_items(db)[0]["description"] == "Renew passport"
