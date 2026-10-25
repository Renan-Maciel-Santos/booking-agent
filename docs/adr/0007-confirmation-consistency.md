# ADR 0007: The confirmation shown is the action executed

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** Renan Maciel

## Context

[ADR 0005](0005-conversation-flow-and-state-machine.md) defines the conversation state machine, including the `AWAITING_CONFIRMATION` stage in which the customer approves a booking, reschedule or cancellation before it is executed.

A review of the showcase work (#11) found that the message shown to the customer in that stage and the action executed after a "sim" could come from different sources and diverge:

1. **Context-dependent replies.** "Pode cancelar" means *yes* when the pending action is a cancellation and *no* when it is a booking or a reschedule. Treating it as a static affirmative token booked appointments the customer had just declined.
2. **Detail changes during confirmation.** When the customer changed the date or time while a reschedule or cancellation was pending, the confirmation prompt was rendered by booking-only code. Depending on the code path, the customer confirmed one thing and the system executed another: a duplicate appointment, or a cancellation the customer never saw (#14).
3. **Fabricated details.** Missing slot values were filled with plausible defaults ("Cliente", "R$ 0,00"), so a confirmation could display data that did not match what would be executed.
4. **Misleading back-out message.** Saying "não" to a cancellation produced "a solicitação foi cancelada", which reads as if the appointment had been cancelled.

## Decision

The governing principle is: **the confirmation shown to the customer is always the action that gets executed.** Both are derived from the same pending flow and the same data, so they cannot diverge.

### 1. Typed pending flow

The pending action is a `ConversationFlow` (`BOOK`, `RESCHEDULE`, `CANCEL`) persisted with every transition into a stage that depends on it. A missing or invalid flow is a state error: the service logs a structured event, resets to `IDLE`, replies neutrally and executes nothing. No default flow is assumed.

### 2. Deterministic, flow-aware confirmation replies

`classify_fast_path_candidate` maps a normalized reply to `AFFIRMATIVE`, `NEGATIVE` or `CANCEL_ACTION` by exact match after normalization (never substring matching, so "não pode cancelar" is not a candidate). The pure function `resolve_confirmation(candidate, flow)` decides the outcome with an exhaustive `match`:

| Candidate | BOOK | RESCHEDULE | CANCEL |
|---|---|---|---|
| AFFIRMATIVE | execute | execute | execute |
| NEGATIVE | abort | abort | abort |
| CANCEL_ACTION | abort | abort | execute |

Only this deterministic path can move a conversation to `EXECUTING`. A reply the fast path does not recognize goes to the LLM for interpretation, but no LLM result can execute a pending action; an unrecognized reply produces a clarification prompt. This keeps [ADR 0001](0001-llm-only-at-the-edge.md) intact: the LLM interprets, Python decides.

### 3. Single entry point into the confirmation stage

`_enter_confirmation(flow, slots, details)` is the only code that moves a conversation into `AWAITING_CONFIRMATION`. It selects the prompt template from the flow with an exhaustive `match`, generates a new `action_id`, and persists the flow and the pending action together. Callers pass fully populated, typed `ConfirmationDetails`; they never pass free-form text.

`ConfirmationDetails.from_slots` has no fabricated defaults. Missing required data raises `MissingConfirmationDataError` listing every missing field, and the caller fails safe (structured log with field names only, reset to `IDLE`, neutral reply, nothing executed). The only default is the barber, because "qualquer barbeiro disponível" is a real business choice.

### 4. Detail changes are dispatched per flow

When a message in `AWAITING_CONFIRMATION` carries a new date or time, the change is handled according to the pending flow:

- **BOOK:** availability is re-checked for the new booking.
- **RESCHEDULE:** the target of the same reschedule is recomputed; the original appointment is kept.
- **CANCEL:** the customer changed their mind, so the flow becomes a RESCHEDULE of the original appointment and the prompt asks to confirm the move from the original time to the new one.

Whenever the content of the confirmation changes, the pending `action_id` is replaced, never mutated, so a delayed or replayed "sim" cannot execute an outdated version. The flow is preserved through `OFFERING_ALTERNATIVES` when the new time is unavailable.

### 5. Validated reschedule origin

`_load_reschedule_origin` requires the original appointment to be present, confirmed and owned by the customer of the conversation before it is used. Any failure (`missing_id`, `invalid_id`, `not_found`, `not_confirmed`, `not_owned`) is logged with the reason and fails safe. The ownership check is defence in depth: the identifier comes from server-side state, but a cheap check prevents a future bug elsewhere from exposing another customer's appointment.

### 6. Flow-specific back-out messages

Backing out of a confirmation produces a message that states what happened in that flow: no appointment was made (BOOK), the original time is kept (RESCHEDULE), or the appointment is still confirmed (CANCEL).

## Consequences

### Positive

- The prompt and the executed action cannot diverge: both come from the same flow and data.
- Invalid or incomplete state never reaches execution; it fails safe with a precise, PII-free log event.
- The decision table is a pure function, covered by a 9-case truth table test.
- Conversation tests drive full dialogues to the final confirmation and assert the resulting appointments in PostgreSQL.

### Negative / Trade-offs

- Replies outside the fast-path vocabulary need an extra turn (clarification) instead of being executed from an LLM interpretation.
- A cancellation that turns into a reschedule is a product decision encoded in the service; a different policy would require changing this dispatch.
- Required fields per flow are still checked at runtime; [ADR 0008](0008-typed-conversation-slots.md) types the state, and per-stage types are deferred to the extraction of pure state transitions (#9).

## References

- [ADR 0001](0001-llm-only-at-the-edge.md): LLM only at the edge
- [ADR 0005](0005-conversation-flow-and-state-machine.md): Conversation flow and state machine
- [ADR 0008](0008-typed-conversation-slots.md): Typed conversation slots
- #12: implementation; #14: confirmation prompt and executed action could diverge
