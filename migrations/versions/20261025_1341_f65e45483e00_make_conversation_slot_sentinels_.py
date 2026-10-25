"""make conversation slot sentinels explicit

Revision ID: f65e45483e00
Revises: 48450e9dcbdd
Create Date: 2026-10-25 13:41:35.918912
"""

# Sequence is used in the revision identifiers' type hints.
from collections.abc import Sequence

# SQLAlchemy types and constructs.
import sqlalchemy as sa

# Alembic operations API.
from alembic import op

# Identifier of this revision.
revision: str = "f65e45483e00"
# Identifier of the parent revision.
down_revision: str | None = "48450e9dcbdd"
# Branch labels, if any.
branch_labels: str | Sequence[str] | None = None
# Revisions this one depends on.
depends_on: str | Sequence[str] | None = None

ANY_BARBER_SQL = (
    "UPDATE conversation_states "
    "SET slots = jsonb_set(slots, '{barber_id}', '\"any\"') "
    "WHERE slots->'barber_id' = 'null'::jsonb"
)
EMPTY_EXECUTING_MESSAGE_SQL = (
    "UPDATE conversation_states "
    "SET slots = slots - 'executing_message_id' "
    "WHERE slots->>'executing_message_id' = ''"
)
DOWNGRADE_BARBER_SQL = (
    "UPDATE conversation_states "
    "SET slots = jsonb_set(slots, '{barber_id}', 'null'::jsonb) "
    "WHERE slots->>'barber_id' = 'any'"
)


def upgrade() -> None:
    """Apply this migration."""
    op.execute(sa.text(ANY_BARBER_SQL))
    op.execute(sa.text(EMPTY_EXECUTING_MESSAGE_SQL))


def downgrade() -> None:
    """Revert this migration."""
    # Revert explicit "any" back to JSON null sentinel.
    op.execute(sa.text(DOWNGRADE_BARBER_SQL))
    # Note: empty string for executing_message_id is not restored because
    # key absence has the exact same behavior: no message ID matches.
