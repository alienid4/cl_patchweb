"""每週部門週報排程進入點（#8）。由 cron／systemd timer 每週一 08:00 呼叫：

    python -m webvuln.weekly

寄給「有開啟每週報告(weekly_report)且有信箱」的部門窗口(dept_admin)，各寄自己部門的週報
（內含直達連結，點開可列印／存 PDF）。寄件人用系統預設寄件人 from_default（自動寄送無操作者）。
失敗或未設定時印訊息並以非 0 離開，方便 cron 記錄。
"""
from __future__ import annotations

import sys

from .db import SessionLocal, init_db
from . import appsettings, mailer


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        cfg = appsettings.get_email_config(db)
        try:
            summary = mailer.send_weekly(db, cfg)
        except ValueError as e:
            print("[weekly] 未執行：%s" % e)
            return 2
        print("[weekly] 寄出 %d　略過 %d　失敗 %d" % (
            summary["sent"], summary["skipped"], summary["failed"]))
        for d in summary["details"]:
            print("  - %s → %s %s" % (d.get("owner"), d.get("to") or "(無)",
                                      ("失敗：" + d["error"]) if d.get("error") else "OK"))
        return 1 if summary["failed"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
