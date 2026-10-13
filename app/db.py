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
    # shape -- classification_version is the most selective field, sort_key is
    # the normalized-UTC sort for oldest-eligible-first order.
    db.raw_emails_dump.create_index([("classification_version", 1), ("sort_key", 1)])
    # Backs the skill's end-of-run label breakdown.
    db.raw_emails_dump.create_index("label_applied")


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
        """Atomically finds AND claims the oldest (by sort_key) email eligible for
        classification, in one find_one_and_update -- so two concurrent callers
        can never be handed the same email.

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

        Uses return_document=ReturnDocument.BEFORE, not AFTER, and manually
        patches label_claimed_at/claim_id onto the returned dict -- a
        deliberate workaround for a confirmed mongomock 4.3.0 bug (tested
        against both mongomock and real Atlas): find_one_and_update fails to
        match when a multi-condition query + sort + projection + an update
        that $sets the SAME field one condition filters on are combined with
        return_document=AFTER. BEFORE sidesteps it with no change in actual
        write behavior.
        """
        before = self._collection.find_one_and_update(
            {
                "classification_version": {"$lt": max_classification_version},
                "label_attempts": {"$lt": max_attempts},
                "label_claimed_at": {"$lt": claim_cutoff_iso},
                "history_only": {"$ne": True},
            },
            {"$set": {"label_claimed_at": now_iso, "claim_id": claim_id}},
            sort=[("sort_key", 1)],
            return_document=ReturnDocument.BEFORE,
            projection={"_id": 0},
        )
        if before is None:
            return None
        before["label_claimed_at"] = now_iso
        before["claim_id"] = claim_id
        return before

    def claim_batch_unclassified(
        self,
        max_classification_version: int,
        claim_cutoff_iso: str,
        max_attempts: int,
        claim_id: str,
        now_iso: str,
        batch_size: int,
    ) -> list[JsonDoc]:
        """Claims up to batch_size oldest-eligible emails as a LOOP of
        individually atomic claim_next_unclassified calls, all sharing the
        same claim_id -- not a find() + one update_many (that had a real
        TOCTOU window: two concurrent batch callers could both read the same
        unclaimed candidates before either's write committed). Each slot in
        the loop is its own atomic find_one_and_update, so two concurrent
        batch claims can never be handed the same document, same guarantee
        claim_next_unclassified already had.

        Same eligibility rule as claim_next_unclassified. Returns [] if
        nothing is eligible, otherwise up to batch_size documents, oldest
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
        self, message_id: str, claim_id: str | None, set_fields: JsonDoc, push_fields: JsonDoc | None = None
    ) -> JsonDoc | None:
        """Compare-and-swap: only applies set_fields (and push_fields, each
        appended via $push) if the document's CURRENT claim_id still equals
        claim_id. Returns the resulting document, or None if claim_id no
        longer matches -- the claim was lost (lease expired and reclaimed,
        or invalidated by a concurrent reopen/auto-close). The caller (see
        app.tools.persist_raw_email_label/mark_raw_email_label_failed) is
        responsible for distinguishing "claim lost" from "message_id doesn't
        exist at all" -- this method returns None for both, on purpose (it
        has no business-meaning knowledge, same boundary as the rest of this
        class).

        Uses BEFORE, not AFTER, and reconstructs the after-state manually --
        same documented mongomock-AFTER-bug workaround as claim_next_unclassified,
        since the query filters on claim_id and set_fields may include a new
        claim_id (the exact same-field-filtered-and-set combination)."""
        update: JsonDoc = {"$set": set_fields}
        if push_fields:
            update["$push"] = push_fields
        before = self._collection.find_one_and_update(
            {"message_id": message_id, "claim_id": claim_id},
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
