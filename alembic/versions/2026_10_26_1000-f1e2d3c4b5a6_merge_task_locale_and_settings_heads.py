"""merge_task_locale_and_settings_heads

Revision ID: f1e2d3c4b5a6
Revises: a3f1c2d4e5b6, d8343eb91516
Create Date: 2026-10-26 10:00:00.000000

add_task_locale and add_settings_table were both branched off
4ce9d3e29956 independently, leaving two heads. No overlap between the
two (tasks.locale column vs. a new settings table), so this merge has
no schema changes of its own.
"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, Sequence[str], None] = ('a3f1c2d4e5b6', 'd8343eb91516')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
