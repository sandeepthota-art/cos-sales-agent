"""Watches a local directory for Gmail-export JSON files (the exact FileEmailProvider
shape -- {"messages": [...]}) and turns newly-discovered or newly-changed files into
raw-email-dict batches for Scheduler. Reuses FileEmailProvider unchanged for the
actual export -> raw-email-dict conversion and malformed-message skipping; this
module only adds the "which files are new/changed" bookkeeping, backed by MongoDB
(IngestedFileRepository, app/database/repositories.py) so restart never loses state
and a second process sees the same picture of what's already been ingested.

Per-file tracking here is a pure efficiency optimization, not a correctness
mechanism -- app.pipeline.run_pipeline's own message_id uniqueness + COMPLETED-skip
already makes even a full, untracked re-scan of every file on every poll safe at the
email level. Without this tracking, a long-running scheduler would re-read,
re-hash, and re-JSON-parse every historical file, forever, on every single poll.

A file is only marked "fully ingested" (and thus skipped on the next unchanged
poll) when its most recent batch had zero pipeline failures -- a file containing
even one FAILED message keeps being retried every poll, exactly mirroring how
repeatedly running --mode=file against the same export already retries any message
not at ProcessingStage.COMPLETED.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pymongo.database import Database

from app.database.repositories import IngestedFileRepository
from app.interfaces.source import Source
from app.providers.email.file import FileEmailProvider

logger = logging.getLogger(__name__)


def _fingerprint(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


class FolderSource(Source):
    def __init__(self, db: Database, folder: str):
        self._folder = Path(folder)
        self._repo = IngestedFileRepository(db)

    def get_new_batches(self) -> list[dict[str, Any]]:
        self._folder.mkdir(parents=True, exist_ok=True)
        batches: list[dict[str, Any]] = []

        for path in sorted(self._folder.glob("*.json")):
            try:
                fingerprint, size = _fingerprint(path)
            except OSError as exc:
                logger.warning("folder source: could not read %s: %s", path, exc)
                continue

            record = self._repo.find_one({"filename": path.name})
            if record and record.get("fingerprint") == fingerprint and record.get("all_completed"):
                continue

            try:
                provider = FileEmailProvider(path=str(path))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                logger.warning("folder source: skipping malformed file %s: %s", path, exc)
                continue

            emails = provider.fetch_emails(limit=provider.total_found)
            if not emails:
                # Every message in the file was malformed at the FileEmailProvider
                # level -- nothing more this file will ever contribute. Record it so
                # an unchanged, all-malformed file isn't re-read every poll forever.
                self._repo.upsert_by_key(
                    {"filename": path.name},
                    {
                        "filename": path.name,
                        "path": str(path),
                        "fingerprint": fingerprint,
                        "size": size,
                        "all_completed": True,
                        "message_count": 0,
                    },
                )
                continue

            batches.append(
                {
                    "source_ref": {"filename": path.name, "fingerprint": fingerprint, "size": size},
                    "emails": emails,
                }
            )
        return batches

    def mark_batch_processed(self, source_ref: dict[str, Any], failed_count: int) -> None:
        self._repo.upsert_by_key(
            {"filename": source_ref["filename"]},
            {
                "filename": source_ref["filename"],
                "path": str(self._folder / source_ref["filename"]),
                "fingerprint": source_ref["fingerprint"],
                "size": source_ref["size"],
                "all_completed": failed_count == 0,
            },
        )
