---
name: llm-integration
description: Rules for integrating LLMs (OpenAI, Agents SDK, structured outputs) into deterministic applications. Use this skill whenever you add or change an LLM adapter, a prompt, a structured-output schema, an extraction step, a natural-language query feature, an evaluation set, or any code path where model output influences what the system does — even if the request only says "use AI to understand the message".
---

# LLM integration

The model is a parser, not a decision maker. It turns free text into a validated,
closed structure. Every decision and every write happens in deterministic code that
can be unit-tested without the model.

## Architecture

- Put the model behind a port (a `Protocol` in the domain, e.g. `IntentParser`,
  `LLMAdapterPort`). Services depend on the port, never on a vendor SDK.
- The adapter is created once at application startup and injected; never instantiate
  a client per request or as a default argument.
- Output goes into a Pydantic model with `extra="forbid"`, enums for closed choices,
  `AwareDatetime` for times and strict lengths. Invalid output is a handled failure,
  not something to "fix up" silently.
- The model never: writes SQL, chooses which action runs without validation, calls
  tools with side effects, or produces the final user-facing text unless that is an
  explicit, reviewed design choice. Replies come from templates.
- For natural-language questions over data, the model selects from an allow-list of
  metrics, dimensions and filters. Deterministic code builds the query and runs it
  with a read-only role.

## Providers and configuration

- Provider is explicit configuration, e.g. `LLM_PROVIDER=openai|fake`, defaulting to
  the real provider.
- In real-provider mode, a missing API key fails at startup with a clear message. No
  dummy keys as defaults.
- The fake provider is deterministic (keyword rules mapping to real enum values),
  logs a WARNING at startup, and is documented as a local demo/testing mode.
- Model name, timeout, max retries and temperature come from settings.

## Failure handling

- Set explicit timeouts and bounded retries on the client.
- Never catch provider errors and return an empty result. Wrap them in a domain
  exception (`LLMExtractionError(...) from exc`) so the caller can mark the work as
  failed (retryable) and return a generic 503.
- Never call the model while holding a database transaction, a row lock or a pooled
  connection.
- Distinguish "model is unavailable" (retry later) from "model understood nothing"
  (ask the user a clarifying question).

## Prompts

- Prompts are versioned files or constants with a version identifier, not strings
  scattered across the code.
- State the output contract, the allowed values and how to express "unknown". Ask
  for `null`/`unknown` instead of guesses.
- Give the current date and business timezone explicitly when relative dates matter
  ("tomorrow at 3pm"); never let the model assume them.
- Keep user text clearly delimited and treat it as data. Assume it may contain
  instructions (prompt injection); the closed schema and deterministic services are
  the defence.

## Evaluation

- Maintain a small evaluation set (start with 30–50 realistic messages, including
  typos, slang, relative dates, mixed intents and injection attempts) with expected
  structured outputs.
- Run it with a script that reports accuracy per field and lists failures. Record the
  baseline in the README or docs and re-run it whenever the prompt, schema or model
  changes.
- Evaluation runs against the real provider are opt-in (marked tests or a separate
  command) so CI does not depend on a paid external API; CI uses the fake provider
  and contract tests.

## Cost and privacy

- Log model name, latency and token usage per call (not the message content).
- Send the minimum personal data needed; mask or omit identifiers in prompts.
- Document the provider's data handling and any data sent in the README.

## Tests that must exist

- Contract tests: the adapter maps a recorded provider response to the domain model.
- Invalid or extra fields from the model are rejected.
- Provider failure → domain exception → work marked failed and 503 (no silent
  success, no empty result).
- Missing key in real-provider mode fails at startup.
- Services are tested with the fake/port implementation only, without network.
