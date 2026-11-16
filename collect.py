"""
collect.py — 建立 / 更新價格資料庫（只用 Python 標準庫）
=========================================================
用法（在專案資料夾執行）：

    python collect.py official   # 自動抓 UNIQLO、GU 日本官網 7 個類別的價格
    python collect.py shopee     # 匯入 raw/shopee/ 裡從瀏覽器存下的蝦皮 JSON
    python collect.py all        # 兩者都做

蝦皮 JSON 檔名規則：  品牌_類別.json，例如
    raw/shopee/UNIQLO_長袖.json
    raw/shopee/GU_配件.json
（同一類別想保留多天資料，可加日期：UNIQLO_長袖_20261117.json）

輸出（會與舊資料合併，同一天同一商品只保留一筆）：
    data/official_snapshot.csv
    data/shopee_snapshot.csv
"""

from __future__ import annotations

import csv
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from catalog import (BRANDS, CATEGORIES, clean_official_rows, parse_official_items,
                     parse_shopee_payload)

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
RAW_SHOPEE_DIR = BASE / "raw" / "shopee"
OFFICIAL_CSV = DATA_DIR / "official_snapshot.csv"
SHOPEE_CSV = DATA_DIR / "shopee_snapshot.csv"

OFFICIAL_FIELDS = ["collected_at", "brand", "category", "商品編號", "官網商品名稱",
                   "定價(JPY)", "特價(JPY)", "官網連結"]
SHOPEE_FIELDS = ["collected_at", "brand", "category", "商品名稱", "價格", "累積銷量",
                 "評分", "賣家地區", "出貨地", "商品連結"]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


# ----------------------------------------------------------------------------
# CSV 讀寫與合併
# ----------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def merge_and_write(path: Path, fields: list[str], new_rows: list[dict], key_fields: list[str]) -> int:
    """新舊資料合併；key 相同時以新資料覆蓋。回傳合併後總筆數。"""
    def key(r: dict) -> tuple:
        # collected_at 只取日期：同一天重跑不會重複
        return tuple(str(r.get(k, ""))[:10] if k == "collected_at" else str(r.get(k, "")) for k in key_fields)

    merged = {key(r): r for r in read_csv(path)}
    for r in new_rows:
        merged[key(r)] = {f: ("" if r.get(f) is None else r.get(f)) for f in fields}
    rows = sorted(merged.values(), key=lambda r: (r["collected_at"], r["brand"], r["category"]))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig：Excel 開啟不亂碼
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ----------------------------------------------------------------------------
# 日本官網
# ----------------------------------------------------------------------------

class Blocked(Exception):
    """官網回 403 / 429：立刻停止，避免越擋越嚴重。"""


def fetch_official(brand: str, keyword: str, limit: int = 36) -> dict:
    site = BRANDS[brand]
    query = urllib.parse.urlencode({"q": keyword, "limit": limit, "offset": 0, "httpFailure": "true"})
    req = urllib.request.Request(f"{site['api']}?{query}", headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja-JP,ja;q=0.9,zh-TW;q=0.8",
        "Referer": site["search_page"].format(q=urllib.parse.quote(keyword)),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise Blocked(f"HTTP {exc.code}") from exc
        raise


def collect_official() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_rows: list[dict] = []
    print(f"=== 日本官網價格收集（{now}）===")
    try:
        for brand in BRANDS:
            for category, cfg in CATEGORIES.items():
                rows: list[dict] = []
                for kw in cfg["jp"]:
                    time.sleep(random.uniform(2.0, 4.0))  # 放慢速度，對官網友善
                    try:
                        rows += parse_official_items(fetch_official(brand, kw), brand)
                    except Blocked:
                        raise
                    except Exception as exc:  # noqa: BLE001  單一關鍵字失敗不影響其他
                        print(f"  ⚠️ {brand} {category}（{kw}）失敗：{type(exc).__name__}: {exc}")
                rows = clean_official_rows(rows, category)
                for r in rows:
                    r.update(collected_at=now, brand=brand, category=category)
                all_rows += rows
                print(f"  {brand:<6} {category}：{len(rows)} 件")
    except Blocked as exc:
        print(f"  ⛔ 官網拒絕存取（{exc}），停止收集；已取得的資料仍會存檔。")

    if all_rows:
        total = merge_and_write(OFFICIAL_CSV, OFFICIAL_FIELDS, all_rows,
                                ["collected_at", "brand", "category", "商品編號"])
        print(f"✅ 寫入 {OFFICIAL_CSV.relative_to(BASE)}（資料庫共 {total} 筆）")
    else:
        print("沒有取得任何官網資料，資料庫未變更。")


# ----------------------------------------------------------------------------
# 蝦皮（匯入瀏覽器存下的 JSON）
# ----------------------------------------------------------------------------

def collect_shopee() -> None:
    print("=== 匯入蝦皮 JSON ===")
    files = sorted(RAW_SHOPEE_DIR.glob("*.json"))
    if not files:
        RAW_SHOPEE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"找不到 JSON 檔。請把檔案放進 {RAW_SHOPEE_DIR.relative_to(BASE)}，檔名如 UNIQLO_長袖.json")
        return

    all_rows: list[dict] = []
    for f in files:
        parts = f.stem.split("_")
        if len(parts) < 2 or parts[0] not in BRANDS or parts[1] not in CATEGORIES:
            print(f"  ⚠️ 略過 {f.name}：檔名需為 品牌_類別.json（品牌：{'/'.join(BRANDS)}；"
                  f"類別：{'/'.join(CATEGORIES)}）")
            continue
        brand, category = parts[0], parts[1]
        try:
            payload = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  ⚠️ {f.name} 不是有效的 JSON：{exc}")
            continue
        rows = parse_shopee_payload(payload)
        collected_at = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        for r in rows:
            r.update(collected_at=collected_at, brand=brand, category=category)
        all_rows += rows
        print(f"  {f.name}：{len(rows)} 件（{collected_at}）")

    if all_rows:
        total = merge_and_write(SHOPEE_CSV, SHOPEE_FIELDS, all_rows,
                                ["collected_at", "brand", "category", "商品連結"])
        print(f"✅ 寫入 {SHOPEE_CSV.relative_to(BASE)}（資料庫共 {total} 筆）")
    else:
        print("沒有可匯入的蝦皮商品，資料庫未變更。")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("official", "all"):
        collect_official()
    if cmd in ("shopee", "all"):
        collect_shopee()
    if cmd not in ("official", "shopee", "all"):
        print(__doc__)
