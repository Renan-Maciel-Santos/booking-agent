"""Unit tests for RetentionService with in-memory fakes and a fixed clock."""

from datetime import UTC, datetime, timedelta

from booking_agent.database.models import ProcessedMessage
from booking_agent.services.retention_service import RetentionReport, RetentionService
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_uow import FakeUnitOfWorkFactory, InMemoryDataStore


async def test_retention_service_calculates_cutoffs_from_clock() -> None:
    # Arrange fixed clock and custom retention settings
    fixed_now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=7,
        processed_message_delete_after_days=90,
        conversation_state_delete_after_days=30,
        retention_batch_size=500,
    )
    store = InMemoryDataStore()
    uow_factory = FakeUnitOfWorkFactory(store=store, clock=clock)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    # Act
    report: RetentionReport = await service.run()

    # Assert expected cutoffs match exact policy calculation
    expected_redact_cutoff = fixed_now - timedelta(days=7)
    expected_delete_messages_cutoff = fixed_now - timedelta(days=90)
    expected_delete_conversations_cutoff = fixed_now - timedelta(days=30)

    assert report.redact_cutoff == expected_redact_cutoff
    assert report.delete_messages_cutoff == expected_delete_messages_cutoff
    assert report.delete_conversations_cutoff == expected_delete_conversations_cutoff
    assert report.redacted_responses == 0
    assert report.deleted_messages == 0
    assert report.deleted_conversation_states == 0


async def test_retention_service_executes_against_fake_repositories() -> None:
    fixed_now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(fixed_now)
    settings = build_test_settings(
        processed_message_redact_after_days=5,
        processed_message_delete_after_days=10,
        conversation_state_delete_after_days=8,
        retention_batch_size=10,
    )
    store = InMemoryDataStore()

    # Seed 1 message eligible for redaction (created 6 days ago)
    store.processed_messages["msg_redact"] = ProcessedMessage(
        message_id="msg_redact",
        status="done",
        response_text="Sensitive text",
        created_at=fixed_now - timedelta(days=6),
        updated_at=fixed_now - timedelta(days=6),
    )
    # Seed 1 message eligible for deletion (response already redacted previously)
    store.processed_messages["msg_delete"] = ProcessedMessage(
        message_id="msg_delete",
        status="done",
        response_text=None,
        created_at=fixed_now - timedelta(days=12),
        updated_at=fixed_now - timedelta(days=12),
    )
    # Seed 1 inactive conversation state eligible for deletion (updated 9 days ago)
    store.seed_conversation_state(
        phone="+5511999999999",
        stage="idle",
        slots={},
        updated_at=fixed_now - timedelta(days=9),
    )
    # Seed 1 executing conversation state (updated 9 days ago, but executing stage must be kept)
    store.seed_conversation_state(
        phone="+5511888888888",
        stage="executing",
        slots={},
        updated_at=fixed_now - timedelta(days=9),
    )

    uow_factory = FakeUnitOfWorkFactory(store=store, clock=clock)
    service = RetentionService(uow_factory=uow_factory, clock=clock, settings=settings)

    report = await service.run()

    # msg_redact has response_text redacted; msg_delete is deleted
    assert report.redacted_responses == 1
    assert report.deleted_messages == 1
    assert report.deleted_conversation_states == 1

    # Verify state in store
    assert store.processed_messages["msg_redact"].response_text is None
    assert "msg_delete" not in store.processed_messages
    assert "+5511999999999" not in store.conversation_states
    assert "+5511888888888" in store.conversation_states
