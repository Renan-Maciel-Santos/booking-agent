-- Default demo business hours: Tuesday to Saturday 09:00 - 19:00
-- Monday (0) and Sunday (6) are closed (no row)
INSERT INTO business_hours (day_of_week, opens_at, closes_at)
VALUES
    (1, '09:00:00', '19:00:00'),
    (2, '09:00:00', '19:00:00'),
    (3, '09:00:00', '19:00:00'),
    (4, '09:00:00', '19:00:00'),
    (5, '09:00:00', '19:00:00')
ON CONFLICT (day_of_week) DO UPDATE
SET opens_at = EXCLUDED.opens_at,
    closes_at = EXCLUDED.closes_at,
    updated_at = now();
