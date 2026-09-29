"""設定：DB 路徑等。正式機用環境變數 WEBVULN_DB_URL 指到 /opt/webvuln/data/vuln.db。"""
from __future__ import annotations

import os
from pathlib import Path

# 開發預設：APP/data/vuln.db（已 gitignore）。正式機用 env 覆蓋。
_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "vuln.db"

DB_URL: str = os.environ.get("WEBVULN_DB_URL") or f"sqlite:///{_DEFAULT_DB.as_posix()}"


def default_db_path() -> Path:
    return _DEFAULT_DB


# 申請作業提前期（天）：到期前多久就該進入申請程序。過了「行動線」還沒動＝註定逾期。
# 預設全域 30 天，可用 env 覆寫；可依嚴重度覆寫（短 SLA 的 Critical 會早早觸發＝立即行動）。
APPLICATION_LEAD_DAYS: int = int(os.environ.get("WEBVULN_LEAD_DAYS", "30"))

# 依嚴重度覆寫，例如 {"Critical": 14}；預設空＝全部用 APPLICATION_LEAD_DAYS
_SEV_LEAD_ENV = os.environ.get("WEBVULN_LEAD_DAYS_BY_SEV", "")


def _parse_sev_lead(raw: str) -> dict:
    out: dict[str, int] = {}
    for pair in raw.split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            try:
                out[k.strip()] = int(v)
            except ValueError:
                pass
    return out


SEVERITY_LEAD_DAYS: dict = _parse_sev_lead(_SEV_LEAD_ENV)


def lead_days(severity) -> int:
    return SEVERITY_LEAD_DAYS.get(severity, APPLICATION_LEAD_DAYS)


# SLA 政策天數（各嚴重度應在幾天內修補）。可用 env WEBVULN_SLA_DAYS 覆寫，如 "Critical:7,High:30"。
_DEFAULT_SLA = {"Critical": 7, "High": 30, "Medium": 90, "Low": 180}
_sla_env = _parse_sev_lead(os.environ.get("WEBVULN_SLA_DAYS", ""))
SLA_POLICY_DAYS: dict = {**_DEFAULT_SLA, **_sla_env}
