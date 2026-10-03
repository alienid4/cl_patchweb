"""領域規則（衍生欄位）。

刻意只放「不會出錯的簡單衍生」：effective_due 依序取值、stage 三分、逾期天數。
**民國年/Excel 序號/多表嚴重度對應那種危險解析留在前端已驗證的 JS**（見決策 import-v1-upload），
後端只收前端算好、正規化好的 ISO 日期與嚴重度，避免在 Python 重寫重生 bug。
normalize_* 只是「前端沒帶時」的後備，不是主要來源。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

STAGE_EXCEPTION = "例外管理中"
STAGE_EXTENSION = "首次展延中"
STAGE_ORIGINAL = "原始修補期限"

CLOSE_OPEN = "未結案"
CLOSE_DONE = "已結案"
CLOSE_OTHER = "其他"

SEVERITIES = ("Critical", "High", "Medium", "Low")

# ── 承辦申請管線（case 疊加層）──
CASE_NEW = "未申請"
CASE_WAIT_MGR = "待主管"
CASE_WAIT_SEC = "待資安"
CASE_APPROVED = "核准"
CASE_DONE = "完成"
CASE_RETURNED = "退回補件"

CASE_STATUSES = (CASE_NEW, CASE_WAIT_MGR, CASE_WAIT_SEC, CASE_APPROVED, CASE_DONE, CASE_RETURNED)

# 允許的狀態轉移（其餘一律擋下）：走完 = 完成（終態）；任一審核可退回補件；退回後回到待主管
CASE_TRANSITIONS = {
    CASE_NEW: (CASE_WAIT_MGR,),
    CASE_WAIT_MGR: (CASE_WAIT_SEC, CASE_RETURNED),
    CASE_WAIT_SEC: (CASE_APPROVED, CASE_RETURNED),
    CASE_APPROVED: (CASE_DONE,),
    CASE_RETURNED: (CASE_WAIT_MGR,),
    CASE_DONE: (),
}


def can_transition(frm: str, to: str) -> bool:
    return to in CASE_TRANSITIONS.get(frm, ())


def parse_iso_date(value) -> Optional[dt.date]:
    """只吃 ISO 字串或 date；吃不下回 None（不做民國年換算——那是前端的事）。"""
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# 備註裡的「申請紀錄」判定（C 折衷，2026-10-03 對真實資料校準）：
# 標準寫法是「例外管理(iForm_…)」「首次展延(iForm_…)」，但實務上有人工自由寫法
# （例外申請#…、已簽核例外、已展延、展延申請單#… 等）。C＝出現對應詞＋申請動作就算，
# 並排除「排除/解除」這種消去語。備註空＝沒申請。
_APPLY_ACTION = ("申請", "管理", "簽核", "核准")  # ＋ iForm 編號(見下)


def _has_action(s: str) -> bool:
    return any(k in s for k in _APPLY_ACTION) or ("iform" in s.lower())


def _applied_exc(remark) -> bool:
    """備註是否代表『已申請例外』：含「例外」＋申請動作，且非「排除/解除」。"""
    s = str(remark or "")
    return ("例外" in s) and _has_action(s) and ("排除" not in s) and ("解除" not in s)


def _applied_ext(remark) -> bool:
    """備註是否代表『已申請展延』：含「展延」＋(申請動作 或『已展延』)。"""
    s = str(remark or "")
    return ("展延" in s) and (_has_action(s) or ("已展延" in s))


def compute_effective_due(exception_due, first_extension_due, remediation_due,
                          remark=None) -> Optional[dt.date]:
    """真正到期日。**關鍵規則（2026-10-03 志安釐清）**：
    展延/例外的日期『有填』不代表已申請——要「備註有對應申請紀錄」才算數（備註空＝沒申請）。
      - 備註有「例外管理」且例外核准期限有填 → 用例外核准期限
      - 否則 備註有「首次展延」且首次展延上限有填 → 用首次展延上限
      - 否則 → 一律用修補期限（原始）
    優先序 例外管理 > 首次展延 > 原始修補期限。"""
    if exception_due and _applied_exc(remark):
        return exception_due
    if first_extension_due and _applied_ext(remark):
        return first_extension_due
    return remediation_due


def compute_stage(exception_due, first_extension_due, remediation_due=None, remark=None) -> str:
    """處置階段，判法同 compute_effective_due（備註要有對應申請紀錄才算展延/例外）。"""
    if exception_due and _applied_exc(remark):
        return STAGE_EXCEPTION
    if first_extension_due and _applied_ext(remark):
        return STAGE_EXTENSION
    return STAGE_ORIGINAL


def overdue_days(effective_due, today: Optional[dt.date] = None) -> Optional[int]:
    """逾期天數＝今天 − 真正到期日（正值為已逾期）。不存 DB，查詢時現算，天生避開跨午夜問題。"""
    if not effective_due:
        return None
    today = today or dt.date.today()
    return (today - effective_due).days


# ── 後備正規化（前端沒帶時才用）──
_SEV_MAP = {
    "critical": "Critical", "嚴重": "Critical", "危急": "Critical",
    "high": "High", "高": "High",
    "medium": "Medium", "中": "Medium", "中等": "Medium",
    "low": "Low", "低": "Low",
}


def normalize_severity(raw) -> Optional[str]:
    if raw is None:
        return None
    if raw in SEVERITIES:
        return raw
    key = str(raw).strip().lower()
    return _SEV_MAP.get(key) or _SEV_MAP.get(str(raw).strip())


def classify_close(raw) -> str:
    """未結/進行中→未結案；結案/已結案/已修補且無『未』→已結案；其餘→其他（決策 close-rule）。"""
    if raw is None or str(raw).strip() == "":
        return CLOSE_OPEN
    s = str(raw).strip()
    if "未" in s or "進行中" in s:
        return CLOSE_OPEN
    if any(k in s for k in ("結案", "已結", "已修補", "已修復", "完成")):
        return CLOSE_DONE
    return CLOSE_OTHER
