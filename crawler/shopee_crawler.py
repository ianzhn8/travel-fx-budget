"""
shopee_crawler.py — 蝦皮搜尋結果收集程式（Playwright 瀏覽器自動化）
=====================================================================
收集方式與本專案資料庫（data/shopee_snapshot.csv）實際使用的方法相同：

  1. 開啟一個真正的 Chromium 瀏覽器（有畫面，非無頭模式），由使用者自己登入蝦皮
  2. 依序在搜尋頁搜尋「品牌 + 類別」共 14 組關鍵字（UNIQLO / GU × 7 類別）
  3. 不另外呼叫 API，而是攔截「網頁本身」載入搜尋結果時的 search_items 回應
  4. 每次搜尋之間停 8～12 秒，總共只有 14 次搜尋，與一般人手動瀏覽相當
  5. 回應原樣存成 raw/shopee/品牌_類別.json，再用 collect.py 清洗、寫入資料庫

設計原則（也是報告中的研究倫理說明）：
  - 不破解、不偽造蝦皮的加密簽章，不繞過驗證碼；遇到驗證頁會暫停，請使用者自己完成
  - 不平行、不大量請求；遇到風控錯誤代碼立即停止
  - 只收集公開的搜尋結果（商品名稱、價格、銷量、賣家地區），不收集任何個人資料

安裝（只有收集資料時才需要，Streamlit App 本身不需要）：
    pip install -r crawler/requirements.txt
    python -m playwright install chromium

執行（在專案根目錄）：
    python crawler/shopee_crawler.py
    python collect.py shopee
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from catalog import BRANDS, CATEGORIES  # noqa: E402  與 App 共用同一份品牌 / 類別設定

RAW_SHOPEE_DIR = BASE / "raw" / "shopee"
PROFILE_DIR = BASE / ".browser_profile"   # 保存登入狀態，下次不用重新登入（已列入 .gitignore）
SEARCH_API = "/api/v4/search/search_items"
WAIT_RANGE = (8.0, 12.0)                  # 每次搜尋之間的等待秒數


def build_jobs() -> list[tuple[str, str, str]]:
    """產生 (品牌, 類別, 搜尋字) 清單，例如 ("UNIQLO", "長袖", "UNIQLO 長袖上衣")。"""
    return [(brand, cat, f"{brand} {cfg['tw']}") for brand in BRANDS for cat, cfg in CATEGORIES.items()]


def wait_for_user_if_verification(page) -> None:
    """蝦皮導向登入 / 流量驗證頁時暫停，請使用者在瀏覽器裡自己完成。"""
    while "/verify/" in page.url or "/buyer/login" in page.url:
        input("⏸️  蝦皮要求登入或驗證，請在開啟的瀏覽器中自行完成，完成後按 Enter 繼續…")
        time.sleep(2)


def search_and_capture(page, keyword: str) -> dict | None:
    """前往搜尋頁，攔截網頁本身載入的第一頁 search_items 回應。"""
    def is_search_response(resp) -> bool:
        return SEARCH_API in resp.url and "newest=0" in resp.url and resp.request.method == "GET"

    try:
        with page.expect_response(is_search_response, timeout=30_000) as info:
            page.goto(f"https://shopee.tw/search?keyword={quote(keyword)}", wait_until="domcontentloaded")
        return info.value.json()
    except PlaywrightTimeout:
        wait_for_user_if_verification(page)
        return None
    except Exception as exc:  # noqa: BLE001  單一關鍵字失敗不影響其他
        print(f"  ⚠️ 讀取回應失敗：{type(exc).__name__}: {exc}")
        return None


def summarize(payload: dict) -> tuple[int, int]:
    """回傳 (總筆數, 廣告筆數)。"""
    items = payload.get("items") or []
    return len(items), sum(1 for i in items if i.get("adsid"))


def main() -> None:
    RAW_SHOPEE_DIR.mkdir(parents=True, exist_ok=True)
    jobs = build_jobs()
    print(f"=== 蝦皮搜尋結果收集：共 {len(jobs)} 組關鍵字（{datetime.now():%Y-%m-%d %H:%M}）===")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, locale="zh-TW", viewport={"width": 1280, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://shopee.tw/", wait_until="domcontentloaded")
        input("👉 請在開啟的瀏覽器確認已登入蝦皮（未登入請先登入），完成後按 Enter 開始收集…")

        for n, (brand, cat, keyword) in enumerate(jobs, 1):
            print(f"[{n:2}/{len(jobs)}] 搜尋「{keyword}」")
            payload = search_and_capture(page, keyword)
            if payload is None:  # 可能剛完成驗證，重試一次
                payload = search_and_capture(page, keyword)
            if payload is None:
                print("  ⚠️ 沒有取得搜尋結果，略過")
                continue
            if payload.get("error"):
                print(f"  ⛔ 蝦皮回傳錯誤代碼 {payload.get('error')}（觸發風控），停止收集以免帳號被鎖")
                break

            total, ads = summarize(payload)
            if total - ads < 10:  # 偶爾會只回傳廣告，等一下重搜一次
                print(f"  ↻ 只有 {total - ads} 筆非廣告商品，稍候重新搜尋")
                time.sleep(random.uniform(*WAIT_RANGE))
                payload = search_and_capture(page, keyword) or payload
                total, ads = summarize(payload)

            out = RAW_SHOPEE_DIR / f"{brand}_{cat}.json"
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            print(f"  ✅ {total} 筆（廣告 {ads} 筆）→ {out.relative_to(BASE)}")
            time.sleep(random.uniform(*WAIT_RANGE))  # 放慢速度，對網站友善

        context.close()
    print("完成。接著執行：python collect.py shopee")


if __name__ == "__main__":
    main()
