"""Unit tests verifying application version synchronization with package metadata."""

import importlib.metadata
import tomllib
from pathlib import Path

from booking_agent.main import APP_VERSION, create_app
from tests.conftest import build_test_settings


def test_app_version_matches_package_metadata_and_pyproject() -> None:
    """The FastAPI app version matches importlib metadata and pyproject.toml."""
    pyproject_path = Path(__file__).parents[2] / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)

    expected_version = pyproject_data["project"]["version"]
    metadata_version = importlib.metadata.version("booking-agent")
    app = create_app(build_test_settings())

    assert metadata_version == APP_VERSION
    assert app.version == metadata_version
    assert app.version == expected_version
