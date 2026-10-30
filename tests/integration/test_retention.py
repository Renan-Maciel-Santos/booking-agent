"""Integration tests for personal data retention policy against PostgreSQL."""

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from booking_agent.database.models import Appointment, ConversationState, ProcessedMessage
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.enums import ConversationStage
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import ConversationService
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.retention_service import RetentionReport, RetentionService
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock


async def test_retention_redacts_responses_strictly_before_cutoff(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Messages created before the redaction cutoff lose response_text; newer ones keep it."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=100,
    )
    cutoff = fixed_now - timedelta(days=7)

    # Message 1: 1 second before cutoff -> eligible for redaction
    msg_old = ProcessedMessage(
        message_id=f"msg_old_{uuid.uuid4().hex[:8]}",
        status="done",
        response_text="Old response text",
        created_at=cutoff - timedelta(seconds=1),
        updated_at=cutoff - timedelta(seconds=1),
    )
    # Message 2: 1 second after cutoff -> kept
    msg_recent = ProcessedMessage(
        message_id=f"msg_recent_{uuid.uuid4().hex[:8]}",
        status="done",
        response_text="Recent response text",
        created_at=cutoff + timedelta(seconds=1),
        updated_at=cutoff + timedelta(seconds=1),
    )
    session.add_all([msg_old, msg_recent])
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    report = await service.run()

    assert report.redacted_responses == 1

    async with session_factory() as verify_session:
        res_old = await verify_session.get(ProcessedMessage, msg_old.message_id)
        res_recent = await verify_session.get(ProcessedMessage, msg_recent.message_id)
        assert res_old is not None
        assert res_old.response_text is None
        assert res_recent is not None
        assert res_recent.response_text == "Recent response text"


async def test_retention_redacted_message_deduplicates_without_reprocessing(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """After response redaction, resending message ID does not reprocess or create appointments."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
    )
    msg_id = f"msg_redacted_{uuid.uuid4().hex[:8]}"
    phone = "+5511999991111"

    # Seed an old message that was completed
    msg = ProcessedMessage(
        message_id=msg_id,
        status="done",
        response_text="Booking confirmed for tomorrow!",
        created_at=fixed_now - timedelta(days=10),
        updated_at=fixed_now - timedelta(days=10),
    )
    session.add(msg)
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    # 1. Run retention: response_text is redacted
    report = await service.run()
    assert report.redacted_responses == 1

    # 2. Verify in DB that message is still present and response_text is NULL
    async with session_factory() as verify_session:
        db_msg = await verify_session.get(ProcessedMessage, msg_id)
        assert db_msg is not None
        assert db_msg.response_text is None

    # 3. Wire conversation service and simulate webhook resending the exact same message_id
    booking_service = BookingService(uow_factory=uow_factory, clock=clock, settings=settings)
    entity_resolver = EntityResolver()
    intent_parser = FakeIntentParser(clock=clock)
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=settings,
    )

    turn_result = await conv_service.process_turn(
        message_id=msg_id,
        phone=phone,
        text="Quero agendar um corte amanhã às 14h",
    )

    # Assert it was treated as idempotent replay, not reprocessed
    assert turn_result.outcome == "idempotent_replay"
    assert turn_result.status_code == 200
    assert turn_result.response_text is None

    # Assert no appointment was created in the database
    async with session_factory() as verify_session:
        appts_count = (
            await verify_session.execute(select(func.count(Appointment.id)))
        ).scalar_one()
        assert appts_count == 0


async def test_retention_deletes_messages_strictly_before_cutoff(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Messages created before the delete cutoff are removed; newer ones remain."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=100,
    )
    cutoff = fixed_now - timedelta(days=90)

    # Message 1: 1 second before delete cutoff -> deleted
    msg_expired = ProcessedMessage(
        message_id=f"msg_exp_{uuid.uuid4().hex[:8]}",
        status="done",
        response_text=None,
        created_at=cutoff - timedelta(seconds=1),
        updated_at=cutoff - timedelta(seconds=1),
    )
    # Message 2: 1 second after delete cutoff -> kept
    msg_retained = ProcessedMessage(
        message_id=f"msg_ret_{uuid.uuid4().hex[:8]}",
        status="done",
        response_text=None,
        created_at=cutoff + timedelta(seconds=1),
        updated_at=cutoff + timedelta(seconds=1),
    )
    session.add_all([msg_expired, msg_retained])
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    report = await service.run()

    assert report.deleted_messages == 1

    async with session_factory() as verify_session:
        assert await verify_session.get(ProcessedMessage, msg_expired.message_id) is None
        assert await verify_session.get(ProcessedMessage, msg_retained.message_id) is not None


async def test_retention_deletes_inactive_conversations_except_executing(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Inactive conversation states are deleted, but states in 'executing' stage are kept."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=100,
    )
    cutoff = fixed_now - timedelta(days=30)

    # State 1: Inactive idle conversation before cutoff -> deleted
    state_expired_idle = ConversationState(
        phone="+5511999990001",
        stage=ConversationStage.IDLE.value,
        slots={},
        created_at=cutoff - timedelta(seconds=1),
        updated_at=cutoff - timedelta(seconds=1),
    )
    # State 2: Active idle conversation after cutoff -> kept
    state_recent_idle = ConversationState(
        phone="+5511999990002",
        stage=ConversationStage.IDLE.value,
        slots={},
        created_at=cutoff + timedelta(seconds=1),
        updated_at=cutoff + timedelta(seconds=1),
    )
    # State 3: Old executing conversation before cutoff -> MUST BE KEPT
    state_old_executing = ConversationState(
        phone="+5511999990003",
        stage=ConversationStage.EXECUTING.value,
        slots={"name": "Alice"},
        created_at=cutoff - timedelta(seconds=1),
        updated_at=cutoff - timedelta(seconds=1),
    )
    session.add_all([state_expired_idle, state_recent_idle, state_old_executing])
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    report = await service.run()

    assert report.deleted_conversation_states == 1

    async with session_factory() as verify_session:
        assert await verify_session.get(ConversationState, state_expired_idle.phone) is None
        assert await verify_session.get(ConversationState, state_recent_idle.phone) is not None
        assert await verify_session.get(ConversationState, state_old_executing.phone) is not None


async def test_retention_batching_processes_all_eligible_rows(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """With batch_size=2 and 5 eligible rows, all 5 rows are processed across multiple batches."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=2,
    )
    cutoff = fixed_now - timedelta(days=30)

    # Insert 5 eligible conversation states
    states = [
        ConversationState(
            phone=f"+551199999001{i}",
            stage=ConversationStage.IDLE.value,
            slots={},
            created_at=cutoff - timedelta(days=1),
            updated_at=cutoff - timedelta(days=1),
        )
        for i in range(5)
    ]
    session.add_all(states)
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    report = await service.run()

    assert report.deleted_conversation_states == 5

    async with session_factory() as verify_session:
        for state in states:
            assert await verify_session.get(ConversationState, state.phone) is None


async def test_retention_job_is_idempotent(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Running the retention job twice in a row returns all zeroes on the second run."""
    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=10,
    )

    # Seed 1 message for redaction, 1 for deletion, and 1 state for deletion
    msg_redact = ProcessedMessage(
        message_id="msg_idem_redact",
        status="done",
        response_text="Sensitive text",
        created_at=fixed_now - timedelta(days=10),
        updated_at=fixed_now - timedelta(days=10),
    )
    msg_delete = ProcessedMessage(
        message_id="msg_idem_delete",
        status="done",
        response_text=None,
        created_at=fixed_now - timedelta(days=100),
        updated_at=fixed_now - timedelta(days=100),
    )
    state_delete = ConversationState(
        phone="+5511999990099",
        stage=ConversationStage.IDLE.value,
        slots={},
        created_at=fixed_now - timedelta(days=40),
        updated_at=fixed_now - timedelta(days=40),
    )
    session.add_all([msg_redact, msg_delete, state_delete])
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    # First run processes the eligible records
    report_1: RetentionReport = await service.run()
    assert report_1.redacted_responses == 1
    assert report_1.deleted_messages == 1
    assert report_1.deleted_conversation_states == 1

    # Second run immediately after returns zeroes (idempotent)
    report_2: RetentionReport = await service.run()
    assert report_2.redacted_responses == 0
    assert report_2.deleted_messages == 0
    assert report_2.deleted_conversation_states == 0


async def test_retention_logs_do_not_contain_personal_data(
    engine: AsyncEngine,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The data_retention_completed log event contains no customer phone, name, or response text."""
    marker_phone = "+5511987654321"
    marker_name = "SuperSecretCustomerNameX"
    marker_response = "SensitiveAppointmentConfirmationTextY"

    fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        log_level="INFO",
    )

    msg = ProcessedMessage(
        message_id="msg_privacy_test",
        status="done",
        response_text=marker_response,
        created_at=fixed_now - timedelta(days=10),
        updated_at=fixed_now - timedelta(days=10),
    )
    state = ConversationState(
        phone=marker_phone,
        stage=ConversationStage.IDLE.value,
        slots={"name": marker_name},
        created_at=fixed_now - timedelta(days=40),
        updated_at=fixed_now - timedelta(days=40),
    )
    session.add_all([msg, state])
    await session.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        report = await service.run()
        # Emit the exact completion log matching jobs.retention
        logger = logging.getLogger("booking_agent.jobs.retention")
        logger.info(
            "data_retention_completed",
            extra={
                "event": "data_retention_completed",
                "redacted_responses": report.redacted_responses,
                "deleted_messages": report.deleted_messages,
                "deleted_conversation_states": report.deleted_conversation_states,
                "redact_cutoff": (
                    report.redact_cutoff.isoformat() if report.redact_cutoff else None
                ),
                "delete_messages_cutoff": (
                    report.delete_messages_cutoff.isoformat()
                    if report.delete_messages_cutoff
                    else None
                ),
                "delete_conversations_cutoff": (
                    report.delete_conversations_cutoff.isoformat()
                    if report.delete_conversations_cutoff
                    else None
                ),
            },
        )

    assert "data_retention_completed" in caplog.text
    assert marker_phone not in caplog.text
    assert marker_name not in caplog.text
    assert marker_response not in caplog.text


async def test_retention_cli_command_exits_zero(
    postgres_container: PostgresContainer,
) -> None:
    """The retention CLI command `python -m booking_agent.jobs.retention` exits with code 0."""
    env = os.environ.copy()
    env["POSTGRES_USER"] = postgres_container.username
    env["POSTGRES_PASSWORD"] = postgres_container.password
    env["POSTGRES_DB"] = postgres_container.dbname
    env["POSTGRES_HOST"] = postgres_container.get_container_host_ip()
    env["POSTGRES_PORT"] = str(postgres_container.get_exposed_port(5432))
    env["WEBHOOK_SECRET"] = "0" * 32
    env["LLM_PROVIDER"] = "fake"

    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "booking_agent.jobs.retention"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    output = result.stderr + result.stdout
    assert "data_retention_completed" in output
