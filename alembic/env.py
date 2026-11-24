"""Alembic 環境：用 webvuln 的 metadata 與 DB_URL；SQLite 開 batch 模式(支援 ALTER)。"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 讓 webvuln 可 import（APP 在 alembic/ 的上一層）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from webvuln.config import DB_URL  # noqa: E402
from webvuln.models import Base  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", DB_URL)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL, target_metadata=target_metadata,
        literal_binds=True, render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_as_batch=True,  # SQLite 需要 batch 才能 ALTER
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
