"""Generic abstraction for anything Scheduler can poll for new ingestible email
batches. A concrete Source is responsible for (a) deciding what "new" means for
its own medium, and (b) converting newly-discovered data into the same raw-email
dict shape app.email.models.parse_email() already accepts everywhere else in this
codebase (see app.providers.email.mock.MockEmailProvider / app.providers.email.file.
FileEmailProvider). Scheduler never branches on which Source implementation it
holds -- it only calls these two methods -- so a future Source (S3, IMAP poll,
whatever) requires zero changes to app/scheduler.py.

Each batch dict has exactly two keys:
    "source_ref": Any opaque, Source-defined token. Scheduler never inspects it --
        it only passes it back unchanged to mark_batch_processed().
    "emails": list[dict] of raw-email dicts, in the same shape EmailProvider.
        fetch_emails() already returns.

mark_batch_processed() is only ever called AFTER a batch has been run through the
full pipeline -- never at discovery time -- so a mid-pipeline crash never causes a
batch to be silently marked done without actually being processed.
"""

from abc import ABC, abstractmethod
from typing import Any


class Source(ABC):
    @abstractmethod
    def get_new_batches(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def mark_batch_processed(self, source_ref: Any, failed_count: int) -> None:
        ...
