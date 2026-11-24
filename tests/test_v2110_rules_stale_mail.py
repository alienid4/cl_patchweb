"""V2.110 裁判：最新一批還沒用新判斷規則重算時，排程不寄、發告警；立即寄出也擋。"""
import datetime as dt

import pytest

from webvuln import automail, logic
from webvuln.models import ImportBatch


def _import(client):
    fs = [dict(sheet_key="1-x", plugin_id="1", name="v", host="h1", severity="High", department="A",
               owner="x", remediation_due=(dt.date.today() + dt.timedelta(days=5)).isoformat(), close_status="未結案")]
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs,
                                            "rules_version": logic.RULES_VERSION}).status_code == 200


def _set_latest_rules(s, v):
    b = s.query(ImportBatch).filter(ImportBatch.is_latest.is_(True)).first()
    b.rules_version = v
    s.commit()


@pytest.mark.rules_gate
def test_rules_stale_detected(client, session):
    _import(client)
    assert automail._rules_stale(session) is None
    _set_latest_rules(session, "OLD")
    msg = automail._rules_stale(session)
    assert msg and "OLD" in msg


@pytest.mark.rules_gate
def test_run_now_blocked_when_stale(client, session):
    _import(client)
    _set_latest_rules(session, "OLD")
    try:
        automail.run_now(session, dt.datetime.now(), "tester")
        assert False, "應該被擋"
    except ValueError as e:
        assert "重算" in str(e)
