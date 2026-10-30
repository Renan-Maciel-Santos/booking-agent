"""Unit tests for the --pace option in scripts/demo_conversation.py."""

import argparse

import pytest

from scripts.demo_conversation import build_parser, run_demo, validate_pace


def test_pace_default_is_zero() -> None:
    """The default value of --pace is 0.0."""
    parser = build_parser()
    args = parser.parse_args([])
    assert args.pace == 0.0


def test_negative_pace_is_rejected_by_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    """Negative --pace values are rejected by the argument parser with a clear message."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--pace", "-0.5"])

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "pace must be non-negative" in captured.err


def test_validate_pace_rejects_negative_value() -> None:
    """Direct validator rejects negative floats with ArgumentTypeError."""
    with pytest.raises(argparse.ArgumentTypeError, match="pace must be non-negative"):
        validate_pace("-1.0")


def test_validate_pace_rejects_non_numeric() -> None:
    """Direct validator rejects invalid float strings with ArgumentTypeError."""
    with pytest.raises(argparse.ArgumentTypeError, match="invalid float value"):
        validate_pace("invalid")


def test_pace_greater_than_zero_pauses_once_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pace > 0, time.sleep is invoked exactly once per turn with the pace duration."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", sleep_calls.append)

    # Mock send_message to return HTTP 200 without network I/O
    def fake_send_message(*args: object, **kwargs: object) -> tuple[int, str, float]:
        return 200, "Simulated bot response", 15.0

    monkeypatch.setattr("scripts.demo_conversation.send_message", fake_send_message)
    dummy_secret = "0" * 32

    exit_code = run_demo(
        base_url="http://testserver",
        secret=dummy_secret,
        phone="+5511999998888",
        delay=0.4,
        idempotency_check=False,
        pace=1.5,
    )

    assert exit_code == 0
    # The conversation demo has 7 turns; each turn must trigger one sleep(1.5)
    assert len(sleep_calls) == 7
    assert sleep_calls == [1.5] * 7


def test_pace_zero_preserves_default_delay_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pace == 0.0, the script falls back to delay between turns (turns 2-7)."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", sleep_calls.append)

    def fake_send_message(*args: object, **kwargs: object) -> tuple[int, str, float]:
        return 200, "Simulated bot response", 15.0

    monkeypatch.setattr("scripts.demo_conversation.send_message", fake_send_message)
    dummy_secret = "0" * 32

    exit_code = run_demo(
        base_url="http://testserver",
        secret=dummy_secret,
        phone="+5511999998888",
        delay=0.4,
        idempotency_check=False,
        pace=0.0,
    )

    assert exit_code == 0
    # Turns 2 to 7 trigger sleep(delay); turn 1 does not sleep
    assert len(sleep_calls) == 6
    assert sleep_calls == [0.4] * 6
