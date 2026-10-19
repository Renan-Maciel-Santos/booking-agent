---
name: testing-strategy
description: How to design tests that actually prove behaviour in Python/pytest projects with PostgreSQL. Use this skill whenever you write, change, fix or review tests, fixtures, mocks or fakes, CI test steps, or whenever a task says "add tests", "make the tests pass" or "fix the failing test" — and before claiming any behaviour is verified.
---

# Testing strategy

A green test suite only matters if each test would turn red when the behaviour it
names breaks. Optimize for tests that fail for the right reason.

## Choosing the test level

- **Unit tests**: domain rules and services, with fakes that implement the project's
  Protocols (in-memory repositories, fixed clock, fake LLM). No network, no database,
  milliseconds each.
- **Integration tests**: anything whose correctness depends on the database or the
  framework — constraints, transactions, locks, migrations, RLS, permissions, SQL,
  dependency wiring. Use a real PostgreSQL (testcontainers or the CI service). A mock
  cannot prove a lock, a constraint or a permission.
- **End-to-end checks**: the Quick Start and the main user flow, run exactly as
  documented.

## Fakes over mocks

- Prefer small fakes that implement a Protocol over `MagicMock` patched into internal
  attributes. Fakes keep tests readable and survive refactors.
- Never add test-only branches, flags or hooks to production code. If code is hard to
  test, inject the dependency (clock, unit of work, adapter) instead.
- Time is injected (`Clock`), never patched globally.

## Prove each test can fail

- After writing a test, break the behaviour on purpose (remove a constraint, invert a
  condition, reintroduce a bug) and confirm the test fails with a clear message. Undo
  the break; never commit it.
- A test that only checks call order on mocks does not prove locking, isolation or
  transactional behaviour.
- Never change a correct test to make wrong code pass. If a test and the code
  disagree, decide which one encodes the intended behaviour and report it.

## Concurrency

- Make concurrency deterministic: open transaction A and leave it uncommitted, start
  B as a task, assert B is still pending after a short timeout, then commit or roll
  back A and assert B's outcome.
- Use short timeouts everywhere so a defect fails fast instead of hanging.

## Isolation and order independence

- Each test creates the data it needs and does not modify data owned by other tests.
- Reset state per test (e.g. a fixture that truncates domain tables) instead of
  global updates inside tests.
- Tests never depend on seed/demo data.
- Run the suite twice in a row and in random order (e.g. pytest-randomly with a
  fixed seed) before declaring it stable.

## Integration tests are never silently skipped

- If Docker or the database is unavailable, integration tests fail, they do not
  skip. Any skip must be explicit and reported.
- Verify that fixtures actually run: a misnamed fixture can make a whole suite error
  out or never execute. Report how many integration tests actually ran.
- CI runs the same commands as local development, with the same environment variables
  documented in the README.

## What every change should include

- New behaviour: at least one test for the happy path and the most important failure
  path.
- Bug fix: a test that reproduces the bug first, then the fix.
- Security-relevant change: the negative tests from the secure-backend skill.
- Schema change: the migration cycle and model/migration sync tests stay green.

## Reporting results

Report totals per level (unit, integration), how many integration tests ran against
a real database, any skipped test with its reason, and the literal output of any
failure.
