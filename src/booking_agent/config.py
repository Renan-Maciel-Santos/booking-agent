"""Application settings loaded exclusively from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
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
