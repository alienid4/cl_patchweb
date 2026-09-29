"""驗證與稽核：本地帳號密碼(pbkdf2, stdlib 零相依)、session、audit。

密碼雜湊刻意用 stdlib hashlib.pbkdf2_hmac（Django 風格字串），不引 bcrypt/argon2 的 C 相依，
221 佈署零額外套件。日後轉 AD 只改 authenticate()（見 config.AUTH_BACKEND）——這是留好的 seam。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import AuditLog, User, UserSession

_ALGO = "pbkdf2_sha256"
_ITER = 210_000


def hash_password(password: str, *, iterations: int = _ITER, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{_ALGO}${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        algo, iter_s, salt, hexhash = stored.split("$", 3)
        if algo != _ALGO:
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iter_s))
        return hmac.compare_digest(calc.hex(), hexhash)  # 定時比對，防時序側錄
    except (ValueError, TypeError):
        return False


# ── 帳號 ──
def create_user(session: Session, username: str, password: Optional[str], role: str = config.ROLE_VIEWER,
                display_name: Optional[str] = None, email: Optional[str] = None,
                department: Optional[str] = None) -> User:
    if role not in config.ROLES:
        raise ValueError(f"未知角色：{role}")
    u = User(
        username=username,
        password_hash=hash_password(password) if password else None,
        display_name=display_name or username, email=email, department=department,
        role=role, is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def authenticate(session: Session, username: str, password: str) -> Optional[User]:
    """回傳驗證通過的 User，否則 None。AD seam：AUTH_BACKEND!='local' 時改走 AD（尚未實作）。"""
    u = session.execute(select(User).where(User.username == username)).scalars().first()
    if not u or not u.is_active:
        return None
    if config.AUTH_BACKEND == "local":
        return u if verify_password(password, u.password_hash) else None
    # TODO(W4-B): AD/LDAP bind 驗證；成功則 get-or-create 本地 User（password_hash 留空）
    raise NotImplementedError(f"AUTH_BACKEND={config.AUTH_BACKEND} 尚未實作")


# ── session ──
def create_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now()
    session.add(UserSession(
        token=token, user_id=user.id, created_at=now,
        expires_at=now + dt.timedelta(hours=config.SESSION_TTL_HOURS),
    ))
    session.commit()
    return token


def get_session_user(session: Session, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    s = session.execute(select(UserSession).where(UserSession.token == token)).scalars().first()
    if not s or s.expires_at < dt.datetime.now():
        return None
    u = session.get(User, s.user_id)
    return u if (u and u.is_active) else None


def revoke_session(session: Session, token: Optional[str]) -> None:
    if not token:
        return
    s = session.execute(select(UserSession).where(UserSession.token == token)).scalars().first()
    if s:
        session.delete(s)
        session.commit()


# ── 稽核 ──
def log_audit(session: Session, *, username: Optional[str], action: str,
              target: Optional[str] = None, detail: Optional[str] = None,
              ip: Optional[str] = None) -> None:
    session.add(AuditLog(username=username, action=action, target=target, detail=detail, ip=ip))
    session.commit()
