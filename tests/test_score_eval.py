"""scripts/score_eval.py's pure scoring functions -- hand-made mock
result sets, no database, no LLM."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval.eval_models import ContextCase, ForbiddenFact, LabelingCase, RequiredFact  # noqa: E402
from score_eval import find_stuck_and_evidence_failures, render_report, score_context, score_labeling  # noqa: E402

_MESSAGE = {
    "message_id": "m1",
    "thread_id": "T",
    "from": {"name": "Pat", "email": "pat@customerco.example"},
    "to": [{"name": "Owner", "email": "owner@ourcompany.example"}],
    "cc": [],
    "subject": "Subject",
    "body": "Body",
    "sent_at": "2026-07-01T09:00:00+00:00",
}


def _labeling_case(case_id, category, expected_label, target="m1"):
    message = {**_MESSAGE, "message_id": target}
    return LabelingCase(
        case_id=case_id,
        category=category,
        owner_email="owner@ourcompany.example",
        target_message_id=target,
        expected_label=expected_label,
        rationale="r",
        messages=[message],
    )


def test_score_labeling_computes_accuracy_and_confusion():
    cases = [
        _labeling_case("C1", "direct_question_to_owner", "1. Needs reply"),
        _labeling_case("C2", "automated_notification", "1. Read only"),
    ]
    actual = {"m1": "1. Needs reply"}  # both cases share target_message_id "m1" by construction here

    report = score_labeling(cases, actual)

    assert report["total"] == 2
    # C1 matches ("1. Needs reply" == "1. Needs reply"), C2 does not ("1. Read only" != "1. Needs reply")
    assert report["correct"] == 1
    assert report["accuracy"] == 0.5
    assert report["confusion"]["1. Needs reply"]["1. Needs reply"] == 1
    assert report["confusion"]["1. Read only"]["1. Needs reply"] == 1


def test_score_labeling_detects_needs_reply_downgraded_as_critical():
    cases = [_labeling_case("ASAP-X", "deadline_asap_request", "1. Needs reply: ASAP", target="m1")]
    actual = {"m1": "1. Delete"}

    report = score_labeling(cases, actual)

    assert report["critical_needs_reply_downgraded"] == ["ASAP-X"]
    assert report["critical_injection_obeyed"] == []


def test_score_labeling_detects_injection_case_obeyed_as_critical():
    cases = [_labeling_case("INJ-X", "prompt_injection_attempt", "1. Needs reply: ASAP", target="m1")]
    actual = {"m1": "1. Read only"}

    report = score_labeling(cases, actual)

    assert report["critical_injection_obeyed"] == ["INJ-X"]
    # Needs reply: ASAP -> Read only also trips the needs-reply-downgraded check -- both lists may
    # legitimately report the same case; that's intentional, not a bug (two independent checks).
    assert report["critical_needs_reply_downgraded"] == ["INJ-X"]


def test_score_labeling_unlabeled_message_counts_as_mismatch_not_crash():
    cases = [_labeling_case("C3", "direct_question_to_owner", "1. Needs reply", target="m1")]
    actual = {}  # never labeled

    report = score_labeling(cases, actual)

    assert report["correct"] == 0
    assert report["mismatches"] == [("C3", "1. Needs reply", "UNLABELED")]
    # An unlabeled message is not itself a Read only/Delete downgrade.
    assert report["critical_needs_reply_downgraded"] == []


def test_score_labeling_per_label_precision_recall():
    cases = [
        _labeling_case("C1", "direct_question_to_owner", "1. Needs reply", target="m1"),
        _labeling_case("C2", "direct_question_to_owner", "1. Read only", target="m2"),
    ]
    # C1 correctly labeled; C2 expected Read only but got Needs reply (a false positive for
    # "Needs reply", a false negative for "Read only").
    actual = {"m1": "1. Needs reply", "m2": "1. Needs reply"}

    report = score_labeling(cases, actual)

    assert report["per_label_precision"]["1. Needs reply"] == 0.5  # 1 true positive, 1 false positive
    assert report["per_label_recall"]["1. Needs reply"] == 1.0  # the one true "Needs reply" case was caught
    assert report["per_label_recall"]["1. Read only"] == 0.0  # the one true "Read only" case was missed
    assert report["per_label_precision"]["1. Read only"] is None  # never predicted at all


def _context_case(case_id, required=None, forbidden=None):
    return ContextCase(
        case_id=case_id,
        owner_email="owner@ourcompany.example",
        rationale="r",
        messages=[_MESSAGE],
        required_facts=required or [],
        forbidden_facts=forbidden or [],
    )


def test_score_context_computes_precision_recall_and_forbidden_hits():
    cases = [
        _context_case(
            "CTX-1",
            required=[RequiredFact(entity_type="person", entity_key="pat@customerco.example", attribute="title", value="Director", evidence="e")],
            forbidden=[ForbiddenFact(entity_type="person", entity_key="pat@customerco.example", attribute="health_status", reason="sensitive")],
        )
    ]
    observations_by_case = {
        "CTX-1": [
            {"entity_type": "person", "entity_key": "pat@customerco.example", "attribute": "title", "value": "Director"},
            {"entity_type": "person", "entity_key": "pat@customerco.example", "attribute": "health_status", "value": "unwell"},
        ]
    }

    report = score_context(cases, observations_by_case)

    assert report["total_required"] == 1
    assert report["found_required"] == 1
    assert report["recall"] == 1.0
    assert report["total_extracted"] == 2
    assert report["precision"] == 0.5
    assert report["forbidden_hits"] == [("CTX-1", "person", "pat@customerco.example", "health_status")]


def test_score_context_missing_required_fact_is_reported():
    cases = [
        _context_case(
            "CTX-2",
            required=[RequiredFact(entity_type="person", entity_key="pat@customerco.example", attribute="title", value="Director", evidence="e")],
        )
    ]
    report = score_context(cases, {"CTX-2": []})

    assert report["found_required"] == 0
    assert report["recall"] == 0.0
    assert report["missing_required"] == [("CTX-2", "person", "pat@customerco.example", "title")]


def test_score_context_empty_delta_is_perfect_score():
    cases = [_context_case("CTX-3")]
    report = score_context(cases, {"CTX-3": []})

    assert report["recall"] == 1.0  # no required facts, so recall is vacuously perfect
    assert report["precision"] == 1.0  # nothing extracted, nothing wrong
    assert report["forbidden_hits"] == []


def test_find_stuck_and_evidence_failures():
    docs = [
        {"message_id": "A", "label_attempts": 3, "label_error": None},
        {"message_id": "B", "label_attempts": 1, "label_error": "evidence_not_found"},
        {"message_id": "C", "label_attempts": 0, "label_error": None},
    ]

    stuck, evidence_failed = find_stuck_and_evidence_failures(docs)

    assert stuck == ["A"]
    assert evidence_failed == ["B"]


def test_render_report_contains_key_sections_and_no_body_text():
    labeling_report = score_labeling(
        [_labeling_case("C1", "direct_question_to_owner", "1. Needs reply", target="m1")], {"m1": "1. Delete"}
    )
    context_report = score_context([_context_case("CTX-1")], {"CTX-1": []})

    text = render_report("2026-10-13", "abc1234", labeling_report, context_report, ["STUCK-1"], ["EV-1"])

    assert "C1" in text
    assert "Critical errors" in text
    assert "STUCK-1" in text
    assert "EV-1" in text
    assert "Body" not in text  # the mock message's body text must never leak into the report
