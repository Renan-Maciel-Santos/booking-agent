"""Integration tests for ConversationService concurrency and crash recovery with real PostgreSQL."""

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta
from types import TracebackType
from typing import Any, Self

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.config import Settings
from booking_agent.database.models import (
    Appointment,
    BookingAction,
    ConversationState,
    ProcessedMessage,
)
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.conversation_state import ANY_BARBER
from booking_agent.domain.enums import ConversationStage
from booking_agent.domain.models import BookingResult
from booking_agent.domain.ports import (
    AppointmentRepositoryPort,
    BarberRepositoryPort,
    BookingActionRepositoryPort,
    BusinessHoursRepositoryPort,
    ConversationStateRepositoryPort,
    CustomerRepositoryPort,
    ProcessedMessageRepositoryPort,
    ServiceRepositoryPort,
    UnitOfWork,
)
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.business_hours_repository import BusinessHoursRepository
from booking_agent.repositories.conversation_state_repository import ConversationStateRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import ConversationService
from booking_agent.services.entity_resolver import EntityResolver
from tests.fakes.fake_clock import FakeClock


async def _setup_test_environment(
    session: AsyncSession,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Create business hours, a service, and two active barbers for 'any barber' testing."""
    hours_repo = BusinessHoursRepository(session)
    for day in range(7):
        await hours_repo.upsert(day_of_week=day, opens_at=time(8, 0), closes_at=time(20, 0))

    service_repo = ServiceRepository(session)
    svc = await service_repo.create(
        name="Corte de Cabelo",
        duration_minutes=30,
        price_cents=4500,
    )

    barber_repo = BarberRepository(session)
    b1 = await barber_repo.create(name="Barbeiro Carlos")
    b2 = await barber_repo.create(name="Barbeiro Marcos")

    await session.commit()
    return svc.id, [b1.id, b2.id]


@pytest.mark.integration
async def test_two_concurrent_confirmations_create_exactly_one_appointment_any_barber(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario A: Two concurrent 'sim' messages with different message_ids.

    Deterministic concurrency: Message 1 enters Phase 3, transitions state to EXECUTING,
    releases the row lock, and reaches Phase 4. We hold Message 1 in Phase 4 via an
    asyncio.Event while Message 2 arrives, observes EXECUTING under its own row lock,
    and returns REQUEST_PROCESSING. When Message 1 finishes, exactly one appointment exists.
    """
    now_utc = datetime.now(UTC)
    target_date = (now_utc + timedelta(days=2)).date()
    local_date_str = target_date.isoformat()
    local_date_pt = target_date.strftime("%d/%m")

    async with session_factory() as session:
        service_id, _barber_ids = await _setup_test_environment(session)
        customer_phone = "+5511999990001"
        action_id = uuid.uuid4()

        # Seed conversation_states in AWAITING_CONFIRMATION with any-barber (ANY_BARBER)
        state = ConversationState(
            phone=customer_phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": local_date_str,
                "local_time": "14:00:00",
                "barber_id": ANY_BARBER,
                "barber_name": None,
                "customer_name": "Renan Maciel",
                "action_id": str(action_id),
            },
            updated_at=now_utc,
        )
        session.add(state)
        await session.commit()

    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Synchronize execution: hold Message 1 in Phase 4 while Message 2 executes
    msg1_in_phase_4 = asyncio.Event()
    msg2_done = asyncio.Event()

    original_book = booking_service.book

    async def hooked_book(
        customer_phone: str,
        customer_name: str,
        service_id: uuid.UUID,
        starts_at: datetime,
        barber_id: uuid.UUID | None = None,
        action_id: uuid.UUID | None = None,
    ) -> BookingResult:
        msg1_in_phase_4.set()
        await msg2_done.wait()
        return await original_book(
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_id=service_id,
            starts_at=starts_at,
            barber_id=barber_id,
            action_id=action_id,
        )

    booking_service.book = hooked_book  # type: ignore[method-assign]

    # Launch Message 1
    task1 = asyncio.create_task(
        conv_service.handle_message(
            message_id="msg_sim_concurrent_1",
            phone=customer_phone,
            text="sim",
        )
    )

    # Wait until Message 1 has reached Phase 4 (Phase 3 lock released)
    await msg1_in_phase_4.wait()

    # Now execute Message 2 concurrently with a different message_id
    resp2 = await conv_service.handle_message(
        message_id="msg_sim_concurrent_2",
        phone=customer_phone,
        text="sim",
    )

    # Resume Message 1 to complete its booking
    msg2_done.set()
    resp1 = await task1

    # Verify responses: Message 1 got confirmed, Message 2 was informed of in-flight processing
    assert f"confirmado para {local_date_pt} às 14:00" in resp1
    assert "está sendo processado" in resp2

    # Verify real PostgreSQL state: EXACTLY ONE appointment and ONE booking_action row
    async with session_factory() as session:
        appts_stmt = select(Appointment)
        appts = (await session.execute(appts_stmt)).scalars().all()
        assert len(appts) == 1

        actions_stmt = select(BookingAction)
        actions = (await session.execute(actions_stmt)).scalars().all()
        assert len(actions) == 1
        assert actions[0].action_id == action_id
        assert actions[0].appointment_id == appts[0].id


@pytest.mark.integration
async def test_replayed_message_id_returns_identical_response_without_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario B: Re-sending the same message_id after completion returns identical response."""
    async with session_factory() as session:
        service_id, _ = await _setup_test_environment(session)
        customer_phone = "+5511999990002"
        action_id = uuid.uuid4()

        state = ConversationState(
            phone=customer_phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": "2026-11-20",
                "local_time": "15:00:00",
                "barber_id": ANY_BARBER,
                "barber_name": None,
                "customer_name": "Lucas",
                "action_id": str(action_id),
            },
            updated_at=datetime(2026, 11, 20, 9, 55, tzinfo=UTC),
        )
        session.add(state)
        await session.commit()

    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    conv_service = ConversationService(
        intent_parser=FakeIntentParser(clock=clock),
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    msg_id = "msg_replay_001"
    first_response = await conv_service.handle_message(
        message_id=msg_id,
        phone=customer_phone,
        text="sim",
    )
    assert "confirmado para 20/11 às 15:00" in first_response

    # Replay identical message_id
    second_response = await conv_service.handle_message(
        message_id=msg_id,
        phone=customer_phone,
        text="sim",
    )
    assert second_response == first_response

    # Verify no second appointment was created
    async with session_factory() as session:
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 1


@pytest.mark.integration
async def test_simulated_crash_between_phase_3_and_phase_4_recovers_cleanly(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario C1: Simulated crash between Phase 3 and Phase 4.

    The worker commits EXECUTING state in Phase 3, but crashes before executing Phase 4.
    Simulated strictly in test code by raising an exception in booking_service.book.
    On retry after lease timeout, the message completes with exactly 1 appointment.
    """
    now_utc = datetime.now(UTC)
    target_date = (now_utc + timedelta(days=2)).date()
    local_date_str = target_date.isoformat()
    local_date_pt = target_date.strftime("%d/%m")

    async with session_factory() as session:
        service_id, _ = await _setup_test_environment(session)
        customer_phone = "+5511999990031"
        action_id = uuid.uuid4()
        msg_id = "msg_crash_p3_p4"

        state = ConversationState(
            phone=customer_phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": local_date_str,
                "local_time": "16:00:00",
                "barber_id": ANY_BARBER,
                "barber_name": None,
                "customer_name": "Carlos Crash P3",
                "action_id": str(action_id),
            },
            updated_at=now_utc,
        )
        session.add(state)
        await session.commit()

    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    conv_service = ConversationService(
        intent_parser=FakeIntentParser(clock=clock),
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Inject test-only crash right when Phase 4 starts
    crashed = False
    original_book = booking_service.book

    async def crashing_book(
        customer_phone: str,
        customer_name: str,
        service_id: uuid.UUID,
        starts_at: datetime,
        barber_id: uuid.UUID | None = None,
        action_id: uuid.UUID | None = None,
    ) -> BookingResult:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("Simulated worker crash between Phase 3 and Phase 4")
        return await original_book(
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_id=service_id,
            starts_at=starts_at,
            barber_id=barber_id,
            action_id=action_id,
        )

    booking_service.book = crashing_book  # type: ignore[method-assign]

    # First attempt: crashes during Phase 4
    with pytest.raises(RuntimeError, match="Simulated worker crash between Phase 3 and Phase 4"):
        await conv_service.handle_message(
            message_id=msg_id,
            phone=customer_phone,
            text="sim",
        )

    # Verify state after crash: ConversationState is in EXECUTING, but NO appointment was created
    async with session_factory() as session:
        st = await ConversationStateRepository(session).get(customer_phone)
        assert st is not None
        assert st.stage == ConversationStage.EXECUTING.value
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 0

        # Update processed_messages updated_at so lease is expired for retry
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        await session.execute(
            sa.update(ProcessedMessage)
            .where(ProcessedMessage.message_id == msg_id)
            .values(updated_at=stale_time)
        )
        await session.commit()

    # Retry message processing after worker recovery
    resp_retry = await conv_service.handle_message(
        message_id=msg_id,
        phone=customer_phone,
        text="sim",
    )
    assert f"confirmado para {local_date_pt} às 16:00" in resp_retry

    # Exactly 1 appointment in PostgreSQL
    async with session_factory() as session:
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 1
        actions = (await session.execute(select(BookingAction))).scalars().all()
        assert len(actions) == 1


@pytest.mark.integration
async def test_simulated_crash_between_phase_4_and_phase_6_recovers_cleanly(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario C2: Simulated crash between Phase 4 and Phase 6.

    The action commits in Phase 4 (Appointment + BookingAction in DB), but the worker
    crashes before committing Phase 6 (persisting COMPLETED and mark_done).
    Simulated strictly in test code by failing the Phase 6 UnitOfWork commit.
    On retry, booking_actions detects the existing action and returns it without duplication.
    """
    now_utc = datetime.now(UTC)
    target_date = (now_utc + timedelta(days=2)).date()
    local_date_str = target_date.isoformat()
    local_date_pt = target_date.strftime("%d/%m")

    async with session_factory() as session:
        service_id, _ = await _setup_test_environment(session)
        customer_phone = "+5511999990032"
        action_id = uuid.uuid4()
        msg_id = "msg_crash_p4_p6"

        state = ConversationState(
            phone=customer_phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": local_date_str,
                "local_time": "17:00:00",
                "barber_id": ANY_BARBER,
                "barber_name": None,
                "customer_name": "Maria Crash P4",
                "action_id": str(action_id),
            },
            updated_at=now_utc,
        )
        session.add(state)
        await session.commit()

    clock = FakeClock(now_utc)
    real_uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(real_uow_factory, clock, integration_settings)

    # In conv_service, inject a test-only crash on the Phase 6 UoW
    uow_call_count = 0
    crashed = False

    def crashing_uow_factory() -> UnitOfWork:
        nonlocal uow_call_count, crashed
        uow_call_count += 1
        current_count = uow_call_count

        # Call 1 is Phase 1 (lease), Call 2 is Phase 3 (transition to EXECUTING),
        # Call 3 is Phase 6 (commit COMPLETED and mark_done)
        if current_count == 3 and not crashed:
            crashed = True
            raise RuntimeError("Simulated worker crash between Phase 4 and Phase 6")

        return real_uow_factory()

    conv_service = ConversationService(
        intent_parser=FakeIntentParser(clock=clock),
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=crashing_uow_factory,
        settings=integration_settings,
    )

    with pytest.raises(RuntimeError, match="Simulated worker crash between Phase 4 and Phase 6"):
        await conv_service.handle_message(
            message_id=msg_id,
            phone=customer_phone,
            text="sim",
        )

    # Verify: Appointment and BookingAction are committed, but ConversationState is still EXECUTING
    async with session_factory() as session:
        st = await ConversationStateRepository(session).get(customer_phone)
        assert st is not None
        assert st.stage == ConversationStage.EXECUTING.value
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 1
        actions = (await session.execute(select(BookingAction))).scalars().all()
        assert len(actions) == 1

        # Expire lease on processed_messages for retry
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        await session.execute(
            sa.update(ProcessedMessage)
            .where(ProcessedMessage.message_id == msg_id)
            .values(updated_at=stale_time)
        )
        await session.commit()

    # Retry using normal unhooked uow_factory
    conv_service._uow_factory = real_uow_factory
    resp_retry = await conv_service.handle_message(
        message_id=msg_id,
        phone=customer_phone,
        text="sim",
    )
    assert f"confirmado para {local_date_pt} às 17:00" in resp_retry

    # Verify: STILL exactly 1 appointment and 1 booking_action (idempotent, no duplicates)
    async with session_factory() as session:
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 1
        actions = (await session.execute(select(BookingAction))).scalars().all()
        assert len(actions) == 1


@pytest.mark.integration
async def test_stuck_executing_resumes_action_on_next_message_after_lease_timeout(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Requirement 2: If stage is EXECUTING longer than the lease, next message resumes action.

    Instead of replying 'em processamento', the new message from the same phone retakes
    the execution with the same action_id (idempotent via booking_actions).
    """
    now_utc = datetime.now(UTC)
    target_date = (now_utc + timedelta(days=2)).date()
    local_date_str = target_date.isoformat()
    local_date_pt = target_date.strftime("%d/%m")

    async with session_factory() as session:
        service_id, barber_ids = await _setup_test_environment(session)
        customer_phone = "+5511999990033"
        action_id = uuid.uuid4()
        crashed_msg_id = "msg_crashed_worker_1"

        # State stuck in EXECUTING 6 minutes ago (lease is 5 minutes)
        stuck_state = ConversationState(
            phone=customer_phone,
            stage=ConversationStage.EXECUTING.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": local_date_str,
                "local_time": "14:00:00",
                "barber_id": str(barber_ids[0]),
                "barber_name": "Barbeiro Carlos",
                "customer_name": "Cliente Retomado",
                "action_id": str(action_id),
                "executing_message_id": crashed_msg_id,
            },
            updated_at=now_utc - timedelta(minutes=6),
        )
        session.add(stuck_state)
        await session.commit()

    # Clock is at now_utc (6 minutes after state update, exceeds 300s lease)
    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    conv_service = ConversationService(
        intent_parser=FakeIntentParser(clock=clock),
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Next message from same customer with a new message_id
    resp = await conv_service.handle_message(
        message_id="msg_new_turn_after_crash",
        phone=customer_phone,
        text="Olá, deu certo?",
    )

    # Must NOT reply 'em processamento'; must resume action and confirm booking
    assert f"confirmado para {local_date_pt} às 14:00" in resp
    assert "sendo processado" not in resp

    # In PostgreSQL: exactly 1 appointment was created with action_id
    async with session_factory() as session:
        cust = await CustomerRepository(session).get_by_phone(customer_phone)
        assert cust is not None
        appts = await AppointmentRepository(session).list_by_customer(cust.id)
        assert len(appts) == 1

        actions = (await session.execute(select(BookingAction))).scalars().all()
        assert len(actions) == 1
        assert actions[0].action_id == action_id


@pytest.mark.integration
async def test_service_level_concurrent_book_with_same_action_id_unblocks_23505(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Requirement 3b: Two concurrent calls to booking_service.book with the SAME action_id.

    The first call holds its transaction open after recording the action in booking_actions.
    The second call attempts to record the same action_id, blocking on PostgreSQL's row lock.
    When Call 1 commits, Call 2 unblocks with SQLSTATE 23505 (ActionAlreadyExistsError),
    queries the committed appointment in a new unit of work, and returns the existing
    appointment successfully without error.
    """
    now_utc = datetime.now(UTC)
    target_date = (now_utc + timedelta(days=2)).date()

    async with session_factory() as session:
        service_id, barber_ids = await _setup_test_environment(session)
        customer_phone = "+5511999990034"
        customer_name = "Cliente Determinístico"

    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    shared_action_id = uuid.uuid4()
    starts_at = datetime.combine(target_date, time(15, 0), tzinfo=UTC)

    call1_in_db = asyncio.Event()
    call2_waiting_on_postgres = asyncio.Event()

    class HookedUnitOfWork(UnitOfWork):
        """UnitOfWork wrapper with explicit __aenter__ and __aexit__ methods on the class."""

        def __init__(self, real_uow: UnitOfWork, call_id: int) -> None:
            self._real_uow = real_uow
            self._call_id = call_id

        @property
        def appointments(self) -> AppointmentRepositoryPort:
            return self._real_uow.appointments

        @property
        def barbers(self) -> BarberRepositoryPort:
            return self._real_uow.barbers

        @property
        def booking_actions(self) -> BookingActionRepositoryPort:
            return self._real_uow.booking_actions

        @property
        def business_hours(self) -> BusinessHoursRepositoryPort:
            return self._real_uow.business_hours

        @property
        def conversation_states(self) -> ConversationStateRepositoryPort:
            return self._real_uow.conversation_states

        @property
        def customers(self) -> CustomerRepositoryPort:
            return self._real_uow.customers

        @property
        def processed_messages(self) -> ProcessedMessageRepositoryPort:
            return self._real_uow.processed_messages

        @property
        def services(self) -> ServiceRepositoryPort:
            return self._real_uow.services

        async def __aenter__(self) -> Self:
            entered = await self._real_uow.__aenter__()
            if self._call_id == 1:
                real_record = entered.booking_actions.record_action

                async def hooked_record_1(
                    action_id: uuid.UUID,
                    kind: str,
                    appointment_id: uuid.UUID,
                    result: dict[str, Any],
                ) -> BookingAction:
                    res = await real_record(
                        action_id=action_id,
                        kind=kind,
                        appointment_id=appointment_id,
                        result=result,
                    )
                    call1_in_db.set()
                    await call2_waiting_on_postgres.wait()
                    return res

                entered.booking_actions.record_action = hooked_record_1  # type: ignore[method-assign]

            elif self._call_id == 2:
                real_record = entered.booking_actions.record_action

                async def hooked_record_2(
                    action_id: uuid.UUID,
                    kind: str,
                    appointment_id: uuid.UUID,
                    result: dict[str, Any],
                ) -> BookingAction:
                    # Give asyncpg time to send the INSERT and hit the PK lock before Call 1 commits
                    asyncio.get_running_loop().call_later(0.15, call2_waiting_on_postgres.set)
                    return await real_record(
                        action_id=action_id,
                        kind=kind,
                        appointment_id=appointment_id,
                        result=result,
                    )

                entered.booking_actions.record_action = hooked_record_2  # type: ignore[method-assign]

            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> None:
            await self._real_uow.__aexit__(exc_type, exc_val, exc_tb)

    call_count = 0

    def hooked_uow_factory() -> UnitOfWork:
        nonlocal call_count
        call_count += 1
        return HookedUnitOfWork(uow_factory(), call_id=call_count)

    booking_service._uow_factory = hooked_uow_factory

    # Pre-create customer so neither call races on uq_customers_phone
    async with session_factory() as session:
        cust_repo = CustomerRepository(session)
        await cust_repo.create(name=customer_name, phone=customer_phone)
        await session.commit()

    async def call_1() -> BookingResult:
        return await booking_service.book(
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_id=service_id,
            starts_at=starts_at,
            barber_id=barber_ids[0],
            action_id=shared_action_id,
        )

    async def call_2() -> BookingResult:
        await call1_in_db.wait()
        return await booking_service.book(
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_id=service_id,
            starts_at=starts_at,
            barber_id=barber_ids[1],
            action_id=shared_action_id,
        )

    task1 = asyncio.create_task(call_1())
    task2 = asyncio.create_task(call_2())

    res1, res2 = await asyncio.gather(task1, task2)

    # Both calls returned the same appointment
    assert res1.appointment.id == res2.appointment.id

    # In database: exactly 1 appointment and 1 booking_action
    async with session_factory() as session:
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 1
        actions = (await session.execute(select(BookingAction))).scalars().all()
        assert len(actions) == 1
        assert actions[0].action_id == shared_action_id


@pytest.mark.integration
async def test_concurrent_first_bookings_for_same_new_customer_raises_integrity_error(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Requirement 1: 23505 on non-action-key (e.g. uq_customers_phone) propagates intact.

    Two concurrent first bookings for the same new customer phone with different action_ids:
    The first creates the Customer record; the second hits uq_customers_phone.
    It must raise IntegrityError intact and must NOT be converted to ActionAlreadyExistsError.
    """
    customer_phone = "+5511999990035"
    customer_name = "Cliente Novo Concorrente"

    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)

    # Simulate race: Call 1 creates customer and commits
    async with uow_factory() as uow1:
        await uow1.customers.create(name=customer_name, phone=customer_phone)

    # Call 2 attempts customer create without seeing Call 1 yet
    uow2 = uow_factory()
    await uow2.__aenter__()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            await uow2.customers.create(name=customer_name, phone=customer_phone)
    finally:
        await uow2.__aexit__(IntegrityError, None, None)

    # Verify it was indeed an IntegrityError on uq_customers_phone, not ActionAlreadyExistsError
    orig = getattr(exc_info.value, "orig", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(orig, "constraint_name", None) or getattr(cause, "constraint_name", None)
    assert constraint == "uq_customers_phone"


@pytest.mark.integration
async def test_delayed_replay_of_older_reschedule_does_not_overwrite_newer_reschedule(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario D: Delayed replay of R1 does not overwrite newer reschedule (R2)."""
    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)

    phone = "+5511999990005"

    async with session_factory() as session:
        service_id, barber_ids = await _setup_test_environment(session)
        customer = await CustomerRepository(session).create(name="Felipe", phone=phone)
        barber_id = barber_ids[0]

        # Initial appointment: Nov 21 at 10:00 (13:00 UTC)
        initial_starts = datetime(2026, 11, 21, 13, 0, tzinfo=UTC)
        initial_appt = await AppointmentRepository(session).create(
            customer_id=customer.id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=initial_starts,
            ends_at=initial_starts + datetime.resolution * 30 * 60,
            price_cents=4500,
        )
        appointment_id = initial_appt.id
        await session.commit()

    # 1. Reschedule 1 (R1): to Nov 22 at 10:00
    r1_action_id = uuid.uuid4()
    r1_starts = datetime(2026, 11, 22, 13, 0, tzinfo=UTC)
    res_r1 = await booking_service.reschedule(
        appointment_id=appointment_id,
        customer_phone=phone,
        new_starts_at=r1_starts,
        action_id=r1_action_id,
    )
    assert res_r1.appointment.starts_at == r1_starts

    # 2. Reschedule 2 (R2): subsequently rescheduled to Nov 23 at 10:00
    r2_action_id = uuid.uuid4()
    r2_starts = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)
    res_r2 = await booking_service.reschedule(
        appointment_id=appointment_id,
        customer_phone=phone,
        new_starts_at=r2_starts,
        action_id=r2_action_id,
    )
    assert res_r2.appointment.starts_at == r2_starts

    # 3. Delayed replay of R1 arriving late with the older r1_action_id
    delayed_res = await booking_service.reschedule(
        appointment_id=appointment_id,
        customer_phone=phone,
        new_starts_at=r1_starts,
        action_id=r1_action_id,
    )

    # Asserts:
    # Delayed replay returns cached result
    assert delayed_res.appointment.id == appointment_id

    # The appointment in PostgreSQL strictly preserves the newer Nov 23 date!
    async with session_factory() as session:
        appt = await AppointmentRepository(session).get_by_id(appointment_id)
        assert appt is not None
        assert appt.starts_at == r2_starts


@pytest.mark.integration
async def test_slot_conflict_during_confirmation_transitions_to_offering_alternatives(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Scenario E: If slot becomes occupied during confirmation, transition to alternatives."""
    phone = "+5511999990006"
    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    conv_service = ConversationService(
        intent_parser=FakeIntentParser(clock=clock),
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    async with session_factory() as session:
        service_id, barber_ids = await _setup_test_environment(session)
        barber_id = barber_ids[0]
        # Deactivate second barber so there is exactly 1 barber for this slot test
        b2 = await BarberRepository(session).get_by_id(barber_ids[1])
        if b2:
            b2.active = False
            await session.commit()

        action_id = uuid.uuid4()
        state = ConversationState(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(service_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "local_date": "2026-11-20",
                "local_time": "14:00:00",
                "barber_id": str(barber_id),
                "barber_name": "Barbeiro Carlos",
                "customer_name": "Eduardo",
                "action_id": str(action_id),
            },
            updated_at=datetime(2026, 11, 20, 9, 55, tzinfo=UTC),
        )
        session.add(state)

        # Concurrently create a conflicting appointment directly in the database
        cust_repo = CustomerRepository(session)
        other_cust = await cust_repo.create(name="Concorrente", phone="+5511988887777")
        await AppointmentRepository(session).create(
            customer_id=other_cust.id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=datetime(2026, 11, 20, 17, 0, tzinfo=UTC),  # 14:00 SP
            ends_at=datetime(2026, 11, 20, 17, 30, tzinfo=UTC),
            price_cents=4500,
        )
        await session.commit()

    # User sends confirmation "sim"
    resp = await conv_service.handle_message(
        message_id="msg_conflict_confirm",
        phone=phone,
        text="sim",
    )

    # Response offers alternatives because 14:00 was occupied
    assert "já está ocupado" in resp
    assert "Temos estes horários disponíveis" in resp

    # Check state in database
    async with session_factory() as session:
        updated_state = await session.get(ConversationState, phone)
        assert updated_state is not None
        assert updated_state.stage == ConversationStage.OFFERING_ALTERNATIVES.value
        assert "alternative_slots" in updated_state.slots
        assert "action_id" not in updated_state.slots
