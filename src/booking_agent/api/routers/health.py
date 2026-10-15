"""Liveness and readiness endpoints."""

# APIRouter groups routes; Response lets us set the status code; status holds HTTP constants.
from fastapi import APIRouter, Response, status

# Injected service alias.
from booking_agent.api.dependencies import HealthServiceDependency

# Public response contract.
from booking_agent.api.schemas import HealthResponse

# All routes in this module live under /health.
router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
def live(service: HealthServiceDependency) -> HealthResponse:
    """Liveness probe: the process is up and answering.

    Args:
        service: Injected health service.

    Returns:
        Always status "ok".
    """
    # Delegate to the service and map the result to the HTTP schema.
    return HealthResponse.from_report(service.check_liveness())


@router.get("/ready", response_model=HealthResponse)
async def ready(service: HealthServiceDependency, response: Response) -> HealthResponse:
    """Readiness probe: the app can serve traffic (database reachable).

    Args:
        service: Injected health service.
        response: Response object used to set the HTTP status.

    Returns:
        Health report; HTTP 503 when a dependency is unavailable.
    """
    # Ask the service whether every dependency is reachable.
    report = await service.check_readiness()
    # 503 tells load balancers and orchestrators to stop routing traffic here.
    if not report.is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    # Render the report as JSON.
    return HealthResponse.from_report(report)


# Alias for the readiness probe, kept for simple monitors that expect /health.
router.add_api_route("", ready, methods=["GET"], response_model=HealthResponse)
