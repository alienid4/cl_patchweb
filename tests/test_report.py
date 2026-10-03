import datetime as dt

from webvuln import cases, importer, query
from webvuln.models import Finding
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _load(session):
    findings = [
        # A: 未結、原始、到期 05-20 High 提前期30 → 行動線已過 → 應申請未申請(未過真正到期日=如期)
        FindingIn(host="a", severity="High", owner="玄慈", department="資訊架構部",
                  remediation_due="2026-05-20", close_status="未結案"),
        # B: 已例外管理中(備註有申請紀錄)→ 已申請處置中；到期 05-25 → 如期
        FindingIn(host="b", severity="High", owner="喬峰", department="資訊架構部",
                  remediation_due="2026-05-01", exception_due="2026-05-25", close_status="未結案",
                  remark="例外管理(iForm_9)"),
        # C: 未結、原始、到期很遠 → 不需申請、如期
        FindingIn(host="c", severity="Low", owner="阿朱", department="資安部",
                  remediation_due="2026-12-01", close_status="未結案"),
        # E: 未結、原始、已逾修補期限且無申請 → 應申請未申請 且 落後(逾期)
        FindingIn(host="e", severity="High", owner="段譽", department="資訊架構部",
                  remediation_due="2026-04-01", close_status="未結案"),
    ]
    return importer.create_batch(session, ImportIn(findings=findings))


def test_weekly_report_buckets(session):
    _load(session)
    r = query.weekly_report(session, today=TODAY)
    assert r["unresolved"] == 4
    assert r["need_apply_count"] == 2          # A、E
    assert r["applied_count"] == 1             # B(例外管理)
    assert r["apply_universe"] == 3            # 2 未申請 + 1 已申請
    assert r["overdue"] == 1                   # E 已逾真正到期日
    assert r["on_track"] == 3                  # A、B、C
    hosts_need = {x["host"] for x in r["need_apply_list"]}
    assert hosts_need == {"a", "e"}
    hosts_over = {x["host"] for x in r["overdue_list"]}
    assert hosts_over == {"e"}


def test_weekly_report_department_filter(session):
    _load(session)
    r = query.weekly_report(session, department="資安部", today=TODAY)
    assert r["unresolved"] == 1                # 只有 C
    assert r["need_apply_count"] == 0
    assert r["overdue"] == 0


def test_weekly_report_target_date_summary(session):
    _load(session)
    # 幫 A 填預計完成日(未來) → 進「已回報」；E 不填 → 「未回報(要催)」
    a = session.query(Finding).filter_by(host="a").one()
    cases.set_overlay(session, a.id, {"target_date": "2026-05-18"})
    r = query.weekly_report(session, today=TODAY)
    assert r["target"]["with_target"] == 1     # A
    assert r["target"]["no_target"] == 2       # E、B(母體內其餘未填)
    assert r["target"]["target_overdue"] == 0  # A 的預計日在未來
    assert r["target"]["target_soon"] == 1     # A 預計 8 天後完成


def test_target_date_overlay_survives_reimport(session):
    f = FindingIn
    importer.create_batch(session, ImportIn(findings=[
        f(host="h7", plugin_id="p7", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    fid = session.query(Finding).one().id
    out = cases.set_overlay(session, fid, {"target_date": "2026-11-30"})
    assert out["target_date"] == "2026-11-30"
    rows = query.find(session, status="未結案")
    assert rows[0]["target_date"] == "2026-11-30"
    # 重匯(Excel 無此欄)→ 預計完成日仍在
    importer.create_batch(session, ImportIn(findings=[
        f(host="h7", plugin_id="p7", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    rows2 = query.find(session, status="未結案")
    assert rows2[0]["target_date"] == "2026-11-30"
    # 清除
    cases.set_overlay(session, fid, {"target_date": ""})
    rows3 = query.find(session, status="未結案")
    assert rows3[0]["target_date"] is None


def test_report_endpoint(client):
    client.post("/api/import", json={"findings": [
        {"host": "a", "severity": "High", "remediation_due": "2026-04-01", "close_status": "未結案"},
    ]})
    r = client.get("/api/report")
    assert r.status_code == 200
    body = r.json()
    assert "need_apply_count" in body and "target" in body
