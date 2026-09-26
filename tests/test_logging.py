import json
import logging

from app.config.logging import _JSONFormatter, configure_logging


def _make_record(msg="pipeline stage transition", **extra):
    record = logging.LogRecord(
        name="app.pipeline.stage", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_supplied_structured_fields():
    record = _make_record(message_id="msg_001", thread_id="t1", stage="COMPLETED", duration_ms=12.5)

    payload = json.loads(_JSONFormatter().format(record))

    assert payload["message_id"] == "msg_001"
    assert payload["thread_id"] == "t1"
    assert payload["stage"] == "COMPLETED"
    assert payload["duration_ms"] == 12.5
    assert payload["message"] == "pipeline stage transition"
    assert payload["level"] == "INFO"


def test_json_formatter_omits_fields_that_were_never_supplied():
    record = _make_record(message_id="msg_001", stage="ANALYZED")

    payload = json.loads(_JSONFormatter().format(record))

    assert "thread_id" not in payload
    assert "duration_ms" not in payload
    assert "error_type" not in payload
    assert "error_message" not in payload


def test_json_formatter_never_raises_on_a_record_with_no_structured_fields_at_all():
    record = _make_record()

    payload = json.loads(_JSONFormatter().format(record))  # must not raise

    assert payload["message"] == "pipeline stage transition"


def test_configure_logging_structured_true_produces_valid_json_output(capsys):
    configure_logging(structured=True)
    logging.getLogger("app.pipeline.stage").info("test", extra={"message_id": "msg_001", "stage": "COMPLETED"})

    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["message_id"] == "msg_001"
    assert payload["stage"] == "COMPLETED"


def test_configure_logging_structured_false_keeps_plain_text_format(capsys):
    configure_logging(structured=False)
    logging.getLogger("app.pipeline.stage").info("plain text message")

    captured = capsys.readouterr()
    last_line = captured.err.strip().splitlines()[-1]
    assert "plain text message" in last_line
    assert not last_line.strip().startswith("{")
