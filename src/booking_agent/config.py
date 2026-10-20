"""Application settings loaded exclusively from environment variables."""

from functools import lru_cache
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # LLM adapter configuration
    llm_provider: Literal["openai", "fake"] = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=10.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    openai_api_key: SecretStr | None = None

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
