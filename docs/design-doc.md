# Design Doc: Booking Agent

| Field   | Value                         |
|---------|-------------------------------|
| Author  | Renan Maciel                  |
| Status  | Draft (scaffold merged)       |
| Updated | 2026-10-15                    |

## 1. Context

Small service businesses take bookings through chat apps. Staff answer by hand,
which is slow, inconsistent and error-prone (double bookings, forgotten
cancellations). This project builds a conversational booking agent for a
fictional barbershop, **Northside Barbers**, to demonstrate a production-grade
architecture for LLM-backed transactional systems.

## 2. Goals

- Let customers book, reschedule and cancel appointments in natural language.
- Guarantee that every action respects business rules, whatever the LLM outputs.
- Prevent double booking, including under concurrent requests.
- Keep every decision auditable (messages, intents and actions persisted).
- Keep the LLM provider replaceable behind a single port.

## 3. Non-goals

- Payments and deposits.
- Multi-language support (English only for now).
- A web UI or admin dashboard.
- Multi-tenancy (one business per deployment).
- Direct integration with a messaging channel; an orchestrator (e.g. n8n) calls the API.

## 4. Architecture

```mermaid
flowchart LR
    channel["Channel / n8n<br/>(WhatsApp, web chat)"] -->|HTTP| api["FastAPI<br/>routers"]
    api --> parser["IntentParser<br/>(LLM, structured output)"]
    parser -->|BookingIntent| service["BookingService<br/>(deterministic rules)"]
    api --> service
    service --> repos["Repositories<br/>(parameterized SQL)"]
    repos --> db[("PostgreSQL")]
```

Layers and responsibilities:

| Layer            | Package                      | Responsibility                                   |
|------------------|------------------------------|--------------------------------------------------|
| API              | `api/`                       | HTTP mapping, validation, status codes. No rules |
| Services         | `services/`                  | Business rules and orchestration                 |
| Domain           | `domain/`                    | Models (`BookingIntent`) and ports               |
| Repositories     | `repositories/`              | The only code that touches the database          |
| Integrations     | `integrations/llm/`          | Adapters for external providers                  |
| Infrastructure   | `database/`, `config.py`     | Engine, sessions, settings, logging              |

Dependencies point inwards: services depend on ports (`Protocol`s), never on
concrete adapters. Wiring happens in `api/dependencies.py`.

### Request flow (target)

1. The channel posts a message to the API.
2. `IntentParser` returns a validated `BookingIntent` or `UNKNOWN`.
3. `BookingService` loads `conversation_state`, merges the new entities and
   decides the next step (ask for missing data, propose slots, confirm).
4. Writes happen in a single transaction; database constraints reject conflicts.
5. The reply text is produced from templates by the service, not by the LLM.

## 5. Data model

```mermaid
erDiagram
    customers ||--o{ appointments : books
    services ||--o{ appointments : "is booked as"
    barbers ||--o{ appointments : "is assigned to"
    customers {
        uuid id PK
        text phone UK "E.164 format"
        text name
        timestamptz created_at
        timestamptz updated_at
    }
    services {
        uuid id PK
        text name UK
        int duration_minutes "> 0"
        int price_cents ">= 0"
        bool active
        timestamptz created_at
        timestamptz updated_at
    }
    barbers {
        uuid id PK
        text name
        bool active
        timestamptz created_at
        timestamptz updated_at
    }
    appointments {
        uuid id PK
        uuid customer_id FK
        uuid service_id FK
        uuid barber_id FK
        timestamptz starts_at
        timestamptz ends_at
        int price_cents ">= 0"
        text status "confirmed | cancelled | completed (AppointmentStatus)"
        timestamptz created_at
        timestamptz updated_at
    }
    conversation_states {
        text phone PK "E.164 format"
        text stage
        jsonb slots
        timestamptz created_at
        timestamptz updated_at
    }
    processed_messages {
        text message_id PK
        text status "processing | done | failed (MessageStatus)"
        timestamptz created_at
        timestamptz updated_at
    }
    business_hours {
        smallint day_of_week PK "0=Monday .. 6=Sunday"
        time opens_at
        time closes_at
        timestamptz created_at
        timestamptz updated_at
    }
```

### 5.1 Tables and Constraints

#### `processed_messages`
Guarantees idempotency for incoming webhook calls.
- **Columns**:
  - `message_id`: `text PRIMARY KEY` (channel/webhook message identifier)
  - `status`: `text NOT NULL` (`processing`, `done`, or `failed` via domain `MessageStatus` `StrEnum`)
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (status IN ('processing', 'done', 'failed'))`
- **Rationale & Mechanics**: In messaging channels like WhatsApp, network retries and webhook deduplication issues from upstream orchestrators (e.g. n8n or WhatsApp Cloud API) can deliver the exact same message multiple times. Without an idempotency layer at the edge, duplicate deliveries could trigger duplicate booking operations or corrupt the conversation state.
  - An atomic acquisition query (`INSERT ... ON CONFLICT (message_id) DO UPDATE ...`) ensures that only one worker acquires the message.
  - Messages in `failed` status can be retried immediately.
  - Messages stuck in `processing` can be reacquired after a configurable timeout (e.g., if a worker crashed before completing).

#### `customers`
Stores customer identity and contact information.
- **Columns**:
  - `id`: `uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `phone`: `text NOT NULL UNIQUE` (customer identifier in E.164 format, e.g. `+1234567890`)
  - `name`: `text NOT NULL`
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (phone ~ '^\+[1-9]\d{1,14}$')` (enforces valid E.164 phone numbering)
- **Indexes**:
  - `pk_customers` on `id`
  - `uq_customers_phone` (unique) on `phone`
- **Creation Timing**: `customers.name` is `NOT NULL`. A customer record is created only at the moment of booking confirmation, once the customer's name has been elicited and validated during the conversation. Prior to booking, only `conversation_states` exists.

#### `services`
Catalog of barbershop services and pricing.
- **Columns**:
  - `id`: `uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `name`: `text NOT NULL UNIQUE`
  - `duration_minutes`: `integer NOT NULL`
  - `price_cents`: `integer NOT NULL`
  - `active`: `boolean NOT NULL DEFAULT true`
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (duration_minutes > 0)`
  - `CHECK (price_cents >= 0)`
- **Indexes**:
  - `pk_services` on `id`
  - `uq_services_name` (unique) on `name`

#### `barbers`
The schedulable resource.
- **Columns**:
  - `id`: `uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `name`: `text NOT NULL`
  - `active`: `boolean NOT NULL DEFAULT true`
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Indexes**:
  - `pk_barbers` on `id`
- **Rationale**: Even if Northside Barbers operates with a single chair initially, modeling `barbers` as a distinct entity decouples the business logic from a singleton assumption. It makes the double-booking exclusion constraint explicitly scoped per resource (`barber_id WITH =`), allows multiple staff members without future schema migrations, and cleanly separates staff management from customer accounts. Indexes on `active` are intentionally omitted until concrete query patterns require them.

#### `appointments`
Booked service instances for a specific customer, barber, and time slot.
- **Columns**:
  - `id`: `uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `customer_id`: `uuid NOT NULL REFERENCES customers(id) ON DELETE RESTRICT`
  - `service_id`: `uuid NOT NULL REFERENCES services(id) ON DELETE RESTRICT`
  - `barber_id`: `uuid NOT NULL REFERENCES barbers(id) ON DELETE RESTRICT`
  - `starts_at`: `timestamptz NOT NULL`
  - `ends_at`: `timestamptz NOT NULL`
  - `price_cents`: `integer NOT NULL`
  - `status`: `text NOT NULL DEFAULT 'confirmed'` (`confirmed`, `cancelled`, or `completed` via domain `AppointmentStatus` `StrEnum`)
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (ends_at > starts_at)`
  - `CHECK (price_cents >= 0)`
  - `CHECK (status IN ('confirmed', 'cancelled', 'completed'))`
  - `CONSTRAINT appointments_barber_slot_excl EXCLUDE USING gist (barber_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (status = 'confirmed')` (requires `btree_gist`)
- **Indexes**:
  - `pk_appointments` on `id`
  - `ix_appointments_customer_id` on `customer_id`
  - `ix_appointments_barber_id_starts_at` on `barber_id, starts_at`
  - GiST index created automatically by the exclusion constraint
- **Service Layer Invariants**:
  - `price_cents`: Represents the price frozen at the exact time of booking. Future catalog price updates do not retroactively alter confirmed appointments.
  - `ends_at = starts_at + service.duration_minutes`: Computed and guaranteed by the `BookingService` layer before persisting.

#### `business_hours`
Configurable operating hours for the business by day of the week.
- **Columns**:
  - `day_of_week`: `smallint PRIMARY KEY` (ISO weekday index: `0` = Monday, `1` = Tuesday, ..., `6` = Sunday)
  - `opens_at`: `time NOT NULL` (e.g. `09:00:00`, local time)
  - `closes_at`: `time NOT NULL` (e.g. `19:00:00`, local time)
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (day_of_week BETWEEN 0 AND 6)`
  - `CHECK (closes_at > opens_at)`
- **Closed Days Representation**: A day without a row in `business_hours` represents a closed day. There is no `is_closed` boolean column, avoiding duplicate or conflicting representations of closed days.
- **Migration vs. Seed Discipline**: Migrations define solely table schema and structural constraints. Demonstration or initial business hours are provided via a separate seed script (`src/booking_agent/database/seeds.py`), never inside Alembic migrations. Unit and integration tests create their own fixture business hours.
- **Scope Boundary**:
  - Overnight shifts extending past midnight are out of scope (`closes_at > opens_at` enforced).
  - Intra-day intervals or split shifts (e.g. lunch breaks) are out of scope.
  - Individual barber working rosters, split shifts, vacations, and personal breaks are out of scope. All active barbers are available during general business hours unless occupied by an existing confirmed appointment.

#### `conversation_states`
Tracks multi-turn chat interaction state and extracted slot values per customer phone number.
- **Columns**:
  - `phone`: `text PRIMARY KEY` (E.164 phone number)
  - `stage`: `text NOT NULL DEFAULT 'idle'`
  - `slots`: `jsonb NOT NULL DEFAULT '{}'::jsonb`
  - `created_at`: `timestamptz NOT NULL DEFAULT now()`
  - `updated_at`: `timestamptz NOT NULL DEFAULT now()`
- **Constraints**:
  - `CHECK (phone ~ '^\+[1-9]\d{1,14}$')`
- **Decoupling from `customers`**: `conversation_states` has **no foreign key** to `customers`. Conversations frequently occur before a customer record exists (e.g. an unknown caller inquiring about services or starting a booking before their name is collected). Keying by phone number keeps dialogue state completely independent of customer profile creation.

### 5.2 Timestamp and Audit Rules
- **Database layer**: All timestamps are stored in UTC using PostgreSQL `timestamptz`. Repositories and SQL queries operate strictly in UTC without local conversions.
- **Service layer**: The business timezone (e.g. barbershop local time) is managed strictly within the service layer. It converts user-requested dates and times to UTC before querying or persisting, and formats UTC timestamps back to local time when generating customer responses.
- **`updated_at` management**: Updating `updated_at = now()` is the explicit responsibility of the repository layer on every SQL `UPDATE` statement. No database triggers are used, keeping data mutation logic explicit in application code.

### 5.3 Double-booking Prevention
Exclusivity is enforced at the database level through a PostgreSQL exclusion constraint with the `btree_gist` extension:
`EXCLUDE USING gist (barber_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (status = 'confirmed')`.
The detailed architectural rationale, concurrency safety analysis, and error translation are documented in [ADR 0002](adr/0002-double-booking-exclusion-constraint.md).

## 6. Booking service

`BookingService` is the core business orchestration layer. It enforces domain invariants, performs slot calculations, executes database mutations via a unit of work, and translates database exclusion violations into domain-level conflict outcomes.

### 6.1 Operations and Result Types

Operations accept typed inputs and return explicitly-typed domain models (defined in `booking_agent.domain.models`), never raw dictionaries or loose tuples.

```mermaid
flowchart LR
    Caller -->|list_availability| BS[BookingService]
    Caller -->|book| BS
    Caller -->|reschedule| BS
    Caller -->|cancel| BS
    BS -->|AvailabilityResult| Caller
    BS -->|BookingResult| Caller
    BS -->|RescheduleResult| Caller
    BS -->|CancelResult| Caller
```

#### 1. `list_availability`
- **Inputs**:
  - `barber_id: UUID | None = None` (optional; if omitted, unions open slots across all active barbers)
  - `service_id: UUID`
  - `target_date: date` (local calendar date for the business)
- **Output**:
  - `AvailabilityResult`:
    - `barber_id: UUID | None`
    - `service_id: UUID`
    - `target_date: date`
    - `slots: Sequence[datetime]` (local timezone-aware start datetimes; may be empty if fully booked)
- **Domain Errors**:
  - `BarberNotFoundError`: Explicitly requested barber does not exist or is inactive.
  - `ServiceNotFoundError`: Service does not exist or is inactive.
  - `InvalidTimeWindowError`: Requested date is in the past or exceeds `settings.max_booking_horizon_days`.
- **Rules**:
  1. Retrieve active `Service` (duration in minutes) and target barber(s). If `barber_id` is provided, fetch that specific active barber; if omitted, fetch all active barbers.
  2. Validate `target_date` against the booking horizon: must not be in the past and must not exceed `clock.now().astimezone(business_tz).date() + timedelta(days=settings.max_booking_horizon_days)`.
  3. Query `business_hours` for `target_date.weekday()`. If no row exists, the business is closed on this day; return empty slots.
  4. Generate candidate slot start times starting at `opens_at`, incrementing by `settings.slot_granularity_minutes` (e.g. 15 min), ensuring `candidate_start + service.duration_minutes <= closes_at`.
  5. Discard any candidate slot whose UTC start time is earlier than `clock.now() + min_advance_notice`.
  6. Calculate the day's query window in UTC: `[window_start_utc, window_end_utc)`.
  7. For the target barber (or each active barber if `barber_id is None`), query confirmed appointments within that window using the PostgreSQL range overlap operator `&&`:
     `AppointmentRepository.list_by_barber_and_window(barber_id, window_start_utc, window_end_utc)`.
     The GiST index can be used by PostgreSQL's query planner to accelerate this overlap check.
  8. For each barber, filter out candidate slots whose half-open interval `[candidate_start, candidate_start + duration)` overlaps with any existing booking's `[starts_at, ends_at)`.
  9. If `barber_id` was omitted ("any barber"), take the union of available start times across all active barbers.
  10. Return remaining available slots localized to the business timezone.

#### 2. `book`
- **Inputs**:
  - `customer_phone: str` (E.164 format, e.g. `+1234567890`)
  - `customer_name: str` (non-empty string collected during dialogue)
  - `service_id: UUID`
  - `starts_at: datetime` (MUST be timezone-aware datetime)
  - `barber_id: UUID | None = None` (optional; if omitted, deterministically selects an available active barber)
- **Output**:
  - `BookingResult`:
    - `appointment: Appointment` (persisted appointment entity)
- **Domain Errors**:
  - `BarberNotFoundError`: Explicitly requested barber does not exist or is inactive; or no active barbers are available.
  - `ServiceNotFoundError`: Service does not exist or is inactive.
  - `SlotInThePastError`: `starts_at <= clock.now()`.
  - `MinimumNoticeViolationError`: `starts_at < clock.now() + min_advance_notice`.
  - `InvalidTimeWindowError`: `starts_at` exceeds `settings.max_booking_horizon_days`.
  - `OutsideBusinessHoursError`: Slot or duration overflows operating hours on that weekday in business local timezone.
  - `SlotUnavailableError`: Slot is already booked or concurrent conflict occurred (carries `alternative_slots: Sequence[datetime]`, which may be empty).
- **Rules**:
  1. Validate that `starts_at` is timezone-aware and convert to UTC.
  2. Verify start time is strictly in the future and satisfies minimum advance notice against `clock.now()`.
  3. Convert `starts_at` to the business local timezone to determine the local weekday and local time.
  4. Validate `target_date` does not exceed `settings.max_booking_horizon_days`.
  5. Validate slot falls strictly within `business_hours` for that local weekday (must have a row in `business_hours` and `local_starts_at >= opens_at` and `local_starts_at + duration <= closes_at`).
  6. Compute `ends_at = starts_at + timedelta(minutes=service.duration_minutes)`.
  7. Fetch active service and freeze current `price_cents`.
  8. Resolve barber assignment:
     - If `barber_id` is provided, verify barber is active.
     - If `barber_id is None` ("any barber"), query active barbers and deterministically select the first barber who has no overlapping confirmed booking for that slot (ordered by name, then id). If none is available, raise `SlotUnavailableError`.
  9. Ensure customer profile exists: find by `phone` or create customer JIT with `customer_name`.
  10. Attempt to persist the appointment inside a unit of work context manager:
      `AppointmentRepository.create(...)`.
  11. **Conflict Recovery via Unit of Work**:
      - If concurrent insertion violates `appointments_barber_slot_excl` (SQLSTATE `23P01`), `AppointmentRepository` raises `SlotUnavailableError`.
      - The exception exits the unit of work context manager block, which automatically performs a rollback on exit. `BookingService` does NOT call `session.rollback()`.
      - In the `except SlotUnavailableError as exc:` handler, `BookingService` opens a **new** unit of work to query alternative open slots via `list_availability` for the same date (up to $N$ slots, e.g. 3). Note that alternatives may be empty if the schedule is fully booked.
      - `BookingService` raises a new domain `SlotUnavailableError(..., alternative_slots=alternatives) from exc`, chaining to the original exception without mutating the repository error.

#### 3. `reschedule`
- **Inputs**:
  - `appointment_id: UUID`
  - `customer_phone: str` (E.164 phone of requester)
  - `new_starts_at: datetime` (MUST be timezone-aware datetime)
- **Output**:
  - `RescheduleResult`:
    - `appointment: Appointment` (updated appointment entity)
- **Domain Errors**:
  - `AppointmentNotFoundError`: Appointment does not exist.
  - `NotAppointmentOwnerError`: Appointment does not belong to `customer_phone`.
  - `AppointmentNotConfirmedError`: Appointment status is not `confirmed`.
  - `AppointmentInThePastError`: Existing appointment has already occurred.
  - `SlotInThePastError`: `new_starts_at <= clock.now()`.
  - `MinimumNoticeViolationError`: `new_starts_at < clock.now() + min_advance_notice`.
  - `InvalidTimeWindowError`: `new_starts_at` exceeds `settings.max_booking_horizon_days`.
  - `OutsideBusinessHoursError`: New slot overflows business hours in local timezone.
  - `SlotUnavailableError`: Target slot is occupied by another booking (carries `alternative_slots: Sequence[datetime]`).
- **Rules**:
  1. Inside a unit of work, load existing appointment with customer details.
  2. Enforce preconditions:
     - Appointment must exist (`AppointmentNotFoundError`).
     - Appointment customer phone must match `customer_phone` (`NotAppointmentOwnerError`).
     - Appointment status must be `confirmed` (`AppointmentNotConfirmedError`).
     - Current appointment `starts_at` must be in the future relative to `clock.now()`.
  3. Validate `new_starts_at` is timezone-aware, future, satisfies minimum notice, does not exceed horizon, and fits within business hours for that local weekday.
  4. Compute `new_ends_at = new_starts_at + timedelta(minutes=service.duration_minutes)`.
  5. Execute in-place `UPDATE appointments SET starts_at = :new_starts_at, ends_at = :new_ends_at, updated_at = now() WHERE id = :appointment_id`.
  6. **Self-Overlap Mechanics**: PostgreSQL evaluates exclusion constraints on `UPDATE` across *distinct* rows. Shifting an appointment by 15 minutes (partially overlapping its own previous interval) succeeds without false-positive collision.
  7. If another booking conflicts (`23P01`), the unit of work context manager block exits and rolls back automatically. `BookingService` opens a new unit of work to retrieve alternative slots via `list_availability`, and raises `SlotUnavailableError(..., alternative_slots=alternatives) from exc`.

#### 4. `cancel`
- **Inputs**:
  - `appointment_id: UUID`
  - `customer_phone: str` (E.164 phone of requester)
- **Output**:
  - `CancelResult`:
    - `appointment: Appointment` (cancelled appointment entity)
- **Domain Errors**:
  - `AppointmentNotFoundError`: Appointment does not exist.
  - `NotAppointmentOwnerError`: Appointment does not belong to `customer_phone`.
  - `AppointmentInThePastError`: Cannot cancel past appointments (`starts_at <= clock.now()`).
- **Rules**:
  1. Inside a unit of work, load existing appointment.
  2. Verify appointment customer phone matches `customer_phone`.
  3. Verify `starts_at > clock.now()` (past appointments cannot be cancelled).
  4. **Idempotence**: If the appointment is already in `cancelled` status, the operation succeeds as a no-op and returns the cancelled appointment.
  5. If `confirmed`: update status to `cancelled`, set `updated_at = now()`. The partial exclusion constraint predicate (`WHERE status = 'confirmed'`) instantly frees the time slot.

### 6.2 Business Hours Configuration & Storage
- Operating hours reside in the `business_hours` database table (`day_of_week` PK, `opens_at`, `closes_at`). Business hours are dynamic operational data and are not hardcoded in source code.
- **Closed Days**: A day without a row represents a closed day.
- **Database Migrations vs. Seeds**: Migrations define solely table schema. Demonstration opening hours (e.g. Tuesday through Saturday 09:00–19:00) live in a separate seed script (`src/booking_agent/database/seeds.py`), never inside Alembic migrations. Tests set up their own business hours fixtures.
- A `BusinessHoursRepository` exposes `list_all()` and `get_by_day(day_of_week: int)`.
- **Scope Boundary**:
  - Shifts past midnight and intra-day intervals/breaks (e.g. lunch) are out of scope.
  - Individual barber working schedules (split shifts, rotations, vacations, individual lunch breaks) are explicitly out of scope. All active barbers are available during general business hours unless occupied by an existing confirmed appointment.

### 6.3 Time Handling, Timezone Lifecycle, and DST Resilience
- **Configuration**: `Settings` exposes:
  - `business_timezone: str` (e.g. `"America/Sao_Paulo"`)
  - `slot_granularity_minutes: int` (e.g. `15`)
  - `min_booking_advance_minutes: int` (e.g. `60`)
  - `max_booking_horizon_days: int` (e.g. `30`)
- **No Hardcoded Defaults**: Environment settings require explicit configuration in production.
- **Strict Timezone Discipline**:
  - Public operations accept ONLY timezone-aware `datetime` objects.
  - A single conversion function (`localize_datetime`) converts raw local date + time (from dialogue parsing) into a timezone-aware `datetime` in the business timezone:
    - **Non-existent times (Spring-forward gap)**: Local wall-clock times that do not exist (e.g. 02:30 during a 02:00 -> 03:00 jump) are detected during `zoneinfo` localization by verifying round-trip equality. They are rejected with `InvalidLocalTimeError`.
    - **Ambiguous times (Fall-back fold)**: Local times that occur twice (e.g. 01:30 during repeated transition) are resolved using Python's `fold=0` (first occurrence) by default.
    - **Note on Brazil**: Brazil has not observed DST since 2019; this handling exists to keep the codebase robust and correct for any configured business timezone.
  - All weekday, opening hours, and closing hours validations are executed AFTER converting the datetime to the business timezone (e.g. a UTC timestamp at 22:00 corresponds to 19:00 in São Paulo on that local date).

### 6.4 Injected Clock Port
- The service **never** calls `datetime.now()` or `datetime.utcnow()` directly.
- `BookingService` constructor receives an injected `Clock` protocol:
  ```python
  class Clock(Protocol):
      def now(self) -> datetime:
          """Return current timezone-aware UTC datetime."""
          ...
  ```
- **Production**: Uses `SystemClock` (`datetime.now(timezone.utc)`).
- **Testing**: Unit tests inject a `FixedClock` or `FrozenClock`.
- Time-based policies (minimum advance notice, rejecting past appointments, max horizon) evaluate deterministically against `clock.now()`.

### 6.5 Windowed Availability & GiST Index Overlap Query
- Instead of scanning all appointments for a barber, availability queries are strictly bounded to the requested day window `[window_start_utc, window_end_utc)`.
- `AppointmentRepository.list_by_barber_and_window` executes:
  ```sql
  SELECT * FROM appointments
  WHERE barber_id = :barber_id
    AND status = 'confirmed'
    AND tstzrange(starts_at, ends_at, '[)') && tstzrange(:window_start, :window_end, '[)')
  ORDER BY starts_at;
  ```
- The PostgreSQL exclusion constraint GiST index can be used by the query planner to evaluate this overlap condition efficiently.
- Unbounded listing of appointments (`SELECT * FROM appointments WHERE barber_id = :id`) is strictly forbidden in service workflows.

### 6.6 Booking Flow, Price Freezing, and Rollback on Conflict
- `ends_at = starts_at + timedelta(minutes=service.duration_minutes)`.
- Current active catalog price is copied into `price_cents`, freezing the price against future catalog adjustments.
- Customer records are created JIT during the booking transaction once the customer's name has been elicited.
- If a race condition triggers SQLSTATE `23P01` (`exclusion_violation` / `SlotUnavailableError`):
  1. PostgreSQL aborts the transaction (`InFailedSqlTransaction`).
  2. The unit of work context manager block exits, which automatically rolls back the session.
  3. `BookingService` catches `SlotUnavailableError`, creates a new unit of work, and calls `list_availability` to generate up to $N$ alternative open slots on that day (which may be empty if no slots are available).
  4. `BookingService` raises `SlotUnavailableError` populated with `alternative_slots: Sequence[datetime]` chained to the original exception (`from exc`).

### 6.7 Rescheduling via In-Place `UPDATE`
- Rescheduling modifies `starts_at` and `ends_at` on the existing appointment via SQL `UPDATE`.
- **Why UPDATE instead of Cancel + Create**:
  1. PostgreSQL exclusion constraints evaluate an updated row against all *other* matching rows. It does not compare a row against itself.
  2. Shifting an appointment by 15 minutes (which partially overlaps its own previous slot) succeeds without conflict.
  3. Avoids race conditions where a customer cancels their appointment only to find their new slot taken, losing their original booking.
  4. Preserves primary key identity and prevents distortion of cancellation audit metrics.
- Documented in [ADR 0003](adr/0003-time-handling-and-rescheduling.md).

### 6.8 Ownership, Preconditions, and Idempotent Cancellation
- Rescheduling and cancellation verify that the appointment belongs to the caller's phone (`customer_phone`).
- Rescheduling and cancellation require the appointment to be in `confirmed` status and in the future relative to `clock.now()`.
- Cancellation is **idempotent**: calling `cancel` on an appointment that is already `cancelled` succeeds and returns the cancelled appointment without raising an error.

### 6.9 Follow-ups from Schema Review (Issue #4)
- **Status Enums**: Replaced free-text status strings with typed `StrEnum`s:
  - `AppointmentStatus`: `CONFIRMED = "confirmed"`, `CANCELLED = "cancelled"`, `COMPLETED = "completed"`.
  - `MessageStatus`: `PROCESSING = "processing"`, `DONE = "done"`, `FAILED = "failed"`.
- **Windowed Overlap Query**: Replaced unbounded barber appointment listing with `list_by_barber_and_window` using range overlap `&&` leveraging the GiST index.
- **Rollback Requirement**: Documented in `AppointmentRepository` docstrings that `SlotUnavailableError` leaves the PostgreSQL transaction aborted, requiring caller rollback before running subsequent queries.

## 7. Alternatives considered

**LLM with free tool calling deciding everything.** The model would receive tools
like `check_availability` and `create_appointment` and decide the sequence.
Rejected because:

- Correctness would depend on the model following instructions on every call.
- It is hard to test deterministically and hard to audit.
- Prompt injection could trigger real actions.
- Cost and latency grow with multi-step tool loops.

See [ADR 0001](adr/0001-llm-only-at-the-edge.md).

**Rule-based NLU (regex/keywords) without an LLM.** Rejected: brittle with real
user language (typos, relative dates, slang). It remains an option for a cheap
pre-filter.

**Rescheduling via Cancel + Create.** Rejected: creating the new slot first causes
a collision if it overlaps with the existing slot; cancelling first risks leaving
the customer with no appointment if the new slot cannot be secured; and it pollutes
cancellation metrics. See [ADR 0003](adr/0003-time-handling-and-rescheduling.md).

**Global time functions (`datetime.now()`).** Rejected: direct calls prevent
deterministic unit testing and require monkeypatching libraries that leak across
asynchronous tasks. Using an injected `Clock` protocol ensures strict test isolation.

## 8. Risks and mitigations

| Risk                         | Mitigation                                                                  |
|------------------------------|-----------------------------------------------------------------------------|
| Hallucinated entities        | Strict Pydantic schema; service validates against real catalog and slots    |
| Double booking (concurrency) | DB exclusion constraint + transactions; conflict mapped to a friendly reply |
| Token cost                   | Single short extraction call; small model; no tool loops; usage logged      |
| Prompt injection             | LLM has no tools or data access; output limited to a closed schema          |
| Timezone bugs and DST gaps   | `timestamptz` in DB, strict `ZoneInfo` at service edge, gap/fold handling   |
| Aborted transaction locks    | Mandatory `rollback()` on `SlotUnavailableError` before querying alternatives|
| LLM provider outage          | Timeouts and a graceful "try again" reply; provider behind a port           |

## 9. Milestones

1. **Scaffold**: layout, tooling, CI, health checks, `IntentParser` port. (Completed)
2. **Schema and repositories**: migrations for the draft model, repositories with
   integration tests, exclusion constraint. (Completed)
3. **BookingService**: availability, booking, rescheduling and cancellation rules;
   time handling and deterministic Clock. (In Progress - Design Complete)
4. **OpenAI adapter**: structured-output adapter with contract tests and evals.
5. **Conversation flow**: `conversation_state` state machine and message endpoint.
6. **Observability and hardening**: request IDs, metrics, rate limiting, auth.
