import datetime as dt

from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _load(session, imported=None):
    findings = [
        # A: 未結、原始修補期限、到期 05-20(10天後)。High 提前期30 → 行動線 04-20 已過 → 應提申請未提
        FindingIn(host="a", severity="High", owner="玄慈", department="資訊架構部",
                  remediation_due="2026-05-20", close_status="未結案"),
        # B: 未結、已例外管理中(不算應提申請未提,因已申請過)
        FindingIn(host="b", severity="High", owner="喬峰", department="資訊架構部",
                  remediation_due="2026-05-01", exception_due="2026-05-25", close_status="未結案"),
        # C: 未結、原始、到期很遠 2026-12-01 → 未過行動線 → 不算
        FindingIn(host="c", severity="Low", owner="阿朱", department="資安部",
                  remediation_due="2026-12-01", close_status="未結案"),
        # D: 未結、無負責人、無到期日 → 缺口
        FindingIn(host="d", severity="Medium", owner="", department="資訊架構部",
                  close_status="未結案"),
    ]
    b = importer.create_batch(session, ImportIn(findings=findings))
    if imported:
        b.imported_at = imported
        session.commit()
    return b


def test_should_apply(session):
    _load(session)
    s = query.summary(session, today=TODAY)
    assert s["should_apply"] == 1          # 只有 A
    rows = query.find(session, only_should_apply=True, today=TODAY)
    assert len(rows) == 1 and rows[0]["host"] == "a"
    assert rows[0]["action_line"] == "2026-04-20"   # 05-20 − 30 天


def test_gaps(session):
    _load(session)
    s = query.summary(session, today=TODAY)
    assert s["gaps"]["no_owner"] == 1      # D
    assert s["gaps"]["no_due"] == 1        # D
    assert len(query.find(session, no_owner=True, today=TODAY)) == 1
    assert len(query.find(session, no_due=True, today=TODAY)) == 1


def test_freshness(session):
    _load(session, imported=dt.datetime(2026, 5, 3, 9, 0))
    s = query.summary(session, today=TODAY)
    assert s["freshness"]["days_ago"] == 7   # 05-10 − 05-03
