# ADR 0008: Typed conversation slots validated at the repository boundary

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** Renan Maciel

## Context

Conversation state is persisted in `conversation_states.slots` (JSONB) and handled by `ConversationService` as `dict[str, Any]`. Seventeen keys are written across the service in five groups:

| Group | Keys |
|---|---|
| Booking target | `service_id`, `service_name`, `service_price_cents`, `barber_id`, `barber_name`, `local_date`, `local_time`, `customer_name` |
| Reschedule or cancellation origin | `appointment_id`, `old_date_time` |
| Selection in progress | `candidate_appointment_ids`, `alternative_slots`, `awaiting` |
| Confirmation | `flow`, `action_id` |
| Execution | `executing_message_id` |

Identifiers, dates and times are stored as strings and re-parsed at every read. Nothing guarantees which keys exist or what type they hold, so each reader re-validates, or does not. The defects fixed in #12 (missing `flow`, missing `appointment_id`, fabricated confirmation defaults) and the malformed-date fallback tracked in #16 share this root cause. Two encodings carry hidden meaning: `barber_id = None` means "any available barber", and `executing_message_id = ""` is used as an empty sentinel.

The diagnostic for #16 also found:

- **Dead code.** `ConfirmationDetails.from_slots` reads six aliases (`service`, `date`, `time`, `name`, `price`, `barber`) that are never written anywhere in `src/`. `service_duration_minutes` is written but never read.
- **A dependency-rule violation.** `domain/ports.py` imports the SQLAlchemy model, so the conversation port exposes `stage: str` and `slots: dict[str, Any]` to the domain instead of domain types.
- **A natural validation boundary.** State is loaded in exactly two places (`get(..., for_update=True)`) and saved through `upsert`.
- **A size constraint.** `_transition_state` is a single method of roughly 575 lines. Encoding per-stage state in types now would require rewriting it.

## Decision

1. **`ConversationSlots` domain model.** A Pydantic model in the domain layer with real types (`UUID`, `date`, `time`, enums), `extra="forbid"` so unknown keys fail, and `validate_assignment=True` so every assignment in the service is validated. It is mutable because the service updates slots field by field. JSON keys and ISO formats stay unchanged, so existing rows remain loadable.
2. **Explicit meaning instead of sentinels.** "Any available barber" becomes an explicit value rather than `None` in `barber_id`, and `executing_message_id` uses `None` for absence instead of an empty string.
3. **Validation at the repository boundary.** The conversation-state repository returns a domain `ConversationSnapshot` (`stage: ConversationStage`, `slots: ConversationSlots`, `updated_at`) and accepts the same types on upsert. Validation happens once, when state crosses the persistence boundary. The domain port no longer imports the ORM model.
4. **Invalid state fails safe, never repaired by guessing.** A row that fails validation is not filled in with plausible values. The service logs a structured `invalid_conversation_state` event (field names only, never values), resets the conversation to `IDLE` and replies neutrally.
5. **Dead code is removed explicitly.** The slot-key aliases are deleted. `service_duration_minutes` is removed from the code and from existing rows by an Alembic data migration, so persisted data matches the model instead of being silently tolerated.
6. **One model for every stage, for now.** Per-stage or per-flow types (a discriminated union) are deferred until the state transitions are extracted into pure functions (#9). At that point each function receives and returns the state of its own stage, and the union becomes a local change instead of a rewrite of `_transition_state`.

The work is delivered in two pull requests: first the removal of dead code and malformed-value fallbacks with the data migration, then the typed model and the repository boundary.

## Consequences

### Positive

- Invalid conversation state fails once, at load time, with a precise error.
- Types document the state, and mypy checks every read and write of a slot.
- The domain no longer depends on the persistence model for this aggregate.
- Hidden meanings (`None` as "any barber", `""` as "no message") become explicit.

### Negative / Trade-offs

- A single model cannot express which fields are required in each stage. Those invariants remain runtime checks (for example `ConfirmationDetails`) until the follow-up after #9.
- Conversations whose persisted state is invalid are reset instead of resumed.
- The change is mechanical but wide: every slot access in `ConversationService` and every test that builds a slot dictionary must be updated.

## References

- [ADR 0005](0005-conversation-flow-and-state-machine.md): Conversation flow and state machine
- [ADR 0007](0007-confirmation-consistency.md): The confirmation shown is the action executed
- #16: typed conversation slots; #9: extraction of pure state transitions
