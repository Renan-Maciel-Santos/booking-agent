"""HTTP router for incoming channel messages (POST /v1/messages)."""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from booking_agent.api.dependencies import (
    ConversationServiceDependency,
    UowFactoryDependency,
    get_app_settings,
)
from booking_agent.api.rate_limiter import RateLimiterDependency
from booking_agent.api.schemas import IncomingMessageRequest, MessageResponse
from booking_agent.api.security import verify_webhook_signature
from booking_agent.config import Settings
from booking_agent.domain.errors import LLMConfigurationError, LLMUnavailableError
from booking_agent.services.conversation_service import mask_phone

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])


@router.post(
    "/v1/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Turn processed successfully or duplicate completed message replayed"},
        202: {"description": "Duplicate message still in progress within lease (no text sent)"},
        400: {"description": "Malformed JSON, unknown extra fields, or invalid phone/text bounds"},
        401: {"description": "Missing/invalid HMAC signature or drifted timestamp"},
        413: {"description": "Payload exceeds maximum allowed body size"},
        429: {"description": "Too many requests from phone number"},
        503: {"description": "Transient failure (LLM outage, request timeout, DB error)"},
    },
)
async def receive_message(
    payload: IncomingMessageRequest,
    _raw_body: Annotated[bytes, Depends(verify_webhook_signature)],
    rate_limiter: RateLimiterDependency,
    conversation_service: ConversationServiceDependency,
    uow_factory: UowFactoryDependency,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    """Process an incoming chat message turn from a verified channel webhook."""
    # 1. Rate limiting check per phone number
    await rate_limiter.check(payload.phone)

    # 2. Process conversation turn bounded by total request timeout
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            result = await conversation_service.process_turn(
                message_id=payload.message_id,
                phone=payload.phone,
                text=payload.text,
            )
    except TimeoutError as exc:
        async with uow_factory() as uow:
            await uow.processed_messages.mark_failed(payload.message_id)
        logger.warning(
            "Request timeout exceeded for message %s",
            payload.message_id,
            extra={
                "phone_masked": mask_phone(payload.phone),
                "message_id": payload.message_id,
                "text_len": len(payload.text),
                "outcome": "timeout",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable. Please retry later.",
        ) from exc
    except (LLMUnavailableError, LLMConfigurationError) as exc:
        logger.warning(
            "LLM service failure for message %s: %s",
            payload.message_id,
            exc,
            extra={
                "phone_masked": mask_phone(payload.phone),
                "message_id": payload.message_id,
                "text_len": len(payload.text),
                "outcome": "llm_unavailable",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable. Please retry later.",
        ) from exc

    # 3. Structured observability telemetry (message text is NEVER logged)
    logger.info(
        "Message processed successfully",
        extra={
            "phone_masked": mask_phone(payload.phone),
            "message_id": payload.message_id,
            "text_len": len(payload.text),
            "outcome": result.outcome,
            "fast_path": result.fast_path,
            "stage_from": result.stage_from,
            "stage_to": result.stage_to,
            "phase1_lease_ms": round(result.phase1_lease_ms, 2),
            "phase2_llm_ms": round(result.phase2_llm_ms, 2),
            "phase3_state_ms": round(result.phase3_state_ms, 2),
            "phase4_action_ms": round(result.phase4_action_ms, 2),
            "total_duration_ms": round(result.total_duration_ms, 2),
        },
    )

    # 4. Map HTTP status code
    if result.status_code == status.HTTP_202_ACCEPTED:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=MessageResponse(response_text=None).model_dump(),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=MessageResponse(response_text=result.response_text).model_dump(),
    )
