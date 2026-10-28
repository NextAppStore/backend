"""lab task types

Revision ID: 584a54efa288
Revises: a9b8c7d6e5f4
Create Date: 2026-10-28 15:00:00.000000

Adds the ``LAB_REASSIGN``, ``LAB_RESET``, ``LAB_ADD_STUDENT`` and
``LAB_REMOVE_STUDENT`` values to the Postgres ``tasktype`` enum, for the
Kubernetes-App student-pod management actions (``labctl`` ported to the
worker, see backend#37). ``labctl passwd`` is not its own task type: it
rotates the secret and then reassigns, so it is carried by
``LAB_REASSIGN`` with an extra payload flag.

See ``0e43dd2bc856_redeploy.py`` for why this is hand-written (Alembic
autogenerate does not diff Postgres enum values) and why ``ADD VALUE``
needs to run outside the migration's implicit transaction.

Downgrade: rebuilds the enum without the four new values, as in
``0e43dd2bc856_redeploy.py``. Destructive, dev/local resets only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '584a54efa288'
down_revision: Union[str, None] = 'a9b8c7d6e5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_VALUES = ("LAB_REASSIGN", "LAB_RESET", "LAB_ADD_STUDENT", "LAB_REMOVE_STUDENT")


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("COMMIT"))
    for value in _NEW_VALUES:
        connection.execute(sa.text(f"ALTER TYPE tasktype ADD VALUE IF NOT EXISTS '{value}'"))


def downgrade() -> None:
    # Destructive: removes rows of the new types before rebuilding the enum.
    # Intended for dev/local resets only (see 0e43dd2bc856_redeploy.py).
    in_clause = ", ".join(f"'{v}'" for v in _NEW_VALUES)
    op.execute(f"DELETE FROM tasks WHERE type::text IN ({in_clause})")
    op.execute("ALTER TYPE tasktype RENAME TO tasktype_old")
    op.execute(
        "CREATE TYPE tasktype AS ENUM "
        "('DEPLOY', 'UPDATE', 'DESTROY', 'PAUSE', 'RESUME', 'REDEPLOY')"
    )
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN type TYPE tasktype "
        "USING type::text::tasktype"
    )
    op.execute("DROP TYPE tasktype_old")
