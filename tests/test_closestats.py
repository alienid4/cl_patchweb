import datetime as dt

from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _imp(session, findings):
    importer.create_batch(session, ImportIn(findings=findings))


def test_close_stats_delta(session):
    f = FindingIn
    # 第一批：三筆未結（含 host 大小寫/空白，測正規化鍵）
    _imp(session, [
        f(host="H1 ", plugin_id="p1", sheet_key="s", owner="玄慈",
          remediation_due="2026-06-01", close_status="未結案"),
        f(host="h2", plugin_id="p2", sheet_key="s", owner="喬峰",
          remediation_due="2026-06-01", close_status="未結案"),
        f(host="h3", plugin_id="p3", sheet_key="s", owner="玄慈",
          remediation_due="2026-06-01", close_status="未結案"),
    ])
    # 第二批：h1 與 h2 結案（h1 用不同大小寫/空白仍視為同一），h3 仍未結，新增 h4 已結
    _imp(session, [
        f(host="h1", plugin_id="p1", sheet_key="s", owner="玄慈",
          remediation_due="2026-06-01", close_status="已結案"),
        f(host="H2 ", plugin_id="p2", sheet_key="s", owner="喬峰",
          remediation_due="2026-06-01", close_status="已結案"),
        f(host="h3", plugin_id="p3", sheet_key="s", owner="玄慈",
          remediation_due="2026-06-01", close_status="未結案"),
        f(host="h4", plugin_id="p4", sheet_key="s", owner="喬峰",
          remediation_due="2026-06-01", close_status="已結案"),
    ])

    r = query.close_stats(session, today=TODAY)
    assert r["new_closed"] == 3          # h1, h2（上期未結→結）＋ h4（新出現即結）
    assert r["source_confirmed"] == 3
    assert r["claimed_unconfirmed"] == 0
    by = {x["name"]: x["closed"] for x in r["by_closer"]}
    assert by.get("玄慈") == 1 and by.get("喬峰") == 2


def test_close_stats_first_batch(session):
    f = FindingIn
    _imp(session, [
        f(host="h1", plugin_id="p1", owner="玄慈",
          remediation_due="2026-06-01", close_status="已結案"),
    ])
    # 只有一批：無上期，已結者算「新出現即結」
    r = query.close_stats(session, today=TODAY)
    assert r["new_closed"] == 1
