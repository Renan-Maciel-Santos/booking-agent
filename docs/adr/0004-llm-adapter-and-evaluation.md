# ADR 0004: LLM adapter architecture, local time extraction, and evaluation strategy

- **Status:** Accepted
- **Date:** 2026-10-20
- **Deciders:** Renan Maciel

## Context

Following [ADR 0001](0001-llm-only-at-the-edge.md), the system employs an LLM strictly at the edge to convert free-form customer messages into a structured, validated `BookingIntent` domain model. Business rules, validation, scheduling, and database access remain strictly deterministic within the service layer.

When designing the concrete LLM adapter and its integration lifecycle, five core architectural problems arise:

1. **Client Library Selection:** Should the integration use the high-level OpenAI Agents SDK (`openai-agents`) or the official `openai` SDK directly?
2. **Temporal Parsing and Timezone Arithmetic:** Customers speak in relative terms ("tomorrow at 3pm", "next Friday morning"). Should the LLM calculate the absolute UTC timestamp directly, or output local date, time, and period components?
3. **Provider Configuration and Lifecycle:** How should the application support both production LLM providers (e.g. OpenAI) and local/testing environments without leaking API costs or introducing flaky dependencies?
4. **Failure Classification and Error Isolation:** How should transient network errors, rate limits, model refusals, and schema validation failures be distinguished and handled without producing corrupt states, endless retry loops, or silent failures?
5. **Prompt Injection Defenses and Data Privacy:** How do we protect against adversarial prompt injections embedded within incoming chat messages and guarantee customer data privacy?
6. **Evaluation Strategy:** How do we prove extraction accuracy and detect regressions across colloquial language, typos, and relative dates without making CI dependent on a paid, rate-limited external API?

## Decision

We establish six core architectural decisions governing the LLM adapter, its data contract, and its evaluation pipeline:

### 1. Direct Official `openai` SDK (`AsyncOpenAI`) over Agents SDK

We choose the official `openai` Python library directly and remove the `openai-agents` dependency from `pyproject.toml`.

**Rationale:**
- In accordance with [ADR 0001](0001-llm-only-at-the-edge.md), the LLM has zero execution privileges: no tool calling, no autonomous loops, and no agent handoffs.
- The Agents SDK is designed for multi-agent workflows and autonomous tool execution. For a single-turn structured parsing call, it adds unnecessary abstraction layers.
- The official `AsyncOpenAI` client natively supports Pydantic structured outputs (`client.beta.chat.completions.parse` with strict schema enforcement), fine-grained timeout controls (`timeout=...`), and retry limits (`max_retries=...`).

### 2. Local Date, Time, and Period Extraction Instead of UTC

The LLM extracts calendar date (`local_date: date | None`), wall-clock time (`local_time: time | None`), and period preference (`time_preference: TimePreference | None` with values `morning`, `afternoon`, `evening`) in the barbershop's frame of reference, instead of outputting an absolute UTC `datetime`:

- **Prompt context injection:** The system prompt injects the current reference date, current wall-clock time, weekday, and business timezone (e.g., `Current Reference: 2026-10-20 14:30:00 (Tuesday) in America/Sao_Paulo`).
- **Deterministic localization:** The deterministic service layer converts `local_date` and `local_time` into a timezone-aware `datetime` using `localize_datetime` (established in [ADR 0003](0003-time-handling-and-rescheduling.md)).
- **Why no `appointment_id`:** Real customers never cite UUIDs in chat messages. For rescheduling or cancellation, the service retrieves upcoming confirmed appointments using the customer's verified phone number (`customer_phone`). If multiple exist, the dialogue asks the customer to choose by date.

**Rationale:**
- **LLM arithmetic limitations:** LLMs frequently make arithmetic mistakes when calculating timezone offsets (e.g. subtracting 3 hours across midnight, leap years, or daylight saving transitions). Forcing the model to output UTC introduces hallucination risks and off-by-one date errors.
- **DST anomalies:** Daylight Saving Time transitions can create non-existent wall-clock times (spring-forward gaps) or ambiguous times (fall-back folds). Handling these anomalies deterministically requires Python's standard `zoneinfo` module and strict validation in code, which `localize_datetime` already provides.
- **Turn-by-turn dialogue flexibility:** In multi-turn chat dialogues, a customer may specify a date in one message ("tomorrow") and a time in a later message ("at 3pm") or a general period ("in the morning"). Decoupling date, time, and period enables the conversation state machine to merge slot entities incrementally.

### 3. Separate Adapter-Specific Schema for OpenAI Strict Mode

OpenAI's Structured Outputs strict mode (`strict: true`) requires that **all fields** in the JSON schema be listed as `required` (with optionality expressed via nullable types `type: ["string", "null"]`). To satisfy this constraint without compromising the clean domain model:
- The adapter defines an internal schema (`_LLMBookingIntentSchema`) where all fields are required and nullable.
- The adapter converts `_LLMBookingIntentSchema` into the domain `BookingIntent`, parsing date and time strings into Python `date` and `time` objects.

### 4. Explicit Provider Selection and Fail-Fast Lifecycle

The system supports explicit provider configuration via `LLM_PROVIDER: Literal["openai", "fake"]` in `Settings`:

- **Production mode (`openai`):**
  - Requires `OPENAI_API_KEY: SecretStr`.
  - **Fail-fast on startup:** If `LLM_PROVIDER="openai"` and `OPENAI_API_KEY` is missing or empty, application startup in lifespan raises a clear `ValueError`, halting startup immediately. No placeholder keys or test fallbacks are allowed in production mode.
- **Testing mode (`fake`):**
  - Uses a deterministic, rule-based `FakeIntentParser`.
  - Logs a prominent `WARNING` at startup indicating mock operation.
  - Used for local development and CI test suites without network dependencies or API costs.
- **Single Lifespan Instance:** The adapter is created exactly once during application startup and attached to `app.state.intent_parser`, injected via `api/dependencies.py`. Clients are never instantiated per request.

### 5. Three Distinct Failure Categories and Domain Exceptions

The adapter never swallows external errors or catches broad `Exception`s. Errors are strictly categorized into three operational outcomes:

1. **`LLMConfigurationError` (System Configuration & Credentials):**
   - **Causes:** Invalid API keys (401 `AuthenticationError`), permission denied (403 `PermissionDeniedError`), non-existent model name (404 `NotFoundError`), or malformed API request (400 `BadRequestError`, 422 `UnprocessableEntityError`).
   - **Handling:** Wrapped into domain `LLMConfigurationError from exc`.
   - **Outcome:** System-level defect. Message status is marked as `failed` in `processed_messages`, HTTP 503 is returned to the webhook channel, and the event is logged at `ERROR` level to trigger operational alerts. Retries will not succeed until configuration is corrected.
2. **`LLMUnavailableError` (Transient Provider Outage):**
   - **Causes:** Network dropped, timeout exceeded, rate limit hit (429), or upstream HTTP 5xx returned by OpenAI (`openai.APIConnectionError`, `openai.RateLimitError`, `openai.APITimeoutError`, `openai.InternalServerError`).
   - **Handling:** Wrapped into domain `LLMUnavailableError from exc`.
   - **Outcome:** Message status is marked as `failed` in `processed_messages`, HTTP 503 is returned to the channel, and the incoming webhook channel retries delivery with exponential backoff. Logged at `WARNING` level.
3. **`LLMInvalidOutputError` (Unparseable, Refused, or Filtered Output):**
   - **Causes:** Model refusal (`choice.message.refusal`), output truncation (`LengthFinishReasonError`), content filter activation (`ContentFilterFinishReasonError`), or schema/domain validation failure (`pydantic.ValidationError`, `ValueError` during date/time conversion).
   - **Handling:** Wrapped into domain `LLMInvalidOutputError from exc`.
   - **Outcome:** Retrying the exact same message would produce an identical refusal or validation failure. The message is marked as `done` in `processed_messages` (non-retryable), and a friendly conversational clarification prompt asks the customer to rephrase.
4. **Logging Discipline:** The error is logged **once**, in the layer that handles it (e.g. router/worker), with `exc_info=True`. Adapters do not log and re-raise.
5. **Connection hygiene:** LLM calls are strictly forbidden while holding database transactions, table/row locks, or pooled database connections.

### 6. Data Privacy, Prompt Versioning, and Opt-in Evaluation

- **Data Privacy:** Only the raw user message text is sent to the LLM. Customer phone numbers, database UUIDs, or past appointment history are **never** included in prompts.
- **Versioned Prompts & Injection Defense:** Prompts are stored in versioned files (`src/booking_agent/integrations/llm/prompts/v1.txt`). User input is enclosed in structural delimiters (`<user_message>...</user_message>`). OpenAI Structured Outputs (`response_format=_LLMBookingIntentSchema`) with `additionalProperties: false` guarantees the syntactic JSON schema structure. However, field values can still be manipulated by adversarial prompt injections; therefore, all extracted entities are validated deterministically by the service layer against the catalog and operational constraints. The LLM has zero execution privileges (no tool calling, no APIs, no database access).
- **Evaluation Dataset (`tests/evals/intents.jsonl`):** 30 to 50 realistic Portuguese test cases representing Brazilian barbershop interactions, supporting lists of valid alternative phrases for free-text fields.
- **Telemetry & Normalization:** The evaluation runner (`tests/evals/run_eval.py`) strips case and accents (Unicode NFKD normalization) for string comparisons and logs model, prompt version, timestamp, field accuracy, latency, and token consumption.
- **CI Isolation:** Tests marked with `@pytest.mark.eval` are excluded by default in `pyproject.toml` (`addopts = "--strict-markers -ra -m 'not eval'"`). CI never calls paid external APIs.

## Consequences

**Positive**
- **Eliminates timezone hallucination:** The LLM extracts wall-clock values matching customer speech; deterministic code handles UTC conversions and DST safety.
- **Fail-fast operational safety:** Misconfigured API keys abort startup immediately rather than failing during customer interactions.
- **Zero test cost and zero CI flakiness:** CI passes in milliseconds without external network dependencies or API token expenses.
- **Clean error taxonomy:** Transient outages trigger retries with 503s; model refusals trigger conversational re-prompting without retries.
- **Data privacy guaranteed:** Zero PII or database identifiers are exposed to external LLM providers.
- **Measurable extraction quality:** The evaluation harness provides objective metrics before promoting prompt or model revisions.

**Negative**
- **Two-phase timestamp resolution:** Timezone-aware datetimes require combining `local_date` and `local_time` through `localize_datetime` in the service layer rather than receiving a unified datetime directly from the parser.
- **Dataset maintenance:** Expanding catalog services or supporting new regional slang requires curating and updating test cases in `intents.jsonl`.
