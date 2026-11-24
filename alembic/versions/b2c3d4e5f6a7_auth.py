"""auth: app_user / user_session / audit_log (W4)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-29

手寫遷移：三表全新，create_all 亦能建；此檔讓正式機可版控升級。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_user',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('display_name', sa.String(length=100), nullable=True),
        sa.Column('email', sa.String(length=200), nullable=True),
        sa.Column('department', sa.String(length=200), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('app_user', schema=None) as b:
        b.create_index(b.f('ix_app_user_username'), ['username'], unique=True)
        b.create_index(b.f('ix_app_user_role'), ['role'], unique=False)

    op.create_table(
        'user_session',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('user_session', schema=None) as b:
        b.create_index(b.f('ix_user_session_token'), ['token'], unique=True)
        b.create_index(b.f('ix_user_session_user_id'), ['user_id'], unique=False)
        b.create_index(b.f('ix_user_session_expires_at'), ['expires_at'], unique=False)

    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('username', sa.String(length=100), nullable=True),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('target', sa.String(length=200), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('audit_log', schema=None) as b:
        b.create_index(b.f('ix_audit_log_at'), ['at'], unique=False)
        b.create_index(b.f('ix_audit_log_username'), ['username'], unique=False)
        b.create_index(b.f('ix_audit_log_action'), ['action'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('audit_log', schema=None) as b:
        b.drop_index(b.f('ix_audit_log_action'))
        b.drop_index(b.f('ix_audit_log_username'))
        b.drop_index(b.f('ix_audit_log_at'))
    op.drop_table('audit_log')
    with op.batch_alter_table('user_session', schema=None) as b:
        b.drop_index(b.f('ix_user_session_expires_at'))
        b.drop_index(b.f('ix_user_session_user_id'))
        b.drop_index(b.f('ix_user_session_token'))
    op.drop_table('user_session')
    with op.batch_alter_table('app_user', schema=None) as b:
        b.drop_index(b.f('ix_app_user_role'))
        b.drop_index(b.f('ix_app_user_username'))
    op.drop_table('app_user')
