"""create initial schema

Revision ID: 9cea7d012872
Revises:
Create Date: 2026-10-18 13:19:01.623118
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9cea7d012872"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create initial tables, constraints, indexes and btree_gist extension."""
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")

    op.create_table(
        "customers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
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
        sa.UniqueConstraint("phone", name="uq_customers_phone"),
        sa.CheckConstraint(
            "phone ~ '^[+][1-9][0-9]{1,14}$'",
            name="ck_customers_phone_e164",
        ),
    )

    op.create_table(
        "services",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.UniqueConstraint("name", name="uq_services_name"),
        sa.CheckConstraint("duration_minutes > 0", name="ck_services_duration_positive"),
        sa.CheckConstraint("price_cents >= 0", name="ck_services_price_non_negative"),
    )

    op.create_table(
        "barbers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
    )

    op.create_table(
        "appointments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "customers.id",
                ondelete="RESTRICT",
                name="fk_appointments_customer_id_customers",
            ),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "services.id",
                ondelete="RESTRICT",
                name="fk_appointments_service_id_services",
            ),
            nullable=False,
        ),
        sa.Column(
            "barber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "barbers.id",
                ondelete="RESTRICT",
                name="fk_appointments_barber_id_barbers",
            ),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'confirmed'"),
        ),
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
        sa.CheckConstraint("ends_at > starts_at", name="ck_appointments_ends_after_starts"),
        sa.CheckConstraint("price_cents >= 0", name="ck_appointments_price_non_negative"),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed')",
            name="ck_appointments_status_valid",
        ),
    )

    op.create_index(
        "ix_appointments_customer_id",
        "appointments",
        ["customer_id"],
    )
    op.create_index(
        "ix_appointments_barber_id_starts_at",
        "appointments",
        ["barber_id", "starts_at"],
    )

    # Enforce mutual exclusion per barber using a GiST index on half-open timestamp range.
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT appointments_barber_slot_excl
        EXCLUDE USING gist (
            barber_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        WHERE (status = 'confirmed');
        """
    )

    op.create_table(
        "conversation_states",
        sa.Column("phone", sa.String(), primary_key=True),
        sa.Column("stage", sa.String(), nullable=False, server_default=sa.text("'idle'")),
        sa.Column(
            "slots",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
            "phone ~ '^[+][1-9][0-9]{1,14}$'",
            name="ck_conversation_states_phone_e164",
        ),
    )

    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
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
            "status IN ('processing', 'done', 'failed')",
            name="ck_processed_messages_status_valid",
        ),
    )


def downgrade() -> None:
    """Drop all tables and extensions created in upgrade."""
    op.drop_table("processed_messages")
    op.drop_table("conversation_states")
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_barber_slot_excl;")
    op.drop_index("ix_appointments_barber_id_starts_at", table_name="appointments")
    op.drop_index("ix_appointments_customer_id", table_name="appointments")
    op.drop_table("appointments")
    op.drop_table("barbers")
    op.drop_table("services")
    op.drop_table("customers")
    op.execute("DROP EXTENSION IF EXISTS btree_gist;")
