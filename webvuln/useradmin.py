"""帳號管理 CLI（本地帳號）。密碼不寫死：從 env WEBVULN_PWD 取，沒有就隨機產生並印出一次。

用法：
  py -m webvuln.useradmin add <username> <role> [--name N] [--email E] [--dept D]
  py -m webvuln.useradmin passwd <username>
  py -m webvuln.useradmin list
role: admin / 承辦 / viewer
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

from sqlalchemy import select

from . import config, security
from .db import SessionLocal, init_db
from .models import User


def _pwd() -> tuple[str, bool]:
    env = os.environ.get("WEBVULN_PWD")
    if env:
        return env, False
    return secrets.token_urlsafe(12), True


def cmd_add(args) -> int:
    init_db()
    with SessionLocal() as s:
        if s.execute(select(User).where(User.username == args.username)).scalars().first():
            print(f"帳號已存在：{args.username}", file=sys.stderr)
            return 1
        pwd, generated = _pwd()
        u = security.create_user(s, args.username, pwd, role=args.role,
                                 display_name=args.name, email=args.email, department=args.dept)
        print(f"已建立 {u.username}（{u.role}）")
        if generated:
            print(f"  臨時密碼（請立即變更）：{pwd}")
    return 0


def cmd_passwd(args) -> int:
    init_db()
    with SessionLocal() as s:
        u = s.execute(select(User).where(User.username == args.username)).scalars().first()
        if not u:
            print(f"查無帳號：{args.username}", file=sys.stderr)
            return 1
        pwd, generated = _pwd()
        u.password_hash = security.hash_password(pwd)
        s.commit()
        print(f"已重設 {u.username} 密碼")
        if generated:
            print(f"  臨時密碼（請立即變更）：{pwd}")
    return 0


def cmd_list(_args) -> int:
    init_db()
    with SessionLocal() as s:
        for u in s.execute(select(User).order_by(User.id)).scalars().all():
            flag = "" if u.is_active else "（停用）"
            print(f"{u.id:>3}  {u.username:<16} {u.role:<8} {u.display_name or ''}{flag}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="webvuln.useradmin")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增帳號")
    a.add_argument("username")
    a.add_argument("role", choices=config.ROLES)
    a.add_argument("--name")
    a.add_argument("--email")
    a.add_argument("--dept")
    a.set_defaults(func=cmd_add)

    pw = sub.add_parser("passwd", help="重設密碼")
    pw.add_argument("username")
    pw.set_defaults(func=cmd_passwd)

    ls = sub.add_parser("list", help="列出帳號")
    ls.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
