-- Idempotent demo seed data for Northside Barbers
-- Services, Barbers, and Operating Hours.
-- Safe to execute repeatedly without creating duplicate rows.

-- 1. Barbershop Services
INSERT INTO services (id, name, duration_minutes, price_cents, active)
VALUES
    ('a0000000-0000-0000-0000-000000000001', 'Corte de Cabelo', 30, 4500, true),
    ('a0000000-0000-0000-0000-000000000002', 'Barba', 30, 3500, true),
    ('a0000000-0000-0000-0000-000000000003', 'Corte e Barba', 60, 7000, true),
    ('a0000000-0000-0000-0000-000000000004', 'Sobrancelha', 15, 2000, true)
ON CONFLICT (name) DO UPDATE
SET duration_minutes = EXCLUDED.duration_minutes,
    price_cents = EXCLUDED.price_cents,
    active = EXCLUDED.active,
    updated_at = now();

-- 2. Barbers
INSERT INTO barbers (id, name, active)
VALUES
    ('b0000000-0000-0000-0000-000000000001', 'Carlos', true),
    ('b0000000-0000-0000-0000-000000000002', 'Bruno', true),
    ('b0000000-0000-0000-0000-000000000003', 'Rafael', true)
ON CONFLICT (id) DO UPDATE
SET name = EXCLUDED.name,
    active = EXCLUDED.active,
    updated_at = now();

-- 3. Business Hours: Monday (0) through Saturday (5) 09:00 - 19:00 (Sunday closed)
INSERT INTO business_hours (day_of_week, opens_at, closes_at)
VALUES
    (0, '09:00:00', '19:00:00'),
    (1, '09:00:00', '19:00:00'),
    (2, '09:00:00', '19:00:00'),
    (3, '09:00:00', '19:00:00'),
    (4, '09:00:00', '19:00:00'),
    (5, '09:00:00', '19:00:00')
ON CONFLICT (day_of_week) DO UPDATE
SET opens_at = EXCLUDED.opens_at,
    closes_at = EXCLUDED.closes_at,
    updated_at = now();
