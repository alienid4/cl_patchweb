import datetime as dt

from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _load(session):
    f = FindingIn
    findings = [
        f(host="a1", severity="Critical", owner="玄慈", department="資訊架構部",
          remediation_due="2026-04-01", close_status="未結案"),   # 玄慈 逾期
        f(host="a2", severity="High", owner="玄慈", department="資訊架構部",
          remediation_due="2026-04-05", close_status="未結案"),    # 玄慈 逾期
        f(host="b1", severity="Medium", owner="喬峰", department="資訊架構部",
          remediation_due="2026-04-10", close_status="未結案"),    # 喬峰 逾期
        f(host="c1", severity="Low", owner="喬峰", department="資安部",
          remediation_due="2026-03-01", close_status="已結案"),    # 喬峰 已結
    ]
    importer.create_batch(session, ImportIn(findings=findings))


def test_ranking_by_owner(session):
    _load(session)
    r = query.ranking_by_owner(session, today=TODAY)
    assert r[0]["name"] == "玄慈"          # 逾期最多在前
    assert r[0]["overdue"] == 2 and r[0]["unresolved"] == 2
    qiao = next(x for x in r if x["name"] == "喬峰")
    assert qiao["overdue"] == 1 and qiao["closed"] == 1


def test_sla(session):
    _load(session)
    s = {row["severity"]: row for row in query.sla(session, today=TODAY)}
    assert s["Critical"]["policy_days"] == 7
    assert s["Critical"]["unresolved"] == 1 and s["Critical"]["overdue"] == 1
    assert s["Critical"]["met_rate"] == 0.0     # 唯一一筆逾期 → 0%
    assert s["Low"]["unresolved"] == 0 and s["Low"]["met_rate"] == 100.0
