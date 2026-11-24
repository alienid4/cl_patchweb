"""領域規則（衍生欄位）。

刻意只放「不會出錯的簡單衍生」：effective_due 依序取值、stage 三分、逾期天數。
**民國年/Excel 序號/多表嚴重度對應那種危險解析留在前端已驗證的 JS**（見決策 import-v1-upload），
後端只收前端算好、正規化好的 ISO 日期與嚴重度，避免在 Python 重寫重生 bug。
normalize_* 只是「前端沒帶時」的後備，不是主要來源。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

# 規則版本（V2.110）：不是程式版本。期限解析（文字日期、多日期取最早）、單號欄、備註申請判定、結案分類
# 這類「會改變 effective_due／stage／close_status」的規則一改就要改這裡，**前端 sheets.js MultiSheet.RULES_VERSION
# 必須同值**（test_v2110_import_check 會檢查）。最新一批的版本跟這裡不同 → 管理者開頁時前端就地重算（不新增批次）。
RULES_VERSION = "V2.108"

STAGE_EXCEPTION = "例外管理中"
STAGE_EXTENSION = "首次展延中"
STAGE_ORIGINAL = "原始修補期限"

CLOSE_OPEN = "未結案"
CLOSE_DONE = "已結案"
CLOSE_OTHER = "其他"

SEVERITIES = ("Critical", "High", "Medium", "Low")

# ── 管理人手動「處理進度」標註（系統疊加層，不碰資安的 Excel）──
# （舊的 6 關手動申請管線 CASE_NEW→…→完成 已於 V2.17 退場，狀態改為下列進度＋備註自動判定）
# 用途：資安發新表前，承辦口頭回報進度，管理人先自己標著，知道這筆正在處理。
PROGRESS_NONE = ""
PROGRESS_WIP = "處理中"
PROGRESS_APPLY_EXT = "要申請展延"  # 第一次做不完，要走展延
PROGRESS_APPLY_EXC = "要申請例外"  # 展延後還做不完，要走例外
PROGRESS_RESCAN = "等複掃"        # 承辦回報已做完、等資安複掃確認
# 送審中（等資安核准）（2026-10-19）：承辦已把展延／例外申請送給資安，但資安還沒更新 Excel。
# 只是承辦疊加層的標記：不改到期日、不改處置階段、仍算未結／逾期；只讓它離開「應申請未申請」、
# 改算在「送審中（等核准）」。資安核准反映到 Excel 後，匯入時自動解除（見 cases.auto_resolve_submitted）。
PROGRESS_SUBMIT_EXT = "展延送審中"
PROGRESS_SUBMIT_EXC = "例外送審中"
PROGRESS_SUBMITTED = (PROGRESS_SUBMIT_EXT, PROGRESS_SUBMIT_EXC)
# 資安已確認結案（待 Excel 更新）（2026-10-28）：資安已用 email／口頭／iForm 確認修好、結案，但來源 Excel 還沒更新。
# 必須附證據（確認人／確認日期／依據，存在 Case.confirm_*）。系統所有計數把它當「已結案」（未結、逾期、需申請…都扣掉），
# 但 Excel／finding 一律不改；Excel 真的結案後匯入時自動解除（見 cases.auto_resolve_confirmed）。
PROGRESS_SEC_CONFIRMED = "資安已確認結案"
# find(status=...) 用的「有效結案狀態」：Excel 未結案、但資安已確認 → 這個值（不是 Excel 的值，只在系統內）
STATUS_SEC_CONFIRMED = "資安已確認"
PROGRESS_VALUES = (PROGRESS_WIP, PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC, PROGRESS_RESCAN,
                   PROGRESS_SUBMIT_EXT, PROGRESS_SUBMIT_EXC, PROGRESS_SEC_CONFIRMED)
# 畫面／匯出顯示用名稱（存值不動）：等複掃→結案申請中（2026-10-06）；送審中一律標「等核准」，絕不顯示成已核准；
# 資安已確認一律帶「待 Excel 更新」，絕不只寫「已結案」（2026-10-28）
PROGRESS_DISPLAY = {
    PROGRESS_RESCAN: "結案申請中",
    PROGRESS_SUBMIT_EXT: "展延送審中（等核准）",
    PROGRESS_SUBMIT_EXC: "例外送審中（等核准）",
    PROGRESS_SEC_CONFIRMED: "資安已確認結案（待 Excel 更新）",
}


def progress_display(p) -> str:
    return PROGRESS_DISPLAY.get(p or "", p or "")

# 複掃對帳三態（結論一律以 Excel 為主；標註只是過程參考）：
RESCAN_CONFIRMED = "已確認結案"   # 標等複掃 且 來源(Excel)已結 → 功成身退
RESCAN_WAITING = "等複掃確認"     # 標等複掃 且 來源未結，但標記在最新匯入「之後」(資料還沒輪到，正常在途)
RESCAN_SUSPECT = "可疑待查"       # 標等複掃 且 來源未結，且已「跨過一次新匯入」仍未結 → 可能浮報

# 申請(展延/例外)對帳三態（拿資安 Excel 的官方處置階段比對）：
APPLY_REFLECTED = "已反映"        # 官方階段已顯示申請成功(展延/例外)
APPLY_SUBMITTING = "送審中"       # 官方還沒變，但標記在最新匯入之後(資料還沒輪到)
APPLY_PENDING = "待查"           # 官方還沒變，且已跨過一次新匯入仍沒變 → 說要申請卻沒送/沒成


def classify_rescan(progress, source_closed, marked_at, latest_imported_at):
    """只對『等複掃』的標註做對帳；回傳三態之一或 None(未標等複掃)。以 Excel 結案與否為準。"""
    if progress != PROGRESS_RESCAN:
        return None
    if source_closed:
        return RESCAN_CONFIRMED
    if latest_imported_at is None or marked_at is None:
        return RESCAN_WAITING
    return RESCAN_SUSPECT if marked_at <= latest_imported_at else RESCAN_WAITING


def classify_apply(progress, stage, marked_at, latest_imported_at):
    """對『要申請展延/要申請例外』做對帳；拿官方處置階段(資安 Excel)比對。回傳三態或 None。"""
    if progress == PROGRESS_APPLY_EXT:
        reflected = stage in (STAGE_EXTENSION, STAGE_EXCEPTION)  # 到展延或更後(例外)都算已反映
    elif progress == PROGRESS_APPLY_EXC:
        reflected = stage == STAGE_EXCEPTION
    else:
        return None
    if reflected:
        return APPLY_REFLECTED
    if latest_imported_at is None or marked_at is None:
        return APPLY_SUBMITTING
    return APPLY_PENDING if marked_at <= latest_imported_at else APPLY_SUBMITTING


def submit_reflected(progress, stage) -> bool:
    """送審中的申請，資安 Excel 的處置階段是否已顯示核准（展延送審中→展延或例外；例外送審中→例外）。"""
    if progress == PROGRESS_SUBMIT_EXT:
        return stage in (STAGE_EXTENSION, STAGE_EXCEPTION)
    if progress == PROGRESS_SUBMIT_EXC:
        return stage == STAGE_EXCEPTION
    return False


def submit_pending(progress, stage) -> bool:
    """標了送審中、且資安 Excel 還沒反映核准（＝真的在等核准）。"""
    return progress in PROGRESS_SUBMITTED and not submit_reflected(progress, stage)


# 送審中對帳三態（2026-10-19）
SUBMIT_WAITING = "等核准"         # 已送審、資安還沒核准（正常在途）
SUBMIT_STALE = "送審逾時"         # 送審超過 N 天（系統設定 submit_stale_days，預設 14）仍未核准 → 要追
SUBMIT_STALE_DAYS_DEFAULT = 14


def submit_age_days(marked_at, now=None) -> Optional[int]:
    if marked_at is None:
        return None
    now = now or dt.datetime.now()
    return (now - marked_at).days


def classify_submit(progress, stage, marked_at, stale_days=None, now=None, approval_expired=False):
    """對『展延送審中／例外送審中』做對帳：已反映(資安核准且還沒過期)／送審逾時／等核准；非送審中回 None。
    approval_expired＝這筆的例外／展延今天已過期：那個核准已失效，承辦送的是續申請，照樣等核准。"""
    if progress not in PROGRESS_SUBMITTED:
        return None
    if submit_reflected(progress, stage) and not approval_expired:
        return APPLY_REFLECTED
    n = SUBMIT_STALE_DAYS_DEFAULT if stale_days is None else stale_days
    if marked_at is not None and ((now or dt.datetime.now()) - marked_at) > dt.timedelta(days=n):
        return SUBMIT_STALE
    return SUBMIT_WAITING


# 資安已確認結案對帳（2026-10-28）
CONFIRM_WAITING = "已確認待更新"     # 資安已確認、Excel 還沒更新（正常在途）
CONFIRM_STALE = "確認逾期未更新"     # 確認日期超過 N 天（confirm_stale_days，預設 14）Excel 仍未結案 → 要追
CONFIRM_STALE_DAYS_DEFAULT = 14


def confirm_age_days(confirm_date, today=None) -> Optional[int]:
    if confirm_date is None:
        return None
    return ((today or dt.date.today()) - confirm_date).days


def classify_confirm(progress, source_closed, confirm_date, stale_days=None, today=None):
    """對『資安已確認結案』做對帳：Excel 已結案→已確認結案；確認超過 N 天 Excel 仍未結→確認逾期未更新；否則已確認待更新。"""
    if progress != PROGRESS_SEC_CONFIRMED:
        return None
    if source_closed:
        return RESCAN_CONFIRMED
    n = CONFIRM_STALE_DAYS_DEFAULT if stale_days is None else stale_days
    age = confirm_age_days(confirm_date, today)
    if age is not None and age > n:
        return CONFIRM_STALE
    return CONFIRM_WAITING


def classify_progress(progress, source_closed, stage, marked_at, latest_imported_at,
                      stale_days=None, now=None, approval_expired=False,
                      confirm_date=None, confirm_stale_days=None):
    """綜合：等複掃走 classify_rescan，要申請展延/例外走 classify_apply，送審中走 classify_submit，
    資安已確認走 classify_confirm，其餘 None。"""
    if progress == PROGRESS_SEC_CONFIRMED:
        return classify_confirm(progress, source_closed, confirm_date, confirm_stale_days,
                                now.date() if now else None)
    if progress == PROGRESS_RESCAN:
        return classify_rescan(progress, source_closed, marked_at, latest_imported_at)
    if progress in PROGRESS_SUBMITTED:
        return classify_submit(progress, stage, marked_at, stale_days, now, approval_expired)
    return classify_apply(progress, stage, marked_at, latest_imported_at)


# 需人工追查的「⚠️」狀態(可疑/待查/送審逾時)——主管週報用來點出風險
# 送審逾時也列入待追查（2026-10-19）：送出去太久資安沒動靜，跟「說要申請卻沒反映」一樣要有人去追。
# 確認逾期未更新也列入（2026-10-28）：資安說結了、Excel 卻遲遲不更新，要有人去追資安更新 Excel。
FLAGGED_STATES = (RESCAN_SUSPECT, APPLY_PENDING, SUBMIT_STALE, CONFIRM_STALE)


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


# 單號欄（V2.108，2026-11-23 真實 Excel 校準）：「10-外部威脅情資」不在備註寫 iForm，而是另有
# 「例外單號」「弱點修補展延單號」欄。欄名同時含「例外」＋「單號」的欄有填＝已申請例外；
# 含「展延」＋「單號」的欄有填＝已申請展延。前端 utils.js ticketApplied 同規則（兩邊要一致）。
# 只填佔位字（-、無、N/A…）不算有填。
_TICKET_BLANK = {"", "-", "－", "—", "無", "n/a", "na", "/", "none"}


def _ticket_filled(raw, word: str) -> bool:
    if not isinstance(raw, dict):
        return False
    for k, v in raw.items():
        ks = str(k or "")
        if word in ks and "單號" in ks:
            s = str(v if v is not None else "").strip()
            if s.lower() not in _TICKET_BLANK:
                return True
    return False


def applied_exc(remark, raw=None) -> bool:
    """已申請例外：備註規則（_applied_exc）或「例外…單號」欄有填。"""
    return _applied_exc(remark) or _ticket_filled(raw, "例外")


def applied_ext(remark, raw=None) -> bool:
    """已申請展延：備註規則（_applied_ext）或「展延…單號」欄有填。"""
    return _applied_ext(remark) or _ticket_filled(raw, "展延")


def compute_effective_due(exception_due, first_extension_due, remediation_due,
                          remark=None, raw=None) -> Optional[dt.date]:
    """真正到期日。**關鍵規則（2026-10-03 志安釐清）**：
    展延/例外的日期『有填』不代表已申請——要「有對應申請紀錄」才算數（備註空且無單號＝沒申請）。
    申請紀錄＝備註寫了（例外管理(iForm_…) 等），或原始列的單號欄有填（V2.108，raw＝原始整列）。
      - 已申請例外 且例外核准期限有填 → 用例外核准期限
      - 否則 已申請展延 且首次展延上限有填 → 用首次展延上限
      - 否則 → 一律用修補期限（原始）
    優先序 例外管理 > 首次展延 > 原始修補期限。"""
    if exception_due and applied_exc(remark, raw):
        return exception_due
    if first_extension_due and applied_ext(remark, raw):
        return first_extension_due
    return remediation_due


def compute_stage(exception_due, first_extension_due, remediation_due=None, remark=None, raw=None) -> str:
    """處置階段，判法同 compute_effective_due（要有對應申請紀錄才算展延/例外）。"""
    if exception_due and applied_exc(remark, raw):
        return STAGE_EXCEPTION
    if first_extension_due and applied_ext(remark, raw):
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
