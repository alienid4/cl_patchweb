"""建立／重設三個本地測試帳號（供角色測試；AD 已啟用時也能用——見 security.authenticate「本地優先」）。

    python -m webvuln.seedtest [密碼]

密碼：命令列參數優先，否則讀 env WEBVULN_TEST_PASSWORD，再否則預設 'test-1234'（請自行改）。
建立／重設：
  superadmin  → super_admin（最高權限；也是「AD 角色調錯被鎖在外」時的本地救援帳號）
  admin       → dept_admin（部門窗口；部門請登入後由 superadmin 於「帳號與權限」指定）
  user        → user（一般使用者；要對應某負責人就把 display_name 改成那個人名）

本地帳號有 password_hash，AD 帳號沒有，兩者不會互相誤認。重跑＝重設密碼與角色（冪等）。
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
    pw = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("WEBVULN_TEST_PASSWORD") or "test-1234"
    init_db()
    db = SessionLocal()
    try:
        for username, disp, role in ACCOUNTS:
            u = db.execute(select(User).where(User.username == username)).scalars().first()
            if u is None:
                u = User(username=username, display_name=disp, role=role, is_active=True)
                db.add(u)
                action = "建立"
            else:
                u.role = role
                u.is_active = True
                if not u.display_name:
                    u.display_name = disp
                action = "重設"
            u.password_hash = security.hash_password(pw)
            db.commit()
            print("  %s %-11s 角色=%s" % (action, username, config.canon_role(role)))
        print("完成。密碼＝ %s" % pw)
        print("用法：登出後，用上面任一帳號＋這組密碼登入，即可切換角色測試。")
        print("提醒：superadmin 是本地救援帳號，請在調整 AD 帳號角色前先確認它可登入。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
