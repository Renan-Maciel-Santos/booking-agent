# ADR 0001: LLM only at the edge

- **Status:** Accepted
- **Date:** 2026-10-15
- **Deciders:** Renan Maciel

## Context

The booking agent receives free-form messages ("can I get a haircut tomorrow at 3?")
and must create, move or cancel appointments for Northside Barbers. Two broad
designs exist:

1. **Agentic tool calling:** give the LLM tools such as `create_appointment` and let
   it decide what to call, in which order, and with which arguments.
2. **LLM at the edge:** the LLM only converts text into a structured, validated
   `BookingIntent`. A deterministic Python service decides and executes everything.

Booking is a transactional domain. A wrong action (double booking, cancelling the
wrong appointment) has a direct business cost, and the rules (opening hours,
service durations, overlap) are fully specifiable in code.

## Decision

The LLM is used **only** to extract intent and entities into the Pydantic model
`BookingIntent` (structured output). It has no tools and no database access.

- `domain/ports.py` defines the `IntentParser` port; adapters live in
  `integrations/llm/`.
- All business rules (availability, conflicts, rescheduling, cancellation) live in
  `services/` and are deterministic and unit-testable.
- All persistence goes through `repositories/`, with database constraints as the
  final guard against invalid states (e.g. overlapping appointments).

## Consequences

**Positive**

- Hallucinations cannot become actions: invalid or unexpected output fails Pydantic
  validation (`extra="forbid"`, enums, timezone-aware datetimes).
- Business rules are testable without an LLM, using `FakeIntentParser`.
- Prompt injection has a small blast radius: the worst case is a wrong *intent*,
  which the service still validates against real data.
- The provider is swappable (OpenAI, Anthropic, local models) behind one port.
- Lower and more predictable token cost: one short extraction call per message.

**Negative**

- Less flexible: new capabilities require code, not just a new prompt.
- Multi-turn dialogue (asking for missing fields) must be modelled explicitly
  through `conversation_state`, which is extra design work.
- Ambiguous messages fall back to `UNKNOWN` and need a clarification flow.

**Revisit when** the product needs open-ended tasks that cannot be expressed as a
small, closed set of intents.
