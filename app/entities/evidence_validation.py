"""Evidence validation for person-attributed facts (PersonFactMention), adapted
from origin/main's app.context.py::_validate_delta -- that branch requires every
claimed fact's cited evidence to be a literal (conservatively normalized)
substring of its own cited source message's text before persisting it.
ingestion-dump's EmailAnalysis/PersonFactMention schema has no evidence concept
at all today, so this module is additive: PersonFactMention.evidence is a new,
optional field (app.analysis.schemas), and this module is the one place that
decides what to do when a caller actually supplies it.

Enforcement task update: evidence is now MANDATORY for every
person_facts_mentioned entry, regardless of why it's missing. `evidence is
None` (the field never supplied) and `evidence == ""`/whitespace-only (the
field supplied but left blank) are both rejected -- there is no longer an
opt-in/unvalidated pass-through for either shape. This is a deliberate
reversal of this module's original backward-compatibility design (which
treated `None` as "this caller predates the field, let it through
unvalidated"): once evidence is the enforced contract, a caller that omits it
is indistinguishable in effect from one that fabricates it -- both let an
unsupported claim reach persistence -- so both are rejected identically.

The schema field itself (app.analysis.schemas.PersonFactMention.evidence)
remains `str | None = None`, NOT a required Pydantic field -- making it
required would mean a single missing per-fact evidence string fails
`EmailAnalysis` parsing entirely (see app.analysis.extractor.
analyze_email_with_validation's retry-then-fail-the-whole-call behavior on a
ValidationError), which would reject an entire email's analysis over one bad
fact. Enforcement therefore lives here, at the per-fact filter boundary, so
exactly one fact is dropped and every other fact in the same email (and the
rest of that email's processing) is unaffected.

Known, stated limitation (never silently assumed away): a substring match
proves the cited evidence text is REAL (not fabricated, not borrowed from a
different email) -- it does not prove the evidence SUPPORTS the specific
claimed value. "The meeting was not approved" is a perfectly real substring of
an email that says exactly that, and would pass a pure presence check even if
the claimed value is "approved" -- the opposite conclusion. This module adds
one narrow, explicitly-heuristic guard for that specific failure shape
(negation present in the evidence but not echoed in the value) -- it is not,
and cannot be, a general contradiction or semantic-correctness detector. Two
facts from two different emails that contradict each other are entirely out of
scope here: this module only ever checks one fact against its own single
cited source email.
"""

import logging
import re

from app.analysis.schemas import PersonFactMention
from app.email.models import Email

logger = logging.getLogger(__name__)

# Conservative: collapses all whitespace runs (newlines, tabs, multiple spaces)
# to a single space and lowercases, exactly matching origin/main's own
# `_normalize_for_match` -- tolerant of formatting differences (line wrapping,
# extra blank lines) without being so aggressive it starts matching text that
# isn't really there.
_WHITESPACE_RE = re.compile(r"\s+")

# Heuristic only (see module docstring): common negation markers that, if
# present in the cited evidence but entirely absent from the claimed value,
# suggest the value may assert the opposite of what the evidence actually
# says. Deliberately short and conservative -- this is a smell test that
# produces a conservative rejection, not a parser of grammatical negation.
_NEGATION_MARKERS = (
    "not ", "n't", "never", "no longer", "unable to", "failed to",
    "cancel", "without", "didn't", "won't", "wasn't", "isn't", "doesn't",
)


def normalize_for_match(text: str) -> str:
    """Whitespace-collapsing, case-insensitive normalization -- the only
    transformation applied before a substring check. No stemming, no
    punctuation stripping: those would make the match more permissive, not
    more conservative, and could start matching text that was never really
    there."""
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _contains_negation(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(marker in normalized for marker in _NEGATION_MARKERS)


def _has_unmirrored_negation(evidence: str, value: str) -> bool:
    """True when the evidence contains a negation marker that the claimed
    value does not also contain -- a conservative, explicitly heuristic smell
    test for "the value asserts the opposite of what the evidence says" (see
    module docstring). A value that itself echoes the negation (e.g. value=
    "not approved") is not flagged -- it may genuinely be reporting the
    negative outcome, which is exactly what the evidence supports."""
    return _contains_negation(evidence) and not _contains_negation(value)


def validate_person_fact_mention(
    mention: PersonFactMention, email: Email
) -> tuple[bool, str | None]:
    """Decides whether `mention` may be persisted, given the ONE email it was
    extracted from (the caller must always pass the email this specific
    mention came from -- there is no mechanism here to check it against any
    other message, by construction, never by convention alone).

    Evidence is mandatory: a mention with no evidence at all (None) is
    rejected exactly like one with blank evidence -- there is no longer an
    unvalidated pass-through for either shape (see module docstring).

    Returns (True, None) when valid. Returns (False, reason) otherwise; the
    reason is a short, stable string safe to log, never the raw email body
    or the full evidence text.
    """
    if mention.evidence is None:
        return False, "missing_evidence"

    if not mention.evidence.strip():
        return False, "empty_evidence"

    if email is None:
        return False, "missing_source_email"

    source_text = normalize_for_match(f"{email.subject or ''} {email.body or ''}")
    evidence_text = normalize_for_match(mention.evidence)

    if not evidence_text:
        return False, "empty_evidence"

    if not source_text.strip():
        return False, "missing_source_body"

    if evidence_text not in source_text:
        return False, "evidence_not_found_in_source_email"

    if _has_unmirrored_negation(mention.evidence, mention.value):
        return False, "negation_mismatch_heuristic"

    return True, None


def filter_validated_person_facts(
    mentions: list[PersonFactMention], email: Email, thread_id: str
) -> list[PersonFactMention]:
    """Drops any mention that doesn't validate -- including one with no
    evidence at all -- called once, before any of `mentions` reaches
    knowledge persistence, so an unsupported fact is omitted rather than
    written and later corrected."""
    kept: list[PersonFactMention] = []
    for mention in mentions:
        valid, reason = validate_person_fact_mention(mention, email)
        if valid:
            kept.append(mention)
        else:
            logger.warning(
                "evidence validation: dropping person_facts_mentioned entry "
                "person_name=%r category=%s thread_id=%s message_id=%s reason=%s",
                mention.person_name, mention.category, thread_id, email.message_id if email else None, reason,
            )
    return kept
