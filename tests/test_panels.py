import datetime as dt

from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _load(session):
    f = FindingIn
    findings = [
        # Critical 已逾期
        f(host="a1", severity="Critical", remediation_due="2026-04-01", close_status="未結案"),
        # Critical 30天內
        f(host="a2", severity="Critical", remediation_due="2026-05-20", close_status="未結案"),
        # High 已逾期，且有例外核准期限(未到期)→ stage 例外管理中、安全名單
        f(host="b1", severity="High", exception_due="2026-06-01", close_status="未結案"),
        # Medium 首次展延中
        f(host="c1", severity="Medium", first_extension_due="2026-05-20", close_status="未結案"),
        # 已結案不計入
        f(host="d1", severity="Low", remediation_due="2026-04-01", close_status="已結案"),
    ]
    importer.create_batch(session, ImportIn(findings=findings))


def test_matrix_reconciles(session):
    _load(session)
    m = query.matrix(session, today=TODAY)
    assert m["total"] == 4                       # 未結案 4
    assert m["cells"]["Critical"]["已逾期"] == 1
    assert m["cells"]["Critical"]["30天內"] == 1
    assert m["row_totals"]["Critical"] == 2
    # 列總和 == 未結案總數（對帳）
    assert sum(m["row_totals"].values()) == m["total"]
    # 欄總和 == 未結案總數（對帳）
    assert sum(m["col_totals"].values()) == m["total"]


def test_stage_stats(session):
    _load(session)
    s = query.stage_stats(session, today=TODAY)
    assert s["total"] == 4
    by = {x["key"]: x["count"] for x in s["stages"]}
    assert by["例外管理中"] == 1       # b1 有例外核准期限
    assert by["首次展延中"] == 1       # c1 有首次展延
    assert by["原始修補期限"] == 2     # a1,a2
    assert s["safe_count"] == 1        # b1 例外核准期限 2026-06-01 > TODAY
