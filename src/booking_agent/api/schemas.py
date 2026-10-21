"""Request and response schemas exposed by the HTTP API."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from booking_agent.services.health_service import ComponentStatus, HealthReport, HealthStatus


class HealthResponse(BaseModel):
    """JSON body returned by the health endpoints."""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    checks: dict[str, ComponentStatus]

    @classmethod
    def from_report(cls, report: HealthReport) -> "HealthResponse":
        """Map the service-layer report to the HTTP schema.

        Args:
            report: Report produced by HealthService.

        Returns:
            Serializable response body.
        """
        # Copy so the response never shares mutable state with the report.
        return cls(status=report.status, checks=dict(report.checks))


class IncomingMessageRequest(BaseModel):
    """External incoming message envelope validated at the HTTP boundary."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-\.:]{1,128}$")
    phone: str = Field(pattern=r"^\+[1-9]\d{1,14}$")
    text: str = Field(min_length=1, max_length=1000)
    timestamp: AwareDatetime

    @field_validator("text")
    @classmethod
    def validate_non_empty_text(cls, v: str) -> str:
        """Ensure message text is not empty or whitespace-only."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Message text must not be empty or whitespace-only")
        return stripped


class MessageResponse(BaseModel):
    """External outgoing response envelope for messaging adapters."""

    model_config = ConfigDict(extra="forbid")

    response_text: str | None = None
