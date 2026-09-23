"""開發/示範用種子資料（天龍八部假資料）。

用法：py -m webvuln.seed   （會寫進設定的 DB，預設 APP/data/vuln.db）
※ 這是假資料,供 demo 與驗證。真實/正式弱點資料一律走網頁匯入,不進版控。
"""
from __future__ import annotations

from .db import SessionLocal, init_db
from .importer import create_batch
from .schemas import FindingIn, ImportIn

ARCH = "資訊架構部"
SEC = "資安部"


def sample() -> ImportIn:
    f = FindingIn
    findings = [
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="10420", name="SMBv1 通訊協定啟用", host="10.30.1.11",
          severity="Critical", department=ARCH, owner="玄慈", remediation_due="2026-06-01", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="42873", name="SSL 憑證過期", host="10.30.1.12",
          severity="High", department=ARCH, owner="喬峰", remediation_due="2026-08-01",
          first_extension_due="2026-08-15", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="10070", name="目錄瀏覽未關閉", host="10.21.1.2",
          severity="Medium", department=ARCH, owner="玄慈", remediation_due="2026-07-01",
          exception_due="2026-10-10", close_status="未結案"),
        f(sheet_key="2-IoT設備弱點", plugin_id="50011", name="預設密碼未更改", host="10.40.1.5",
          severity="Critical", department=SEC, owner="阿朱", remediation_due="2026-10-10", close_status="未結案"),
        f(sheet_key="2-IoT設備弱點", plugin_id="50022", name="韌體版本過舊", host="10.40.1.6",
          severity="High", department=SEC, owner="鳩摩智", remediation_due="2026-11-15", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="33851", name="弱式加密演算法", host="10.30.1.20",
          severity="Medium", department=ARCH, owner="白世鏡", remediation_due="2027-01-20", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="20007", name="X-Frame-Options 缺失", host="10.21.1.9",
          severity="Low", department=ARCH, owner="阮星竹", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="11219", name="開放連接埠", host="10.30.1.30",
          severity="High", department=SEC, owner="游坦之", remediation_due="2026-06-20", close_status="未結案"),
        f(sheet_key="1-系統弱點掃描弱點", plugin_id="19506", name="修補程式已安裝", host="10.30.1.40",
          severity="Medium", department=ARCH, owner="喬峰", remediation_due="2026-04-01", close_status="已修補"),
        f(sheet_key="2-IoT設備弱點", plugin_id="50099", name="Telnet 服務啟用", host="10.40.1.8",
          severity="Low", department=SEC, owner="阿朱", remediation_due="2026-03-01", close_status="已結案"),
    ]
    return ImportIn(source_file="測試假資料_天龍八部.xlsx", note="seed demo", findings=findings)


def main() -> None:
    init_db()
    with SessionLocal() as s:
        b = create_batch(s, sample())
        print(f"seeded batch {b.id}: {b.row_count} findings（is_latest={b.is_latest}）")


if __name__ == "__main__":
    main()
