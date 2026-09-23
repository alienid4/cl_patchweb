"""API 進出結構（Pydantic v2）。

匯入契約：前端(沿用單機版 JS)已把 Excel 解析成一批 finding，日期為 ISO 字串、
嚴重度已正規化、結案已分類；後端只負責存與算 effective_due/stage。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class FindingIn(BaseModel):
    sheet_key: Optional[str] = None
    plugin_id: Optional[str] = None
    name: Optional[str] = None
    host: Optional[str] = None
    severity: Optional[str] = None          # 已正規化 Critical/High/Medium/Low
    severity_raw: Optional[str] = None
    department: Optional[str] = None
    owner: Optional[str] = None
    remediation_due: Optional[str] = None       # ISO date 或 null（前端已解析民國年）
    first_extension_due: Optional[str] = None
    exception_due: Optional[str] = None
    close_status: Optional[str] = None          # 未結案/已結案/其他（前端已分類）
    close_date: Optional[str] = None
    remark: Optional[str] = None


class ImportIn(BaseModel):
    source_file: Optional[str] = None
    note: Optional[str] = None
    findings: list[FindingIn] = []


class ImportResult(BaseModel):
    batch_id: int
    row_count: int
    is_latest: bool
