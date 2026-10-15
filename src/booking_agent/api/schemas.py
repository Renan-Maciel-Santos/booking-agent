"""Response schemas exposed by the HTTP API."""

# BaseModel defines the public JSON contract; ConfigDict freezes it.
from pydantic import BaseModel, ConfigDict

# Enums reused so the HTTP contract and the service stay in sync.
from booking_agent.services.health_service import ComponentStatus, HealthReport, HealthStatus


class HealthResponse(BaseModel):
    """JSON body returned by the health endpoints."""

    # Responses are immutable value objects.
    model_config = ConfigDict(frozen=True)

    # Aggregated application status.
    status: HealthStatus
    # Per-dependency status; empty for liveness.
    checks: dict[str, ComponentStatus]

    @classmethod
    def from_report(cls, report: HealthReport) -> "HealthResponse":
        """Map the service-layer report to the HTTP schema.

        Args:
            report: Report produced by HealthService.

        Returns:
            Serializable response body.
        """
        # Copy the dict so the response never shares mutable state with the report.
        return cls(status=report.status, checks=dict(report.checks))
