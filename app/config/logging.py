import json
import logging

# BRD gap-analysis C1: fields every stage-transition log record may carry, beyond
# logging.LogRecord's own standard attributes -- pulled out via getattr (never
# raises for a record that omits some of them, e.g. one logged from outside the
# pipeline) rather than assuming every record supplies all of them.
_STRUCTURED_FIELDS = (
    "message_id", "thread_id", "stage", "failed_stage", "duration_ms", "error_type", "error_message",
)


class _JSONFormatter(logging.Formatter):
    """Emits one JSON object per line: timestamp, level, logger name, message, plus
    any of _STRUCTURED_FIELDS actually present on the record (via `extra=`). Never
    includes a field that wasn't supplied -- an absent field is omitted, not null,
    so a log-processing query can distinguish "not applicable" from "empty"."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", structured: bool = False) -> None:
    """structured=True (or LOG_FORMAT=json, wired in main.py) switches every log
    line to one JSON object -- for a production deployment's log aggregator.
    structured=False (the default, unchanged from before this addition) keeps the
    original plain-text format, so no existing behavior/test is affected unless a
    caller opts in explicitly.
    """
    handler = logging.StreamHandler()
    if structured:
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers = [handler]
