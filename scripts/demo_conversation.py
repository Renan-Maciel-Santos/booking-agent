"""Standalone demonstration script simulating a 7-turn customer conversation.

Exercises POST /v1/messages with valid HMAC-SHA256 signatures, simulating
booking, name elicitation, confirmation, rescheduling, and cancellation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from booking_agent.api.security import compute_signature
from booking_agent.config import validate_webhook_secret_value

RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
DIM = "\033[2m"

BANNER_DIVIDER = "=" * 68


def print_banner() -> None:
    """Print the CLI banner."""
    sys.stdout.write(f"\n{BOLD}{CYAN}{BANNER_DIVIDER}{RESET}\n")
    sys.stdout.write(
        f"{BOLD}{CYAN}    Northside Barbers - Conversational Booking Demonstration    {RESET}\n"
    )
    sys.stdout.write(f"{BOLD}{CYAN}{BANNER_DIVIDER}{RESET}\n\n")
    sys.stdout.flush()


def print_card(
    turn: int,
    total: int,
    user_text: str,
    bot_text: str,
    status_code: int,
    latency_ms: float,
) -> None:
    """Render a formatted, colorful terminal card for a conversation turn.

    Args:
        turn: Current turn index (1-based).
        total: Total number of turns.
        user_text: Customer message sent.
        bot_text: Assistant reply returned by API.
        status_code: HTTP response status code.
        latency_ms: Round-trip HTTP latency in milliseconds.
    """
    sys.stdout.write(f"{BOLD}--- Turn {turn}/{total} ---------------------------------{RESET}\n")
    sys.stdout.write(f"{BOLD}{BLUE}Customer 👤:{RESET} {user_text}\n")
    sys.stdout.write(
        f"{DIM}             HTTP {status_code} | Round-trip: {YELLOW}{latency_ms:.1f}ms{RESET}\n"
    )
    sys.stdout.write(f"{BOLD}{GREEN}Bot 💈:     {RESET}{bot_text}\n\n")
    sys.stdout.flush()


def send_message(
    client: httpx.Client,
    base_url: str,
    secret: str,
    phone: str,
    text: str,
    message_id: str | None = None,
) -> tuple[int, str, float]:
    """Dispatch a signed message to POST /v1/messages and measure latency.

    Args:
        client: HTTP client instance.
        base_url: Target API base URL.
        secret: Webhook shared secret for HMAC-SHA256 signature.
        phone: Sender phone number (E.164).
        text: Customer message text.
        message_id: Optional explicit message UUID.

    Returns:
        Tuple of (HTTP status code, bot response text, latency in ms).
    """
    msg_id = message_id or f"msg-{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(UTC).isoformat()
    now_epoch = str(int(time.time()))

    payload_dict = {
        "message_id": msg_id,
        "phone": phone,
        "text": text,
        "timestamp": now_iso,
    }
    raw_body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")

    digest = compute_signature(secret, int(now_epoch), raw_body)
    signature_header = f"sha256={digest}"

    endpoint = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature_header,
        "X-Timestamp": now_epoch,
    }

    t0 = time.perf_counter()
    try:
        response = client.post(endpoint, content=raw_body, headers=headers)
        latency_ms = (time.perf_counter() - t0) * 1000
        data = response.json() if response.text else {}
        response_text = data.get("response_text") or ""
        return response.status_code, response_text, latency_ms
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return 0, f"HTTP Error: {exc}", latency_ms


def run_demo(
    base_url: str,
    secret: str,
    phone: str,
    delay: float,
    idempotency_check: bool,
) -> int:
    """Execute the multi-turn conversational demonstration.

    Args:
        base_url: Base URL of the running API.
        secret: Webhook HMAC secret.
        phone: Customer phone number.
        delay: Artificial delay between turns in seconds.
        idempotency_check: Whether to verify idempotent duplicate replay after Turn 1.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    print_banner()
    sys.stdout.write(f"Connecting to: {BOLD}{base_url}{RESET}\n")
    sys.stdout.write(f"Customer Phone: {BOLD}{phone}{RESET}\n\n")

    turns = [
        (
            "Olá! Gostaria de agendar um corte para amanhã às 15h.",
            "Initial booking request with date and time",
        ),
        (
            "Renan Maciel",
            "Provide elicited customer name",
        ),
        (
            "Sim",
            "Confirm the proposed appointment",
        ),
        (
            "Gostaria de remarcar para sexta-feira às 16h.",
            "Reschedule request",
        ),
        (
            "Confirmado",
            "Confirm rescheduling",
        ),
        (
            "Preciso cancelar meu agendamento.",
            "Cancellation request",
        ),
        (
            "Sim, pode cancelar.",
            "Confirm cancellation",
        ),
    ]

    total_turns = len(turns)
    with httpx.Client(timeout=30.0) as client:
        for idx, (user_text, description) in enumerate(turns, start=1):
            if idx > 1 and delay > 0:
                time.sleep(delay)

            message_id = f"demo-msg-{idx}-{uuid.uuid4().hex[:8]}"
            status_code, bot_reply, latency_ms = send_message(
                client=client,
                base_url=base_url,
                secret=secret,
                phone=phone,
                text=user_text,
                message_id=message_id,
            )

            if status_code != 200:
                sys.stdout.write(
                    f"{RED}[FAIL] Turn {idx} ({description}) returned "
                    f"HTTP {status_code}: {bot_reply}{RESET}\n"
                )
                return 1

            print_card(
                turn=idx,
                total=total_turns,
                user_text=user_text,
                bot_text=bot_reply,
                status_code=status_code,
                latency_ms=latency_ms,
            )

            # Optional idempotency verification on Turn 1
            if idx == 1 and idempotency_check:
                sys.stdout.write(
                    f"{YELLOW}[Idempotency Verification] "
                    f"Replaying Turn 1 with identical message_id...{RESET}\n"
                )
                replay_status, replay_reply, replay_lat = send_message(
                    client=client,
                    base_url=base_url,
                    secret=secret,
                    phone=phone,
                    text=user_text,
                    message_id=message_id,
                )
                if replay_status == 200 and replay_reply == bot_reply:
                    sys.stdout.write(
                        f"{GREEN}[PASS] Idempotent replay returned HTTP 200 "
                        f"with identical cached text ({replay_lat:.1f}ms).{RESET}\n\n"
                    )
                else:
                    sys.stdout.write(
                        f"{RED}[FAIL] Replay mismatch! "
                        f"Status: {replay_status}, Reply: {replay_reply}{RESET}\n"
                    )
                    return 1

    sys.stdout.write(f"{BOLD}{GREEN}All 7 conversation turns completed successfully! 🎉{RESET}\n\n")
    return 0


def resolve_webhook_secret() -> str:
    """Resolve the webhook secret from environment or .env file.

    Returns:
        Resolved secret string.
    """
    env_val = os.getenv("WEBHOOK_SECRET")
    if env_val and env_val.strip():
        return env_val.strip()

    for candidate in (Path(".env"), Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                trimmed = line.strip()
                if trimmed.startswith("WEBHOOK_SECRET=") and not trimmed.startswith("#"):
                    val = trimmed.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        return val

    return ""


def main() -> None:
    """Parse CLI arguments and launch the conversation demo."""
    # Ensure stdout handles UTF-8 characters across platforms (including Windows cp1252)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Simulate a 7-turn booking conversation against POST /v1/messages.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BOOKING_AGENT_URL", "http://localhost:8000"),
        help="Base URL of the booking-agent API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--phone",
        default="+5511999998888",
        help="Customer phone number in E.164 format (default: +5511999998888)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Delay in seconds between turns to simulate typing (default: 0.4)",
    )
    parser.add_argument(
        "--idempotency-check",
        action="store_true",
        help="Verify idempotent replay after Turn 1",
    )

    args = parser.parse_args()
    raw_secret = resolve_webhook_secret()
    try:
        secret = validate_webhook_secret_value(raw_secret)
    except ValueError as exc:
        sys.stderr.write(f"{RED}Error: {exc}{RESET}\n")
        sys.exit(1)

    exit_code = run_demo(
        base_url=args.base_url,
        secret=secret,
        phone=args.phone,
        delay=args.delay,
        idempotency_check=args.idempotency_check,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
