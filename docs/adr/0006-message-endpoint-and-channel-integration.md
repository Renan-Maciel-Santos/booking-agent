# ADR 0006: Message endpoint, webhook authentication, rate limiting, and channel integration

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** Renan Maciel

## Context

The booking agent core domain is governed by deterministic business rules in `BookingService` ([ADR 0002](0002-double-booking-exclusion-constraint.md), [ADR 0003](0003-time-handling-and-rescheduling.md)), strict LLM structured parsing at the system boundary ([ADR 0001](0001-llm-only-at-the-edge.md), [ADR 0004](0004-llm-adapter-and-evaluation.md)), and a multi-turn conversation orchestrator managing finite state transitions, confirmations, and action idempotency ([ADR 0005](0005-conversation-flow-and-state-machine.md)).

To make this conversational capability available to end users, the service must interface with external messaging channels (such as WhatsApp). Exposing an HTTP endpoint on the public internet introduces critical security, architectural, and operational questions:

1. **Endpoint Contract & Failure Mapping:** How should the HTTP contract be structured to cleanly pass incoming messages and return replies while accurately conveying failure modes (validation errors, LLM provider downtime, duplicate message delivery) through standard HTTP status codes?
2. **Webhook Security & Replay Prevention:** Webhooks arrive from external orchestrators. How do we authenticate incoming webhook deliveries, guarantee payload integrity, and prevent replay attacks where an attacker captures and re-sends valid past requests? Why is a static API key insufficient?
3. **Denial-of-Service & Resource Exhaustion:** How do we protect the LLM token budget, memory buffers, and database connections from oversized payloads, Slowloris attacks, and abusive flood traffic from specific phone numbers?
4. **Channel Decoupling (Hexagonal Architecture):** WhatsApp Cloud APIs use deeply nested JSON structures, send transient delivery receipts, require URL verification handshakes, and change SDK formats over time. Should the FastAPI backend couple directly to WhatsApp protocols?
5. **Observability and Privacy:** How do we correlate incoming HTTP requests across all logs and traces without leaking customer Personally Identifiable Information (PII) such as phone numbers and raw message text?
6. **Local Developer & Demo Experience:** How can developers, reviewers, CI pipelines, and Quick Start guides exercise a full multi-turn conversation without setting up a Meta developer account, phone numbers, or WhatsApp Business webhooks?

## Decision

We establish six architectural decisions governing the message endpoint, webhook security, channel integration, and observability:

### 1. Strongly-Typed Webhook Contract (`POST /v1/messages`) and HTTP Status Mapping

We expose a single, channel-agnostic endpoint `POST /v1/messages`.

#### Request Schema:
```json
{
  "message_id": "msg_01JBCDEFGHJKMNPQRSTVWXYZ01",
  "phone": "+5511999998888",
  "text": "Quero agendar corte amanhã às 15h",
  "timestamp": "2026-10-21T12:00:00Z"
}
```
- Validated via Pydantic model with `ConfigDict(extra="forbid")`.
- `message_id`: printable string, length 1–128 (`^[a-zA-Z0-9_\-\.:]{1,128}$`).
- `phone`: international E.164 string (`^\+[1-9]\d{1,14}$`).
- `text`: non-empty stripped string, max 1,000 characters.
- `timestamp`: timezone-aware datetime (`AwareDatetime`) representing the origin timestamp of the message as reported by the messaging channel (e.g. WhatsApp message timestamp, NOT server or n8n local time).

#### Response Schema:
```json
{
  "response_text": "Para quem é o agendamento? Por favor, informe seu nome completo."
}
```
Field `response_text` is `str | None`.

#### HTTP Status Codes and Failure Alignment:
- **`200 OK`:** Normal turn progression; cached response for deduplicated message replays (`processed_messages.status == 'done'`); and conversational clarification when LLM output cannot be parsed (`LLMInvalidOutputError`).
- **`202 Accepted`:** Deduplicated replay of a message whose turn is currently processing (`processed_messages.status == 'processing'` within active lease). Returns `{"response_text": null}`. The channel adapter drops this response and sends nothing to the user.
- **`400 Bad Request`:** Malformed JSON, schema validation failures, unrecognized extra fields, or invalid phone numbers.
- **`401 Unauthorized`:** Missing or invalid HMAC signature, missing timestamp, or timestamp drifted outside the tolerance window.
- **`413 Payload Too Large`:** Request body exceeds configured `max_body_bytes` (64 KB). Terminated strictly with 413.
- **`429 Too Many Requests`:** Sender phone number exceeded the rate limit threshold (> 10 req/min). Channel adapter does not retry; sends at most a polite cooldown notice to the customer.
- **`503 Service Unavailable`:** Transient upstream LLM provider failures (`LLMUnavailableError`), unconfigured credentials (`LLMConfigurationError`), or database unavailability.
  - *Retry Protocol:* When returning 503, the internal cleanup handler marks the lease as `failed` in `processed_messages`. The caller is instructed to retry with exponential backoff using the **same `message_id`**, ensuring clean lease re-acquisition upon recovery.

### 2. Webhook Authentication via HMAC-SHA256 and Timestamp Window

Authentication is enforced via cryptographic HMAC-SHA256 signatures over the raw request body with an enforced timestamp freshness window.

#### Cryptographic Specification:
- **Configuration:** Shared secret key configured via `config.Settings.webhook_secret` as `SecretStr`. No default value is permitted; startup fails if omitted.
- **Required Headers:**
  - `X-Signature: sha256=<hex_digest>`
  - `X-Timestamp: <unix_epoch_seconds>` (dispatch timestamp in UTC)
- **Signed Payload:**
  $$\text{Payload} = \text{utf8}(\text{timestamp}) \mathbin{\Vert} \text{b"."} \mathbin{\Vert} \text{raw\_body\_bytes}$$
  Binding the timestamp into the HMAC payload ensures that neither the timestamp nor the body can be altered in transit without invalidating the digest.
- **Tolerance Window:** The request dispatch timestamp is compared against server UTC time:
  $$\lvert \text{now\_utc} - \text{timestamp} \rvert \le 300 \text{ seconds (5 minutes)}$$
- **Verification:** Verification uses constant-time comparison (`hmac.compare_digest(expected, provided)`) to defend against timing attacks.

#### Why HMAC-SHA256 instead of a Static API Key for Webhooks?
1. **Payload Integrity:** A static API key (e.g. `Authorization: Bearer <key>`) authenticates only the HTTP header. It provides no cryptographic guarantee that the JSON payload was not modified, truncated, or injected in transit by an intermediary proxy or CDN. HMAC guarantees exact bit-for-bit payload integrity.
2. **Replay Attack Elimination:** If an HTTP request with an API key is captured in transit or leaked in gateway logs, an attacker can replay that request indefinitely. Coupling HMAC with an enforced 5-minute timestamp tolerance window ensures that replayed requests expire and are rejected after 300 seconds.
3. **Zero Secret Exposure:** The secret key is never transmitted across the wire, preventing credential exposure during network sniffing or proxy inspection.
4. **Industry Best Practice:** Established webhook platforms (Stripe, GitHub, Shopify, Slack) use HMAC-SHA256 with timestamp tolerance as the standard webhook security model.

### 3. Request Bounds, Timeouts, and In-Memory Phone Rate Limiting

To prevent resource exhaustion, the endpoint enforces strict request limits:

1. **Payload Limits:**
   - Raw request body capped at 64 KB (`65,536 bytes`), enforced at the ASGI middleware layer before full buffering in memory. Over-limit bodies are terminated strictly with `413 Payload Too Large`.
   - User message text capped at 1,000 characters in the Pydantic schema.
2. **Timeouts and Request Deadlines:**
   - The API enforces a total per-request timeout (`config.Settings.request_timeout_seconds`, e.g. 15s; no test values as default), bounded by LLM timeouts and database operations.
   - The channel adapter (n8n) HTTP timeout **must be strictly greater** than the API deadline (e.g. 25s or 30s) to avoid premature client-side disconnections.
3. **Phone Rate Limiting:**
   - Enforced per sender phone number using an in-memory sliding window limiter (threshold: **10 requests per minute** per phone).
   - Excess requests receive `429 Too Many Requests` without triggering LLM extraction or database operations.
   - The channel adapter (n8n) does not retry upon receiving 429; it dispatches a polite cooldown message to the customer.
   - *Architecture Trade-off & Known Limitation:* The in-memory limiter is zero-dependency and optimal for single-node deployments. If horizontal multi-instance scaling is required, distributed rate limiting via Redis or PostgreSQL unlogged tables will be added as a post-launch enhancement.

### 4. Observability, Request Correlation, and Privacy

1. **Request Correlation (`X-Request-ID`):**
   - Middleware inspects incoming `X-Request-ID` (preserving it if valid) or generates a fresh UUID v4.
   - Propagated through Python's `contextvars` to all loggers.
   - Automatically injected as `"request_id": "<uuid>"` in every structured JSON log line.
   - Returned in response headers as `X-Request-ID`.
2. **PII and Message Privacy:**
   - Phone numbers are masked in all logs: `+5511*****8888`.
   - Customer message text is **strictly forbidden from being logged at any log level** (DEBUG, INFO, WARNING, ERROR). Only message character length (`text_len: int`) and message ID are logged.
   - Customer names are masked or omitted in logs.
3. **Telemetry & Granular Latencies:**
   - Every completed request emits a structured completion event recording:
     - `phase1_lease_ms`, `phase2_llm_ms`, `phase3_state_ms`, `phase4_action_ms`, `total_duration_ms`.
     - `outcome`: `booking_confirmed`, `reschedule_confirmed`, `cancel_confirmed`, `slot_conflict`, `llm_clarification`, `idempotent_replay`, `duplicate_in_progress`, `llm_unavailable`, `rate_limited`.
     - `fast_path`: boolean indicating whether the turn bypassed the LLM.

### 5. n8n as External Channel Adapter (Ports and Adapters)

The backend exposes a channel-agnostic contract. **n8n** is designated as the external channel adapter:

- **Responsibilities:**
  1. Receives webhooks from WhatsApp providers (Cloud API, Evolution API).
  2. Filters out noise (delivery receipts, status updates, read receipts, media).
  3. Maps WhatsApp payload into canonical `POST /v1/messages` format using WhatsApp's original epoch message timestamp.
  4. Signs the request with HMAC-SHA256 and sets `X-Timestamp`.
  5. Dispatches request to the backend with a timeout (25s) strictly exceeding the API deadline (15s).
  6. Dispatches `response_text` to the customer over WhatsApp if status is `200 OK` and text is present. Drops response if status is `202 Accepted` or `response_text` is null.
  7. On `503 Service Unavailable`, executes automated exponential backoff retries (1s, 3s, 9s, max 3 attempts) using the identical `message_id`.
  8. On `429 Too Many Requests`, does **not** retry; sends a polite cooldown notification to the customer.
- **Artifact:** The repository versions a pre-configured, tested workflow at `integrations/n8n/whatsapp_booking_agent_workflow.json`.

### 6. Standalone CLI Demonstration Script (`scripts/demo_conversation.py`)

To guarantee seamless local development, testing, Quick Start verification, and video/GIF generation without WhatsApp dependencies:

- A standalone Python script `scripts/demo_conversation.py` runs against `http://localhost:8000` with `LLM_PROVIDER=fake`.
- Computes valid HMAC signatures locally using the configured `WEBHOOK_SECRET`.
- Simulates a complete, multi-turn customer dialogue: booking a haircut, rescheduling to another day, and cancelling the booking.
- Renders formatted terminal cards with distinct colors showing user text, bot responses, latencies, and HTTP status codes.

## Consequences

### Positive:
- **Security:** Public endpoint is protected against spoofing, payload tampering, replay attacks, heap memory exhaustion, and rate limit abuse.
- **Architectural Purity:** Backend has zero dependencies on proprietary WhatsApp APIs, maintaining clean Hexagonal Architecture.
- **Reliability:** 503 retry semantics integrated with message lease recovery ensure resilient self-healing during transient LLM outages.
- **Traceability:** Request ID correlation and phase-by-phase latency metrics provide instant observability without exposing customer PII.
- **Developer Experience:** CLI demonstration script allows end-to-end multi-turn verification in under 5 seconds without external credentials.

### Negative / Trade-offs:
- **Single-Node Rate Limiting:** In-memory rate limiting is node-local; scaling across multiple instances requires a distributed cache (Redis) or database-backed rate limiting.
- **Clock Drift Sensitivity:** Server clock must be synchronized via NTP; drift $> 300\text{s}$ will cause valid webhook requests to fail verification.
- **External Adapter Dependency:** Production deployment requires running an orchestrator (n8n or equivalent serverless function) to bridge WhatsApp and the backend.
- **Out-of-Order Rapid Messages:** Known limitation: if a customer sends multiple messages in rapid succession, their processing order is not strictly guaranteed because Phase 2 LLM extraction executes concurrently before Phase 3 database locking.

## References
- [ADR 0001: LLM only at the edge](0001-llm-only-at-the-edge.md)
- [ADR 0004: LLM adapter and evaluation](0004-llm-adapter-and-evaluation.md)
- [ADR 0005: Conversation flow and state machine](0005-conversation-flow-and-state-machine.md)
- `docs/design-doc.md`
- Skills: `secure-backend`, `observability`, `testing-strategy`, `llm-integration`
