"""Application settings loaded exclusively from environment variables."""

# Cache the settings object so the environment is parsed only once per process.
from functools import lru_cache

# Pydantic primitives used to declare validated fields.
from pydantic import Field, SecretStr

# BaseSettings reads values from the environment and an optional .env file.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration.

    Every value comes from the environment so the same image runs unchanged in
    development, CI and production.
    """

    # Read an optional .env file, ignore unrelated variables and keep names case-insensitive.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Human-readable application name used in logs and OpenAPI metadata.
    app_name: str = "booking-agent"
    # Deployment environment label (development, ci, production...).
    app_env: str = "development"
    # Minimum log level emitted by the root logger.
    log_level: str = "INFO"

    # Database user, shared with the postgres container through the same variable.
    postgres_user: str
    # Database password kept as SecretStr so it is masked in repr() and logs.
    postgres_password: SecretStr
    # Database name.
    postgres_db: str
    # Database host name (service name inside docker-compose).
    postgres_host: str = "localhost"
    # Database TCP port, constrained to the valid port range.
    postgres_port: int = Field(default=5432, ge=1, le=65535)

    # Upper bound, in seconds, for the readiness database probe.
    db_health_timeout_seconds: float = Field(default=2.0, gt=0)
    # Number of persistent connections kept in the SQLAlchemy pool.
    db_pool_size: int = Field(default=5, ge=1)

    # Plain property (not a computed_field) so the URL, which embeds the password,
    # never appears in repr(), model_dump() or logs.
    @property
    def database_url(self) -> str:
        """Build the asyncpg URL from discrete variables to avoid duplicated config.

        Returns:
            SQLAlchemy URL using the asyncpg driver.
        """
        # Unwrap the secret only at the moment the URL is assembled.
        password = self.postgres_password.get_secret_value()
        # Compose the async driver URL from the individual parts.
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Returns:
        Cached Settings object built from the environment.
    """
    # Required fields are filled from the environment; missing ones raise ValidationError.
    return Settings()
