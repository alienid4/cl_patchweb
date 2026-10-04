"""系統設定存取（DB key-value，value 為 JSON）。AD 連線設定放這，Super Admin 於畫面編輯、免重部署。

AD 設定（key='ad'）欄位：
  enabled        是否啟用 AD 登入（false＝維持本地/免登入）
  servers        LDAP 伺服器 IP 清單（依序嘗試），例 ["10.0.0.1","10.0.0.2"]
  port           預設 389
  encryption     none | starttls | ldaps（預設 none）
  bind_style     upn | nt | dn（預設 upn）
  upn_suffix     UPN 後綴，例 corp.example.com（bind_style=upn 用）
  nt_domain      NT 網域，例 corp（bind_style=nt 用）
  dn_template    DN 樣板，含 {login}（bind_style=dn 用，少用）
  base_dn        搜尋基準 DN，例 DC=corp,DC=example,DC=com（讀 displayName/部門用）
  name_attr      顯示名屬性（預設 displayName）→ 對 Excel 負責人名
  dept_attr      部門屬性（預設 department）
  login_attr     以員編搜尋時比對的屬性（預設 sAMAccountName）
  super_admins   Super Admin 的員編清單
  emp_to_owner   {員編: 負責人名} 覆寫對照（displayName 對不上時補）
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppSetting

# LDAP 伺服器「站點預設」：公開 repo 不寫死內網 IP（資安）。由 Super Admin 於設定畫面填一次、存 DB。
# 站點名稱可當填寫提示；IP 留空，使用者填入後存進 DB（key='ldap_presets'），之後畫面即可用選的。
LDAP_SERVER_PRESETS = [
    {"site": "敦南", "ips": []},
    {"site": "內湖", "ips": []},
    {"site": "板橋", "ips": []},
]

AD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "servers": [],   # 由設定畫面填（公開 repo 不寫死內網 IP）
    "port": 389,
    "encryption": "none",
    "bind_style": "upn",
    "upn_suffix": "",
    "nt_domain": "",
    "dn_template": "",
    "base_dn": "",
    "name_attr": "displayName",
    "dept_attr": "department",
    "login_attr": "sAMAccountName",
    "super_admins": [],
    "emp_to_owner": {},
}


def get_json(session: Session, key: str, default: Any = None) -> Any:
    row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalars().first()
    if not row or not row.value:
        return default
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return default


def set_json(session: Session, key: str, value: Any) -> None:
    row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalars().first()
    payload = json.dumps(value, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        session.add(AppSetting(key=key, value=payload))
    session.commit()


def get_ldap_presets(session: Session) -> list[dict]:
    """站點→IP 預設（存 DB，可編輯；公開 repo 不放內網 IP）。沒存過就回站名、IP 空。"""
    saved = get_json(session, "ldap_presets", None)
    if isinstance(saved, list) and saved:
        return saved
    return [dict(p) for p in LDAP_SERVER_PRESETS]


def set_ldap_presets(session: Session, presets: list) -> list:
    clean = []
    for p in (presets or []):
        if isinstance(p, dict) and p.get("site"):
            ips = p.get("ips") or []
            if isinstance(ips, str):
                ips = [x.strip() for x in ips.replace(",", " ").split() if x.strip()]
            clean.append({"site": str(p["site"]), "ips": [str(x).strip() for x in ips if str(x).strip()]})
    set_json(session, "ldap_presets", clean)
    return clean


def get_ad_config(session: Session) -> dict:
    """AD 設定（預設值 + DB 覆寫）。敏感欄位不含密碼（AD 登入靠使用者自己的帳密，系統不存）。"""
    cfg = dict(AD_DEFAULTS)
    saved = get_json(session, "ad", {}) or {}
    if isinstance(saved, dict):
        cfg.update(saved)
    return cfg


def set_ad_config(session: Session, patch: dict) -> dict:
    """部分更新 AD 設定；回傳更新後的完整設定。"""
    cfg = get_ad_config(session)
    for k, v in (patch or {}).items():
        if k in AD_DEFAULTS:
            cfg[k] = v
    # 只存與預設不同的鍵？為簡單起見存完整一份（不含未知鍵）
    clean = {k: cfg[k] for k in AD_DEFAULTS}
    set_json(session, "ad", clean)
    return clean
