"""Integration test for the conversation slot cleanup migration."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ConversationState
from booking_agent.domain.enums import ConversationStage

REVISION_ID = "48450e9dcbdd"


def _load_migration_sql() -> str:
    """Load REMOVE_DEAD_SLOT_KEYS_SQL directly from the migration file."""
    migration_files = list(Path("migrations/versions").glob(f"*{REVISION_ID}*.py"))
    assert len(migration_files) == 1, (
        f"Expected exactly one migration file for revision {REVISION_ID}, found {migration_files}"
    )
    spec = importlib.util.spec_from_file_location("remove_dead_slots_migration", migration_files[0])
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql: str = module.REMOVE_DEAD_SLOT_KEYS_SQL
    return sql


@pytest.mark.integration
async def test_remove_dead_slot_keys_sql_removes_key_without_advancing_updated_at(
    session: AsyncSession,
) -> None:
    """Verify dead slot key is removed without altering other keys or updated_at."""
    # Arrange
    cleanup_sql = _load_migration_sql()

    fixed_dt_1 = datetime(2026, 1, 10, 14, 0, 0, tzinfo=UTC)
    fixed_dt_2 = datetime(2026, 1, 12, 16, 30, 0, tzinfo=UTC)

    phone_with_key = "+5511987650001"
    initial_slots_with_key = {
        "flow": "book",
        "service_id": "11111111-1111-1111-1111-111111111111",
        "service_name": "Corte de Cabelo",
        "service_price_cents": 4500,
        "service_duration_minutes": 30,
        "customer_name": "Renan Maciel",
        "local_date": "2026-11-20",
        "local_time": "14:00:00",
    }

    phone_without_key = "+5511987650002"
    initial_slots_without_key = {
        "flow": "book",
        "service_id": "22222222-2222-2222-2222-222222222222",
        "service_name": "Barba Completa",
        "service_price_cents": 3500,
        "customer_name": "Lucas Silva",
        "local_date": "2026-11-21",
        "local_time": "15:00:00",
    }

    state_with_key = ConversationState(
        phone=phone_with_key,
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=initial_slots_with_key,
        updated_at=fixed_dt_1,
    )
    state_without_key = ConversationState(
        phone=phone_without_key,
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=initial_slots_without_key,
        updated_at=fixed_dt_2,
    )

    session.add_all([state_with_key, state_without_key])
    await session.commit()

    # Act: execute migration SQL
    await session.execute(text(cleanup_sql))
    await session.commit()

    # Assert: query refreshed state directly from database
    session.expire_all()

    stmt_1 = select(ConversationState).where(ConversationState.phone == phone_with_key)
    res_1 = await session.execute(stmt_1)
    reloaded_1 = res_1.scalar_one()

    stmt_2 = select(ConversationState).where(ConversationState.phone == phone_without_key)
    res_2 = await session.execute(stmt_2)
    reloaded_2 = res_2.scalar_one()

    # Conversation 1: service_duration_minutes removed, other keys intact
    assert "service_duration_minutes" not in reloaded_1.slots
    expected_slots_1 = dict(initial_slots_with_key)
    del expected_slots_1["service_duration_minutes"]
    assert reloaded_1.slots == expected_slots_1

    # Cleanup must not advance updated_at (preserving conversation TTL expiry)
    assert reloaded_1.updated_at == fixed_dt_1

    # Conversation 2: slots and updated_at completely unchanged
    assert reloaded_2.slots == initial_slots_without_key
    assert reloaded_2.updated_at == fixed_dt_2
