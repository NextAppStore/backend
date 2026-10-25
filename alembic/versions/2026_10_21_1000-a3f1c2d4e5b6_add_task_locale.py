"""add_task_locale

Revision ID: a3f1c2d4e5b6
Revises: 4ce9d3e29956
Create Date: 2026-10-21 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c2d4e5b6'
down_revision: Union[str, None] = '4ce9d3e29956'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get 'de', the default language of the UI.
    op.add_column(
        'tasks',
        sa.Column('locale', sa.String(length=2), server_default='de', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('tasks', 'locale')
