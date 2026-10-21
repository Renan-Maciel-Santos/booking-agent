"""Unit tests for the JSON log formatter."""

import json
import logging

from booking_agent.logging_config import JsonFormatter


def _make_record(**extra: object) -> logging.LogRecord:
    """Create a log record with optional extra attributes."""
    record = logging.LogRecord(
        "test.logger", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    # Simulate `logger.info(..., extra={...})`.
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_record_as_json_with_extra_context() -> None:
    # Act: format a record carrying context.
    output = json.loads(JsonFormatter().format(_make_record(customer_id="cust-123")))

    # Assert core fields and context.
    assert output["level"] == "INFO"
    assert output["logger"] == "test.logger"
    assert output["message"] == "hello world"
    assert output["extra"] == {"customer_id": "cust-123"}


def test_formats_record_as_json_with_top_level_request_id() -> None:
    # Act: format a record carrying request_id.
    output = json.loads(JsonFormatter().format(_make_record(request_id="abc-123")))

    # Assert request_id is promoted to top-level and omitted from extra.
    assert output["request_id"] == "abc-123"
    assert "extra" not in output


def test_drops_uvicorn_color_message() -> None:
    # Act: format a record with uvicorn's ANSI duplicate.
    output = json.loads(JsonFormatter().format(_make_record(color_message="\x1b[36mhi")))

    # Assert the noisy field is not emitted.
    assert "extra" not in output
