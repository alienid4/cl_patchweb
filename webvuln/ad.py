"""AD／LDAP 登入（用 ldap3）。以員編綁定、成功後讀 displayName(中文名)＋部門。

設定來自 appsettings.get_ad_config()（DB，可於畫面編輯）。開發端連不到正式 LDAP，
故另提供 test_connection() 供設定畫面「測試連線」當場驗證。綁定失敗一律回 None／錯誤訊息，
不吐 LDAP 內部細節給前端（錯誤訊息精簡）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import appsettings, config, deptmap
from .models import User


def _bind_identity(cfg: dict, login: str) -> str:
    """依綁定方式把員編組成 LDAP 綁定帳號。"""
    style = (cfg.get("bind_style") or "upn").lower()
    if style == "nt":
        return f"{cfg.get('nt_domain', '')}\\{login}"
    if style == "dn":
        tmpl = cfg.get("dn_template") or "{login}"
        return tmpl.replace("{login}", login)
    # upn（預設）
    suffix = cfg.get("upn_suffix", "")
    return f"{login}@{suffix}" if suffix else login


def _make_server_pool(cfg: dict):
    from ldap3 import Server, ServerPool, Tls  # 延遲匯入：沒裝 ldap3 時才報錯
    import ssl
    enc = (cfg.get("encryption") or "none").lower()
    port = int(cfg.get("port") or 389)
    use_ssl = (enc == "ldaps")
    tls = None
    if enc in ("ldaps", "starttls"):
        tls = Tls(validate=ssl.CERT_NONE)   # 內網、未必有可驗證憑證；不驗證(使用者環境決定)
    servers = [s.strip() for s in (cfg.get("servers") or []) if s.strip()]
    if not servers:
        raise ValueError("未設定 LDAP 伺服器")
    pool = ServerPool([Server(h, port=port, use_ssl=use_ssl, tls=tls, get_info=None) for h in servers],
                      pool_strategy="FIRST", active=True, exhaust=True)
    return pool


def _bind(cfg: dict, login: str, password: str):
    """回傳 (ok, conn_or_None, err)。成功時 conn 已綁定（呼叫端用完要 unbind）。"""
    if not login or not password:
        return False, None, "帳號或密碼為空"
    try:
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        return False, None, "伺服器未安裝 ldap3"
    try:
        pool = _make_server_pool(cfg)
    except ValueError as e:
        return False, None, str(e)
    conn = None
    try:
        conn = Connection(pool, user=_bind_identity(cfg, login), password=password,
                          auto_bind=True, receive_timeout=10)
        if (cfg.get("encryption") or "none").lower() == "starttls":
            conn.start_tls()
        return True, conn, None
    except LDAPException as e:
        if conn:
            try: conn.unbind()
            except Exception: pass
        return False, None, "綁定失敗（帳號／密碼或伺服器設定）"
    except Exception:  # noqa: BLE001 - 連線層任何錯都當失敗，不外洩細節
        if conn:
            try: conn.unbind()
            except Exception: pass
        return False, None, "無法連線 LDAP 伺服器"


def _esc(v) -> str:
    """LDAP 篩選值跳脫（RFC 4515）。"""
    s = "" if v is None else str(v)
    return (s.replace("\\", "\\5c").replace("*", "\\2a").replace("(", "\\28").replace(")", "\\29")
            .replace("\x00", "\\00"))


def _lookup_attrs(cfg: dict, conn, login: str) -> dict:
    """綁定後以員編搜尋，取 displayName／部門。取不到就回空（用員編當名）。"""
    base = cfg.get("base_dn") or ""
    if not base:
        return {}
    name_attr = cfg.get("name_attr") or "displayName"
    dept_attr = cfg.get("dept_attr") or "department"
    mail_attr = cfg.get("mail_attr") or "mail"
    login_attr = cfg.get("login_attr") or "sAMAccountName"
    emp_attr = cfg.get("emp_attr") or "employeeID"
    try:
        flt = f"({login_attr}={_esc(login)})"   # 登入字串跳脫，避免改寫 LDAP 篩選條件（檢核 #14）
        conn.search(base, flt, attributes=[name_attr, dept_attr, mail_attr, emp_attr])
        if conn.entries:
            e = conn.entries[0]
            out = {"_matches": len(conn.entries)}
            if name_attr in e and e[name_attr].value:
                out["name"] = str(e[name_attr].value)
            if dept_attr in e and e[dept_attr].value:
                out["department"] = str(e[dept_attr].value)
            if mail_attr in e and e[mail_attr].value:
                out["email"] = str(e[mail_attr].value)
            if emp_attr in e and e[emp_attr].value:
                out["employee_id"] = str(e[emp_attr].value)
            return out
    except Exception:  # noqa: BLE001
        pass
    return {}


def test_connection(cfg: dict, login: str, password: str) -> dict:
    """設定畫面「測試連線」：回 {ok, message, name, department}。"""
    ok, conn, err = _bind(cfg, login, password)
    if not ok:
        return {"ok": False, "message": err or "綁定失敗"}
    attrs = _lookup_attrs(cfg, conn, login)
    try: conn.unbind()
    except Exception: pass
    msg = "綁定成功"
    if attrs.get("name"):
        msg += f"；displayName＝{attrs['name']}"
    if attrs.get("department"):
        msg += f"；部門＝{attrs['department']}"
    msg += "；員編＝%s" % (attrs.get("employee_id") or "（讀不到，AD 登入不會寫進名錄，請檢查「員編屬性」）")
    if not attrs:
        msg += "（但 base_dn 搜尋不到屬性，請確認 base_dn／屬性名）"
    return {"ok": True, "message": msg, "name": attrs.get("name"), "department": attrs.get("department"),
            "employee_id": attrs.get("employee_id")}


def fetch_mails_by_names(cfg: dict, login: str, password: str, names: list) -> dict:
    """用給定帳密綁定 AD，依『顯示名』逐一搜 mail。回 {name: mail}（只含查到的）。

    供「收件人涵蓋率」批次補 email：很多負責人從沒登入過系統、沒信箱，
    由 Super Admin 一次用自己的帳密對 AD 撈齊。回 {ok, found:{...}, error}。
    """
    ok, conn, err = _bind(cfg, login, password)
    if not ok:
        return {"ok": False, "found": {}, "error": err or "綁定失敗"}
    base = cfg.get("base_dn") or ""
    name_attr = cfg.get("name_attr") or "displayName"
    mail_attr = cfg.get("mail_attr") or "mail"
    found: dict[str, str] = {}
    try:
        if not base:
            return {"ok": False, "found": {}, "error": "未設定 base_dn，無法搜尋"}
        seen = set()
        for raw in names:
            nm = (raw or "").strip()
            if not nm or nm in seen:
                continue
            seen.add(nm)
            try:
                esc = nm.replace("\\", "\\5c").replace("(", "\\28").replace(")", "\\29").replace("*", "\\2a")
                conn.search(base, f"({name_attr}={esc})", attributes=[mail_attr])
                if conn.entries:
                    e = conn.entries[0]
                    if mail_attr in e and e[mail_attr].value:
                        found[nm] = str(e[mail_attr].value)
            except Exception:  # noqa: BLE001 - 單筆查詢失敗略過
                continue
    finally:
        try:
            conn.unbind()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "found": found, "error": None}


def authenticate_ad(session: Session, cfg: dict, login: str, password: str) -> Optional[User]:
    """AD 綁定成功 → get-or-create 本地 User。相容舊呼叫，只回 User|None（原因見 _ex 版）。"""
    return authenticate_ad_ex(session, cfg, login, password)[0]


def authenticate_ad_ex(session: Session, cfg: dict, login: str, password: str):
    """回 (User|None, 原因字串)。綁定成功 → get-or-create 本地 User；失敗帶明確原因。
    角色：員編在 super_admins 清單→super_admin，否則 user。"""
    ok, conn, err = _bind(cfg, login, password)
    if not ok:
        return None, (err or "AD 綁定失敗（帳號／密碼或伺服器設定）")
    attrs = _lookup_attrs(cfg, conn, login)
    try: conn.unbind()
    except Exception: pass

    # 負責人名：emp_to_owner 覆寫優先，否則用 AD displayName，再退回員編
    owner_name = (cfg.get("emp_to_owner") or {}).get(login) or attrs.get("name") or login
    department = deptmap.clean(attrs.get("department")) or None
    email = (attrs.get("email") or "").strip() or None
    is_super = login in (cfg.get("super_admins") or [])

    u = session.execute(select(User).where(User.username == login)).scalars().first()
    if u is None:
        # 首次登入：帶入 AD 名字／信箱；部門自動對應成 Excel 短名（V2.112，對不到就留空，原始值存 ad_department）
        u = User(username=login, password_hash=None, display_name=owner_name,
                 email=email, role=(config.ROLE_SUPER if is_super else config.ROLE_USER),
                 is_active=True)
        session.add(u)
        if department:
            deptmap.apply_department(u, department, deptmap.known_departments(session), db=session,
                                     trigger="login", who=login)
    else:
        if not u.is_active:
            return None, "此帳號已停用，請聯絡管理員"
        # 同步 AD 顯示名與信箱（信箱以 AD 為權威）；部門見 deptmap.apply_department。
        u.display_name = owner_name
        if email:
            u.email = email
        if department:   # 部門只在空白或上次也是自動對應時才改；手填／來源不明、窗口／管理員不動（V2.112）
            deptmap.apply_department(u, department, deptmap.known_departments(session), db=session,
                                     trigger="login", who=login)
        if is_super and config.canon_role(u.role) != config.ROLE_SUPER:
            u.role = config.ROLE_SUPER
    session.commit()
    session.refresh(u)
    u._ad_attrs = dict(attrs)   # 名錄同步在 api_login 做（有 IP 可記稽核；失敗不影響登入）
    return u, "ok"   # 一律回 (User|None, 原因)；V2.62 漏改這兩條路，AD 登入成功反而 500
