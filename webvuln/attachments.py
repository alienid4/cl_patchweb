"""申請佐證文件（展延／例外的 WBS、理由說明等）服務層。

掛在弱點(穩定鍵 vuln_key)上，跟 Case 疊加層同命：重匯 Excel 不洗、對 Excel 唯讀。
檔案存 config.UPLOAD_DIR/stored_name，DB 只存 metadata；同 sha256 共用一個實體檔，
刪到最後一個參照才刪檔。上傳走 raw body（不靠 python-multipart，利於離線安裝）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cases, config
from .models import Attachment, Finding

KINDS = ("展延申請書", "例外申請書", "WBS", "佐證", "其他")


def _upload_dir() -> Path:
    d = config.UPLOAD_DIR
    d.mkdir(parents=True, exist_ok=True)   # 啟動或首次上傳時自動建（正式機免手動）
    return d


def _ext(name: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,8})$", name or "")
    return m.group(1).lower() if m else ""


def _row(a: Attachment) -> dict:
    return {
        "id": a.id, "vuln_key": a.vuln_key, "kind": a.kind,
        "orig_name": a.orig_name, "size": a.size, "content_type": a.content_type,
        "uploaded_by": a.uploaded_by,
        "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
    }


def list_for_finding(session: Session, finding_id: int) -> list[dict]:
    f = session.get(Finding, finding_id)
    if f is None:
        raise ValueError("弱點不存在")
    vk = cases.key_str(f)
    rows = session.execute(
        select(Attachment).where(Attachment.vuln_key == vk).order_by(Attachment.uploaded_at.desc())
    ).scalars().all()
    return [_row(a) for a in rows]


def counts_by_vuln_key(session: Session) -> dict:
    """{vuln_key: 附件數}，供 findings 清單顯示 📎N（一次抓、不 N+1）。"""
    out: dict[str, int] = {}
    for (vk,) in session.execute(select(Attachment.vuln_key)).all():
        out[vk] = out.get(vk, 0) + 1
    return out


def save_for_finding(session: Session, finding_id: int, data: bytes, orig_name: str,
                     kind: str | None, username: str | None) -> dict:
    """存一個附件：驗副檔名/大小 → sha256 去重 → 寫檔(若新) → 入 metadata。回傳該筆 row。"""
    f = session.get(Finding, finding_id)
    if f is None:
        raise ValueError("弱點不存在")
    orig_name = (orig_name or "檔案").strip()[:300]
    ext = _ext(orig_name)
    if ext not in config.UPLOAD_ALLOWED_EXT:
        raise ValueError("不支援的檔案類型（允許：%s）" % "、".join(sorted(e[1:] for e in config.UPLOAD_ALLOWED_EXT)))
    if not data:
        raise ValueError("空檔案")
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise ValueError("檔案過大（上限 %d MB）" % (config.UPLOAD_MAX_BYTES // (1024 * 1024)))
    kind = (kind or "其他").strip()
    if kind not in KINDS:
        kind = "其他"

    vk = cases.key_str(f)
    digest = hashlib.sha256(data).hexdigest()
    d = _upload_dir()

    # 去重：同 sha256 已存過就共用實體檔，不重複寫
    dupe = session.execute(
        select(Attachment).where(Attachment.sha256 == digest).limit(1)
    ).scalars().first()
    if dupe and (d / dupe.stored_name).exists():
        stored = dupe.stored_name
    else:
        stored = uuid.uuid4().hex + ext
        (d / stored).write_bytes(data)

    a = Attachment(
        vuln_key=vk, sheet_key=f.sheet_key, plugin_id=f.plugin_id, host=f.host,
        kind=kind, orig_name=orig_name, stored_name=stored, sha256=digest,
        size=len(data), content_type=None, uploaded_by=username,
        uploaded_at=dt.datetime.now(),
    )
    session.add(a)
    session.commit()
    return _row(a)


def get_file(session: Session, att_id: int):
    """回傳 (Attachment, 絕對路徑 Path) 或 None。路徑一定在 UPLOAD_DIR 內（防穿越）。"""
    a = session.get(Attachment, att_id)
    if a is None:
        return None
    d = _upload_dir().resolve()
    p = (d / a.stored_name).resolve()
    if d not in p.parents or not p.exists():
        return None
    return a, p


def delete(session: Session, att_id: int) -> bool:
    """刪一筆附件 metadata；若無其他筆參照同一實體檔，連實體檔一起刪。"""
    a = session.get(Attachment, att_id)
    if a is None:
        return False
    stored = a.stored_name
    session.delete(a)
    session.commit()
    others = session.execute(
        select(Attachment).where(Attachment.stored_name == stored).limit(1)
    ).scalars().first()
    if others is None:
        p = (_upload_dir() / stored)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    return True
