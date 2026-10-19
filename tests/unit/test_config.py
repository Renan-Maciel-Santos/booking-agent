"""Unit tests for environment-driven settings."""

from tests.conftest import build_test_settings


def test_database_url_is_built_from_parts() -> None:
    # Arrange settings with explicit connection parts.
    settings = build_test_settings(postgres_host="db", postgres_port=6543)

    # Assert the async driver URL is composed correctly.
    assert settings.database_url == ("postgresql+asyncpg://test_user:test_password@db:6543/test_db")


def test_password_is_masked_in_repr() -> None:
    # Arrange default test settings.
    settings = build_test_settings()

    # Assert the secret never leaks through repr or serialization (logs, tracebacks).
    assert "test_password" not in repr(settings)
    assert "test_password" not in str(settings.model_dump())


def test_business_settings_defaults() -> None:
    settings = build_test_settings()
    assert settings.business_timezone == "America/Sao_Paulo"
    assert settings.slot_granularity_minutes == 15
    assert settings.min_booking_advance_minutes == 60
    assert settings.max_booking_horizon_days == 30


def test_invalid_timezone_raises_validation_error() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Invalid IANA timezone"):
        build_test_settings(business_timezone="Invalid/Timezone_Name")
