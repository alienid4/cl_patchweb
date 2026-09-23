import datetime as dt

from webvuln import logic


def test_parse_iso_date():
    assert logic.parse_iso_date("2026-05-01") == dt.date(2026, 5, 1)
    assert logic.parse_iso_date(dt.date(2026, 5, 1)) == dt.date(2026, 5, 1)
    assert logic.parse_iso_date("") is None
    assert logic.parse_iso_date(None) is None
    assert logic.parse_iso_date("not-a-date") is None


def test_effective_due_order():
    exc = dt.date(2026, 3, 1)
    ext = dt.date(2026, 4, 1)
    rem = dt.date(2026, 5, 1)
    assert logic.compute_effective_due(exc, ext, rem) == exc      # 例外優先
    assert logic.compute_effective_due(None, ext, rem) == ext     # 無例外→首次展延
    assert logic.compute_effective_due(None, None, rem) == rem    # 皆無→修補期限
    assert logic.compute_effective_due(None, None, None) is None


def test_stage():
    d = dt.date(2026, 5, 1)
    assert logic.compute_stage(d, None) == logic.STAGE_EXCEPTION
    assert logic.compute_stage(None, d) == logic.STAGE_EXTENSION
    assert logic.compute_stage(None, None) == logic.STAGE_ORIGINAL


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
