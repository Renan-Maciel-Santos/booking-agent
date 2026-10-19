# ADR 0003: Time handling, injected clock, and in-place rescheduling

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** Renan Maciel

## Context

Northside Barbers operates in a physical location with local business hours, serving customers in a specific timezone (e.g. `America/Sao_Paulo`). However, scheduling appointments requires precise time handling across multiple architectural layers:

1. **Storage vs. Presentation:** Storing timestamps with local offsets in the database complicates interval indexing, sorting, and cross-timezone consistency. PostgreSQL provides robust support for UTC timestamps with `timestamptz`.
2. **Daylight Saving Time (DST) Transitions:** Wall-clock times during DST transitions can either be non-existent (spring-forward gap) or ambiguous (fall-back fold). Handling these anomalies deterministically is essential to avoid ghost slots or double bookings.
3. **Deterministic Testing:** Time-sensitive business rules (such as rejecting slots in the past and enforcing minimum advance notice) must be tested thoroughly without reliance on wall-clock time, system sleeps, or global monkeypatching.
4. **Rescheduling Overlapping Slots:** Customers frequently request to shift appointments slightly earlier or later (e.g. moving a 10:00–10:45 appointment to 10:15–11:00). The new interval `[10:15, 11:00)` partially overlaps with the current interval `[10:00, 10:45)`. If rescheduling is implemented as a delete-and-reinsert or cancel-and-recreate, the system risks either self-collision against its own active appointment or creating race conditions and dangling audit records.
5. **Aborted Database Transactions:** PostgreSQL enforces mutual exclusion via the `appointments_barber_slot_excl` constraint. When a conflict occurs, PostgreSQL raises error code `23P01` (`exclusion_violation`) and transitions the database transaction into an aborted state (`InFailedSqlTransaction`). Any subsequent query executed on the same session will immediately fail unless the transaction is rolled back first.

## Decision

We establish four core architectural decisions for time handling, testing, rescheduling, and conflict recovery:

### 1. UTC in Persistence, Timezone Isolation at Service Boundary
- **PostgreSQL Layer:** All appointment boundaries (`starts_at`, `ends_at`) and audit timestamps (`created_at`, `updated_at`) are stored as `timestamptz` in UTC. Database queries and the GiST exclusion constraint operate exclusively on UTC ranges.
- **Service Layer Boundary:** Local time conversions are strictly encapsulated within `BookingService`. The business timezone (e.g. `America/Sao_Paulo`), slot granularity (e.g. 15 minutes), minimum advance notice (e.g. 60 minutes), and maximum booking horizon (e.g. 30 days) are injected from configuration (`Settings`).
- **Strict Datetime Inputs:** Public service operations accept ONLY timezone-aware `datetime` objects.
- **Single Localization Function:** A dedicated helper (`localize_datetime`) converts raw local date and time from user dialogues into a timezone-aware `datetime` in the barbershop's timezone using Python's standard `zoneinfo.ZoneInfo`:
  - **Non-existent times (Spring-Forward Gap):** When clocks skip forward (e.g. 02:00 -> 03:00), local times within the skipped interval do not exist. The helper detects non-existent times and raises `InvalidLocalTimeError`.
  - **Ambiguous times (Fall-Back Fold):** When clocks repeat an interval (e.g. 01:00 -> 02:00 occurs twice), Python provides the `fold` attribute. The helper resolves ambiguous times deterministically using `fold=0` (standard/pre-transition occurrence).
  - **Note on Brazil:** While Brazil has not observed DST since 2019, this handling ensures the codebase remains robust and correct across any configured business timezone.
- **Validation in Business Local Time:** All business validations (day of the week, business opening/closing hours, max booking horizon) are evaluated after converting the input timestamp into the business local timezone (e.g., UTC 22:00 corresponds to 19:00 in São Paulo on that local date).

### 2. Injected `Clock` Port for Deterministic Testing
`BookingService` never calls `datetime.now()` or `datetime.utcnow()` directly. Instead, it receives an injected `Clock` protocol:

```python
class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware datetime in UTC."""
        ...
```

- **Production:** Uses `SystemClock`, which calls `datetime.now(timezone.utc)`.
- **Testing:** Unit tests inject a `FixedClock` or `FrozenClock` with controllable UTC timestamps.
- **Rules Governed by Clock:**
  - Rejecting bookings with `starts_at <= clock.now()` (`SlotInThePastError`).
  - Rejecting bookings with `starts_at < clock.now() + timedelta(minutes=min_advance_notice)` (`MinimumNoticeViolationError`).
  - Rejecting bookings beyond `clock.now().astimezone(business_tz).date() + timedelta(days=max_booking_horizon_days)` (`InvalidTimeWindowError`).
  - Disallowing cancellation or rescheduling of appointments whose start time has already passed (`AppointmentInThePastError`).

### 3. In-Place `UPDATE` for Rescheduling
Rescheduling is executed via an in-place SQL `UPDATE` of `starts_at` and `ends_at` on the existing appointment row, rather than cancelling and creating a new record:

```sql
UPDATE appointments
SET starts_at = :new_starts_at,
    ends_at = :new_ends_at,
    updated_at = now()
WHERE id = :appointment_id
RETURNING *;
```

- **Mechanics of PostgreSQL Exclusion Constraints:** PostgreSQL evaluates exclusion constraints on `UPDATE` by checking the updated tuple against all *other* matching rows in the table. The row being updated is not compared against itself.
- **Self-Overlap Safety:** Shifting an appointment by 15 minutes (e.g. from `[10:00, 10:45)` to `[10:15, 11:00)`) succeeds without conflict, because PostgreSQL checks whether any *other* confirmed appointment overlaps with `[10:15, 11:00)`.
- **Audit & Identity Preservation:** The appointment retains its original `id`, `customer_id`, `created_at`, and frozen `price_cents`. No spurious cancellation metrics or ghost appointments are created.

### 4. Conflict Recovery via Unit of Work Rollback and Alternative Search
When a concurrent booking or rescheduling attempt triggers a PostgreSQL exclusion constraint violation (`23P01`), `AppointmentRepository` translates it into `SlotUnavailableError`.

- Because PostgreSQL aborts the entire transaction block upon an exclusion violation, the active database session enters an aborted state.
- `BookingService` does not directly call `session.rollback()`. Instead, the exception exits the unit of work context manager block, which rolls back and closes the aborted session automatically on exit.
- In the `except SlotUnavailableError as exc:` block, `BookingService` opens a **new** unit of work to query alternative open slots via `list_availability`.
- `BookingService` raises a new domain `SlotUnavailableError(..., alternative_slots=alternatives) from exc`, chaining to the original repository exception.
- Note: If no alternate slots are free within the schedule, the returned alternatives list will simply be empty.

## Alternatives Considered

1. **`timestamp without time zone` in Database:**
   - *Why rejected:* Storing local timestamps without timezone offsets makes time arithmetic, interval indexing, and daylight saving comparisons ambiguous and fragile.
2. **Monkeypatching System Time (e.g. `freezegun` or `unittest.mock`):**
   - *Why rejected:* Mocking global time functions introduces side effects across asynchronous tasks, leaks into concurrent test runs, and obscures dependency contracts. An injected `Clock` protocol is explicit, type-safe, and zero-overhead.
3. **Rescheduling via Cancel + Create:**
   - *Why rejected:* If a customer reschedules from 10:00 to 10:15, creating the new appointment first fails due to the exclusion constraint overlapping with the old slot. Cancelling the old appointment first leaves the system vulnerable to losing the original slot if the subsequent creation fails. Furthermore, it generates artificial cancellation records that distort business analytics.
4. **Querying Alternatives within the Aborted Transaction:**
   - *Why rejected:* PostgreSQL raises `InFailedSqlTransaction` (`current transaction is aborted, commands ignored until end of transaction block`) on any query issued after an exclusion error until an explicit rollback occurs. Managing this via unit of work exit guarantees clean transaction boundaries.

## Consequences

### Positive

- **Concurrency Safety:** Rescheduling avoids false-positive exclusion collisions while maintaining database-level protection against overlapping with other bookings.
- **Deterministic, Reliable Tests:** All time-based business rules (past checks, lead time, horizon, slot availability) are tested deterministically using a fixed clock without monkeypatching.
- **DST Resilience:** Explicit conversion rules prevent ghost bookings during spring-forward gaps and disambiguate fall-back periods.
- **Clean Error Recovery:** Clean unit of work exit semantics prevent leaked aborted transactions and allow alternative slot recommendations to execute safely in a fresh session.

### Negative

- **Protocol Overhead:** Requires defining and injecting the `Clock` port throughout the service construction graph (`api/dependencies.py`).
- **Timezone Discipline:** Developers must always ensure timestamps passed into domain logic are timezone-aware and converted to UTC before persistence.
