"""例外管理中、例外核准期限未到 → 任何統計都不可算成逾期（2026-10-05 使用者回報畫面出現「逾期天數 -87」）。

重現使用者那筆：修補期限 2026/02/18 早已過，但已申請例外、核准到 2026/12/31。
"""
import datetime as dt

from webvuln import importer, logic, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 10, 5)


def _load(session):
    importer.create_batch(session, ImportIn(source_file="t.xlsx", findings=[
        # 例外管理中：原始修補期限已過，例外核准到年底
        FindingIn(host="10.30.9.11", plugin_id="237246", name="VMware ESXi 測試", severity="High",
                  department="資訊架構部", owner="林平之", remediation_due="2026-02-18",
                  first_extension_due="2026-05-18", exception_due="2026-12-31",
                  remark="例外管理(iForm_123)", close_status="未結案"),
        # 對照組：同樣日期但備註沒有申請紀錄 → 依規則用修補期限，確實逾期
        FindingIn(host="10.30.9.12", plugin_id="237246", name="VMware ESXi 測試", severity="High",
                  department="資訊架構部", owner="林平之", remediation_due="2026-02-18",
                  first_extension_due="2026-05-18", exception_due="2026-12-31",
                  remark="", close_status="未結案"),
    ]))


def test_effective_due_and_stage():
    exc, ext, rem = dt.date(2026, 12, 31), dt.date(2026, 5, 18), dt.date(2026, 2, 18)
    assert logic.compute_effective_due(exc, ext, rem, "例外管理(iForm_123)") == exc
    assert logic.compute_stage(exc, ext, rem, "例外管理(iForm_123)") == logic.STAGE_EXCEPTION
    # 未到期回負數（距到期還有 87 天）；前端只把 > 0 當逾期顯示
    assert logic.overdue_days(exc, TODAY) == -87


def test_not_counted_overdue_anywhere(session):
    _load(session)
    rows = {r["host"]: r for r in query.find(session, today=TODAY)}
    exc_row, plain_row = rows["10.30.9.11"], rows["10.30.9.12"]
    assert exc_row["effective_due"] == "2026-12-31"
    assert exc_row["overdue_days"] == -87          # 不是逾期
    assert plain_row["overdue_days"] == 229        # 對照組：沒申請紀錄，確實逾期

    s = query.summary(session, today=TODAY)
    assert s["overdue"] == 1                       # 只有對照組那筆

    owners = query.ranking_by_owner(session, today=TODAY)
    assert sum(o["overdue"] for o in owners) == 1
