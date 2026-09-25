import pytest
from pydantic import ValidationError

from app.analysis.schemas import EmailAnalysis, Fact


def test_email_analysis_defaults():
    analysis = EmailAnalysis(email_id="msg_001", summary="s", intent="evaluation")
    assert analysis.facts == []
    assert analysis.pain_points == []
    assert analysis.competitors == []


def test_email_analysis_accepts_facts():
    analysis = EmailAnalysis(
        email_id="msg_001",
        summary="s",
        intent="evaluation",
        facts=[Fact(subject="Customer", predicate="uses", object="Salesforce")],
    )
    assert analysis.facts[0].object == "Salesforce"


def test_email_analysis_entity_signal_defaults():
    analysis = EmailAnalysis(email_id="msg_001", summary="s", intent="evaluation")
    assert analysis.people_mentioned == []
    assert analysis.projects_mentioned == []
    assert analysis.commitments_mentioned == []
    assert analysis.meetings_mentioned == []
    assert analysis.personal_items_mentioned == []
    assert analysis.goal_pillar == ""
    assert analysis.label_applied == "Undecided"
    assert analysis.priority == "P2"
    assert analysis.confidence == 0.0


@pytest.mark.parametrize("priority", ["P1", "P2"])
def test_email_analysis_accepts_p1_and_p2(priority):
    analysis = EmailAnalysis(email_id="msg_001", summary="s", intent="evaluation", priority=priority)
    assert analysis.priority == priority


@pytest.mark.parametrize("priority", ["P0", "P3", "High", "Medium", "Low", "p1"])
def test_email_analysis_rejects_any_priority_other_than_p1_or_p2(priority):
    with pytest.raises(ValidationError):
        EmailAnalysis(email_id="msg_001", summary="s", intent="evaluation", priority=priority)


@pytest.mark.parametrize(
    "label",
    [
        "Needs reply: ASAP",
        "Needs reply",
        "Needs reply: mention",
        "Read only",
        "Delete",
        "Undecided",
    ],
)
def test_email_analysis_accepts_every_brd_label(label):
    analysis = EmailAnalysis(
        email_id="msg_001", summary="s", intent="evaluation", label_applied=label
    )
    assert analysis.label_applied == label


def test_email_analysis_rejects_the_retired_needs_reply_soon_label():
    # "Needs reply: Soon" was this repo's original name for the BRD's plain "Needs
    # reply" -- it must no longer validate now that the taxonomy has been replaced.
    with pytest.raises(ValidationError):
        EmailAnalysis(
            email_id="msg_001", summary="s", intent="evaluation", label_applied="Needs reply: Soon"
        )


def test_email_analysis_rejects_an_unknown_label():
    with pytest.raises(ValidationError):
        EmailAnalysis(
            email_id="msg_001", summary="s", intent="evaluation", label_applied="Not A Real Label"
        )


def test_email_analysis_accepts_raw_commitment_with_class_alias():
    analysis = EmailAnalysis.model_validate(
        {
            "email_id": "msg_001",
            "summary": "s",
            "intent": "evaluation",
            "commitments_mentioned": [
                {"what": "send case study", "class": "mine", "date_phrase": "next Friday"}
            ],
        }
    )
    assert analysis.commitments_mentioned[0].commitment_class == "mine"


def test_meetings_field_accepts_plain_strings():
    analysis = EmailAnalysis.model_validate(
        {
            "email_id": "msg_001",
            "summary": "s",
            "intent": "evaluation",
            "meetings": ["call scheduled for next week", "met at Possible in Miami"],
        }
    )
    assert analysis.meetings == ["call scheduled for next week", "met at Possible in Miami"]


def test_meetings_field_rejects_structured_objects():
    # Regression: Claude previously put RawMeeting-shaped dicts into this legacy flat
    # field, which is declared list[str] -- the model correctly rejects that shape, so
    # the fix belongs in the prompt (see test_llm_provider_prompts.py), not here.
    with pytest.raises(ValidationError):
        EmailAnalysis.model_validate(
            {
                "email_id": "msg_001",
                "summary": "s",
                "intent": "evaluation",
                "meetings": [{"date_phrase": "next week", "attendees": ["Tim", "Ashok"]}],
            }
        )


def test_meetings_mentioned_accepts_structured_raw_meeting_objects():
    analysis = EmailAnalysis.model_validate(
        {
            "email_id": "msg_001",
            "summary": "s",
            "intent": "evaluation",
            "meetings_mentioned": [
                {
                    "date_phrase": "next week",
                    "attendees": ["Tim", "Ashok"],
                    "is_past": False,
                    "actions_raised": ["send agenda"],
                }
            ],
        }
    )
    assert analysis.meetings_mentioned[0].date_phrase == "next week"
    assert analysis.meetings_mentioned[0].attendees == ["Tim", "Ashok"]


def test_representative_claude_response_with_both_meeting_fields_parses_successfully():
    # A realistic full response shaped the way the corrected prompt asks for: plain
    # strings in `meetings`, structured objects only in `meetings_mentioned`.
    analysis = EmailAnalysis.model_validate(
        {
            "email_id": "msg_001",
            "summary": "Discussing a potential partnership introduction.",
            "intent": "introduction",
            "meetings": ["met at Possible in Miami"],
            "meetings_mentioned": [
                {
                    "date_phrase": "at Possible in Miami",
                    "attendees": ["Tim", "Ashok"],
                    "is_past": True,
                    "actions_raised": ["send collateral"],
                }
            ],
            "people_mentioned": [{"name": "Tim", "email": None, "org": None, "role_hint": "prospect"}],
        }
    )
    assert analysis.meetings == ["met at Possible in Miami"]
    assert analysis.meetings_mentioned[0].is_past is True
    assert analysis.people_mentioned[0].name == "Tim"
