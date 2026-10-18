"""Integration tests for exclusion constraint and double-booking protection."""

import asyncio
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from booking_agent.domain.errors import SlotUnavailableError
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository


def _random_phone() -> str:
    """Generate a valid numeric E.164 phone number."""
    return f"+1555{secrets.randbelow(9000000) + 1000000}"


async def test_concurrent_booking_blocks_and_fails_on_commit(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Verify session B blocks behind uncommitted session A and fails on commit.

    The second transaction raises SlotUnavailableError once session A commits.
    """
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)

    customer_a = await cust_repo.create(name="Client A", phone=_random_phone())
    customer_b = await cust_repo.create(name="Client B", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=3000,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 1, 14, 30, tzinfo=UTC)

    # a) Session A inserts and flushes without committing
    session_a = AsyncSession(engine)
    try:
        repo_a = AppointmentRepository(session_a)
        await repo_a.create(
            customer_id=customer_a.id,
            service_id=service.id,
            barber_id=barber.id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=service.price_cents,
        )

        # b) Session B attempts to book the same slot as an async task
        async def try_book_b() -> str:
            async with AsyncSession(engine) as session_b:
                repo_b = AppointmentRepository(session_b)
                await repo_b.create(
                    customer_id=customer_b.id,
                    service_id=service.id,
                    barber_id=barber.id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    price_cents=service.price_cents,
                )
                await session_b.commit()
                return "ok"

        task_b = asyncio.create_task(try_book_b())

        # Give task B time to hit PostgreSQL and block on the uncommitted lock
        await asyncio.sleep(0.1)

        # c) Verify B is still blocked: wait_for with shield must raise TimeoutError
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=0.5)

        assert not task_b.done()

        # d) Commit A
        await session_a.commit()

        # e) Await B: must raise SlotUnavailableError
        with pytest.raises(SlotUnavailableError):
            await asyncio.wait_for(task_b, timeout=2.0)

        # f) Verify in DB that exactly one confirmed appointment exists for this slot
        async with AsyncSession(engine) as verify_session:
            appts = await AppointmentRepository(verify_session).list_by_barber(barber.id)
            assert len(appts) == 1
            assert appts[0].customer_id == customer_a.id
            assert appts[0].status == "confirmed"
    finally:
        await session_a.close()


async def test_concurrent_booking_blocks_and_succeeds_on_rollback(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """Verify session B blocks behind uncommitted session A and succeeds when A rolls back."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)

    customer_a = await cust_repo.create(name="Client A", phone=_random_phone())
    customer_b = await cust_repo.create(name="Client B", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=3000,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 1, 15, 30, tzinfo=UTC)

    # a) Session A inserts and flushes without committing
    session_a = AsyncSession(engine)
    try:
        repo_a = AppointmentRepository(session_a)
        await repo_a.create(
            customer_id=customer_a.id,
            service_id=service.id,
            barber_id=barber.id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=service.price_cents,
        )

        # b) Session B attempts to book the same slot as an async task
        async def try_book_b() -> str:
            async with AsyncSession(engine) as session_b:
                repo_b = AppointmentRepository(session_b)
                await repo_b.create(
                    customer_id=customer_b.id,
                    service_id=service.id,
                    barber_id=barber.id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    price_cents=service.price_cents,
                )
                await session_b.commit()
                return "ok"

        task_b = asyncio.create_task(try_book_b())

        # Give task B time to hit PostgreSQL and block on the uncommitted lock
        await asyncio.sleep(0.1)

        # c) Verify B is still blocked: wait_for with shield must raise TimeoutError
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=0.5)

        assert not task_b.done()

        # d) Rollback A
        await session_a.rollback()

        # e) Await B: must succeed because slot is now free
        result_b = await asyncio.wait_for(task_b, timeout=2.0)
        assert result_b == "ok"

        # f) Verify in DB that exactly one confirmed appointment exists, belonging to customer B
        async with AsyncSession(engine) as verify_session:
            appts = await AppointmentRepository(verify_session).list_by_barber(barber.id)
            assert len(appts) == 1
            assert appts[0].customer_id == customer_b.id
            assert appts[0].status == "confirmed"
    finally:
        await session_a.close()


async def test_adjacent_slots_same_barber_are_permitted(session: AsyncSession) -> None:
    """Verify that half-open interval [) permits adjacent appointments."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer_1 = await cust_repo.create(name="Adjacent 1", phone=_random_phone())
    customer_2 = await cust_repo.create(name="Adjacent 2", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=2500,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    slot1_start = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    slot1_end = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
    slot2_start = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
    slot2_end = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)

    appt1 = await appt_repo.create(
        customer_id=customer_1.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=slot1_start,
        ends_at=slot1_end,
        price_cents=service.price_cents,
    )
    appt2 = await appt_repo.create(
        customer_id=customer_2.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=slot2_start,
        ends_at=slot2_end,
        price_cents=service.price_cents,
    )
    await session.commit()

    assert appt1.id != appt2.id
    assert appt1.ends_at == appt2.starts_at


async def test_same_slot_different_barbers_are_permitted(session: AsyncSession) -> None:
    """Verify that the exclusion constraint is scoped per barber."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer_1 = await cust_repo.create(name="DiffBarber 1", phone=_random_phone())
    customer_2 = await cust_repo.create(name="DiffBarber 2", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=3000,
    )
    barber_1 = await barber_repo.create(name=f"Barber 1 {uuid.uuid4().hex[:6]}")
    barber_2 = await barber_repo.create(name=f"Barber 2 {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)

    appt1 = await appt_repo.create(
        customer_id=customer_1.id,
        service_id=service.id,
        barber_id=barber_1.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    appt2 = await appt_repo.create(
        customer_id=customer_2.id,
        service_id=service.id,
        barber_id=barber_2.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    await session.commit()

    assert appt1.barber_id != appt2.barber_id
    assert appt1.starts_at == appt2.starts_at


async def test_cancelled_appointment_frees_slot(session: AsyncSession) -> None:
    """Verify that partial exclusion constraint releases the slot upon cancellation."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer_1 = await cust_repo.create(name="Cancel Cust 1", phone=_random_phone())
    customer_2 = await cust_repo.create(name="Cancel Cust 2", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=3000,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 4, 16, 30, tzinfo=UTC)

    appt1 = await appt_repo.create(
        customer_id=customer_1.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    await session.commit()

    cancelled = await appt_repo.cancel(appt1.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    await session.commit()

    appt2 = await appt_repo.create(
        customer_id=customer_2.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    await session.commit()

    assert appt2.id != appt1.id
    assert appt2.status == "confirmed"
