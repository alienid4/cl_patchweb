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
    raw: Optional[dict] = None                   # 整列原始資料（原欄名→原值），供原封匯出
    row_no: Optional[int] = None                 # Excel 列號（只給匯入檢核報告用，不入庫；V2.110）


class ImportIn(BaseModel):
    source_file: Optional[str] = None
    note: Optional[str] = None
    findings: list[FindingIn] = []
    # 每張來源表的欄位順序：sheet_key -> [原欄名,...]（供原封匯出 1:1）
    sheet_columns: dict[str, list[str]] = {}
    # V2.110 匯入檢核：前端解析時用的規則版本（sheets.js MultiSheet.RULES_VERSION；舊版前端不帶＝None）
    rules_version: Optional[str] = None
    # 前端才知道的檢核（日期格讀不到、已申請卻找不到日期、解析警告），見 sheets.js checkImport
    client_checks: Optional[dict] = None
    # 新檔資料日期比目前最新一批舊時，要使用者明確確認才覆蓋（否則 409）
    confirm_older: bool = False
    # 上一批有資料的編號工作表這次不見／0 筆，要明確確認才匯入（否則 409）
    confirm_missing_sheets: bool = False
    # 確認綁定狀態：使用者按確認時看到的「目前最新一批」id（來自 409 回應）；匯入那一刻不是它 → 再 409
    expected_latest_batch_id: Optional[int] = None


class ImportResult(BaseModel):
    batch_id: int
    row_count: int
    is_latest: bool
    report: Optional[dict] = None                # 匯入檢核報告（同存在批次上）


class RecomputeItem(BaseModel):
    id: int
    remediation_due: Optional[str] = None
    first_extension_due: Optional[str] = None
    exception_due: Optional[str] = None
    close_status: Optional[str] = None
    remark: Optional[str] = None


class RecomputeIn(BaseModel):
    batch_id: int
    rules_version: str                           # 前端算的規則版本，必須等於伺服器的 RULES_VERSION
    items: list[RecomputeItem] = []
