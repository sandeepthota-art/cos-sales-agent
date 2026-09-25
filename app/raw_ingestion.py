from pydantic import BaseModel, Field, ValidationError
from pymongo.database import Database

from app.database.repositories import EmailRepository, ThreadRepository
from app.email.models import parse_email
from app.email.normalizer import normalize_email
from app.email.threading import resolve_thread_id
from app.pipeline import _load_thread_candidates, _upsert_thread
from app.providers.email.file import FileEmailProvider


class RawIngestionSummary(BaseModel):
    input_messages: int
    inserted: int
    already_existed: int
    skipped: int
    failed: int
    unique_threads: int
    failed_message_ids: list[str] = Field(default_factory=list)


def run_raw_file_ingestion(db: Database, input_path: str) -> RawIngestionSummary:
    """Ingests raw email + thread data only -- no LLM call of any kind, no knowledge/
    entity/reply/calendar enrichment. Deliberately imports nothing from
    app.providers.llm.* or app.providers.factory, so no LLM provider can ever be
    instantiated on this path, and no LLM_API_KEY is ever required.

    Reuses the exact same Email parsing (app.email.models.parse_email),
    normalization (app.email.normalizer.normalize_email), thread resolution
    (app.email.threading.resolve_thread_id), and thread upsert logic
    (app.pipeline._load_thread_candidates / _upsert_thread) that the AI-enriched
    `file` mode already uses -- so a raw-ingested email's thread representation is
    identical in shape to one the full pipeline would have produced, and reprocessing
    the same email later through `file` mode behaves exactly as it already does today.
    """
    provider = FileEmailProvider(path=input_path)
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    inserted = 0
    already_existed = 0
    failed = 0
    failed_message_ids: list[str] = []
    touched_thread_ids: set[str] = set()

    # FileEmailProvider always holds every convertible message regardless of
    # EMAIL_LIMIT (see app/providers/email/file.py) -- fetch_emails(limit=total_found)
    # returns all of them, mirroring how --mode=file already ingests a whole export.
    raw_emails = provider.fetch_emails(limit=provider.total_found)

    for raw in raw_emails:
        message_id = raw.get("message_id")
        try:
            email = parse_email(raw)
        except ValidationError:
            failed += 1
            failed_message_ids.append(message_id or "<unknown>")
            continue

        email = normalize_email(email)

        # Never overwrite an email that already exists -- it may already carry full
        # AI enrichment (processing_status, entities_referenced, etc.) from a prior
        # `file`-mode run, which raw ingestion must never downgrade back to a bare
        # raw copy. This is also what makes a second run of the same file a pure
        # no-op for every email it already touched: idempotent by construction.
        if email_repo.find_one({"message_id": email.message_id}) is not None:
            already_existed += 1
            continue

        email_repo.upsert_by_key(
            {"message_id": email.message_id}, email.model_dump(mode="json", by_alias=True)
        )
        inserted += 1

        candidates = _load_thread_candidates(thread_repo)
        thread_id = resolve_thread_id(email, candidates)
        _upsert_thread(thread_repo, thread_id, email)
        touched_thread_ids.add(thread_id)

    return RawIngestionSummary(
        input_messages=provider.total_found,
        inserted=inserted,
        already_existed=already_existed,
        skipped=provider.skipped_malformed,
        failed=failed,
        unique_threads=len(touched_thread_ids),
        failed_message_ids=failed_message_ids,
    )
