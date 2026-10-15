"""Single place where structured JSON logging is configured."""

# Serialize each log record as one JSON document.
import json

# Standard library logging is the backbone; no extra dependency is required.
import logging

# Timestamps are emitted in UTC ISO-8601 format.
from datetime import UTC, datetime

# Attributes every LogRecord has, plus uvicorn's ANSI-colored duplicate message;
# anything else was passed through `extra=` and is treated as context.
_RESERVED_RECORD_ATTRIBUTES: frozenset[str] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        """Convert a log record into a JSON string.

        Args:
            record: The record produced by the logging framework.

        Returns:
            JSON-encoded log line.
        """
        # Base fields present on every log line.
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Collect user-supplied context passed via `logger.info(..., extra={...})`.
        extra_fields = {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED_RECORD_ATTRIBUTES
        }
        # Only add the "extra" key when there is context to show.
        if extra_fields:
            payload["extra"] = extra_fields
        # Attach the formatted traceback when an exception is being logged.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str keeps logging from crashing on non-JSON-serializable values.
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Install the JSON formatter on the root logger.

    Args:
        level: Minimum level name, e.g. "INFO" or "DEBUG".
    """
    # One stream handler writing to stderr, the container-friendly default.
    handler = logging.StreamHandler()
    # Every record leaving this handler is rendered as JSON.
    handler.setFormatter(JsonFormatter())
    # Grab the root logger so every module logger inherits this configuration.
    root_logger = logging.getLogger()
    # Replace existing handlers so repeated calls (e.g. in tests) do not duplicate output.
    root_logger.handlers = [handler]
    # Apply the configured minimum level.
    root_logger.setLevel(level.upper())
    # Route uvicorn's own loggers through the root JSON handler.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        # Drop uvicorn's default handlers so lines are not printed twice.
        logging.getLogger(uvicorn_logger_name).handlers = []
        # Let records bubble up to the root handler.
        logging.getLogger(uvicorn_logger_name).propagate = True
