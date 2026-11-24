"""case_overlay 加 track_note（管理追蹤備註）

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.add_column(sa.Column('track_note', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.drop_column('track_note')
