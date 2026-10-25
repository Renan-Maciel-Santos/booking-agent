"""Integration test for the conversation slot sentinels migration."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ConversationState
from booking_agent.domain.enums import ConversationStage

REVISION_ID = "f65e45483e00"


def _load_migration_module() -> ModuleType:
    """Load migration module directly from the migrations directory."""
    versions_dir = Path(__file__).parents[2] / "migrations" / "versions"
    migration_files = list(versions_dir.glob(f"*{REVISION_ID}*.py"))
    assert len(migration_files) == 1, (
        f"Expected exactly one migration file for revision {REVISION_ID}, found {migration_files}"
    )
    spec = importlib.util.spec_from_file_location("sentinels_migration", migration_files[0])
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
async def test_sentinels_migration_upgrades_and_downgrades_correctly(
    session: AsyncSession,
) -> None:
    """Verify sentinels migration transforms null to 'any' and removes empty executing_message_id.

    Also verifies downgrade and updated_at preservation.
    """
    # Arrange
    migration_mod = _load_migration_module()
    any_barber_sql: str = migration_mod.ANY_BARBER_SQL
    empty_executing_message_sql: str = migration_mod.EMPTY_EXECUTING_MESSAGE_SQL
    downgrade_barber_sql: str = migration_mod.DOWNGRADE_BARBER_SQL

    fixed_dt_null = datetime(2026, 1, 10, 10, 0, 0, tzinfo=UTC)
    fixed_dt_specific = datetime(2026, 1, 10, 11, 0, 0, tzinfo=UTC)
    fixed_dt_absent = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    phone_null = "+5511988880001"
    initial_slots_null = {
        "flow": "book",
        "barber_id": None,
        "executing_message_id": "",
        "customer_name": "Test Null Sentinel",
    }

    phone_specific = "+5511988880002"
    specific_barber_uuid = "11111111-1111-1111-1111-111111111111"
    real_message_id = "msg-live-12345"
    initial_slots_specific = {
        "flow": "book",
        "barber_id": specific_barber_uuid,
        "executing_message_id": real_message_id,
        "customer_name": "Test Specific Barber",
    }

    phone_absent = "+5511988880003"
    initial_slots_absent = {
        "flow": "book",
        "customer_name": "Test Absent Barber",
    }

    state_null = ConversationState(
        phone=phone_null,
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=initial_slots_null,
        updated_at=fixed_dt_null,
    )
    state_specific = ConversationState(
        phone=phone_specific,
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=initial_slots_specific,
        updated_at=fixed_dt_specific,
    )
    state_absent = ConversationState(
        phone=phone_absent,
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=initial_slots_absent,
        updated_at=fixed_dt_absent,
    )

    session.add_all([state_null, state_specific, state_absent])
    await session.commit()

    # Act 1: Execute upgrade SQL statements
    await session.execute(text(any_barber_sql))
    await session.execute(text(empty_executing_message_sql))
    await session.commit()

    # Assert 1: Query refreshed state directly from database
    session.expire_all()

    stmt_null = select(ConversationState).where(ConversationState.phone == phone_null)
    res_null = await session.execute(stmt_null)
    reloaded_null = res_null.scalar_one()

    stmt_specific = select(ConversationState).where(ConversationState.phone == phone_specific)
    res_specific = await session.execute(stmt_specific)
    reloaded_specific = res_specific.scalar_one()

    stmt_absent = select(ConversationState).where(ConversationState.phone == phone_absent)
    res_absent = await session.execute(stmt_absent)
    reloaded_absent = res_absent.scalar_one()

    # Case 1 & 4: null vira "any", "" em executing_message_id é removido
    assert reloaded_null.slots["barber_id"] == "any"
    assert "executing_message_id" not in reloaded_null.slots
    assert reloaded_null.slots["customer_name"] == "Test Null Sentinel"
    assert reloaded_null.slots["flow"] == "book"

    # Case 2 & 5: UUID específico não muda, id de mensagem real não muda
    assert reloaded_specific.slots["barber_id"] == specific_barber_uuid
    assert reloaded_specific.slots["executing_message_id"] == real_message_id
    assert reloaded_specific.slots["customer_name"] == "Test Specific Barber"

    # Case 3: chave barber_id ausente continua ausente
    assert "barber_id" not in reloaded_absent.slots
    assert "executing_message_id" not in reloaded_absent.slots
    assert reloaded_absent.slots == initial_slots_absent

    # Case 6: updated_at de todas as linhas NÃO muda
    assert reloaded_null.updated_at == fixed_dt_null
    assert reloaded_specific.updated_at == fixed_dt_specific
    assert reloaded_absent.updated_at == fixed_dt_absent

    # Act 2: Execute downgrade SQL statement
    await session.execute(text(downgrade_barber_sql))
    await session.commit()

    # Assert 2: Verify downgrade reverts "any" to null and leaves absence intact
    session.expire_all()

    res_null_down = await session.execute(stmt_null)
    reloaded_null_down = res_null_down.scalar_one()

    res_specific_down = await session.execute(stmt_specific)
    reloaded_specific_down = res_specific_down.scalar_one()

    res_absent_down = await session.execute(stmt_absent)
    reloaded_absent_down = res_absent_down.scalar_one()

    # Downgrade restored null for "any"
    assert reloaded_null_down.slots["barber_id"] is None
    # Empty executing_message_id was intentionally not restored
    assert "executing_message_id" not in reloaded_null_down.slots

    # Specific UUID and real message ID remain untouched
    assert reloaded_specific_down.slots["barber_id"] == specific_barber_uuid
    assert reloaded_specific_down.slots["executing_message_id"] == real_message_id

    # Absent keys remain absent
    assert "barber_id" not in reloaded_absent_down.slots

    # updated_at still untouched
    assert reloaded_null_down.updated_at == fixed_dt_null
    assert reloaded_specific_down.updated_at == fixed_dt_specific
    assert reloaded_absent_down.updated_at == fixed_dt_absent
