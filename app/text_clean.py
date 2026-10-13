"""Deterministic, conservative quote/signature stripping for email bodies.
Phase 3 item 1. Never touches stored data -- raw subject/body are never
altered (Mode A's raw-preservation rule). This module only ever produces a
DERIVED view (body_clean), computed on the fly in claim results, never
written to raw_emails_dump.

Conservative by design: when a boundary isn't clearly recognizable, the
original text is kept. The result is never allowed to end up empty --
over-aggressive stripping falls back to the original body unchanged.
"""

import re

_QUOTE_LINE = re.compile(r"^>")
_ON_DATE_WROTE = re.compile(r"^On .{1,120}wrote:\s*$", re.IGNORECASE)
_ORIGINAL_MESSAGE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_OUTLOOK_HEADER = re.compile(r"^(From|Sent|To|Cc|Subject):\s", re.IGNORECASE)
_SIG_DELIMITER = re.compile(r"^--\s?$")
_SIGNOFF = re.compile(r"^(Thanks|Thank you|Regards|Best|Best regards|Kind regards|Sincerely|Cheers)[,.]?\s*$", re.IGNORECASE)
_DISCLAIMER_HINT = re.compile(
    r"(confidential|privileged|intended recipient only|is intended only for|this email and any attachments|"
    r"disclaimer|do not distribute)",
    re.IGNORECASE,
)


def strip_quotes_and_signature(body: str) -> str:
    """Returns body with trailing quoted-reply blocks, Outlook-style
    original-message headers, and common signature/disclaimer blocks
    removed -- everything BEFORE the first recognized boundary is kept
    verbatim. Scans top to bottom, line by line; stops at the FIRST
    boundary found, since quoted/signature content is conventionally at
    the end of a reply, never interleaved with new content above it."""
    if not body:
        return body

    lines = body.splitlines()
    cut_index = len(lines)
    outlook_streak_start: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if _QUOTE_LINE.match(stripped):
            cut_index = i
            break
        if _ON_DATE_WROTE.match(stripped):
            cut_index = i
            break
        if _ORIGINAL_MESSAGE.match(stripped):
            cut_index = i
            break
        if _OUTLOOK_HEADER.match(stripped):
            if outlook_streak_start is None:
                outlook_streak_start = i
            elif i - outlook_streak_start >= 1:
                # Two Outlook-style header lines close together (From:/Sent:
                # or similar) is a quoted header block, not coincidental use
                # of the word "From" in a sentence.
                cut_index = outlook_streak_start
                break
            continue
        else:
            outlook_streak_start = None
        if _SIG_DELIMITER.match(line):
            cut_index = i
            break
        if _SIGNOFF.match(stripped) and i > 0:
            remaining = [l for l in lines[i + 1 : i + 7] if l.strip()]
            # A sign-off followed only by short contact-like lines (name,
            # title, phone) is a real signature. A sign-off followed by
            # more prose, or nothing identifiably short, is left alone.
            if remaining and all(len(l.strip()) < 60 for l in remaining):
                cut_index = i
                break
        if len(stripped) > 40 and _DISCLAIMER_HINT.search(stripped):
            cut_index = i
            break

    cleaned_lines = lines[:cut_index]
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    result = "\n".join(cleaned_lines)
    return result if result.strip() else body
