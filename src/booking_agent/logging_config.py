"""Single place where structured JSON logging is configured."""

import json
import logging
from datetime import UTC, datetime

# Attributes every LogRecord has, plus uvicorn's ANSI-colored duplicate message;
# anything else was passed through `extra=` and is treated as context.
_RESERVED_RECORD_ATTRIBUTES: frozenset[str] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message", "request_id"}
)


class RequestIdFilter(logging.Filter):
    """Inject the active correlation ID from contextvars into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach request_id to the record so it is accessible by formatters and caplog."""
        # Delayed import avoids circular dependency at import time.
        from booking_agent.api.middleware import request_id_ctx

        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        """Convert a log record into a JSON string.

        Args:
            record: The record produced by the logging framework.

        Returns:
            JSON-encoded log line.
        """
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id

        extra_fields = {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED_RECORD_ATTRIBUTES
        }
        if extra_fields:
            payload["extra"] = extra_fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str keeps logging from crashing on non-JSON-serializable values.
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Install the JSON formatter and correlation filter on the root logger.

    Args:
        level: Minimum level name, e.g. "INFO" or "DEBUG".
    """
    # stderr is the container-friendly default destination.
    req_filter = RequestIdFilter()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(req_filter)

    root_logger = logging.getLogger()
    # Add filter to root logger so pytest caplog also receives record.request_id
    root_logger.addFilter(req_filter)

    # Keep external handlers (e.g. pytest caplog) while replacing previous JSON handlers.
    other_handlers = [
        h
        for h in root_logger.handlers
        if not (isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter))
    ]
    root_logger.handlers = [*other_handlers, handler]
    root_logger.setLevel(level.upper())
    # Route uvicorn's loggers through the root JSON handler instead of their own.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(uvicorn_logger_name).handlers = []
        logging.getLogger(uvicorn_logger_name).propagate = True
