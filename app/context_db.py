"""Generic Mongo operations for Phase 4's new collections -- same SRP
boundary as app/db.py's _BaseRepository: these classes know how to
read/write Mongo, never what the fields mean (that's app/context.py's
job). raw_emails_dump's own context-claim fields live on
RawEmailDumpRepository in app/db.py instead, alongside its existing
label-claim methods.
"""

import hashlib
from typing import Any

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

JsonDoc = dict[str, Any]


def value_hash(value: str) -> str:
    """Fixed, process-independent hash (never Python's built-in hash(),
    which is salted per-process) -- used in observations' unique index so
    long values don't blow up the index key size."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class ObservationRepository:
    collection_name = "observations"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def append(self, observation: JsonDoc) -> bool:
        """Idempotent insert: the unique index on (entity_type, entity_key,
        attribute, value_hash, source_message_id) makes re-persisting the
        exact same fact a safe no-op. Returns True if newly inserted,
        False if it already existed (duplicate key, swallowed -- the only
        expected failure mode on a unique-indexed append-only collection)."""
        doc = dict(observation)
        doc["value_hash"] = value_hash(doc["value"])
        try:
            self._collection.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    def for_entity(self, entity_type: str, entity_key: str) -> list[JsonDoc]:
        return list(
            self._collection.find({"entity_type": entity_type, "entity_key": entity_key}, {"_id": 0}).sort(
                [("observed_at", 1), ("recorded_at", 1)]
            )
        )

    def distinct_entities(self) -> list[tuple[str, str]]:
        """Every (entity_type, entity_key) pair with at least one
        observation -- scripts/rebuild_profiles.py's iteration list."""
        return sorted({(d["entity_type"], d["entity_key"]) for d in self._collection.find({}, {"entity_type": 1, "entity_key": 1})})


class PersonRepository:
    collection_name = "people"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def replace_computed_fields(
        self,
        email: str,
        display_names: list[str],
        is_role_address: bool,
        profile: JsonDoc,
        first_seen_at: str,
        last_seen_at: str,
        message_count: int,
        participant_message_ids: list[str],
    ) -> None:
        """Every field here is derived (rebuilt from observations and
        message participation), never incrementally mutated -- keeps this
        always reproducible by rebuild_profiles. participant_message_ids
        is the set app.context merges new claimed messages into (union,
        not increment) so message_count stays correct no matter how many
        times the same claim is (re)persisted."""
        self._collection.update_one(
            {"_id": email},
            {
                "$set": {
                    "display_names": display_names,
                    "is_role_address": is_role_address,
                    "profile": profile,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "message_count": message_count,
                    "participant_message_ids": participant_message_ids,
                }
            },
            upsert=True,
        )

    def find(self, email: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": email})


class OrganizationRepository:
    collection_name = "organizations"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def replace_computed_fields(
        self,
        domain: str,
        names: list[str],
        profile: JsonDoc,
        first_seen_at: str,
        last_seen_at: str,
        message_count: int,
        participant_message_ids: list[str],
    ) -> None:
        self._collection.update_one(
            {"_id": domain},
            {
                "$set": {
                    "names": names,
                    "profile": profile,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "message_count": message_count,
                    "participant_message_ids": participant_message_ids,
                }
            },
            upsert=True,
        )

    def find(self, domain: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": domain})


class PersonOrgLinkRepository:
    collection_name = "person_org_links"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def upsert_link(self, link_id: str, fields: JsonDoc) -> None:
        """link_id is a stable, deterministic key (person_email|org_domain|
        valid_from) -- re-creating the SAME link is an idempotent no-op."""
        self._collection.update_one({"_id": link_id}, {"$set": fields}, upsert=True)

    def current_links_for_person(self, person_email: str) -> list[JsonDoc]:
        return list(self._collection.find({"person_email": person_email, "valid_to": None}, {"_id": 0}))

    def find_by_id(self, link_id: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": link_id})

    def close(self, link_id: str, valid_to: str, evidence: str) -> None:
        self._collection.update_one({"_id": link_id}, {"$set": {"valid_to": valid_to, "close_evidence": evidence}})


class ThreadRepository:
    collection_name = "threads"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def find(self, thread_key: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": thread_key})

    def upsert(self, thread_key: str, fields: JsonDoc) -> None:
        self._collection.update_one({"_id": thread_key}, {"$set": fields}, upsert=True)


class ThreadHistoryRepository:
    collection_name = "threads_history"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def append_snapshot(self, thread_key: str, snapshot: JsonDoc, max_snapshots: int) -> None:
        """$push + $slice caps the array at max_snapshots (keeping the
        most recent ones) in one atomic op -- no separate trim step."""
        self._collection.update_one(
            {"_id": thread_key},
            {"$push": {"snapshots": {"$each": [snapshot], "$slice": -max_snapshots}}},
            upsert=True,
        )

    def find(self, thread_key: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": thread_key})


class EmailInsightRepository:
    collection_name = "email_insights"

    def __init__(self, db: Database) -> None:
        self._collection: Collection = db[self.collection_name]

    def upsert(self, message_id: str, fields: JsonDoc) -> None:
        self._collection.update_one({"_id": message_id}, {"$set": fields}, upsert=True)

    def find(self, message_id: str) -> JsonDoc | None:
        return self._collection.find_one({"_id": message_id})

    def find_many(self, message_ids: list[str]) -> list[JsonDoc]:
        return list(self._collection.find({"_id": {"$in": message_ids}}))
