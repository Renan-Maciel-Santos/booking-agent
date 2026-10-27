# ADR 0010: Personal data retention

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Renan Maciel

## Context

The service stores personal data in two places that grow without limit:

| Table | Personal data | Why it is stored |
|---|---|---|
| `processed_messages` | `response_text` holds the reply sent to the customer, which includes the customer's name, the service and the appointment date and time | Idempotency: when the same `message_id` arrives again (webhook retries), the stored reply is returned instead of processing the message twice |
| `conversation_states` | The customer's phone number is the primary key; `slots` hold the customer's name and appointment identifiers | Multi-turn conversation state |

Nothing ever removes these rows. The 30-minute conversation TTL ([ADR 0005](0005-conversation-flow-and-state-machine.md)) only resets a conversation **when a new message arrives**; a conversation the customer abandons stays in the database indefinitely. Idempotency records are kept forever even though a retried webhook only arrives within a short window.

Keeping personal data longer than its purpose requires conflicts with the data minimisation principle of the LGPD (Lei Geral de Proteção de Dados). Issue #9 tracks a retention job for `response_text`; the diagnostic showed that inactive conversation states need the same treatment.

The two purposes of an idempotency record have different lifetimes: the **reply text** is only needed to answer a retry, while the **message identifier** keeps preventing duplicate processing for as long as the row exists. Deleting the whole row too early would let a late retry be processed again and, for example, create a duplicate appointment.

## Decision

1. **Three retention rules, each configurable through environment variables:**

   | Data | Rule | Default |
   |---|---|---|
   | `processed_messages.response_text` | Set to `NULL` once the record is older than `PROCESSED_MESSAGE_REDACT_AFTER_DAYS`. The row and its `message_id` remain, so deduplication keeps working | 7 days |
   | `processed_messages` rows | Deleted once older than `PROCESSED_MESSAGE_DELETE_AFTER_DAYS` | 90 days |
   | `conversation_states` rows | Deleted once inactive (`updated_at`) for longer than `CONVERSATION_STATE_DELETE_AFTER_DAYS`, **except** rows in the `executing` stage, which may still resume an action | 30 days |

2. **Configuration is validated at startup.** The deletion window for idempotency records must be longer than the redaction window, and the conversation-state deletion window must be longer than the conversation TTL. An invalid combination prevents the application from starting.
3. **A one-shot command, not an in-process scheduler.** The policy runs through `python -m booking_agent.jobs.retention`, triggered by any external scheduler (cron, an n8n schedule, a Kubernetes CronJob). An in-process scheduler would run once per API replica and stop with it. The command is idempotent: running it twice has no additional effect.
4. **Batched, parameterised deletes.** Each rule processes rows in batches of `RETENTION_BATCH_SIZE` until a batch affects no rows, so a large backlog never holds long locks. Cut-off timestamps are computed in Python from the injected clock and passed as parameters; the SQL never calls `now()`, which keeps the job deterministic in tests.
5. **Indexes on the retention timestamps.** `processed_messages(created_at)` and `conversation_states(updated_at)` are indexed so the job does not scan whole tables as they grow.
6. **Observable without exposing data.** The job logs a single structured `data_retention_completed` event with the number of affected rows per rule and the cut-offs used, or `data_retention_failed` with the exception type only. It never logs phone numbers, names or message content.

## Consequences

### Positive

- Personal data in replies is kept only for the idempotency window, while deduplication keeps protecting against duplicate bookings for much longer.
- Abandoned conversations no longer keep customers' phone numbers and names indefinitely.
- The policy is explicit, configurable, tested and visible in logs.
- The command works with any scheduler and with any number of API replicas.

### Negative / Trade-offs

- A retry that arrives after the redaction window is still deduplicated but receives no reply text, so the customer gets no repeated answer. A retry after the deletion window would be processed again.
- Someone must schedule the command; the API does not enforce retention on its own. The README documents a daily schedule.
- Customer and appointment records are business data and are out of scope; their retention requires a separate decision.

## References

- [ADR 0005](0005-conversation-flow-and-state-machine.md): Conversation flow and state machine
- [ADR 0006](0006-message-endpoint-and-channel-integration.md): Message endpoint and channel integration
- #9: retention job for stored message responses
