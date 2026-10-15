"""Fixtures and hooks shared by the whole test suite."""

# Path is used to classify tests by folder.
from pathlib import Path

# pytest hooks and fixtures.
import pytest

# SecretStr wraps the fake password like production does.
from pydantic import SecretStr

# Settings under test.
from booking_agent.config import Settings

# Folder names mapped to the marker applied automatically.
_FOLDER_MARKERS: dict[str, str] = {"unit": "unit", "integration": "integration"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests by folder so `-m unit` / `-m integration` always work.

    Args:
        items: Collected test items.
    """
    # Inspect every collected test.
    for item in items:
        # Find which top-level test folder the file sits in.
        folder_name = Path(item.path).parent.name
        # Apply the matching marker, if the folder is a known suite.
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
    }
    # Let each test customise only what it needs.
    values.update(overrides)
    # _env_file=None isolates tests from any developer .env file.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def test_settings() -> Settings:
    """Default settings for tests."""
    # Delegate to the helper with no overrides.
    return build_test_settings()
