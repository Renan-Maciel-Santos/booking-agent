"""create business hours table

Revision ID: f5b7e21a8d34
Revises: 9cea7d012872
Create Date: 2026-10-19 13:18:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7e21a8d34"
down_revision: str | None = "9cea7d012872"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create business_hours table and constraints."""
    op.create_table(
        "business_hours",
        sa.Column("day_of_week", sa.SmallInteger(), primary_key=True),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("closes_at", sa.Time(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "day_of_week BETWEEN 0 AND 6",
            name="ck_business_hours_day_of_week_range",
        ),
        sa.CheckConstraint(
            "closes_at > opens_at",
            name="ck_business_hours_closes_after_opens",
        ),
    )


def downgrade() -> None:
    """Drop business_hours table."""
    op.drop_table("business_hours")
