# AGENTS.md: conventions for AI-assisted sessions

Read this before changing the code. Everything published (code, docs, commits)
is written in **English**.

## Architecture (non-negotiable)

- **LLM at the edge only.** The LLM returns a validated `BookingIntent` and nothing
  else: no tools, no database access, no reply text that bypasses the service.
  See `docs/adr/0001-llm-only-at-the-edge.md`.
- Layers: `api` (routers, no business rules) → `services` (rules) →
  `repositories` (the only DB access, parameterized queries) → `database`.
- Ports (`Protocol`s) live in `domain/ports.py`; adapters live in `integrations/`.
  Services depend on ports, never on adapters.
- DI through constructors, wired in `api/dependencies.py` with `Depends`.
- Configuration only through env vars via `config.Settings`. No hardcoded values.
- The engine is created in the app lifespan (`main.create_app`) and stored on
  `app.state`. Never create engines at import time.
- Database integrity (FKs, constraints, exclusion constraints) is the last line of
  defence. Do not rely on application checks alone.

## Code style

- Python 3.12, PEP 8, full type hints, `mypy --strict` must pass.
- Google-style docstrings on modules, classes and public functions.
- **Comments explain *why*, never *what*.** Only comment non-obvious decisions,
  trade-offs and gotchas; the code and docstrings must explain the rest. Place
  comments above the line they refer to, never inline at the end of a line. No
  line-by-line comments.
- Use the logger (`logging.getLogger(__name__)`), never `print`. JSON logging is
  configured only in `logging_config.py`.
- Catch specific exceptions only. A bare or broad `except` is forbidden.
- Descriptive names, small functions, named constants instead of magic numbers.
- Secrets use `SecretStr` and must never appear in `repr`, logs or `model_dump`.

## Testing

- `tests/unit/`: no I/O, fakes from `tests/fakes/`. Markers are applied by folder.
- `tests/integration/`: real PostgreSQL via testcontainers. These tests fail (never
  skip) without Docker.
- Every new service rule needs unit tests. Every new repository needs
  integration tests.
- Arrange / Act / Assert, with descriptive test names.

## Workflow

- Run before every commit: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.
- Use Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`, `chore:`,
  `refactor:`, `test:`, `build:`) and keep commits small.
- Record architectural changes as a new ADR in `docs/adr/`.
- Never create `.env` or commit secrets. Only `.env.example` is versioned.
- Do not push. Propose a plan first and wait for approval on non-trivial changes.

## Process and quality rules

- Every commit must pass pre-commit (`uv run pre-commit run --all-files`) and unit tests (`uv run pytest -m unit`).
- Integration tests run against real PostgreSQL (via testcontainers) and are never silently skipped.
- Never alter a correct test to make incorrect code pass.
- Never leave test values or placeholder credentials as defaults in production code.
- Never swallow errors from external services: propagate and handle them explicitly.
- No test-specific logic or bypasses inside production code.
- Never commit agent log files, status files, scratch files, or session artifacts to the repository.
