"""Fixtures and hooks shared by the whole test suite."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from booking_agent.config import Settings

_FOLDER_MARKERS: dict[str, str] = {"unit": "unit", "integration": "integration"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests by folder so `-m unit` / `-m integration` always work.

    Args:
        items: Collected test items.
    """
    for item in items:
        folder_name = Path(item.path).parent.name
        if folder_name in _FOLDER_MARKERS:
            item.add_marker(_FOLDER_MARKERS[folder_name])


def build_test_settings(**overrides: object) -> Settings:
    """Create Settings without touching the real environment or a .env file.

    Args:
        **overrides: Field values replacing the defaults below.

    Returns:
        Settings suitable for tests.
    """
    # Harmless placeholder values; nothing here is a real credential.
    values: dict[str, object] = {
        "postgres_user": "test_user",
        "postgres_password": SecretStr("test_password"),
        "postgres_db": "test_db",
        "postgres_host": "localhost",
        "postgres_port": 5432,
        "log_level": "WARNING",
        "llm_provider": "fake",
        "webhook_secret": SecretStr("test_webhook_secret_key_12345"),
    }
    values.update(overrides)
    # _env_file=None isolates tests from any developer .env file.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def test_settings() -> Settings:
    """Default settings for tests."""
    return build_test_settings()
