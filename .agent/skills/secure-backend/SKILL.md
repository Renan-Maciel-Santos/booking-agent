---
name: secure-backend
description: Security rules and review checklist for Python/FastAPI/PostgreSQL backends and LLM integrations. Use this skill whenever you create or change an endpoint, authentication or authorization, database access, migrations, configuration or secrets, webhooks, logging, HTML rendering, dependencies, or anything that sends user input to an LLM — and before opening any pull request, even if the task does not mention security.
---

# Secure backend

Security here is not a final review step. It is a set of defaults that every change
follows, plus negative tests that prove the defaults hold. When a rule below conflicts
with a quick fix, the rule wins; explain the trade-off instead of silently relaxing it.

## How to use this skill

1. Before writing code, list in one or two sentences what the change exposes: new
   inputs, new data read or written, new external calls, who can trigger it.
2. Apply the rules of every section that matches the change.
3. Write the negative tests listed in "Tests that must exist".
4. Add a "Security considerations" section to the PR description: what was exposed,
   which rules applied, which tests prove them, and any accepted risk.

## Input validation

- Validate every external input with Pydantic models. Use `extra="forbid"` so unknown
  fields are rejected, not silently dropped.
- Accept only timezone-aware datetimes (`AwareDatetime`) where time matters.
- Constrain strings (length, pattern such as E.164 for phones) and numbers (ranges).
- Treat LLM output exactly like user input: validate it against a closed schema.

## Authentication and authorization

- Credentials are never stored in plain text: store a hash of API keys (SHA-256 is
  enough for high-entropy random keys; passwords need a slow hash such as Argon2).
- Show a generated key once; never log it.
- The caller's identity and tenant come from the credential, never from the URL,
  body or headers the caller controls.
- Authorize per operation with explicit scopes (e.g. `webhook:write`, `leads:read`).
  Missing credential → 401; valid credential without the scope → 403. Both with
  generic messages.
- Administrative operations (issuing keys, migrations) use a separate administrative
  identity, never the application role.

## Database access

- Only parameterized queries. Never build SQL with f-strings or concatenation. For
  identifiers (schema or table names) use `psycopg.sql.Identifier` /
  SQLAlchemy constructs, after validating against an allow-list or strict regex.
- The application role has least privilege: no superuser, no `BYPASSRLS`, no
  ownership of the schema, only the grants it needs.
- `SECURITY DEFINER` functions always set a fixed `search_path`, and their `EXECUTE`
  is revoked from `PUBLIC` and granted only to the roles that need it.
- Multi-tenancy is fail-closed: tenant context is set with `SET LOCAL` inside each
  transaction. Session-level tenant context on pooled connections is forbidden,
  because a missed reset leaks one tenant's data to the next request.

## Secrets and configuration

- All configuration comes from environment variables via a settings object.
- Secrets use `SecretStr` and never appear in `repr`, logs or error messages. URLs
  that embed passwords are built on demand and never logged.
- No default values for secrets or credentials, and no test values as defaults in
  production code. Missing required configuration fails at startup with a clear
  message.
- Only `.env.example` with placeholder values is versioned.

## Errors and logging

- Clients get generic error bodies; details go to logs with `exc_info`.
- Never swallow errors from external services (LLM, HTTP APIs, databases). Wrap them
  in a domain exception (`raise DomainError(...) from exc`) and handle them
  explicitly: mark work as failed, return 503, allow retries.
- Mask personal data in logs (phone numbers, e-mails, names). Log identifiers, not
  content.
- Use structured logging with lazy formatting (`logger.info("... %s", value)`).

## HTML and front end

- Server-side templates use autoescape. Never insert untrusted data with
  `innerHTML`; use `textContent` or DOM creation APIs.
- Pages that hold no data may be public; data endpoints require authentication.
- Store browser-side credentials in `sessionStorage` at most, and document it as a
  known limitation.

## Webhooks and external calls

- Webhooks are idempotent: record the message id atomically
  (`INSERT ... ON CONFLICT ... RETURNING`) before doing any work.
- Prefer signed webhooks (HMAC over the raw body plus a timestamp to block replays).
- Every outbound call has an explicit timeout and a bounded retry policy.
- Never hold a database lock or a pooled connection while waiting for an external
  call.

## LLM-specific rules

- The LLM only extracts structured data. It has no tools with side effects, no direct
  database access and never writes SQL.
- Assume prompt injection: user text may contain instructions. The closed output
  schema and the deterministic service layer are the defence; never let model output
  choose which action runs without validation.
- Reply text to end users comes from templates, not from free model output, unless
  explicitly designed otherwise.

## Dependencies and supply chain

- Dependencies are locked (`uv.lock`) and installed with the lock file in CI.
- Check package names carefully before installing; typosquatted names are a common
  attack. Prefer pinned versions for one-off tools (`npx package@1.2.3`).
- Never pipe remote scripts into a shell without reading them first.

## Tests that must exist

For every new or changed endpoint or data path, add tests for the negative cases:

- no credential → 401; wrong scope → 403;
- credential from tenant A cannot read or change data of tenant B;
- payloads with HTML/JS are rendered as text;
- unknown fields and invalid formats are rejected;
- external service failure produces the documented failure behaviour (no silent
  success);
- the application role cannot read tables it should not (connect as that role, not
  as a superuser, or the test proves nothing).

## Forbidden shortcuts

- Changing a correct test so that insecure code passes.
- Catching a broad `Exception` to "keep things running".
- Adding a bypass, admin flag or force option to get past a protection rule.
- Claiming a protection in the README that the code and tests do not prove.
