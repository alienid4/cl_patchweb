import datetime as dt

from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _load(session):
    findings = [
        # A: 未結、已逾期、Critical、資訊架構部
        FindingIn(host="10.30.1.11", severity="Critical", department="資訊架構部",
                  owner="玄慈", remediation_due="2026-04-20", close_status="未結案"),
        # B: 未結、30天內(20天)、Medium、資訊架構部
        FindingIn(host="10.21.1.2", severity="Medium", department="資訊架構部",
                  owner="喬峰", remediation_due="2026-05-30", close_status="未結案"),
        # C: 未結、31–90天(83天)、High、資安部
        FindingIn(host="10.40.1.5", severity="High", department="資安部",
                  owner="阿朱", remediation_due="2026-08-01", close_status="未結案"),
        # D: 已結案、Low、資訊架構部
        FindingIn(host="10.30.1.9", severity="Low", department="資訊架構部",
                  owner="白世鏡", remediation_due="2026-03-01", close_status="已結案"),
        # E: 未結、無到期日、Low、資訊架構部
        FindingIn(host="10.30.1.7", severity="Low", department="資訊架構部",
                  owner="阮星竹", close_status="未結案"),
    ]
    importer.create_batch(session, ImportIn(source_file="t.xlsx", findings=findings))


def test_summary_numbers(session):
    _load(session)
    s = query.summary(session, today=TODAY)
    assert s["unresolved"] == 4          # A,B,C,E
    assert s["overdue"] == 1             # A
    assert s["due_soon"] == 1            # B
    assert s["high_risk"] == 2           # A(Critical)+C(High)
    assert s["closed"] == 1             # D
    assert s["close_rate"] == 20.0       # 1/(4+1)
    assert s["severity"] == {"Critical": 1, "High": 1, "Medium": 1, "Low": 1}


def test_bands_reconcile(session):
    _load(session)
    s = query.summary(session, today=TODAY)
    # 到期時間帶互斥 → 相加＝未結案（對帳鐵則）
    assert sum(s["bands"].values()) == s["unresolved"]
    assert s["bands"]["已逾期"] == 1
    assert s["bands"]["30天內"] == 1
    assert s["bands"]["31–90天"] == 1
    assert s["bands"]["無到期日"] == 1


def test_drilldown_count_matches_card(session):
    _load(session)
    s = query.summary(session, today=TODAY)
    overdue_rows = query.find(session, band="已逾期", today=TODAY)
    assert len(overdue_rows) == s["overdue"]      # 下鑽筆數＝卡片數字
    assert overdue_rows[0]["owner"] == "玄慈"
    assert overdue_rows[0]["overdue_days"] == 20  # 2026-05-10 − 2026-04-20


def test_department_filter(session):
    _load(session)
    s = query.summary(session, department="資訊架構部", today=TODAY)
    assert s["unresolved"] == 3   # A,B,E（C 屬資安部，排除）
    assert query.departments(session) == ["資安部", "資訊架構部"]


def test_keyword_search(session):
    _load(session)
    rows = query.find(session, status="全部", keyword="玄慈", today=TODAY)
    assert len(rows) == 1 and rows[0]["host"] == "10.30.1.11"
