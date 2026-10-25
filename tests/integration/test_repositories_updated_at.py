"""Integration tests verifying that updated_at advances on every repository update."""

import asyncio
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationStage
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.conversation_state_repository import ConversationStateRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.processed_message_repository import ProcessedMessageRepository
from booking_agent.repositories.service_repository import ServiceRepository


def _random_phone() -> str:
    """Generate a valid numeric E.164 phone number."""
    return f"+1555{secrets.randbelow(9000000) + 1000000}"


async def test_customer_updated_at_advances_on_update(session: AsyncSession) -> None:
    repo = CustomerRepository(session)
    customer = await repo.create(name="Initial Name", phone=_random_phone())
    await session.commit()
    t0 = customer.updated_at

    await asyncio.sleep(0.05)
    updated = await repo.update_name(customer.id, name="New Name")
    await session.commit()

    assert updated is not None
    assert updated.updated_at > t0


async def test_service_updated_at_advances_on_update(session: AsyncSession) -> None:
    repo = ServiceRepository(session)
    service = await repo.create(
        name=f"Service {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=2000,
    )
    await session.commit()
    t0 = service.updated_at

    await asyncio.sleep(0.05)
    updated = await repo.update(service.id, price_cents=2500)
    await session.commit()

    assert updated is not None
    assert updated.updated_at > t0


async def test_barber_updated_at_advances_on_update(session: AsyncSession) -> None:
    barber_repo = BarberRepository(session)
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()
    t0 = barber.updated_at

    await asyncio.sleep(0.05)
    updated = await barber_repo.update(barber.id, name="Renamed Barber")
    await session.commit()

    assert updated is not None
    assert updated.updated_at > t0


async def test_appointment_updated_at_advances_on_cancel(session: AsyncSession) -> None:
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer = await cust_repo.create(name="Appt Cust", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=3000,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 6, 10, 30, tzinfo=UTC)
    appt = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    await session.commit()
    t0 = appt.updated_at

    await asyncio.sleep(0.05)
    cancelled = await appt_repo.cancel(appt.id)
    await session.commit()

    assert cancelled is not None
    assert cancelled.updated_at > t0


async def test_conversation_state_updated_at_advances_on_upsert(session: AsyncSession) -> None:
    repo = ConversationStateRepository(session)
    phone = _random_phone()

    initial = await repo.upsert(
        phone=phone,
        stage=ConversationStage.IDLE,
        slots=ConversationSlots(),
    )
    await session.commit()
    t0 = initial.updated_at

    await asyncio.sleep(0.05)
    updated = await repo.upsert(
        phone=phone,
        stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
        slots=ConversationSlots(service_name="haircut"),
    )
    await session.commit()

    assert updated.updated_at > t0


async def test_processed_message_updated_at_advances_on_mark_done(session: AsyncSession) -> None:
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"

    await repo.acquire(msg_id, timeout_seconds=60)
    await session.commit()
    record0 = await repo.get(msg_id)
    assert record0 is not None
    t0 = record0.updated_at

    await asyncio.sleep(0.05)
    await repo.mark_done(msg_id)
    await session.commit()
    session.expire_all()

    record1 = await repo.get(msg_id)
    assert record1 is not None
    assert record1.updated_at > t0
