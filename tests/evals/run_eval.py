"""Evaluation runner for measuring intent and entity extraction accuracy."""

import argparse
import asyncio
import json
import sys
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from booking_agent.config import get_settings
from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.ports import IntentParser
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.integrations.llm.openai_intent_parser import (
    PROMPT_VERSION,
    OpenAIIntentParser,
)
from tests.fakes.fake_clock import FakeClock

DEFAULT_DATASET_PATH = Path(__file__).parent / "intents.jsonl"
DEFAULT_TZ = ZoneInfo("America/Sao_Paulo")


def normalize_text(val: object) -> str | None:
    """Normalize text by lowercasing, stripping whitespace, and removing diacritics."""
    if val is None:
        return None
    text = str(val).strip().lower()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def check_field_match(actual: object, expected: object) -> bool:
    """Check if actual extracted value matches expected ground truth."""
    if expected is None:
        return actual is None
    if isinstance(expected, list):
        if actual is None:
            return False
        norm_actual = normalize_text(actual)
        return any(norm_actual == normalize_text(item) for item in expected)
    if isinstance(expected, str):
        if actual is None:
            return False
        return normalize_text(actual) == normalize_text(expected)
    return bool(actual == expected)


@dataclass
class EvalCaseResult:
    """Detailed result for a single evaluation case."""

    case_id: str
    description: str
    user_message: str
    exact_match: bool
    field_matches: dict[str, bool]
    latency_ms: float
    actual: BookingIntent
    expected: dict[str, Any]


@dataclass
class EvalSummary:
    """Aggregated evaluation telemetry and accuracy statistics."""

    model_name: str
    prompt_version: str
    run_timestamp: str
    total_cases: int
    exact_matches: int
    exact_match_accuracy_pct: float
    field_accuracies: dict[str, float]
    total_latency_ms: float
    avg_latency_ms: float
    total_tokens: int
    results: list[EvalCaseResult] = field(default_factory=list)


def build_parser_for_case(
    provider: str,
    ref_dt: datetime,
    tz: ZoneInfo,
) -> IntentParser:
    """Instantiate a parser pinned to the reference datetime of the test case."""
    clock = FakeClock(ref_dt)
    if provider == "fake":
        return FakeIntentParser(clock=clock, business_timezone=tz)

    settings = get_settings()
    if settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is required to evaluate against the OpenAI provider")

    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    return OpenAIIntentParser(
        client=client,
        model=settings.llm_model,
        clock=clock,
        business_timezone=tz,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
    )


async def evaluate_dataset(
    dataset_path: Path = DEFAULT_DATASET_PATH,
    provider: str = "fake",
    business_timezone: ZoneInfo = DEFAULT_TZ,
) -> EvalSummary:
    """Execute evaluation across all dataset cases."""
    with open(dataset_path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    evaluated_cases: list[EvalCaseResult] = []
    field_match_counts: dict[str, int] = {
        "intent": 0,
        "service_name": 0,
        "local_date": 0,
        "local_time": 0,
        "time_preference": 0,
        "barber_name": 0,
        "customer_name": 0,
    }

    start_total_time = time.perf_counter()

    for line in lines:
        record = json.loads(line)
        case_id: str = record["id"]
        description: str = record["description"]
        user_message: str = record["user_message"]
        expected: dict[str, Any] = record["expected"]

        # Parse reference datetime from dataset record
        ref_date = record.get("reference_date", "2026-10-20")
        ref_time = record.get("reference_time", "14:30:00")
        local_ref = datetime.fromisoformat(f"{ref_date}T{ref_time}").replace(
            tzinfo=business_timezone
        )
        utc_ref = local_ref.astimezone(UTC)

        parser = build_parser_for_case(provider, utc_ref, business_timezone)

        t0 = time.perf_counter()
        actual = await parser.parse(user_message)
        latency_ms = (time.perf_counter() - t0) * 1000

        # Evaluate individual fields
        matches: dict[str, bool] = {
            "intent": check_field_match(actual.intent, expected.get("intent")),
            "service_name": check_field_match(actual.service_name, expected.get("service_name")),
            "local_date": check_field_match(
                actual.local_date.isoformat() if actual.local_date else None,
                expected.get("local_date"),
            ),
            "local_time": check_field_match(
                actual.local_time.isoformat() if actual.local_time else None,
                expected.get("local_time"),
            ),
            "time_preference": check_field_match(
                actual.time_preference, expected.get("time_preference")
            ),
            "barber_name": check_field_match(actual.barber_name, expected.get("barber_name")),
            "customer_name": check_field_match(actual.customer_name, expected.get("customer_name")),
        }

        for k, v in matches.items():
            if v:
                field_match_counts[k] += 1

        is_exact = all(matches.values())

        evaluated_cases.append(
            EvalCaseResult(
                case_id=case_id,
                description=description,
                user_message=user_message,
                exact_match=is_exact,
                field_matches=matches,
                latency_ms=latency_ms,
                actual=actual,
                expected=expected,
            )
        )

    total_latency_ms = (time.perf_counter() - start_total_time) * 1000
    n = len(evaluated_cases)
    exact_count = sum(1 for c in evaluated_cases if c.exact_match)

    field_accuracies = {
        field_name: (count / n) * 100 if n > 0 else 0.0
        for field_name, count in field_match_counts.items()
    }

    model_name = "fake-rule-based" if provider == "fake" else get_settings().llm_model

    return EvalSummary(
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        run_timestamp=datetime.now(UTC).isoformat(),
        total_cases=n,
        exact_matches=exact_count,
        exact_match_accuracy_pct=(exact_count / n) * 100 if n > 0 else 0.0,
        field_accuracies=field_accuracies,
        total_latency_ms=total_latency_ms,
        avg_latency_ms=total_latency_ms / n if n > 0 else 0.0,
        total_tokens=0,
        results=evaluated_cases,
    )


def print_eval_report(summary: EvalSummary) -> None:
    """Print readable terminal report of evaluation results."""
    out = sys.stdout.write
    out("=" * 70 + "\n")
    out("BOOKING AGENT -- LLM ADAPTER EVALUATION REPORT\n")
    out("=" * 70 + "\n")
    out(f"Model:          {summary.model_name}\n")
    out(f"Prompt Version: {summary.prompt_version}\n")
    out(f"Timestamp:      {summary.run_timestamp}\n")
    out(f"Total Cases:    {summary.total_cases}\n")
    out(
        f"Exact Match:    {summary.exact_matches}/{summary.total_cases} "
        f"({summary.exact_match_accuracy_pct:.1f}%)\n"
    )
    out(
        f"Total Latency:  {summary.total_latency_ms:.1f}ms "
        f"(Avg {summary.avg_latency_ms:.1f}ms/call)\n"
    )
    out("-" * 70 + "\n")
    out("Field-by-Field Accuracy:\n")
    for field_name, acc in summary.field_accuracies.items():
        out(f"  - {field_name:<16}: {acc:>5.1f}%\n")
    out("-" * 70 + "\n")

    mismatches = [c for c in summary.results if not c.exact_match]
    if mismatches:
        out(f"Mismatches ({len(mismatches)} cases):\n")
        for m in mismatches[:10]:
            failed_fields = [k for k, v in m.field_matches.items() if not v]
            out(f"  [{m.case_id}] {m.description}\n")
            out(f'    Input:    "{m.user_message}"\n')
            out(f"    Failed:   {', '.join(failed_fields)}\n")
            for f_name in failed_fields:
                act_val = getattr(m.actual, f_name, None)
                exp_val = m.expected.get(f_name)
                out(f"      {f_name}: actual={act_val!r} vs expected={exp_val!r}\n")
        if len(mismatches) > 10:
            out(f"    ... and {len(mismatches) - 10} more.\n")
    else:
        out("All cases matched ground truth perfectly!\n")
    out("=" * 70 + "\n")


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for running evaluation dataset."""
    parser = argparse.ArgumentParser(description="Run booking intent evaluation dataset.")
    parser.add_argument(
        "--provider",
        choices=["fake", "openai"],
        default="fake",
        help="Provider to evaluate against (default: fake).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Path to JSONL dataset file.",
    )
    args = parser.parse_args(argv)

    summary = asyncio.run(evaluate_dataset(dataset_path=args.dataset, provider=args.provider))
    print_eval_report(summary)


if __name__ == "__main__":
    main()
