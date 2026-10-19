---
name: data-architecture
description: PostgreSQL data modeling, migrations, transactions and data-pipeline design rules. Use this skill whenever you design or change tables, constraints, indexes, migrations, repositories, transaction boundaries, seeds or test data, or build ingestion/ETL/dbt pipelines and analytical models — including when a task only says "add a field", "store this" or "load this data".
---

# Data architecture

The database is the last line of defence for data integrity. Rules that the database
can enforce belong in the database; the application adds rules the database cannot
express and documents them as service-layer invariants.

## Workflow

1. Design before code: update the data model section of the design doc (tables,
   columns, types, constraints, indexes, ownership of each invariant). Big decisions
   get an ADR with context, decision, alternatives and consequences.
2. Stop for review before writing migrations when the change is structural.
3. Implement migration, models and repositories together, with integration tests
   against a real PostgreSQL.

## Modeling

- Start from the questions the data must answer and the invariants it must keep.
- Normalize transactional data. Denormalize only for a measured reason, and say so.
- Types: `timestamptz` stored in UTC; money as integer cents (or `numeric`), never
  float; `uuid` keys; `text` with `CHECK` constraints instead of arbitrary
  `varchar(n)`; enums as `text` + `CHECK`, mirrored by a `StrEnum` in the domain.
- Every column's nullability is a decision. If a value is unknown at creation time,
  either allow null and document why, or create the row later (e.g. create the
  customer only when the name is known).
- One representation per concept. If "closed day" can be both a flag and a missing
  row, pick one.
- Snapshot values that must not change retroactively (e.g. price at booking time).
- Associations without a foreign key (e.g. state keyed by phone before a customer
  exists) are allowed but must be explicit in the diagram and the text.

## Constraints and indexes

- Use `NOT NULL`, `CHECK`, `UNIQUE`, `FOREIGN KEY` (with an explicit `ON DELETE`) and
  `EXCLUDE` constraints for invariants. Exclusion constraints with half-open ranges
  `'[)'` prevent overlapping intervals under concurrency.
- Follow a single naming convention (defined on the SQLAlchemy `MetaData`); never
  hand-name objects with a different pattern.
- Add an index only for a concrete query. Verify with `EXPLAIN (ANALYZE, BUFFERS)` on
  realistic volume; with tiny tables the planner may correctly choose a sequential
  scan, so do not claim an index "is used" without evidence.
- No indexes on low-cardinality booleans by default.

## Migrations

- Migrations contain structure only. Demo or initial data lives in seed files, never
  in migrations.
- Every migration has a working downgrade. A test runs upgrade head → downgrade base
  → upgrade head on an empty database.
- A test runs Alembic's `check` so models and migrations cannot drift.
- Never edit a migration that was merged or applied to a real database; add a new
  one. Consolidating into a baseline is acceptable only before any real database
  exists, and must be documented.
- Extensions (e.g. `btree_gist`) are created in migrations; document the privileges
  they require.

## Transactions and concurrency

- Repositories never commit. The caller owns the transaction (unit of work).
- Keep transactions short. Never call an LLM, HTTP API or other slow external service
  while holding a transaction, a row lock or a pooled connection.
- `now()` returns the transaction start time; use `clock_timestamp()` only when the
  real time is needed. Repositories set `updated_at = now()` on every update.
- Prefer atomic statements over check-then-act: `INSERT ... ON CONFLICT ... RETURNING`
  for idempotency, constraints for uniqueness and overlap, `SELECT ... FOR UPDATE`
  only on rows that exist.
- After a constraint violation, the PostgreSQL transaction is aborted. Let the unit
  of work roll it back and run any follow-up queries in a new unit of work.
- Concurrency tests must be deterministic: hold transaction A open, start B, assert
  that B is blocked, then commit or roll back A and assert B's outcome. Never rely
  on `asyncio.gather` timing alone.

## Test data

- Each integration test creates and owns its data. Tests never modify data created
  by other tests, and pass in any order (run them twice and in random order).
- Tests never depend on seed data.
- Prove each test can fail: temporarily break the invariant and confirm the test
  catches it.

## Data pipelines and analytics

- Layers: raw (exactly as received, with source, load timestamp and content hash),
  staging (typed, cleaned, deduplicated), marts (modeled for questions).
- Declare the grain of every fact table before defining columns.
- Loads are incremental and idempotent: re-running a load produces the same result.
  Upsert by natural key; skip unchanged content by hash.
- Sources get revised. Reload a window of recent periods on every run instead of only
  fetching "new" data, and document the window size.
- Record every run in a runs table: start, end, status, rows read and written, error.
- Data quality tests: not null, unique, accepted values, relationships, freshness and
  volume anomalies (a load far smaller than usual must fail or alert).
- Transformations are versioned SQL (dbt), tested in CI, with generated lineage docs.
- Cite data sources and licenses (e.g. ODbL attribution) in the README.

## Documentation checklist

- Data model diagram matches the migrations exactly.
- Every invariant states who guarantees it: database constraint or service layer.
- Known limitations and out-of-scope cases are written down.
