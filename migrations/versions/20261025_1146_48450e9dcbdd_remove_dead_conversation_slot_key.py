"""remove dead conversation slot key

Revision ID: 48450e9dcbdd
Revises: 780a3eeb675f
Create Date: 2026-10-25 11:46:29.322005
"""

# Sequence is used in the revision identifiers' type hints.
from collections.abc import Sequence

# SQLAlchemy types and constructs.
import sqlalchemy as sa

# Alembic operations API.
from alembic import op

# Identifier of this revision.
revision: str = "48450e9dcbdd"
# Identifier of the parent revision.
down_revision: str | None = "780a3eeb675f"
# Branch labels, if any.
branch_labels: str | Sequence[str] | None = None
# Revisions this one depends on.
depends_on: str | Sequence[str] | None = None


REMOVE_DEAD_SLOT_KEYS_SQL = (
    "UPDATE conversation_states "
    "SET slots = slots - 'service_duration_minutes' "
    "WHERE slots ? 'service_duration_minutes'"
)


def upgrade() -> None:
    """Apply this migration."""
    op.execute(sa.text(REMOVE_DEAD_SLOT_KEYS_SQL))


def downgrade() -> None:
    """Revert this migration."""
    # Irreversible data cleanup of a dead key that was never read; no-op on downgrade.
    pass
