"""Response schemas exposed by the HTTP API."""

from pydantic import BaseModel, ConfigDict

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
