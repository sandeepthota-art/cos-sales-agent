from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database

# A MongoDB document as this project reads/writes it (always JSON-safe --
# model_dump(mode="json") on the way in, {"_id": 0} projected out on the way
# out). Named here so every signature in this module says what it means
# instead of repeating "dict[str, Any]".
JsonDoc = dict[str, Any]


def get_client(uri: str) -> MongoClient:
    return MongoClient(uri, serverSelectionTimeoutMS=5000)


def initialize_indexes(db: Database) -> None:
    db.raw_emails_dump.create_index("source_message_id", unique=True)
    # Backs RawEmailDumpRepository.claim_next_unclassified's exact filter+sort
    # shape -- classification_version is the most selective field; sort_key
    # descending + source_message_id descending is the newest-eligible-first
    # order with a deterministic tie-breaker (docs/DECISIONS.md: labeling
    # claims newest first, the opposite of context claiming).
    db.raw_emails_dump.create_index([("classification_version", 1), ("sort_key", -1), ("source_message_id", -1)])
    # Backs the skill's end-of-run label breakdown.
    db.raw_emails_dump.create_index("label_applied")
    # Backs claim_next_context_candidate's filter+sort shape, same reasoning
    # as the classification_version index above, for the independent
    # Phase 4 context-claim lifecycle.
    db.raw_emails_dump.create_index([("context_version", 1), ("thread_id", 1), ("sort_key", 1)])
    # observations is append-only; this unique index is what makes
    # ObservationRepository.append idempotent -- re-persisting the exact
    # same fact (same entity/attribute/value/source message) is a no-op.
    db.observations.create_index(
        [("entity_type", 1), ("entity_key", 1), ("attribute", 1), ("value_hash", 1), ("source_message_id", 1)],
        unique=True,
    )


def initialize_database(client: MongoClient, database_name: str) -> Database:
    db = client[database_name]
    initialize_indexes(db)
    return db


class _BaseRepository:
    """Generic MongoDB operations only -- no collection here knows what any
    field MEANS. That's app.tools' job: this class answers "how do I read/
    write Mongo", never "what should happen when X occurs". Keeping that
    boundary is what lets app.tools stay the single place classification
    lifecycle rules live, and lets this class be reused unchanged for any
    future collection with the same key-by-dict shape.
    """

    collection_name: str

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def upsert_by_key(self, key: JsonDoc, document: JsonDoc) -> JsonDoc:
        self._collection.update_one(key, {"$set": document}, upsert=True)
        return self._collection.find_one(key, {"_id": 0})

    def update_many_by_key(self, key: JsonDoc, update: JsonDoc) -> None:
        self._collection.update_many(key, {"$set": update})

    def find_one(self, key: JsonDoc) -> JsonDoc | None:
        return self._collection.find_one(key, {"_id": 0})

    def find_many(
        self, key: JsonDoc, limit: int | None = None, sort: list[tuple[str, int]] | None = None
    ) -> list[JsonDoc]:
        cursor = self._collection.find(key, {"_id": 0})
        if sort is not None:
            cursor = cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(limit)
        return list(cursor)


class RawEmailDumpRepository(_BaseRepository):
    collection_name = "raw_emails_dump"

    def claim_next_unclassified(
        self, max_classification_version: int, claim_cutoff_iso: str, max_attempts: int, claim_id: str, now_iso: str
    ) -> JsonDoc | None:
        """Atomically finds AND claims the newest (by sort_key, source_message_id
        descending tie-breaker) email eligible for classification, in one
        find_one_and_update -- so two concurrent callers can never be handed
        the same email. Newest-first is a deliberate triage decision (see
        docs/DECISIONS.md); context claiming stays oldest-first.

        Eligible: classification_version < max_classification_version (never
        classified, or classified under an older taxonomy version -- bumping
        the version constant re-opens everything, by design) AND
        label_attempts < max_attempts (circuit breaker) AND label_claimed_at <
        claim_cutoff_iso (an unclaimed document's label_claimed_at is "",
        which sorts before any real ISO timestamp -- this single comparison
        covers "never claimed" and "claim expired" with no $or needed) AND
        history_only is not True (scripts/mark_history_only.py marks old
        backfilled mail history_only=true; such documents are permanently
        excluded from classification, never surfaced by either claim path).

        claim_id is caller-generated (one per single claim, or one SHARED
        across an entire batch -- see claim_batch_unclassified) and stamped
        onto the claimed document alongside label_claimed_at. It's the
        fencing token persist_raw_email_label/mark_raw_email_label_failed
        compare-and-swap against, so a writer whose claim was invalidated
        (lease expired and reclaimed, or cleared by a concurrent
        reopen/auto-close) can never silently overwrite newer work.

        Claims via _claim_sorted_one -- see that method's docstring for why a
        plain single-call find_one_and_update(sort=...) is not safe to use
        here (a confirmed mongomock bug, independent of the documented
        BEFORE/AFTER one: it can apply the write to the wrong document
        whenever natural insertion order differs from sort order).
        """
        return self._claim_sorted_one(
            {
                "classification_version": {"$lt": max_classification_version},
                "label_attempts": {"$lt": max_attempts},
                "label_claimed_at": {"$lt": claim_cutoff_iso},
                "history_only": {"$ne": True},
            },
            {"label_claimed_at": now_iso, "claim_id": claim_id},
            sort=[("sort_key", -1), ("source_message_id", -1)],
        )

    def _claim_sorted_one(self, filter_: JsonDoc, set_fields: JsonDoc, sort: list[tuple[str, int]]) -> JsonDoc | None:
        """Shared two-phase claim used by claim_next_unclassified and
        claim_next_context_candidate: find_one(sort=...) picks the right
        candidate by _id, then find_one_and_update scopes the write to that
        exact _id (re-checking filter_ too, so a concurrent claimer winning
        the race in between makes this return None, same "lost the race"
        outcome as a single atomic call would have given).

        This two-step shape -- rather than one find_one_and_update(sort=...)
        -- is a deliberate workaround for a confirmed mongomock bug (tested
        against mongomock; the single-call form is correct against real
        Atlas): find_one_and_update's `sort` only affects which document is
        chosen for the BEFORE/AFTER return value, NOT which document the
        update is actually applied to -- the write silently lands on the
        first match in natural (insertion) order instead. This stayed
        invisible for the oldest-first queries this project started with
        (insertion order happened to match sort order in every test and in
        practice), but surfaced for real once label-claiming flipped to
        newest-first (see docs/DECISIONS.md): insertion order is oldest
        first, so a naive single-call claim would silently keep claiming
        the OLDEST document while reporting the newest one as claimed.

        Scoping the update to the candidate's own _id sidesteps the bug
        entirely (there's only one document that can possibly match an _id
        filter, so there's no "wrong document in natural order" for
        mongomock to pick), and is exactly as atomic as the single-call form
        against real Atlas, where this workaround is a no-op in behavior.

        Uses return_document=ReturnDocument.BEFORE, not AFTER, and manually
        merges set_fields onto the returned dict -- the same documented
        mongomock 4.3.0 AFTER-bug workaround used elsewhere in this class.
        """
        candidate = self._collection.find_one(filter_, sort=sort, projection={"_id": 1})
        if candidate is None:
            return None
        before = self._collection.find_one_and_update(
            {**filter_, "_id": candidate["_id"]},
            {"$set": set_fields},
            return_document=ReturnDocument.BEFORE,
            projection={"_id": 0},
        )
        if before is None:
            return None
        return {**before, **set_fields}

    def claim_batch_unclassified(
        self,
        max_classification_version: int,
        claim_cutoff_iso: str,
        max_attempts: int,
        claim_id: str,
        now_iso: str,
        batch_size: int,
    ) -> list[JsonDoc]:
        """Claims up to batch_size newest-eligible emails as a LOOP of
        individually atomic claim_next_unclassified calls, all sharing the
        same claim_id -- not a find() + one update_many (that had a real
        TOCTOU window: two concurrent batch callers could both read the same
        unclaimed candidates before either's write committed). Each slot in
        the loop is its own atomic find_one_and_update, so two concurrent
        batch claims can never be handed the same document, same guarantee
        claim_next_unclassified already had.

        Same eligibility rule as claim_next_unclassified. Returns [] if
        nothing is eligible, otherwise up to batch_size documents, newest
        first, each already claimed with the shared claim_id.
        """
        claimed: list[JsonDoc] = []
        for _ in range(batch_size):
            doc = self.claim_next_unclassified(max_classification_version, claim_cutoff_iso, max_attempts, claim_id, now_iso)
            if doc is None:
                break
            claimed.append(doc)
        return claimed

    def update_if_claim_matches(
        self,
        message_id: str,
        claim_id: str | None,
        set_fields: JsonDoc,
        push_fields: JsonDoc | None = None,
        claim_field: str = "claim_id",
    ) -> JsonDoc | None:
        """Compare-and-swap: only applies set_fields (and push_fields, each
        appended via $push) if the document's CURRENT claim_field still
        equals claim_id. Returns the resulting document, or None if it no
        longer matches -- the claim was lost (lease expired and reclaimed,
        or invalidated by a concurrent reopen/auto-close). The caller (see
        app.tools.persist_raw_email_label/mark_raw_email_label_failed, and
        app.context's context-claim release/failure paths via
        claim_field="context_claim_id") is responsible for distinguishing
        "claim lost" from "message_id doesn't exist at all" -- this method
        returns None for both, on purpose (it has no business-meaning
        knowledge, same boundary as the rest of this class).

        claim_field defaults to "claim_id" (the label-claim lineage this
        method was built for) -- app.context passes "context_claim_id" so
        the two independent claim lifecycles (label vs. context) never read
        or write each other's fencing token.

        Uses BEFORE, not AFTER, and reconstructs the after-state manually --
        same documented mongomock-AFTER-bug workaround as claim_next_unclassified,
        since the query filters on claim_field and set_fields may include a
        new value for it (the exact same-field-filtered-and-set combination)."""
        update: JsonDoc = {"$set": set_fields}
        if push_fields:
            update["$push"] = push_fields
        before = self._collection.find_one_and_update(
            {"message_id": message_id, claim_field: claim_id},
            update,
            return_document=ReturnDocument.BEFORE,
            projection={"_id": 0},
        )
        if before is None:
            return None
        after = {**before, **set_fields}
        if push_fields:
            for key, value in push_fields.items():
                after[key] = list(before.get(key) or []) + [value]
        return after

    def mark_synced_if_label_unchanged(self, message_id: str, expected_label_applied: str | None) -> JsonDoc | None:
        """Compare-and-swap: only confirms gmail_label_synced=True if the
        document's CURRENT label_applied still equals expected_label_applied
        (the value the caller just actually applied to the real Gmail
        message). Returns the updated document, or None if label_applied has
        since changed -- a concurrent relabel raced ahead of this
        confirmation, meaning Gmail now holds a STALE value against the
        newer decision, so gmail_label_synced must stay False, not be
        stamped True. Filters on label_applied (not the field being set,
        gmail_label_synced), so this does not hit the AFTER/mongomock bug
        documented on claim_next_unclassified."""
        return self._collection.find_one_and_update(
            {"message_id": message_id, "label_applied": expected_label_applied},
            {"$set": {"gmail_label_synced": True}},
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )

    def claim_next_context_candidate(
        self, claim_cutoff_iso: str, max_attempts: int, claim_id: str, now_iso: str, thread_id: str | None = None
    ) -> JsonDoc | None:
        """Atomically finds AND claims the oldest (by sort_key, thread_id
        ascending tie-breaker) message eligible for context extraction --
        same BEFORE-workaround and same fencing-token shape as
        claim_next_unclassified, but against the context_* fields, which are
        fully independent of the label_* claim lifecycle (a message can be
        label-claimed and context-claimed at the same time; neither path
        reads the other's fields). Oldest-first here is unchanged by the
        label-claim newest-first flip (see docs/DECISIONS.md).

        Eligible: context_version == 0, context_attempts < max_attempts,
        context_claimed_at < claim_cutoff_iso (unclaimed sorts before any
        real timestamp, same "" sentinel trick as the label claim).
        history_only docs ARE eligible here (Phase 4 principle: "history_only
        docs ARE eligible for context") -- deliberately no history_only
        filter, unlike claim_next_unclassified.

        When thread_id is given, the claim is scoped to that thread -- this
        is how claim_thread_for_context extends an already-won anchor claim
        to the rest of its thread without ever being able to pick a
        different thread. Making the anchor claim (thread_id=None) itself
        atomic is what guarantees two concurrent claimers land on disjoint
        threads: each can only ever win ONE specific document via this one
        atomic op, so two callers can't both anchor on the same thread.
        """
        query: JsonDoc = {
            "context_version": 0,
            "context_attempts": {"$lt": max_attempts},
            "context_claimed_at": {"$lt": claim_cutoff_iso},
        }
        if thread_id is not None:
            query["thread_id"] = thread_id
        return self._claim_sorted_one(
            query,
            {"context_claimed_at": now_iso, "context_claim_id": claim_id},
            sort=[("sort_key", 1), ("thread_id", 1)],
        )

    def claim_thread_for_context(
        self, claim_cutoff_iso: str, max_attempts: int, claim_id: str, now_iso: str, max_messages: int
    ) -> list[JsonDoc]:
        """Claims one whole thread's unprocessed messages for context
        extraction: an atomic anchor claim (whichever thread holds the
        oldest eligible message) followed by a loop of atomic claims scoped
        to that thread_id, up to max_messages, all sharing one claim_id --
        same loop-of-atomic-claims shape as claim_batch_unclassified.
        Returns [] if nothing is eligible anywhere. If the anchor message
        has no thread_id at all, returns just that one message (a thread of
        one)."""
        anchor = self.claim_next_context_candidate(claim_cutoff_iso, max_attempts, claim_id, now_iso)
        if anchor is None:
            return []
        claimed = [anchor]
        thread_id = anchor.get("thread_id")
        if thread_id is None:
            return claimed
        for _ in range(max_messages - 1):
            doc = self.claim_next_context_candidate(claim_cutoff_iso, max_attempts, claim_id, now_iso, thread_id=thread_id)
            if doc is None:
                break
            claimed.append(doc)
        return claimed

    def find_by_context_claim_id(self, claim_id: str) -> list[JsonDoc]:
        return list(self._collection.find({"context_claim_id": claim_id}, {"_id": 0}).sort("sort_key", 1))

    def siblings_in_thread(self, thread_id: str, exclude_message_id: str) -> list[JsonDoc]:
        """Every OTHER raw-dumped email in this thread, oldest first -- the
        conversation context a caller needs to classify one message correctly
        (an inbound "Thanks" only makes sense read against what it's replying
        to). Read-only, never used for the claim query itself."""
        return list(
            self._collection.find(
                {"thread_id": thread_id, "message_id": {"$ne": exclude_message_id}}, {"_id": 0}
            ).sort("sort_key", 1)
        )
