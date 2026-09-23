"""設定：DB 路徑等。正式機用環境變數 WEBVULN_DB_URL 指到 /opt/webvuln/data/vuln.db。"""
from __future__ import annotations

import os
from pathlib import Path

# 開發預設：APP/data/vuln.db（已 gitignore）。正式機用 env 覆蓋。
_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "vuln.db"

DB_URL: str = os.environ.get("WEBVULN_DB_URL") or f"sqlite:///{_DEFAULT_DB.as_posix()}"


def default_db_path() -> Path:
    return _DEFAULT_DB
