"""case_overlay 加 department_override（可改部門，負責人可能是別單位的人）

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.add_column(sa.Column('department_override', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.drop_column('department_override')
