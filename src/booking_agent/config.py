"""Application settings loaded exclusively from environment variables."""

from functools import lru_cache
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_WEBHOOK_SECRET_LENGTH: int = 32


def validate_webhook_secret_value(raw: str) -> str:
    """Ensure webhook secret is normalized, non-placeholder, and meets minimum length.

    Args:
        raw: Raw secret string to validate.

    Returns:
        Stripped, validated secret string.

    Raises:
        ValueError: If empty, starts with 'change-me' (case-insensitive), or shorter than 32 chars.
    """
    secret_value = raw.strip()
    if not secret_value:
        raise ValueError("WEBHOOK_SECRET must not be empty")
    suggestion = (
        'Generate a secure secret with: python -c "import secrets; print(secrets.token_hex(32))"'
    )
    if secret_value.lower().startswith("change-me"):
        raise ValueError(
            "WEBHOOK_SECRET must not use an example placeholder starting with 'change-me'. "
            f"{suggestion}"
        )
    if len(secret_value) < MIN_WEBHOOK_SECRET_LENGTH:
        raise ValueError(
            f"WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters long. "
            f"{suggestion}"
        )
    return secret_value


class Settings(BaseSettings):
    """Typed runtime configuration.

    Every value comes from the environment so the same image runs unchanged in
    development, CI and production.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "booking-agent"
    app_env: str = "development"
    log_level: str = "INFO"

    postgres_user: str
    # SecretStr masks the value in repr(), tracebacks and logs.
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)

    db_health_timeout_seconds: float = Field(default=2.0, gt=0)
    db_pool_size: int = Field(default=5, ge=1)

    # Business operating rules and scheduling settings
    business_timezone: str = "America/Sao_Paulo"
    slot_granularity_minutes: int = Field(default=15, ge=1)
    min_booking_advance_minutes: int = Field(default=60, ge=0)
    max_booking_horizon_days: int = Field(default=30, ge=1)
    conversation_ttl_minutes: int = Field(default=30, ge=1)
    message_lease_timeout_seconds: int = Field(default=300, ge=1)

    # Personal data retention policy settings
    processed_message_redact_after_days: int = Field(default=7, ge=1)
    processed_message_delete_after_days: int = Field(default=90, ge=1)
    conversation_state_delete_after_days: int = Field(default=30, ge=1)
    retention_batch_size: int = Field(default=1000, ge=1)

    # LLM adapter configuration
    llm_provider: Literal["openai", "fake"] = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=10.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    openai_api_key: SecretStr | None = None

    # Webhook security, gateway bounds, and rate limit settings
    # SecretStr masks the secret in repr(), tracebacks and logs; required without default.
    webhook_secret: SecretStr
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=1)
    max_body_bytes: int = Field(default=65536, ge=1024)
    max_text_length: int = Field(default=1000, ge=1)
    rate_limit_requests_per_minute: int = Field(default=10, ge=1)
    request_timeout_seconds: float = Field(default=15.0, gt=0)

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: SecretStr) -> SecretStr:
        """Ensure webhook_secret is normalized, non-placeholder, and meets minimum length."""
        validated = validate_webhook_secret_value(v.get_secret_value())
        return SecretStr(validated)

    @field_validator("business_timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Ensure business_timezone is a valid IANA timezone."""
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"Invalid IANA timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def validate_llm_settings(self) -> Self:
        """Validate LLM settings based on the configured provider.

        Fails immediately at startup if openai provider is selected without an API key.
        """
        if self.llm_provider == "openai" and (
            self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip()
        ):
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER is 'openai'")
        return self

    @model_validator(mode="after")
    def validate_retention_settings(self) -> Self:
        """Validate personal data retention policy settings.

        Ensures that processed message deletion window is strictly greater than
        its redaction window, and that conversation state deletion window
        (converted to minutes) is strictly greater than conversation TTL.
        """
        if self.processed_message_delete_after_days <= self.processed_message_redact_after_days:
            raise ValueError(
                "processed_message_delete_after_days must be greater than "
                "processed_message_redact_after_days"
            )
        if (self.conversation_state_delete_after_days * 24 * 60) <= self.conversation_ttl_minutes:
            raise ValueError(
                "conversation_state_delete_after_days (converted to minutes) "
                "must be greater than conversation_ttl_minutes"
            )
        return self

    # A plain property (not a computed_field) keeps the URL, which embeds the
    # password, out of repr() and model_dump().
    @property
    def database_url(self) -> str:
        """Build the asyncpg URL from discrete variables to avoid duplicated config.

        Returns:
            SQLAlchemy URL using the asyncpg driver.
        """
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance, parsed once.

    Returns:
        Cached Settings object built from the environment.
    """
    return Settings()
