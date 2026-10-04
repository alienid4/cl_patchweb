"""建立／重設三個本地測試帳號（供角色測試；AD 已啟用時也能用——見 security.authenticate「本地優先」）。

    python -m webvuln.seedtest [密碼]          # 已存在的帳號「略過」，只建缺的（不覆蓋既有密碼）
    python -m webvuln.seedtest [密碼] --reset  # 連既有帳號的密碼/角色也重設（救援用）

密碼：命令列參數優先，否則讀 env WEBVULN_TEST_PASSWORD，再否則預設 'test-1234'（請自行改）。
帳號：
  superadmin  → super_admin（最高權限；也是「AD 角色調錯被鎖在外」時的本地救援帳號）
  admin       → dept_admin（部門窗口；部門請登入後由 superadmin 於「帳號與權限」指定）
  user        → user（一般使用者；要對應某負責人就把 display_name 改成那個人名）

預設**不重複建立**：已存在就印「已建立（略過）」，不動它。要重設密碼/角色請加 --reset。
本地帳號有 password_hash，AD 帳號沒有，兩者不會互相誤認。
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import select

from . import config, security
from .db import SessionLocal, init_db
from .models import User

ACCOUNTS = [
    ("superadmin", "本地最高管理員", config.ROLE_SUPER),
    ("admin", "本地部門窗口", config.ROLE_DEPT_ADMIN),
    ("user", "本地一般使用者", config.ROLE_USER),
]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    reset = "--reset" in args
    args = [a for a in args if not a.startswith("--")]
    pw = (args[0] if args else "") or os.environ.get("WEBVULN_TEST_PASSWORD") or "test-1234"
    init_db()
    db = SessionLocal()
    try:
        created = skipped = reset_n = 0
        for username, disp, role in ACCOUNTS:
            u = db.execute(select(User).where(User.username == username)).scalars().first()
            if u is None:
                u = User(username=username, display_name=disp, role=role, is_active=True,
                         password_hash=security.hash_password(pw))
                db.add(u); db.commit()
                created += 1
                print("  建立     %-11s 角色=%s" % (username, config.canon_role(role)))
            elif reset:
                u.role = role; u.is_active = True
                if not u.display_name:
                    u.display_name = disp
                u.password_hash = security.hash_password(pw)
                db.commit()
                reset_n += 1
                print("  重設     %-11s 角色=%s（--reset）" % (username, config.canon_role(role)))
            else:
                skipped += 1
                print("  已建立   %-11s 略過（未改動；要重設密碼請加 --reset）" % username)
        print("完成：新建 %d、略過 %d%s。" % (
            created, skipped, ("、重設 %d" % reset_n) if reset_n else ""))
        if created or reset_n:
            print("密碼＝ %s　（登出後用 superadmin / admin / user ＋此密碼登入切換角色）" % pw)
        print("提醒：superadmin 是本地救援帳號，請在調整 AD 帳號角色前先確認它可登入。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
