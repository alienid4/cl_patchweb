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
    """回傳驗證通過的 User，否則 None。

    本地帳號自成一路：**有 password_hash 的帳號（測試帳號／管理員自建）只用本地驗證、不 fallback AD**。
    這讓「AD 已啟用」時仍能用本地帳號登入；也避免本地帳號打錯密碼時誤去 bind AD（拖慢甚至鎖 AD 帳號）。
    沒有本地密碼的帳號（AD 帳號 password_hash 為空）→ 若啟用 AD 則走 AD 綁定（員編），成功 get-or-create。
    """
    return authenticate_detail(session, username, password)[0]


def authenticate_detail(session: Session, username: str, password: str):
    """同 authenticate，但回 (User|None, 原因字串)。原因供登入失敗明確回報（密碼錯／查無／停用／AD…）。"""
    from . import appsettings, ad
    if not username or not password:
        return None, "請輸入帳號與密碼"
    u = session.execute(select(User).where(User.username == username)).scalars().first()
    if u and u.password_hash:   # 本地帳號：只認本地，不往下走 AD
        if not u.is_active:
            return None, "此帳號已停用，請聯絡管理員"
        if verify_password(password, u.password_hash):
            return u, "ok"
        return None, "密碼錯誤"
    ad_cfg = appsettings.get_ad_config(session)
    if ad_cfg.get("enabled") or config.AUTH_BACKEND == "ad":
        user, reason = ad.authenticate_ad_ex(session, ad_cfg, username, password)
        return user, ("ok" if user else reason)
    # 本地模式、此帳號無本地密碼
    if u and not u.password_hash:
        return None, "此帳號是 AD 帳號，但目前未啟用 AD 登入；請用本地帳號或請管理員啟用 AD"
    return None, "查無此帳號"


# ── session ──
def session_ttl_hours(session: Session) -> int:
    """登入有效時數：DB 一般設定優先（畫面可調），讀不到退回 env 預設。"""
    try:
        from . import appsettings
        return int(appsettings.get_general_config(session).get("session_ttl_hours") or config.SESSION_TTL_HOURS)
    except Exception:  # noqa: BLE001
        return config.SESSION_TTL_HOURS


def create_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now()
    session.add(UserSession(
        token=token, user_id=user.id, created_at=now,
        expires_at=now + dt.timedelta(hours=session_ttl_hours(session)),
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
