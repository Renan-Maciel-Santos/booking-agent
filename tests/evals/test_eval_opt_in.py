"""Opt-in evaluation tests for intent parsing benchmarks.

Excluded by default from standard test runs via pyproject.toml addopts (`-m 'not eval'`).
Run explicitly with: uv run pytest -m eval
"""

import pytest

from tests.evals.run_eval import evaluate_dataset


@pytest.mark.eval
async def test_eval_dataset_against_fake_provider() -> None:
    """Opt-in benchmark proving the evaluation dataset runs and produces telemetry."""
    summary = await evaluate_dataset(provider="fake")

    assert summary.total_cases >= 30
    assert summary.prompt_version != ""
    assert "intent" in summary.field_accuracies
    # Fake provider implements core heuristic rules; prove runner executed completely
    assert summary.total_latency_ms > 0
