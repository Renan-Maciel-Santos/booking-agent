"""Liveness and readiness endpoints."""

from fastapi import APIRouter, Response, status

from booking_agent.api.dependencies import HealthServiceDependency
from booking_agent.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
def live(service: HealthServiceDependency) -> HealthResponse:
    """Liveness probe: the process is up and answering.

    Args:
        service: Injected health service.

    Returns:
        Always status "ok".
    """
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
    report = await service.check_readiness()
    if not report.is_healthy:
        # 503 tells load balancers and orchestrators to stop routing traffic here.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse.from_report(report)


# Alias for simple monitors that expect a bare /health endpoint.
router.add_api_route("", ready, methods=["GET"], response_model=HealthResponse)
