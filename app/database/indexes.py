from pymongo.database import Database


def initialize_indexes(db: Database) -> None:
    # message_id now always holds the canonical EML-nnn value (see the canonical ID
    # refactor in app.pipeline.ingest_raw_email) -- unique by construction (next_id),
    # so this index remains a valid, meaningful guarantee, not just a legacy one.
    db.emails.create_index("message_id", unique=True)
    db.emails.create_index("processing_status.stage")
    # No index on emails.id: that field was removed from the emails schema
    # entirely (collection-by-collection cleanup, emails first) -- it was a
    # true duplicate of message_id, verified by searching every reader/writer
    # across the codebase. See scripts/remove_email_id_record_id_date_fields.py.
    # The TRUE dedup guarantee going forward: source_message_id is the permanent,
    # never-reassigned Gmail/provider identity app.pipeline.ingest_raw_email's
    # dedup check now keys on. Sparse: a not-yet-backfilled historical document may
    # not have this field yet.
    db.emails.create_index("source_message_id", unique=True, sparse=True)

    # thread_id now always holds the canonical THR-nnn value -- see
    # app.pipeline._upsert_thread.
    db.threads.create_index("thread_id", unique=True)
    # No index on threads.id: that field was removed from the threads schema
    # entirely (collection-by-collection cleanup, threads second) -- it was a
    # true duplicate of thread_id, verified by searching every reader/writer
    # across the codebase. See scripts/remove_thread_id_field.py.
    # source_thread_id is only ever set when the source actually supplied one --
    # never unique (many threads legitimately have none; see resolve_thread_id).
    db.threads.create_index("source_thread_id", sparse=True)

    db.context_snapshots.create_index(
        [("thread_id", 1), ("triggering_email_id", 1)], unique=True
    )
    db.context_snapshots.create_index([("thread_id", 1), ("context_version", 1)])

    db.knowledge_items.create_index(
        [("thread_id", 1), ("subject_key", 1), ("predicate", 1), ("fact_key", 1)],
        unique=True,
    )

    db.reply_drafts.create_index("source_email_id", unique=True)

    db.calendar_actions.create_index(
        [("thread_id", 1), ("meeting_fingerprint", 1)], unique=True
    )

    db.processing_runs.create_index("started_at")

    db.people.create_index("id", unique=True)
    db.people.create_index("email", unique=True, sparse=True)
    # Non-unique, on the array field itself (a "multikey" index -- MongoDB indexes each
    # array element individually) -- supports resolve_person's no-email thread-scoped
    # lookup (app/entities/resolution.py: db.people.find({"open_threads": thread_id})),
    # which every no-email person mention now runs. Not a compound (name, open_threads)
    # index: the name comparison happens in Python via normalize_text (names aren't
    # stored pre-normalized), so a compound index wouldn't be usable by that query --
    # only open_threads is ever an actual Mongo-side filter.
    db.people.create_index("open_threads")

    db.projects.create_index("id", unique=True)

    db.commitments.create_index("id", unique=True)
    db.commitments.create_index("thread_id")

    db.follow_ups.create_index("id", unique=True)
    db.follow_ups.create_index("commitment_id", sparse=True)
    db.follow_ups.create_index("thread_id", sparse=True)

    db.meetings.create_index("id", unique=True)
    db.meetings.create_index("thread_id")

    db.opportunities.create_index("id", unique=True)

    db.personal_items.create_index("id", unique=True)

    db.ingested_files.create_index("filename", unique=True)

    # Ingestion-dump branch: completely separate from the `emails` collection's
    # own source_message_id index above -- this one backs app.mcp.tools.
    # ingest_raw_email_only's idempotent upsert, never shared with the
    # analysis pipeline's dedup/ProcessingStage machinery.
    db.raw_emails_dump.create_index("source_message_id", unique=True)

    db.person_context_snapshots.create_index("id", unique=True)
    # The idempotency guarantee (Phase 5): a retried email always resolves to the
    # same canonical source_email_id (EML-nnn), so re-running enrichment for the
    # same (person, email) pair upserts over this same key rather than creating a
    # duplicate snapshot.
    db.person_context_snapshots.create_index(
        [("person_id", 1), ("source_email_id", 1)], unique=True
    )
    db.person_context_snapshots.create_index([("person_id", 1), ("created_at", 1)])

    db.thread_events.create_index("id", unique=True)
    # The idempotency guarantee (Thread Events, Phase 3): at most one event per
    # logical (thread, email, event_type, entity_type, entity_id) operation --
    # a retry corrects `operation`/`summary` in place via upsert rather than
    # appending a second record for the same logical observation.
    db.thread_events.create_index(
        [("thread_id", 1), ("email_id", 1), ("event_type", 1), ("entity_type", 1), ("entity_id", 1)],
        unique=True,
    )
    db.thread_events.create_index([("thread_id", 1), ("sequence", 1)], unique=True)
