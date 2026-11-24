"""case_overlay (W3 承辦疊加層)

Revision ID: a1b2c3d4e5f6
Revises: e9f0efaf551e
Create Date: 2026-09-29

手寫遷移：Case 為全新表，create_all 亦能建；此檔讓 alembic 歷史完整、正式機可版控升級。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e9f0efaf551e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'case_overlay',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vuln_key', sa.String(length=400), nullable=False),
        sa.Column('sheet_key', sa.String(length=100), nullable=True),
        sa.Column('plugin_id', sa.String(length=50), nullable=True),
        sa.Column('host', sa.String(length=200), nullable=True),
        sa.Column('department', sa.String(length=200), nullable=True),
        sa.Column('owner', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('last_seen_batch_id', sa.Integer(), nullable=True),
        sa.Column('is_orphan', sa.Boolean(), nullable=False),
        sa.Column('source_closed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('status_changed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('case_overlay', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_case_overlay_vuln_key'), ['vuln_key'], unique=True)
        batch_op.create_index(batch_op.f('ix_case_overlay_department'), ['department'], unique=False)
        batch_op.create_index(batch_op.f('ix_case_overlay_owner'), ['owner'], unique=False)
        batch_op.create_index(batch_op.f('ix_case_overlay_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_case_overlay_last_seen_batch_id'),
                              ['last_seen_batch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_case_overlay_is_orphan'), ['is_orphan'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('case_overlay', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_case_overlay_is_orphan'))
        batch_op.drop_index(batch_op.f('ix_case_overlay_last_seen_batch_id'))
        batch_op.drop_index(batch_op.f('ix_case_overlay_status'))
        batch_op.drop_index(batch_op.f('ix_case_overlay_owner'))
        batch_op.drop_index(batch_op.f('ix_case_overlay_department'))
        batch_op.drop_index(batch_op.f('ix_case_overlay_vuln_key'))
    op.drop_table('case_overlay')
