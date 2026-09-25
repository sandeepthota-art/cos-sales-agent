from datetime import datetime

import pytest
from pydantic import ValidationError

from app.entities.models import Commitment, FollowUp, Meeting, Person, PersonalItem, Project


def test_person_defaults():
    person = Person(id="PER-001", name="Jane Doe", email="jane@example.com")
    assert person.aliases == []
    assert person.review_flag is False
    assert person.source == "gmail"
    assert person.preferences == {}


def test_person_preferences_round_trips_through_the_model():
    person = Person(
        id="PER-001", name="Ashok Ganapam", email="ashok@databeat.io",
        preferences={"voice_signature": "short and concise", "remove_long_dash": True},
    )
    assert person.preferences == {"voice_signature": "short and concise", "remove_long_dash": True}
    dumped = person.model_dump(mode="json")
    assert dumped["preferences"] == {"voice_signature": "short and concise", "remove_long_dash": True}


def test_project_has_no_review_flag_field():
    project = Project(id="PRJ-001", project="Renewal Q4")
    assert not hasattr(project, "review_flag")


def test_commitment_class_field_uses_class_alias():
    commitment = Commitment.model_validate(
        {
            "id": "COM-001",
            "what": "send updated case study",
            "class": "mine",
            "source_record": "msg_001",
            "made_on": "2026-09-13T10:30:00Z",
        }
    )
    assert commitment.commitment_class == "mine"
    dumped = commitment.model_dump(mode="json", by_alias=True)
    assert dumped["class"] == "mine"
    assert "commitment_class" not in dumped


def test_commitment_constructed_by_python_name_also_works():
    commitment = Commitment(
        id="COM-002",
        what="send pricing",
        commitment_class="theirs",
        source_record="msg_002",
        made_on="2026-09-13T10:30:00Z",
    )
    assert commitment.commitment_class == "theirs"


def test_commitment_thread_id_round_trips_through_the_model():
    # Regression: thread_id used to be stamped onto the raw dict AFTER model_dump(),
    # outside the Pydantic schema entirely -- it's now a real model field, so a stored
    # document round-trips through Commitment.model_validate() without losing it.
    commitment = Commitment(
        id="COM-010", what="send pricing", commitment_class="mine",
        source_record="msg_010", made_on="2026-09-13T10:30:00Z", thread_id="thread_abc",
    )
    dumped = commitment.model_dump(mode="json", by_alias=True)
    assert dumped["thread_id"] == "thread_abc"
    reloaded = Commitment.model_validate(dumped)
    assert reloaded.thread_id == "thread_abc"


def test_commitment_thread_id_defaults_to_none():
    commitment = Commitment(
        id="COM-011", what="x", commitment_class="mine", source_record="msg_011", made_on="2026-09-13T10:30:00Z",
    )
    assert commitment.thread_id is None


def test_commitment_rejects_invalid_class():
    with pytest.raises(ValidationError):
        Commitment(
            id="COM-003",
            what="x",
            commitment_class="not_a_real_class",
            source_record="msg_003",
            made_on="2026-09-13T10:30:00Z",
        )


def test_follow_up_requires_at_least_one_link():
    # Was "exactly one" (commitment_id XOR thread_id) -- corrected because a FollowUp
    # derived from a Commitment now carries both commitment_id and thread_id (copied
    # from the commitment's own thread), so it's directly queryable by thread without
    # following commitment_id -> Commitment -> thread_id indirection. Only "neither
    # set" is invalid now.
    with pytest.raises(ValidationError):
        FollowUp(id="FU-001")  # neither set

    both = FollowUp(id="FU-002", commitment_id="COM-001", thread_id="thread_1")
    assert both.commitment_id == "COM-001"
    assert both.thread_id == "thread_1"

    ok = FollowUp(id="FU-003", commitment_id="COM-001")
    assert ok.thread_id is None


def test_follow_up_has_no_extra_fields():
    # BRD 6.4: escalation ladder fields (escalation_level, surfaced, status) plus
    # audience classification and its earliest/latest timing window.
    follow_up = FollowUp(id="FU-004", thread_id="thread_1")
    assert follow_up.model_dump(mode="json").keys() == {
        "id", "commitment_id", "thread_id", "person_id", "org_id",
        "escalation_level", "surfaced", "status",
        "audience", "follow_up_earliest_at", "follow_up_latest_at",
    }


def test_follow_up_escalation_defaults_to_level_one_unsurfaced_active():
    follow_up = FollowUp(id="FU-005", thread_id="thread_1")
    assert follow_up.escalation_level == 1
    assert follow_up.surfaced is False
    assert follow_up.status == "active"


def test_follow_up_accepts_every_brd_escalation_level_and_status():
    for level in (1, 2, 3, 4):
        assert FollowUp(id="FU-006", thread_id="t", escalation_level=level).escalation_level == level
    for status in ("active", "resolved", "dropped"):
        assert FollowUp(id="FU-007", thread_id="t", status=status).status == status


def test_follow_up_rejects_an_escalation_level_outside_one_to_four():
    with pytest.raises(ValidationError):
        FollowUp(id="FU-008", thread_id="t", escalation_level=5)


def test_follow_up_rejects_an_unknown_status():
    with pytest.raises(ValidationError):
        FollowUp(id="FU-009", thread_id="t", status="overdue")


def test_follow_up_audience_defaults_to_none():
    follow_up = FollowUp(id="FU-010", thread_id="t")
    assert follow_up.audience is None
    assert follow_up.follow_up_earliest_at is None
    assert follow_up.follow_up_latest_at is None


def test_follow_up_accepts_every_brd_audience_value():
    for audience in ("internal", "client_fixed_date", "client_open_window", "his_own_question"):
        assert FollowUp(id="FU-011", thread_id="t", audience=audience).audience == audience


def test_follow_up_rejects_an_unknown_audience():
    with pytest.raises(ValidationError):
        FollowUp(id="FU-012", thread_id="t", audience="external")


def test_follow_up_accepts_earliest_and_latest_timing_window():
    follow_up = FollowUp(
        id="FU-013", thread_id="t", audience="internal",
        follow_up_earliest_at=datetime(2026, 1, 2), follow_up_latest_at=datetime(2026, 1, 3),
    )
    assert follow_up.follow_up_earliest_at == datetime(2026, 1, 2)
    assert follow_up.follow_up_latest_at == datetime(2026, 1, 3)


def test_meeting_defaults():
    meeting = Meeting(id="MTG-001")
    assert meeting.attendees == []
    assert meeting.actions_raised == []
    assert meeting.agenda_written is False
    assert meeting.actionable is False


def test_meeting_actionable_can_be_set_true():
    meeting = Meeting(id="MTG-002", actionable=True)
    assert meeting.actionable is True


def test_meeting_thread_id_round_trips_through_the_model():
    # Regression: thread_id used to be stamped onto the raw dict AFTER model_dump(),
    # outside the Pydantic schema -- it's now a real model field.
    meeting = Meeting(id="MTG-003", thread_id="thread_abc")
    dumped = meeting.model_dump(mode="json")
    assert dumped["thread_id"] == "thread_abc"
    reloaded = Meeting.model_validate(dumped)
    assert reloaded.thread_id == "thread_abc"


def test_meeting_thread_id_defaults_to_none():
    assert Meeting(id="MTG-004").thread_id is None


def test_personal_item_defaults():
    item = PersonalItem(id="PSN-001", type="reminder", description="renew passport")
    assert item.status == "open"
    assert item.sender_email is None


def test_personal_item_sender_email_round_trips_through_the_model():
    # Regression: sender_email used to be stamped onto the raw dict AFTER
    # construction, outside the Pydantic schema, and doubled as the dedup key.
    item = PersonalItem(
        id="PSN-002", type="reminder", description="renew passport", sender_email="john@example.com",
    )
    dumped = item.model_dump(mode="json")
    assert dumped["sender_email"] == "john@example.com"
    reloaded = PersonalItem.model_validate(dumped)
    assert reloaded.sender_email == "john@example.com"
