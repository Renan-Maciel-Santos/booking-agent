"""index timestamps used by retention

Revision ID: 8a9c42ef76fb
Revises: f65e45483e00
Create Date: 2026-10-27 16:38:14.681006
"""

# Sequence is used in the revision identifiers' type hints.
from collections.abc import Sequence

# SQLAlchemy types and constructs.
# Alembic operations API.
from alembic import op

# Identifier of this revision.
revision: str = "8a9c42ef76fb"
# Identifier of the parent revision.
down_revision: str | None = "f65e45483e00"
# Branch labels, if any.
branch_labels: str | Sequence[str] | None = None
# Revisions this one depends on.
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create indexes on retention timestamp columns."""
    op.create_index(
        "ix_processed_messages_created_at",
        "processed_messages",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_states_updated_at",
        "conversation_states",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop indexes on retention timestamp columns."""
    op.drop_index("ix_conversation_states_updated_at", table_name="conversation_states")
    op.drop_index("ix_processed_messages_created_at", table_name="processed_messages")
