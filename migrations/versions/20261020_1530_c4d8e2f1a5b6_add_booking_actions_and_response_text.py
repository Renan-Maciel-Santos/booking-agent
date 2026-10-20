"""add booking actions and response text

Revision ID: c4d8e2f1a5b6
Revises: f5b7e21a8d34
Create Date: 2026-10-20 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d8e2f1a5b6"
down_revision: str | None = "f5b7e21a8d34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add response_text to processed_messages and create booking_actions table."""
    op.add_column("processed_messages", sa.Column("response_text", sa.Text(), nullable=True))

    op.create_table(
        "booking_actions",
        sa.Column("action_id", sa.UUID(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "appointment_id",
            sa.UUID(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('book', 'reschedule', 'cancel')",
            name="ck_booking_actions_kind_valid",
        ),
    )
    op.create_index(
        "ix_booking_actions_appointment_id",
        "booking_actions",
        ["appointment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop booking_actions table and response_text column."""
    op.drop_index("ix_booking_actions_appointment_id", table_name="booking_actions")
    op.drop_table("booking_actions")
    op.drop_column("processed_messages", "response_text")
