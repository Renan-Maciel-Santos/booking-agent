# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-30

First release: a conversational booking agent for barbershops that books, reschedules and cancels appointments over WhatsApp, with the LLM limited to interpreting messages and every business decision made in deterministic Python.

### Added

- **Booking engine.** Availability by business hours and barber, booking, in-place rescheduling and cancellation, with an "any available barber" mode that retries the next free barber on conflict ([ADR 0002](docs/adr/0002-double-booking-exclusion-constraint.md), [ADR 0003](docs/adr/0003-time-handling-and-rescheduling.md)).
- **Double-booking protection in the database.** A PostgreSQL exclusion constraint prevents overlapping appointments for the same barber, independently of application code.
- **LLM at the edge only.** OpenAI structured outputs extract the intent and entities from free text; a deterministic fake parser runs the Quick Start and the tests without an API key ([ADR 0001](docs/adr/0001-llm-only-at-the-edge.md), [ADR 0004](docs/adr/0004-llm-adapter-and-evaluation.md)).
- **Conversation state machine.** Multi-turn flows for booking, rescheduling and cancelling, with a confirmation step in which the confirmation shown to the customer is always the action that gets executed ([ADR 0005](docs/adr/0005-conversation-flow-and-state-machine.md), [ADR 0007](docs/adr/0007-confirmation-consistency.md)).
- **Typed conversation state.** Slots are a validated domain model; invalid persisted state fails safe instead of being repaired with guessed values ([ADR 0008](docs/adr/0008-typed-conversation-slots.md)).
- **Stage handlers.** Each conversation stage is a handler with read-only dependencies, dispatched by stage and testable in memory ([ADR 0009](docs/adr/0009-stage-handlers-with-read-only-dependencies.md)).
- **Message endpoint.** `POST /v1/messages` with HMAC-SHA256 webhook signatures, payload bounds, per-phone rate limiting, request correlation IDs and idempotent replay of repeated messages ([ADR 0006](docs/adr/0006-message-endpoint-and-channel-integration.md)).
- **Exactly-once actions.** Bookings, reschedules and cancellations carry an action id stored in `booking_actions`, so retries and concurrent confirmations never create duplicate appointments.
- **WhatsApp integration.** An n8n workflow that signs messages, calls the API and sends the reply only when there is something to send.
- **Personal data retention.** A one-shot command redacts stored replies after the idempotency window, deletes old idempotency records and inactive conversations in batches, and logs only counts ([ADR 0010](docs/adr/0010-personal-data-retention.md)).
- **Operations.** Liveness and readiness health endpoints, structured JSON logging without personal data, a multi-stage Docker image and a Compose stack.
- **Showcase.** An idempotent demo seed and a seven-turn demo script that works on any day of the week.
- **Quality gates.** Pre-commit with ruff and mypy in strict mode, GitHub Actions CI, unit tests with in-memory fakes, integration tests against real PostgreSQL via testcontainers, test order randomised on every run, and an opt-in LLM evaluation dataset.

### Security

- The application refuses to start with a missing, placeholder or short webhook secret, and the webhook signature is verified before the request body is parsed.
- The API is published only on the host loopback by default, and the PostgreSQL port is not published at all unless the optional development Compose file is used.
- Customer phone numbers are masked in logs; message text and customer names are never logged.

[Unreleased]: https://github.com/Renan-Maciel-Santos/booking-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Renan-Maciel-Santos/booking-agent/releases/tag/v0.1.0
