"""merge_settings_and_worker_role_heads

Revision ID: a9b8c7d6e5f4
Revises: f1e2d3c4b5a6, f8a9b0c1d2e3
Create Date: 2026-10-27 13:00:00.000000

postgres_job_queue / worker_role branched off 4ce9d3e29956 independently
from the add_task_locale / add_settings_table branches that
f1e2d3c4b5a6 already merged, leaving two heads again. No overlap
between the two sides (tasks.locale + settings table vs. the job-queue
columns on tasks + the appstore_worker role), so this merge has no
schema changes of its own.
"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, Sequence[str], None] = ('f1e2d3c4b5a6', 'f8a9b0c1d2e3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
