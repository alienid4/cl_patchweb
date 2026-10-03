import datetime as dt

from webvuln import logic


def test_parse_iso_date():
    assert logic.parse_iso_date("2026-05-01") == dt.date(2026, 5, 1)
    assert logic.parse_iso_date(dt.date(2026, 5, 1)) == dt.date(2026, 5, 1)
    assert logic.parse_iso_date("") is None
    assert logic.parse_iso_date(None) is None
    assert logic.parse_iso_date("not-a-date") is None


def test_effective_due_needs_remark():
    """展延/例外的日期要『備註有對應申請紀錄』才算；備註空＝沒申請→用修補期限。"""
    exc = dt.date(2026, 3, 1)
    ext = dt.date(2026, 4, 1)
    rem = dt.date(2026, 5, 1)
    # 備註空：展延/例外都不算 → 一律修補期限
    assert logic.compute_effective_due(exc, ext, rem) == rem
    assert logic.compute_effective_due(exc, ext, rem, "") == rem
    assert logic.compute_effective_due(exc, ext, rem, "設備待汰換") == rem  # 無關備註也不算
    # 備註有「例外管理」→ 例外優先
    assert logic.compute_effective_due(exc, ext, rem, "例外管理(iForm_1)") == exc
    # 備註只有「首次展延」→ 用展延(即使例外日期有填,因為沒申請例外)
    assert logic.compute_effective_due(exc, ext, rem, "首次展延(iForm_2)") == ext
    # 備註兩者都有 → 例外 > 展延
    assert logic.compute_effective_due(exc, ext, rem, "例外管理(iForm_1) 首次展延(iForm_2)") == exc
    # 有申請紀錄但對應日期沒填 → 退回修補
    assert logic.compute_effective_due(None, None, rem, "首次展延(iForm_2)") == rem
    assert logic.compute_effective_due(None, None, None) is None


def test_applied_keyword_C():
    """C 折衷：含申請動作的自由寫法也算；『排除/解除』不算。"""
    for rm in ("例外管理(iForm_1)", "例外申請#25", "已簽核例外(202304004)", "已申請例外#9"):
        assert logic._applied_exc(rm) is True, rm
    for rm in ("例外狀況已排除", "例外已解除", "設備待汰換", ""):
        assert logic._applied_exc(rm) is False, rm
    for rm in ("首次展延(iForm_2)", "已展延", "展延申請單#123", "已申請展延iform#456"):
        assert logic._applied_ext(rm) is True, rm
    for rm in ("設備待汰換", ""):
        assert logic._applied_ext(rm) is False, rm


def test_stage_needs_remark():
    d = dt.date(2026, 5, 1)
    assert logic.compute_stage(d, d, d, "例外管理(iForm_1)") == logic.STAGE_EXCEPTION
    assert logic.compute_stage(d, d, d, "首次展延(iForm_2)") == logic.STAGE_EXTENSION
    assert logic.compute_stage(d, d, d, "") == logic.STAGE_ORIGINAL        # 備註空→原始
    assert logic.compute_stage(d, d, d, "廠商處理中") == logic.STAGE_ORIGINAL  # 無關備註→原始
    assert logic.compute_stage(None, None, None) == logic.STAGE_ORIGINAL


def test_overdue_days():
    today = dt.date(2026, 5, 10)
    assert logic.overdue_days(dt.date(2026, 5, 1), today) == 9   # 逾期 9 天
    assert logic.overdue_days(dt.date(2026, 5, 20), today) == -10  # 未到期
    assert logic.overdue_days(None, today) is None


def test_classify_close():
    assert logic.classify_close("未結案") == logic.CLOSE_OPEN
    assert logic.classify_close("進行中") == logic.CLOSE_OPEN
    assert logic.classify_close("") == logic.CLOSE_OPEN
    assert logic.classify_close("已結案") == logic.CLOSE_DONE
    assert logic.classify_close("已修補") == logic.CLOSE_DONE
    assert logic.classify_close("尚未修補") == logic.CLOSE_OPEN  # 含「未」優先→未結案
    assert logic.classify_close("待評估") == logic.CLOSE_OTHER


def test_normalize_severity():
    assert logic.normalize_severity("嚴重") == "Critical"
    assert logic.normalize_severity("high") == "High"
    assert logic.normalize_severity("中") == "Medium"
    assert logic.normalize_severity("Low") == "Low"
    assert logic.normalize_severity(None) is None
