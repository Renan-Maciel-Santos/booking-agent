# ADR 0009: Stage handlers with read-only dependencies

- **Status:** Accepted
- **Date:** 2026-10-26
- **Deciders:** Renan Maciel

## Context

[ADR 0005](0005-conversation-flow-and-state-machine.md) defines the conversation state machine and the four phases of a turn. In the code, both live in `ConversationService`, which has grown to about 1,880 lines:

| Method | Lines | Role |
|---|---|---|
| `_process_turn` | ~540 | Phase orchestration: idempotency, locking, TTL, fast path, execution, persistence |
| `_transition_state` | ~570 | Decides the next stage, slots and reply for five stages in one method |
| Confirmation helpers | ~300 | `_enter_confirmation`, `_load_reschedule_origin`, `_validate_booking_slots_and_confirm`, `ConfirmationDetails` |

Issue #9 asks to move the stage transition rules into pure functions that can be tested in memory without database access, leaving only phase orchestration in the class.

A diagnostic of `_transition_state` found:

- **Transitions only read.** It touches the unit of work 17 times, and every call is a read (appointments, customers, services, barbers). It also reads availability once through `BookingService.list_availability` and the clock once. It never writes; persistence and execution happen in `_process_turn`.
- **Reads are interleaved with decisions.** What is read depends on what was decided a few lines earlier (for example, the appointment loaded depends on which option the customer picked). Prefetching everything a stage might need would over-fetch and duplicate decision logic.
- **Nothing in the types expresses that transitions only read.** They receive the full `UnitOfWork`, so a future change could add a write inside a transition without any signal.
- **Transitions are tested only end to end.** 57 unit tests drive them through `handle_message` with a fake unit of work; none exercises a single stage in isolation.

## Decision

1. **One handler per stage.** Each stage that the state machine decides (`IDLE`, `COLLECTING_BOOKING_DETAILS`, `CHOOSING_APPOINTMENT`, `OFFERING_ALTERNATIVES`, `AWAITING_CONFIRMATION`) gets its own handler in a `transitions` package, one module per stage. `EXECUTING` and `COMPLETED` remain owned by the turn orchestration in `ConversationService`.
2. **A typed transition result.** Handlers return a frozen `Transition` (`stage: ConversationStage`, `slots: ConversationSlots`, `reply: str`) instead of a `tuple[str, ConversationSlots, str]`, so the next stage is an enum, not a string.
3. **Read-only dependencies, enforced by types.** Handlers depend on a narrow `ConversationReads` protocol that exposes only the reads transitions use (appointments, customer, services, barbers, availability) plus the clock. A unit-of-work-backed adapter implements it in production; tests use a small in-memory implementation. Because the protocol has no write methods, a handler cannot persist or execute anything.
4. **An exhaustive dispatch table.** `ConversationService` selects the handler from a mapping keyed by `ConversationStage`. A test asserts that every stage is either mapped to a handler or explicitly owned by the orchestration, so adding a stage without deciding who handles it fails.
5. **"Pure" means effect-free, not prefetched.** Handlers are deterministic given their inputs, their reads and the clock, and they have no side effects. They are allowed to await reads, because data needs are decided step by step. A strictly prefetched functional core was rejected: it would load data that most turns never use and split each decision across two places.
6. **Incremental extraction, no behaviour change.** The work is delivered in small pull requests: first the `Transition` type, the read protocol and its adapter (with `_transition_state` switched to them), then the stages moved out one or two at a time, and finally `_transition_state` deleted. The existing end-to-end tests stay unchanged as the safety net; each extracted handler also gains focused tests with in-memory reads.

## Consequences

### Positive

- `ConversationService` shrinks to phase orchestration, matching ADR 0005.
- The rule that transitions never write is guaranteed by the type of their dependencies, not by convention.
- Each stage can be tested in isolation with in-memory data, with no database and no fake unit of work.
- The per-stage handlers are the natural place for per-stage slot types, the follow-up deferred by [ADR 0008](0008-typed-conversation-slots.md).

### Negative / Trade-offs

- Handlers are async because they await reads; they are not pure in the strict functional sense.
- The read protocol is a second abstraction over the repositories and must be kept in sync with what handlers need.
- Several pull requests of mechanical moves before the class reaches its final size.
- Turn orchestration (`_process_turn`, about 540 lines) is not addressed here and remains a candidate for a later decision.

## References

- [ADR 0005](0005-conversation-flow-and-state-machine.md): Conversation flow and state machine
- [ADR 0007](0007-confirmation-consistency.md): The confirmation shown is the action executed
- [ADR 0008](0008-typed-conversation-slots.md): Typed conversation slots
- #9: modularize state transitions
