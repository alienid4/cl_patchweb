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
