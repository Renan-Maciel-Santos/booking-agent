"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

# Sequence is used in the revision identifiers' type hints.
from collections.abc import Sequence

# Alembic operations API.
from alembic import op
# SQLAlchemy types and constructs.
import sqlalchemy as sa
${imports if imports else ""}

# Identifier of this revision.
revision: str = ${repr(up_revision)}
# Identifier of the parent revision.
down_revision: str | None = ${repr(down_revision)}
# Branch labels, if any.
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
# Revisions this one depends on.
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this migration."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert this migration."""
    ${downgrades if downgrades else "pass"}
