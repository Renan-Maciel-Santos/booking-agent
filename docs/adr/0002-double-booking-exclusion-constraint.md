# ADR 0002: Double-booking protection via PostgreSQL exclusion constraint

- **Status:** Accepted
- **Date:** 2026-10-18
- **Deciders:** Renan Maciel

## Context

In an appointment booking system for a service business like Northside Barbers, multiple clients may interact with the booking agent at the same time. A customer might attempt to book an appointment for the same barber and time window that another customer is concurrently booking.

Double booking (allocating the same barber to two overlapping appointments at the same time) damages business reputation, disrupts operations, and produces customer frustration.

Traditional application-level concurrency control relies on checking for conflicts (`SELECT ... WHERE overlaps`) before issuing an `INSERT`. Under concurrent execution, two transactions executing the check simultaneously will both observe the slot as available and both execute their `INSERT`, resulting in double booking (Time-of-Check to Time-of-Use race condition).

We need a concurrency control mechanism that guarantees zero overlapping appointments per barber, supports services with differing durations, permits adjacent (back-to-back) appointments, and preserves high concurrency.

## Decision

We enforce double-booking protection directly at the database level using a PostgreSQL **exclusion constraint** backed by a Generalized Search Tree (`GiST`) index with the `btree_gist` extension:

```sql
ALTER TABLE appointments
    ADD CONSTRAINT appointments_barber_slot_excl
    EXCLUDE USING gist (
        barber_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    )
    WHERE (status = 'confirmed');
```

Key aspects of this design:

### 1. Database-level enforcement vs. Application-level checks
The database is the ultimate authority and last line of defence. Application-level checks alone cannot prevent race conditions without coarse-grained locking. By enforcing mutual exclusion at the database engine level, PostgreSQL evaluates the constraint atomically within the transaction commit/insert boundary, ensuring that only one concurrent transaction succeeds.

### 2. Half-open intervals: `'[)'`
We model appointment slots using half-open timestamp ranges `tstzrange(starts_at, ends_at, '[)')`:
- `[` indicates the start time is **inclusive**.
- `)` indicates the end time is **exclusive**.

Under range overlap semantics (`&&`), two ranges overlap if and only if they share at least one point in time. Because the upper bound is exclusive, adjacent intervals do not share any points:
- Slot A: `[10:00, 10:30)`
- Slot B: `[10:30, 11:00)`

Slot A and Slot B share boundary point `10:30`, but since Slot A excludes `10:30` and Slot B includes `10:30`, their intersection is empty (`&&` returns `false`). This permits back-to-back appointments without false conflict errors while strictly forbidding true overlaps (such as `[10:00, 10:30)` and `[10:15, 10:45)`).

### 3. Partial constraint (`WHERE status = 'confirmed'`)
The exclusion constraint only applies to active, confirmed appointments (`WHERE (status = 'confirmed')`). When an appointment is cancelled (`status = 'cancelled'`), it ceases to satisfy the partial index predicate, instantly releasing the time slot for new bookings without deleting historical audit records.

### 4. `btree_gist` extension and privilege requirements
By default, PostgreSQL's built-in `gist` index operator classes support range and geometric types, but not scalar equality (`WITH =`) on types such as `uuid` or `integer`.

The PostgreSQL contrib extension `btree_gist` provides B-tree equivalent indexing behavior within GiST, allowing composite exclusion constraints combining scalar equality (`barber_id WITH =`) and range overlap (`tstzrange WITH &&`).

- **Privilege impact:** Since PostgreSQL 13, `btree_gist` is marked as a *trusted* extension. It is pre-installed in standard PostgreSQL distributions and supported out-of-the-box by cloud managed providers (AWS RDS, GCP Cloud SQL, Supabase, Azure Database for PostgreSQL). Because it is trusted, the database owner can create the extension directly (`CREATE EXTENSION IF NOT EXISTS btree_gist;`) without requiring superuser privileges.

### 5. UTC timestamps and business timezone separation
All appointment boundaries (`starts_at`, `ends_at`) are stored as `timestamptz` in UTC. Range comparisons within PostgreSQL are executed strictly on UTC timestamps. Timezone transformations for business hours, local customer displays, and operational rules remain the responsibility of the service layer.

### 6. Domain error translation (SQLSTATE `23P01`)
When an insertion or update violates the exclusion constraint, PostgreSQL aborts the statement with error code `23P01` (`exclusion_violation`).

In `AppointmentRepository`, this specific error is caught from SQLAlchemy's `IntegrityError` (checking `orig.pgcode == "23P01"`) and translated into a domain exception (`SlotUnavailableError`), chaining the original exception (`raise SlotUnavailableError(...) from exc`). No generic error swallowing or unhandled database leakage occurs.

## Alternatives Considered

1. **`SELECT ... FOR UPDATE` (Pessimistic Row Locking):**
   - *Why rejected:* You cannot lock a row that does not exist yet. Available time slots are empty gaps between existing bookings. To lock a non-existent slot, one would have to lock the entire `barbers` or `appointments` table (or an artificial slot grid table), creating severe transaction contention and destroying concurrency for unrelated time slots.

2. **`UNIQUE (barber_id, starts_at)`:**
   - *Why rejected:* Only works if all appointments have fixed, identical durations (e.g. 30 minutes) on rigid slot boundaries. If Service A takes 45 minutes and starts at 10:00, it ends at 10:45. A unique constraint on `starts_at` would permit Service B to start at 10:30, creating an undetected 15-minute overlap.

3. **Application-level read-before-write:**
   - *Why rejected:* Inherently unsafe under concurrent requests due to race conditions (TOCTOU).

4. **Serializable Isolation Level (`SERIALIZABLE`):**
   - *Why rejected:* While serializable transactions detect read-write conflicts (serialization failures `40001`), they require application-wide retry loops for all transactions, introduce unpredictability under load, and add significant overhead compared to target-specific exclusion constraints.

## Consequences

### Positive
- **Guarantees zero double bookings:** Concurrent booking attempts for the same barber and overlapping times are rejected by PostgreSQL.
- **Variable-duration support:** Services with varying lengths (e.g., 20m, 45m, 60m) are natively and accurately protected.
- **Back-to-back efficiency:** Half-open interval semantics allow seamless adjacent scheduling without artificial buffers.
- **Audit preservation:** Cancelled appointments remain in the table for history without blocking future slots.

### Negative
- **Dependency on `btree_gist`:** Migrations must install the `btree_gist` extension.
- **Index maintenance overhead:** GiST indexes have slightly higher update overhead and disk footprint than simple B-tree indexes, which is negligible for the expected appointment volume of a barbershop.
