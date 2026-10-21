"""explicit pk and restrict fk for booking actions

Revision ID: 780a3eeb675f
Revises: c4d8e2f1a5b6
Create Date: 2026-10-21 09:14:01.186608
"""

from collections.abc import Sequence

from alembic import op

# Identifier of this revision.
revision: str = "780a3eeb675f"
# Identifier of the parent revision.
down_revision: str | None = "c4d8e2f1a5b6"
# Branch labels, if any.
branch_labels: str | Sequence[str] | None = None
# Revisions this one depends on.
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Explicitly name primary key as pk_booking_actions and change FK to RESTRICT."""
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'booking_actions_pkey'
                  AND conrelid = 'booking_actions'::regclass
            ) THEN
                ALTER TABLE booking_actions
                RENAME CONSTRAINT booking_actions_pkey TO pk_booking_actions;
            END IF;
        END $$;
    """)

    op.drop_constraint(
        "fk_booking_actions_appointment_id_appointments",
        "booking_actions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_booking_actions_appointment_id_appointments",
        "booking_actions",
        "appointments",
        ["appointment_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Revert FK to CASCADE."""
    op.drop_constraint(
        "fk_booking_actions_appointment_id_appointments",
        "booking_actions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_booking_actions_appointment_id_appointments",
        "booking_actions",
        "appointments",
        ["appointment_id"],
        ["id"],
        ondelete="CASCADE",
    )
