"""SQLAlchemy 2.0 資料模型（快照式）。

import_batch：一次上傳＝一版快照；finding：明細，屬於某快照。
每次上傳新增一版、不覆蓋；查詢預設看 is_latest=True 那版。歷史留著供趨勢。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    source_file: Mapped[str | None] = mapped_column(String(500))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str | None] = mapped_column(Text)

    findings: Mapped[list["Finding"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    sheets: Mapped[list["SheetColumns"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )


class SheetColumns(Base):
    """每張來源工作表的欄位清單＋順序（供原封匯出 1:1 還原欄序）。"""
    __tablename__ = "sheet_columns"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="CASCADE"), index=True
    )
    sheet_key: Mapped[str] = mapped_column(String(100))
    columns: Mapped[list] = mapped_column(JSON)  # 有序欄名 list

    batch: Mapped["ImportBatch"] = relationship(back_populates="sheets")


class User(Base):
    """登入帳號（本地帳號；日後可切 AD，見 config.AUTH_BACKEND）。"""
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # 本地帳號才有；AD 帳號留空
    display_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="viewer", index=True)  # admin/承辦/viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # #8 每週一自動寄部門週報：由本人(dept_admin)決定開/關，預設關(不煩少用的人)
    weekly_report: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class UserSession(Base):
    """伺服器端 session（cookie 帶隨機 token 對到這裡）。"""
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)


class AuditLog(Base):
    """稽核：誰、何時、對什麼、做了什麼（寫入類動作一律留痕）。"""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now, index=True)
    username: Mapped[str | None] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)  # login/logout/case_transition…
    target: Mapped[str | None] = mapped_column(String(200))      # e.g. case:123
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))


class Case(Base):
    """承辦疊加層：以弱點身分(穩定鍵)為主鍵的申請案，跨快照存活。

    快照(finding)每次匯入重生；case 不重生，只 reconcile：仍在最新快照→更新去正規化欄與 source_closed；
    不在→標 is_orphan(來源已消失，多半修好或移除)。申請管線狀態由承辦手動推進(見 logic.CASE_*)。
    """
    __tablename__ = "case_overlay"

    id: Mapped[int] = mapped_column(primary_key=True)
    vuln_key: Mapped[str] = mapped_column(String(400), unique=True, index=True)  # sheet|plugin|host(正規化)
    sheet_key: Mapped[str | None] = mapped_column(String(100))
    plugin_id: Mapped[str | None] = mapped_column(String(50))
    host: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200), index=True)
    owner: Mapped[str | None] = mapped_column(String(100), index=True)  # 去正規化：最新快照的承辦
    # 管理員在系統內改的負責人（覆蓋 Excel 來的值）；重匯時套回 finding，不會被洗掉
    owner_override: Mapped[str | None] = mapped_column(String(100))
    # 管理員在系統內改的部門（負責人可能是別單位的人）；重匯時套回 finding，不會被洗掉
    department_override: Mapped[str | None] = mapped_column(String(200))
    # 管理追蹤備註（承辦回報「何時做什麼動作」管理人記這裡）；只存系統、不動 Excel 原備註
    track_note: Mapped[str | None] = mapped_column(Text)
    # 預計完成日（承辦回報、管理人登記）；結構化欄位，供週報彙總「預計 X 完成幾支」
    target_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="未申請", index=True)
    note: Mapped[str | None] = mapped_column(Text)
    # reconcile 用
    last_seen_batch_id: Mapped[int | None] = mapped_column(Integer, index=True)
    is_orphan: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # 不在最新快照
    source_closed: Mapped[bool] = mapped_column(Boolean, default=False)          # 最新快照顯示已結案
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.now, onupdate=dt.datetime.now
    )
    # 承辦最後推進管線狀態的時間（只在 transition 更新，reconcile 不動）；供可疑聲稱的匯入時間差比對
    status_changed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class AppSetting(Base):
    """系統設定(key-value，value 存 JSON 字串)。AD 連線設定等放這，Super Admin 於畫面編輯、免重部署。"""
    __tablename__ = "app_setting"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text)   # JSON
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now, onupdate=dt.datetime.now)


class Attachment(Base):
    """申請佐證文件：展延／例外申請的 WBS、理由說明等，掛在弱點(穩定鍵)上。

    以 vuln_key 關聯(跟 Case 疊加層同命)：重匯 Excel 不洗、對 Excel 唯讀。
    檔案實體存檔案系統(config.UPLOAD_DIR/stored_name)，此表只存 metadata；
    同內容(同 sha256)多筆共用一個實體檔，刪到最後一個參照才刪檔。
    """
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    vuln_key: Mapped[str] = mapped_column(String(400), index=True)  # sheet|plugin|host(正規化)
    sheet_key: Mapped[str | None] = mapped_column(String(100))
    plugin_id: Mapped[str | None] = mapped_column(String(50))
    host: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="其他")  # 展延申請書/例外申請書/WBS/佐證/其他
    orig_name: Mapped[str] = mapped_column(String(300))            # 原始檔名(顯示用)
    stored_name: Mapped[str] = mapped_column(String(80), index=True)  # 實體檔名(uuid.ext)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str | None] = mapped_column(String(120))
    uploaded_by: Mapped[str | None] = mapped_column(String(100))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now, index=True)


class MailLog(Base):
    """一鍵發送的逐封寄送紀錄（供「發信紀錄」查：誰在何時寄給誰、成功/失敗/轉窗口）。

    與 AuditLog 分開：AuditLog 記「動作」（寄送這批），MailLog 記「每一封」的結果明細。
    """
    __tablename__ = "mail_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now, index=True)
    sender: Mapped[str | None] = mapped_column(String(100), index=True)   # 操作者員編
    owner: Mapped[str | None] = mapped_column(String(100))                # 該封對應的負責人
    to: Mapped[str | None] = mapped_column(String(300))                   # 實際收件人
    cc: Mapped[str | None] = mapped_column(String(500))
    mode: Mapped[str] = mapped_column(String(20), default="send")         # send/fallback/skip
    status: Mapped[str] = mapped_column(String(20), default="ok")         # ok/failed/skip
    count: Mapped[int] = mapped_column(Integer, default=0)                # 該封涵蓋的弱點數
    error: Mapped[str | None] = mapped_column(Text)


class Finding(Base):
    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="CASCADE"), index=True
    )
    sheet_key: Mapped[str | None] = mapped_column(String(100), index=True)
    plugin_id: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str | None] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(String(200), index=True)
    # 嚴重度：正規化 Critical/High/Medium/Low（前端已對應好）＋保留原值
    severity: Mapped[str | None] = mapped_column(String(20), index=True)
    severity_raw: Mapped[str | None] = mapped_column(String(100))
    department: Mapped[str | None] = mapped_column(String(200), index=True)
    owner: Mapped[str | None] = mapped_column(String(100), index=True)
    # 三個原始期限（前端已解析成 ISO date）
    remediation_due: Mapped[dt.date | None] = mapped_column(Date)      # 修補期限
    first_extension_due: Mapped[dt.date | None] = mapped_column(Date)  # 首次展延上限
    exception_due: Mapped[dt.date | None] = mapped_column(Date)        # 例外核准期限
    # 衍生（入庫時算好；逾期天數不存，查詢時現算）
    effective_due: Mapped[dt.date | None] = mapped_column(Date, index=True)  # 真正到期日
    stage: Mapped[str | None] = mapped_column(String(30), index=True)        # 處置階段
    close_status: Mapped[str | None] = mapped_column(String(30), index=True)  # 未結案/已結案/其他
    close_date: Mapped[dt.date | None] = mapped_column(Date)
    remark: Mapped[str | None] = mapped_column(Text)
    # 整列原始資料（原欄名→原值），供原封匯出；工具用的對映欄仍各自存於上方以利查詢
    raw: Mapped[dict | None] = mapped_column(JSON)

    batch: Mapped["ImportBatch"] = relationship(back_populates="findings")
