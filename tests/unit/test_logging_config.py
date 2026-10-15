"""Unit tests for the JSON log formatter."""

# Parse the formatter output.
import json

# Build log records by hand.
import logging

# System under test.
from booking_agent.logging_config import JsonFormatter


def _make_record(**extra: object) -> logging.LogRecord:
    """Create a log record with optional extra attributes."""
    # Minimal INFO record.
    record = logging.LogRecord(
        "test.logger", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    # Simulate `logger.info(..., extra={...})`.
    for key, value in extra.items():
        setattr(record, key, value)
    # Hand back the populated record.
    return record


def test_formats_record_as_json_with_extra_context() -> None:
    # Act: format a record carrying context.
    output = json.loads(JsonFormatter().format(_make_record(request_id="abc")))

    # Assert core fields and context.
    assert output["level"] == "INFO"
    assert output["logger"] == "test.logger"
    assert output["message"] == "hello world"
    assert output["extra"] == {"request_id": "abc"}


def test_drops_uvicorn_color_message() -> None:
    # Act: format a record with uvicorn's ANSI duplicate.
    output = json.loads(JsonFormatter().format(_make_record(color_message="\x1b[36mhi")))

    # Assert the noisy field is not emitted.
    assert "extra" not in output
