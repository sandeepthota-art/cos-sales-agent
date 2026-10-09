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
        self, max_classification_version: int, claim_cutoff_iso: str, max_attempts: int, now_iso: str
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
        covers "never claimed" and "claim expired" with no $or needed).

        All four thresholds arrive as parameters -- this method has no
        hardcoded knowledge of what they mean (that's app.tools' job); it
        only knows how to run the query/sort/atomic-claim shape.

        Uses return_document=ReturnDocument.BEFORE, not AFTER, and manually
        patches label_claimed_at=now_iso onto the returned dict -- a
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
            },
            {"$set": {"label_claimed_at": now_iso}},
            sort=[("sort_key", 1)],
            return_document=ReturnDocument.BEFORE,
            projection={"_id": 0},
        )
        if before is None:
            return None
        before["label_claimed_at"] = now_iso
        return before

    def claim_batch_unclassified(
        self, max_classification_version: int, claim_cutoff_iso: str, max_attempts: int, now_iso: str, batch_size: int
    ) -> list[JsonDoc]:
        """Atomically finds AND claims up to batch_size oldest-eligible emails
        in ONE round trip (a find + one update_many), instead of batch_size
        separate claim_next_unclassified calls. For a 100-email run in
        batches of 10, this is ~10 Mongo round-trips instead of ~100.

        Deliberately NOT batch_size separate find_one_and_update calls under
        the hood: a find().limit(batch_size) then one update_many on the
        resulting ids is faster (2 round-trips vs batch_size), and the
        single-operator operating model this project is built for (see
        README "What was deliberately not built") doesn't need per-document
        atomicity against a concurrent claimant -- claim_next_unclassified's
        stronger per-document guarantee stays available for callers that do.

        Same eligibility rule as claim_next_unclassified. Returns [] if
        nothing is eligible, otherwise up to batch_size documents, oldest
        first, each with label_claimed_at already patched to now_iso.
        """
        candidates = list(
            self._collection.find(
                {
                    "classification_version": {"$lt": max_classification_version},
                    "label_attempts": {"$lt": max_attempts},
                    "label_claimed_at": {"$lt": claim_cutoff_iso},
                },
                {"_id": 0},
            )
            .sort("sort_key", 1)
            .limit(batch_size)
        )
        if not candidates:
            return []
        message_ids = [doc["message_id"] for doc in candidates]
        self._collection.update_many({"message_id": {"$in": message_ids}}, {"$set": {"label_claimed_at": now_iso}})
        for doc in candidates:
            doc["label_claimed_at"] = now_iso
        return candidates

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
