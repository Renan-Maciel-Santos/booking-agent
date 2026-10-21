"""Unit tests for RequestIdMiddleware, BodySizeLimitMiddleware, and log correlation."""

import json
import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from booking_agent.api.middleware import BodySizeLimitMiddleware, RequestIdMiddleware
from booking_agent.logging_config import JsonFormatter, RequestIdFilter


@pytest.fixture
def middleware_app() -> FastAPI:
    """Create a minimal test FastAPI app with the middlewares attached."""
    app = FastAPI()

    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=100)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/test-echo")
    async def echo_handler(request: Request) -> JSONResponse:
        logging.getLogger("test_middleware").info("inside echo handler")
        return JSONResponse({"status": "ok"})

    @app.post("/test-upload")
    async def upload_handler(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"received_bytes": len(body)})

    return app


@pytest.mark.asyncio
async def test_request_id_middleware_generates_uuid_when_omitted(middleware_app: FastAPI) -> None:
    transport = ASGITransport(app=middleware_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/test-echo")

    assert response.status_code == 200
    req_id = response.headers.get("X-Request-ID")
    assert req_id is not None
    assert len(req_id) >= 32


@pytest.mark.asyncio
async def test_request_id_middleware_preserves_valid_incoming_header(
    middleware_app: FastAPI,
) -> None:
    incoming = "client-req-uuid-12345"
    transport = ASGITransport(app=middleware_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/test-echo", headers={"X-Request-ID": incoming})

    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == incoming


@pytest.mark.asyncio
async def test_request_id_injected_into_log_records_and_json_formatter(
    middleware_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    req_filter = RequestIdFilter()
    formatter = JsonFormatter()
    caplog.handler.addFilter(req_filter)

    incoming_id = "trace-test-uuid-999"
    transport = ASGITransport(app=middleware_app)
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/test-echo", headers={"X-Request-ID": incoming_id})

    assert resp.status_code == 200

    # Verify LogRecord has the attribute
    matching_records = [r for r in caplog.records if r.name == "test_middleware"]
    assert len(matching_records) == 1
    record = matching_records[0]
    assert getattr(record, "request_id", None) == incoming_id

    # Verify JsonFormatter emits request_id
    formatted_json = formatter.format(record)
    parsed = json.loads(formatted_json)
    assert parsed.get("request_id") == incoming_id
    assert parsed.get("message") == "inside echo handler"


@pytest.mark.asyncio
async def test_body_size_limit_middleware_allows_payload_within_limit(
    middleware_app: FastAPI,
) -> None:
    transport = ASGITransport(app=middleware_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/test-upload", content=b"a" * 80)

    assert response.status_code == 200
    assert response.json() == {"received_bytes": 80}


@pytest.mark.asyncio
async def test_body_size_limit_middleware_rejects_with_413_on_content_length(
    middleware_app: FastAPI,
) -> None:
    transport = ASGITransport(app=middleware_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/test-upload", content=b"a" * 150)

    assert response.status_code == 413
    assert response.json() == {"detail": "Payload exceeds maximum allowed size"}
    # Verify X-Request-ID is still set on 413 response
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_body_size_limit_middleware_rejects_streaming_with_413(
    middleware_app: FastAPI,
) -> None:
    async def chunk_generator() -> AsyncIterator[bytes]:
        yield b"a" * 60
        yield b"b" * 60

    transport = ASGITransport(app=middleware_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/test-upload", content=chunk_generator())

    assert response.status_code == 413
    assert response.json() == {"detail": "Payload exceeds maximum allowed size"}
