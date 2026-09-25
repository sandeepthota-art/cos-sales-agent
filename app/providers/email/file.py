import json
from pathlib import Path
from typing import Any

from app.interfaces.email_provider import EmailProvider

_REQUIRED_GMAIL_FIELDS = ("id", "threadId", "subject", "sender", "date", "plaintextBody")


def convert_gmail_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Converts one Gmail-export-shaped message dict ({"id", "threadId", "subject",
    "sender", "toRecipients", "ccRecipients", "date", "labelIds", "plaintextBody"})
    into the raw dict shape app.email.models.parse_email() expects. Returns None if a
    field this function itself needs is missing -- everything else (a genuinely
    malformed address, an empty body) is left for parse_email()'s own validation,
    which the pipeline already handles per-message.

    Shared by FileEmailProvider (JSON export ingestion) and LiveGmailSource (live
    Gmail source, app/providers/source/live_gmail.py) so both normalize identically --
    a Gmail message id/thread id must map to the same canonical email.message_id/
    thread_id regardless of which path it arrived through.
    """
    if any(not message.get(field) for field in _REQUIRED_GMAIL_FIELDS):
        return None

    return {
        "message_id": message["id"],
        "thread_id": message["threadId"],
        "from": {"name": None, "email": message["sender"]},
        "to": [{"name": None, "email": addr} for addr in (message.get("toRecipients") or [])],
        "cc": [{"name": None, "email": addr} for addr in (message.get("ccRecipients") or [])],
        "subject": message["subject"],
        "body": message["plaintextBody"],
        "timestamp": message["date"],
        "labels": message.get("labelIds") or [],
    }


class FileEmailProvider(EmailProvider):
    """Reads a Gmail-label export (the shape produced by exporting a label to JSON:
    {"messages": [{"id", "threadId", "subject", "sender", "toRecipients",
    "ccRecipients", "date", "labelIds", "plaintextBody", ...}]}) and converts each
    message into the raw dict shape app.email.models.parse_email() already expects.

    A message missing one of the fields this provider itself needs to build that raw
    dict (id/threadId/subject/sender/date/plaintextBody) is skipped here, not passed
    downstream -- everything else (a genuinely malformed address, an empty body) is
    left for parse_email()'s existing validation, which the pipeline already handles
    per-message (marks that one FAILED, keeps processing the rest).
    """

    def __init__(self, path: str):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
            raise ValueError(
                f"{path}: expected a JSON object with a top-level 'messages' list, "
                f"got {type(raw).__name__}"
            )

        self.total_found = len(raw["messages"])
        self.skipped_malformed = 0
        self._emails: list[dict[str, Any]] = []
        for message in raw["messages"]:
            converted = self._convert(message)
            if converted is None:
                self.skipped_malformed += 1
                continue
            self._emails.append(converted)

    def _convert(self, message: dict[str, Any]) -> dict[str, Any] | None:
        return convert_gmail_message(message)

    def fetch_emails(self, limit: int) -> list[dict[str, Any]]:
        return self._emails[:limit]

    def send_email(self, to: str, subject: str, body: str) -> None:
        print("[SIMULATED EMAIL SEND]")
        print(f"To: {to}")
        print(f"Subject: {subject}")
        print()
        print(body)
