"""case_overlay 加 target_date（預計完成日）

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.add_column(sa.Column('target_date', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.drop_column('target_date')
