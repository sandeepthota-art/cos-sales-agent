"""Table-driven tests for app/text_clean.py. Synthetic emails only -- no
real addresses or content, per the project's own privacy-scan convention.
"""

import pytest

from app.text_clean import strip_quotes_and_signature

CASES = [
    (
        "plain_no_boundary_kept_verbatim",
        "Can we discuss renewal pricing for next quarter?",
        "Can we discuss renewal pricing for next quarter?",
    ),
    (
        "quoted_reply_block_stripped",
        "Sounds good, let's proceed.\n\n> On Monday we discussed the proposal\n> and agreed on terms.",
        "Sounds good, let's proceed.",
    ),
    (
        "on_date_wrote_block_stripped",
        "Thanks, that works for me.\n\nOn Tue, Oct 7, 2026 at 3:00 PM, Jane Doe <jane@customerco.example> wrote:\nCan we move the call?",
        "Thanks, that works for me.",
    ),
    (
        "outlook_original_message_header_stripped",
        "Approved, go ahead.\n\n-----Original Message-----\nFrom: Jane Doe\nSent: Monday\nTo: Agent\nSubject: Renewal",
        "Approved, go ahead.",
    ),
    (
        "outlook_header_pair_without_separator_stripped",
        "Approved, go ahead.\n\nFrom: Jane Doe\nSent: Monday\nOriginal text here that is now quoted.",
        "Approved, go ahead.",
    ),
    (
        "signature_after_dash_dash_stripped",
        "See you then.\n\n--\nJane Doe\nVP Sales\n555-0100",
        "See you then.",
    ),
    (
        "signoff_with_short_contact_lines_stripped",
        "Let's finalize by Friday.\n\nThanks,\nJane Doe\nVP Sales",
        "Let's finalize by Friday.",
    ),
    (
        "signoff_followed_by_prose_kept",
        "Quick update.\n\nThanks for bearing with us through the migration, "
        "we really appreciate the patience shown by the whole team this quarter.",
        "Quick update.\n\nThanks for bearing with us through the migration, "
        "we really appreciate the patience shown by the whole team this quarter.",
    ),
    (
        "disclaimer_footer_stripped",
        "Here is the contract draft for review.\n\n"
        "This email and any attachments are confidential and intended solely for the addressee.",
        "Here is the contract draft for review.",
    ),
    (
        "empty_body_returns_empty",
        "",
        "",
    ),
    (
        "boundary_on_first_line_falls_back_to_original",
        "> entirely quoted, nothing else",
        "> entirely quoted, nothing else",  # conservative: never return empty
    ),
    (
        "single_word_from_in_prose_not_treated_as_header",
        "This update is from the whole team, not just one person.",
        "This update is from the whole team, not just one person.",
    ),
]


@pytest.mark.parametrize("name,body,expected", CASES, ids=[c[0] for c in CASES])
def test_strip_quotes_and_signature(name, body, expected):
    assert strip_quotes_and_signature(body) == expected
