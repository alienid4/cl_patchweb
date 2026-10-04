"""系統設定存取（DB key-value，value 為 JSON）。AD 連線設定放這，Super Admin 於畫面編輯、免重部署。

AD 設定（key='ad'）欄位：
  enabled        是否啟用 AD 登入（false＝維持本地/免登入）
  servers        LDAP 伺服器 IP 清單（依序嘗試），例 ["10.93.19.1","10.93.19.2"]
  port           預設 389
  encryption     none | starttls | ldaps（預設 none）
  bind_style     upn | nt | dn（預設 upn）
  upn_suffix     UPN 後綴，例 cathaysec.com.tw（bind_style=upn 用）
  nt_domain      NT 網域，例 cathaysec（bind_style=nt 用）
  dn_template    DN 樣板，含 {login}（bind_style=dn 用，少用）
  base_dn        搜尋基準 DN，例 DC=cathaysec,DC=com,DC=tw（讀 displayName/部門用）
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

# 內建 LDAP 伺服器預設（使用者可在畫面選用，也可編輯／新增自訂）
LDAP_SERVER_PRESETS = [
    {"site": "敦南", "ips": ["10.93.19.1", "10.93.19.2"]},
    {"site": "內湖", "ips": ["10.93.3.1", "10.93.3.2"]},
    {"site": "板橋", "ips": ["10.93.168.1", "10.93.168.5"]},
]

AD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "servers": ["10.93.19.1", "10.93.19.2"],   # 預設敦南；可改
    "port": 389,
    "encryption": "none",
    "bind_style": "upn",
    "upn_suffix": "cathaysec.com.tw",
    "nt_domain": "cathaysec",
    "dn_template": "",
    "base_dn": "DC=cathaysec,DC=com,DC=tw",
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
