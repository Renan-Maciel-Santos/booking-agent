---
name: observability
description: Logging, health checks, metrics and error-reporting rules for FastAPI services and data pipelines. Use this skill whenever you add or change logging, health or readiness endpoints, request handling middleware, metrics, background jobs, pipeline runs, error handlers or deployment configuration — and when a task mentions monitoring, debugging production, or "why did this fail".
---

# Observability

Someone who did not write the code should be able to answer "is it working, and if
not, why?" from logs, health endpoints and metrics alone, without reading the code.

## Structured logging

- Log JSON with stable field names: timestamp, level, logger, message, and context
  passed via `extra`.
- One logging configuration module, set up once at startup (never at import time).
- Use lazy formatting (`logger.info("... %s", value)`), never f-strings in log calls.
- Log events, not noise: one line per significant step (request handled, external
  call finished, job completed) with outcome and duration.
- Never log secrets, API keys, full phone numbers, e-mails or message content. Mask or
  hash identifiers.
- Errors are logged once, at the boundary that handles them, with `exc_info=True`.

## Request correlation

- Every request gets a request id (accept an incoming `X-Request-ID` if valid,
  otherwise generate one), returned in the response header and attached to every log
  line of that request (e.g. via a contextvar).
- Background jobs and pipeline runs get a run id with the same treatment.

## Health checks

- Liveness (`/health/live`): the process can answer. Does not touch dependencies, so
  an outage of the database does not cause restart loops.
- Readiness (`/health/ready`): dependencies needed to serve traffic answer within a
  short timeout. Returns 503 with a generic body when they do not.
- Never expose internal details (versions, hostnames, error messages) in health
  responses.

## Metrics

- Track the four golden signals per endpoint or job: request rate, error rate,
  latency (histogram, not average) and saturation (e.g. pool usage).
- Add domain metrics that matter to the business: bookings created, conflicts,
  LLM failures, pipeline rows loaded, data quality test failures.
- Label cardinality stays low: never use user ids, phone numbers or free text as
  metric labels.

## External calls and jobs

- Log provider, duration, outcome and retry count for every external call (LLM, HTTP,
  database administration).
- Pipelines record each run in a runs table (start, end, status, rows read and
  written, error) and fail loudly; a scheduled run that fails must be visible (failed
  CI job, alert), never silently ignored.

## Error responses

- Map domain errors to HTTP status codes in one place (exception handlers).
- Client-facing bodies are generic and include the request id so a user report can be
  traced to the logs.

## Documentation

- The README explains how to read the logs, which health endpoints exist and what each
  metric means.
