"""Unit tests for environment-driven settings."""

import pytest

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


def test_llm_settings_defaults() -> None:
    settings = build_test_settings()
    assert settings.llm_provider == "fake"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_timeout_seconds == 10.0
    assert settings.llm_max_retries == 2
    assert settings.llm_temperature == 0.0
    assert settings.openai_api_key is None


def test_openai_mode_without_key_raises_validation_error() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError, match="OPENAI_API_KEY is required when LLM_PROVIDER is 'openai'"
    ):
        build_test_settings(llm_provider="openai", openai_api_key=None)


def test_openai_mode_with_empty_key_raises_validation_error() -> None:
    import pytest
    from pydantic import SecretStr, ValidationError

    with pytest.raises(
        ValidationError, match="OPENAI_API_KEY is required when LLM_PROVIDER is 'openai'"
    ):
        build_test_settings(llm_provider="openai", openai_api_key=SecretStr("   "))


def test_openai_mode_with_valid_key_succeeds() -> None:
    from pydantic import SecretStr

    settings = build_test_settings(
        llm_provider="openai",
        openai_api_key=SecretStr("sk-valid-key"),
    )
    assert settings.llm_provider == "openai"
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-valid-key"


def test_openai_key_is_masked_in_repr() -> None:
    from pydantic import SecretStr

    settings = build_test_settings(
        llm_provider="openai",
        openai_api_key=SecretStr("sk-super-secret"),
    )
    assert "sk-super-secret" not in repr(settings)
    assert "sk-super-secret" not in str(settings.model_dump())


def test_invalid_llm_provider_raises_validation_error() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        build_test_settings(llm_provider="unsupported_provider")


def test_webhook_settings_defaults() -> None:
    settings = build_test_settings()
    assert settings.webhook_timestamp_tolerance_seconds == 300
    assert settings.max_body_bytes == 65536
    assert settings.max_text_length == 1000
    assert settings.rate_limit_requests_per_minute == 10
    assert settings.request_timeout_seconds == 15.0


def test_webhook_secret_is_masked_in_repr() -> None:
    settings = build_test_settings()
    raw_secret = settings.webhook_secret.get_secret_value()
    assert raw_secret not in repr(settings)
    assert raw_secret not in str(settings.model_dump())


def test_empty_webhook_secret_raises_validation_error() -> None:
    import pytest
    from pydantic import SecretStr, ValidationError

    with pytest.raises(ValidationError, match="WEBHOOK_SECRET must not be empty"):
        build_test_settings(webhook_secret=SecretStr("   "))


def test_invalid_bounds_raise_validation_error() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        build_test_settings(request_timeout_seconds=0.0)

    with pytest.raises(ValidationError):
        build_test_settings(max_body_bytes=500)


def test_placeholder_webhook_secret_raises_validation_error() -> None:
    from pydantic import SecretStr, ValidationError

    with pytest.raises(
        ValidationError,
        match=r"WEBHOOK_SECRET must not use an example placeholder starting with 'change-me'",
    ):
        build_test_settings(webhook_secret=SecretStr("change-me-to-a-secure-random-secret"))


def test_create_app_refuses_to_start_with_placeholder_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from booking_agent.config import get_settings
    from booking_agent.main import create_app

    get_settings.cache_clear()
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    monkeypatch.setenv("WEBHOOK_SECRET", "change-me-to-a-secure-random-secret")

    with pytest.raises(
        ValidationError,
        match=r"WEBHOOK_SECRET must not use an example placeholder starting with 'change-me'",
    ):
        create_app()
    get_settings.cache_clear()


def test_webhook_secret_whitespace_normalized_and_used_in_hmac() -> None:
    from pydantic import SecretStr

    from booking_agent.api.security import compute_signature

    clean_secret = "a" * 32
    padded_secret = f"  \n\t{clean_secret}\n  "
    settings = build_test_settings(webhook_secret=SecretStr(padded_secret))
    assert settings.webhook_secret.get_secret_value() == clean_secret

    timestamp = 1729500000
    raw_body = b'{"hello": "world"}'
    sig_from_settings = compute_signature(
        settings.webhook_secret.get_secret_value(), timestamp, raw_body
    )
    sig_from_clean = compute_signature(clean_secret, timestamp, raw_body)
    sig_from_padded = compute_signature(padded_secret, timestamp, raw_body)

    assert sig_from_settings == sig_from_clean
    assert sig_from_settings != sig_from_padded


def test_webhook_secret_change_me_prefix_rejected() -> None:
    from pydantic import SecretStr, ValidationError

    with pytest.raises(
        ValidationError,
        match=r"WEBHOOK_SECRET must not use an example placeholder starting with 'change-me'",
    ) as exc_info:
        build_test_settings(webhook_secret=SecretStr("change-me-123"))

    assert "change-me-123" not in str(exc_info.value)


def test_webhook_secret_length_validation() -> None:
    from pydantic import SecretStr, ValidationError

    # 31 characters: rejected
    secret_31 = "x" * 31
    with pytest.raises(
        ValidationError,
        match=r"WEBHOOK_SECRET must be at least 32 characters long",
    ) as exc_info:
        build_test_settings(webhook_secret=SecretStr(secret_31))

    assert secret_31 not in str(exc_info.value)
    assert 'python -c "import secrets; print(secrets.token_hex(32))"' in str(exc_info.value)

    # 32 characters: accepted
    secret_32 = "x" * 32
    settings = build_test_settings(webhook_secret=SecretStr(secret_32))
    assert settings.webhook_secret.get_secret_value() == secret_32
