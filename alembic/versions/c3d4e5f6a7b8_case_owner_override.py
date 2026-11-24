"""case_overlay 加 owner_override（管理員改負責人）

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-03

手寫遷移：新增一欄，create_all 亦能建；正式機用此版控升級。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.add_column(sa.Column('owner_override', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as b:
        b.drop_column('owner_override')
