"""
shopee_api.py — 直接呼叫蝦皮搜尋 API 的收集程式（Python 標準庫版）
=================================================================
由程式主動對蝦皮 API 端點發出請求：
    GET https://shopee.tw/api/v4/search/search_items?keyword=...&limit=60&newest=0...
回傳的 JSON 原樣存成 raw/shopee/品牌_類別.json，再用 collect.py shopee 清洗寫入資料庫。

只使用 Python 標準庫（urllib），不需安裝任何套件。

蝦皮 API 需要登入狀態，請提供自己帳號的 Cookie：
  1. 用瀏覽器登入 https://shopee.tw
  2. F12 → Network → 點任一個 shopee.tw 的請求 → Request Headers → 複製 cookie 的值
  3. 執行本程式時貼上（只存在記憶體中，不會寫入任何檔案）
  ⚠️ Cookie 等同你的登入狀態，請勿分享給他人或上傳到 GitHub。

防護設計：
  - 依序執行、每次間隔 8～12 秒，總共 14 次
  - HTTP 403 / 429 或 API 回傳 error 代碼（風控）時立即停止，避免帳號被鎖
  - 不偽造任何簽章、不繞過驗證

執行（在專案根目錄）：
    python crawler/shopee_api.py
    python collect.py shopee
"""

from __future__ import annotations

import getpass
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from catalog import BRANDS, CATEGORIES  # noqa: E402  與 App 共用同一份品牌 / 類別設定

API_URL = "https://shopee.tw/api/v4/search/search_items"
RAW_SHOPEE_DIR = BASE / "raw" / "shopee"
WAIT_RANGE = (8.0, 12.0)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class Blocked(Exception):
    """蝦皮拒絕請求（403 / 429 / 風控錯誤代碼）：立即停止。"""


def call_search_api(keyword: str, cookie: str) -> dict:
    """呼叫一次蝦皮搜尋 API，回傳 JSON。"""
    params = urllib.parse.urlencode({
        "by": "relevancy", "keyword": keyword, "limit": 60, "newest": 0, "order": "desc",
        "page_type": "search", "scenario": "PAGE_GLOBAL_SEARCH", "version": 2,
    })
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": f"https://shopee.tw/search?keyword={urllib.parse.quote(keyword)}",
        "X-Requested-With": "XMLHttpRequest",
        "X-API-SOURCE": "pc",
        "X-Shopee-Language": "zh-Hant",
        "Cookie": cookie,
    }
    csrf = re.search(r"csrftoken=([^;]+)", cookie)
    if csrf:
        headers["X-CSRFToken"] = csrf.group(1)

    req = urllib.request.Request(f"{API_URL}?{params}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise Blocked(f"HTTP {exc.code}") from exc
        raise
    if data.get("error"):
        raise Blocked(f"API 錯誤代碼 {data.get('error')}（{data.get('error_msg') or '風控驗證'}）")
    return data


def main() -> None:
    cookie = getpass.getpass("請貼上蝦皮 Cookie（輸入時不會顯示）：").strip()
    if not cookie:
        print("沒有 Cookie，結束。")
        return
    RAW_SHOPEE_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [(b, cat, f"{b} {cfg['tw']}") for b in BRANDS for cat, cfg in CATEGORIES.items()]
    print(f"=== 蝦皮 API 收集：共 {len(jobs)} 次請求 ===")

    for i, (brand, cat, keyword) in enumerate(jobs, 1):
        try:
            data = call_search_api(keyword, cookie)
        except Blocked as exc:
            print(f"⛔ [{i}/{len(jobs)}] {keyword}：{exc}，停止收集")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ [{i}/{len(jobs)}] {keyword}：{type(exc).__name__}: {exc}，略過")
            continue
        items = data.get("items") or []
        ads = sum(1 for x in items if x.get("adsid"))
        out = RAW_SHOPEE_DIR / f"{brand}_{cat}.json"
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"✅ [{i}/{len(jobs)}] {keyword}：{len(items)} 筆（廣告 {ads} 筆）→ {out.relative_to(BASE)}")
        if i < len(jobs):
            time.sleep(random.uniform(*WAIT_RANGE))
    print("完成。接著執行：python collect.py shopee")


if __name__ == "__main__":
    main()
