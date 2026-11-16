"""
國際旅遊外幣購物預算精算機｜日系服飾版
==========================================
用途：比較「日系大牌服飾在日本官網的價格」與「同品牌在台灣蝦皮的價格」，
      把日幣標價換算成實際台幣成本（含刷卡手續費、退稅），給出「日本買 or 蝦皮買」的建議。

資料來源：
  1. 台灣銀行即時牌告匯率 CSV（日圓現金賣出）
  2. 日本官網商品價格（UNIQLO、GU 的官網公開商品介面；其他品牌手動輸入）
  3. 蝦皮購物搜尋 API（快取、隨機延遲、Session 標頭、Cookie、守門員、離線備援）

執行方式：
  pip install -r requirements.txt
  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# ============================================================================
# 0. 全域常數設定
# ============================================================================

TW_TZ = timezone(timedelta(hours=8))  # 台灣時區

BOT_CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
SHOPEE_SEARCH_URL = "https://shopee.tw/api/v4/search/search_items"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

CURRENCY = "JPY"
FALLBACK_JPY_RATE = 0.2150     # 台銀抓取失敗時的參考值（請手動確認）
DEFAULT_MANUAL_JPY = 2990.0    # 無法自動取得官網價格時的預設值（請手動確認）

# ---- 日本官網：UNIQLO 與 GU 同屬迅銷集團，官網使用相同的公開商品介面 ----
OFFICIAL_SITES: dict[str, dict] = {
    "UNIQLO": {
        "api": "https://www.uniqlo.com/jp/api/commerce/v5/ja/products",
        "search_page": "https://www.uniqlo.com/jp/ja/search?q={q}",
        "product_page": "https://www.uniqlo.com/jp/ja/products/{pid}/00",
    },
    "GU": {
        "api": "https://www.gu-global.com/jp/api/commerce/v5/ja/products",
        "search_page": "https://www.gu-global.com/jp/ja/search?q={q}",
        "product_page": "https://www.gu-global.com/jp/ja/products/{pid}/00",
    },
}

# ---- 品牌與熱門品項設定 ----
# items: 顯示名稱 → (日本官網搜尋關鍵字, 蝦皮搜尋關鍵字)
# aliases: 用來確認蝦皮商品標題真的是這個品牌（ASCII 別名會用單字邊界比對，避免 GU 誤配 GUESS）
CUSTOM_ITEM = "✏️ 自訂品項"
BRANDS: dict[str, dict] = {
    "UNIQLO": {
        "aliases": ["uniqlo", "優衣庫", "ユニクロ"],
        "items": {
            "HEATTECH 發熱衣": ("ヒートテック クルーネック", "HEATTECH 發熱衣"),
            "AIRism 棉質圓領T": ("エアリズムコットン クルーネックT", "AIRism 圓領T"),
            "極輕羽絨外套": ("ウルトラライトダウン", "極輕羽絨外套"),
            "搖粒絨外套": ("フリースフルジップジャケット", "搖粒絨外套"),
            "圓弧形迷你肩背包": ("ラウンドミニショルダーバッグ", "圓弧形迷你肩背包"),
        },
    },
    "GU": {
        "aliases": ["gu", "ジーユー"],
        "items": {
            "寬褲": ("ワイドパンツ", "寬褲"),
            "針織毛衣": ("ニットセーター", "針織毛衣"),
            "大學T": ("スウェットシャツ", "大學T"),
            "牛仔褲": ("デニムパンツ", "牛仔褲"),
        },
    },
    "MUJI 無印良品": {
        "aliases": ["muji", "無印良品", "無印"],
        "items": {
            "法蘭絨襯衫": ("", "法蘭絨襯衫"),
            "羊毛針織衫": ("", "羊毛 針織衫"),
            "羽絨背心": ("", "羽絨背心"),
        },
    },
    "Onitsuka Tiger": {
        "aliases": ["onitsuka", "鬼塚虎", "鬼塚"],
        "items": {
            "MEXICO 66": ("", "MEXICO 66"),
            "SERRANO": ("", "SERRANO"),
        },
    },
    "BEAMS": {
        "aliases": ["beams"],
        "items": {
            "托特包": ("", "托特包"),
            "連帽外套": ("", "連帽外套"),
        },
    },
}

# 台灣縣市清單：用於判斷蝦皮賣家是「台灣現貨」還是「海外出貨」
TW_LOCATIONS = [
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "苗栗", "彰化",
    "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "澎湖", "金門", "連江", "馬祖",
]
SHIP_TW = "台灣現貨"
SHIP_OVERSEAS = "海外出貨"

# ---- 守門員參數：每個外部網站各自節流、限量、被擋就冷卻 ----
GUARD_RULES = {
    "shopee": {"min_interval": 6.0, "hourly_cap": 20, "cooldown": 30 * 60},
    "official": {"min_interval": 3.0, "hourly_cap": 40, "cooldown": 15 * 60},
}

# 蝦皮資料來源模式
MODE_AUTO = "自動（即時優先，失敗自動備援）"
MODE_OFFLINE = "僅離線備援資料（錄影推薦）"
MODE_IMPORT = "匯入我從瀏覽器存下的 JSON"


# ============================================================================
# 1. 工具函式
# ============================================================================

def _to_float(value) -> float | None:
    """安全地把字串轉成 float；空值、'-'、0 或無法轉換時回傳 None。"""
    try:
        num = float(str(value).strip().replace(",", ""))
        return num if num > 0 else None
    except (TypeError, ValueError):
        return None


def classify_shipping(location: str) -> str:
    """依賣家所在地判斷出貨類型（『臺』統一轉為『台』再比對）。"""
    loc = str(location or "").replace("臺", "台").strip()
    if any(city in loc for city in TW_LOCATIONS):
        return SHIP_TW
    return SHIP_OVERSEAS


def title_matches_brand(title: str, aliases: list[str]) -> bool:
    """商品標題是否包含品牌名。ASCII 別名用單字邊界比對（GU 不會配到 GUESS）。"""
    text = str(title or "").lower()
    for alias in aliases:
        a = alias.lower()
        # 仿款標題（類 UNIQLO、UNIQLO 風、仿 UNIQLO）不算該品牌
        if re.search(rf"(類|仿|似)\s*{re.escape(a)}|{re.escape(a)}\s*(風|感|款式?平替)", text):
            return False
        if a.isascii():
            if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text):
                return True
        elif a in text:
            return True
    return False


# ============================================================================
# 2. 守門員（節流 / 每小時上限 / 被擋冷卻）
# ============================================================================

@st.cache_resource
def _guards() -> dict:
    """
    全域請求守門員（跨所有使用者共用，因為對外部網站來說都是同一個 IP）。
    『清除快取』按鈕只清 cache_data，不會重置冷卻狀態。
    """
    return {site: {"lock": threading.Lock(), "last": 0.0, "history": deque(),
                   "blocked_until": 0.0, "block_reason": ""} for site in GUARD_RULES}


def guard_status(site: str) -> dict:
    g, rule = _guards()[site], GUARD_RULES[site]
    now = time.time()
    with g["lock"]:
        while g["history"] and now - g["history"][0] > 3600:
            g["history"].popleft()
        return {"cooldown": max(0, g["blocked_until"] - now), "used": len(g["history"]),
                "cap": rule["hourly_cap"], "reason": g["block_reason"]}


def _guard_acquire(site: str) -> str | None:
    """取得一次請求額度；不允許時回傳原因。允許時會先等到節流間隔已過。"""
    g, rule = _guards()[site], GUARD_RULES[site]
    with g["lock"]:
        now = time.time()
        if now < g["blocked_until"]:
            mins = (g["blocked_until"] - now) / 60
            return f"先前被擋下（{g['block_reason']}），冷卻中，約 {mins:.0f} 分鐘後才會再連線"
        while g["history"] and now - g["history"][0] > 3600:
            g["history"].popleft()
        if len(g["history"]) >= rule["hourly_cap"]:
            return f"已達每小時 {rule['hourly_cap']} 次的自我限制，暫停連線"
        wait = rule["min_interval"] - (now - g["last"])
        if wait > 0:
            time.sleep(wait)
        g["last"] = time.time()
        g["history"].append(g["last"])
    return None


def _guard_trip(site: str, reason: str) -> None:
    """被擋時觸發斷路器，冷卻期間完全不連線。"""
    g = _guards()[site]
    with g["lock"]:
        g["blocked_until"] = time.time() + GUARD_RULES[site]["cooldown"]
        g["block_reason"] = reason


# ============================================================================
# 3. 資料來源 A：台灣銀行即時牌告匯率
# ============================================================================

def parse_bot_csv(text: str) -> dict[str, dict]:
    """
    解析台銀匯率 CSV，回傳 {幣別: {"cash_sell": 現金賣出, "spot_sell": 即期賣出}}。
    台銀 CSV 的欄名有重複（買入 / 賣出兩組都叫「現金」「即期」），
    因此改為在每一列尋找「本行賣出」標記，其後第 1 欄為現金、第 2 欄為即期。
    """
    df = pd.read_csv(io.StringIO(text), header=None, skiprows=1, dtype=str, keep_default_na=False)
    rates: dict[str, dict] = {}
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row.tolist()]
        if not cells or not cells[0]:
            continue
        try:
            sell_idx = cells.index("本行賣出")
        except ValueError:
            continue
        rates[cells[0].upper()] = {
            "cash_sell": _to_float(cells[sell_idx + 1]) if sell_idx + 1 < len(cells) else None,
            "spot_sell": _to_float(cells[sell_idx + 2]) if sell_idx + 2 < len(cells) else None,
        }
    return rates


@st.cache_data(ttl=600, show_spinner=False)
def fetch_bot_jpy_rate() -> dict:
    """
    取得台銀日圓匯率（優先現金賣出）。永不拋出例外。
    回傳：{"ok", "rate", "rate_type", "updated_at", "error"}
    """
    result = {"ok": False, "rate": None, "rate_type": "", "updated_at": None, "error": None}
    try:
        resp = requests.get(BOT_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()

        text = None
        for enc in ("utf-8-sig", "big5", "cp950"):  # 編碼依序嘗試
            try:
                text = resp.content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("無法辨識 CSV 編碼")

        info = parse_bot_csv(text).get(CURRENCY, {})
        if info.get("cash_sell"):
            result.update(rate=info["cash_sell"], rate_type="現金賣出")
        elif info.get("spot_sell"):
            result.update(rate=info["spot_sell"], rate_type="即期賣出")
        else:
            raise ValueError("CSV 中找不到日圓匯率")

        # 更新時間：檔名格式為 ExchangeRate@YYYYMMDDHHMM.csv
        match = re.search(r"@(\d{12})", resp.headers.get("Content-Disposition", ""))
        if match:
            ts = datetime.strptime(match.group(1), "%Y%m%d%H%M")
            result["updated_at"] = ts.strftime("%Y/%m/%d %H:%M") + "（台銀牌告）"
        else:
            result["updated_at"] = datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M") + "（抓取時間）"
        result["ok"] = True
    except requests.exceptions.Timeout:
        result["error"] = "台銀匯率連線逾時"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"台銀匯率連線失敗：{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"台銀匯率解析失敗：{exc}"
    return result


# ============================================================================
# 4. 資料來源 B：日本官網價格（UNIQLO / GU）
# ============================================================================

def parse_official_items(payload: dict, brand: str) -> list[dict]:
    """解析迅銷集團官網商品介面的回傳結果。欄位缺漏的商品直接略過。"""
    site = OFFICIAL_SITES[brand]
    items = []
    for it in (payload.get("result") or {}).get("items") or []:
        prices = it.get("prices") or {}
        base = _to_float((prices.get("base") or {}).get("value"))
        promo = _to_float((prices.get("promo") or {}).get("value"))
        if not base:
            continue
        pid = it.get("productId") or ""
        items.append({
            "官網商品名稱": it.get("name") or "（未命名）",
            "定價(JPY)": base,
            "特價(JPY)": promo if promo and promo < base else None,
            "商品編號": pid,
            "官網連結": site["product_page"].format(pid=pid) if pid else "",
        })
    return items


@st.cache_data(ttl=600, show_spinner=False)
def fetch_official_items(brand: str, jp_keyword: str, limit: int = 24) -> dict:
    """
    搜尋日本官網商品。永不拋出例外；失敗時回傳 ok=False 與原因，由介面改為手動輸入。
    回傳：{"ok": bool, "items": list[dict], "reason": str}
    """
    out = {"ok": False, "items": [], "reason": ""}
    if brand not in OFFICIAL_SITES:
        out["reason"] = "此品牌沒有可用的官網公開介面，請手動輸入日本標價"
        return out
    if not jp_keyword:
        out["reason"] = "未設定官網搜尋關鍵字"
        return out

    denied = _guard_acquire("official")
    if denied:
        out["reason"] = f"官網{denied}"
        return out

    site = OFFICIAL_SITES[brand]
    try:
        time.sleep(random.uniform(1.0, 2.0))
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja-JP,ja;q=0.9,zh-TW;q=0.8,en;q=0.7",
            "Referer": site["search_page"].format(q=quote(jp_keyword)),
        })
        params = {"q": jp_keyword, "limit": limit, "offset": 0, "httpFailure": "true"}
        resp = session.get(site["api"], params=params, timeout=(5, 10))

        if resp.status_code in (403, 429):
            _guard_trip("official", str(resp.status_code))
            out["reason"] = f"官網拒絕存取（HTTP {resp.status_code}）"
            return out
        resp.raise_for_status()

        items = parse_official_items(resp.json(), brand)
        if not items:
            out["reason"] = "官網查無商品，請換個日文關鍵字"
            return out
        out.update(ok=True, items=items)
    except requests.exceptions.Timeout:
        out["reason"] = "官網連線逾時"
    except requests.exceptions.RequestException as exc:
        out["reason"] = f"官網連線失敗：{type(exc).__name__}"
    except ValueError:
        out["reason"] = "官網回傳內容不是有效的 JSON"
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"官網非預期錯誤：{type(exc).__name__}"
    return out


# ============================================================================
# 5. 資料來源 C：蝦皮購物 API
# ============================================================================

def _build_shopee_session(keyword: str, cookie: str) -> requests.Session:
    """建立帶完整瀏覽器標頭的 Session；若有 Cookie 則一併帶入。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": f"https://shopee.tw/search?keyword={quote(keyword)}",
        "X-Requested-With": "XMLHttpRequest",
        "X-API-SOURCE": "pc",
        "X-Shopee-Language": "zh-Hant",
    })
    cookie = (cookie or "").strip()
    if cookie:
        session.headers["Cookie"] = cookie
        csrf = re.search(r"csrftoken=([^;]+)", cookie)
        if csrf:
            session.headers["X-CSRFToken"] = csrf.group(1)
    return session


def _parse_shopee_items(payload: dict) -> list[dict]:
    """把蝦皮 API 回傳的 JSON 轉成商品清單；價格單位為『元 × 100000』。"""
    items = []
    for raw in payload.get("items") or []:
        basic = raw.get("item_basic") or raw  # 新舊版結構相容
        price_raw = basic.get("price") or basic.get("price_min")
        if not price_raw:
            continue
        price = price_raw / 100000
        if price <= 0:
            continue
        shopid, itemid = basic.get("shopid"), basic.get("itemid")
        location = basic.get("shop_location") or "未知"
        items.append({
            "商品名稱": basic.get("name", "（未命名商品）"),
            "價格": round(price, 0),
            "累積銷量": int(basic.get("historical_sold") or basic.get("sold") or 0),
            "評分": round(float((basic.get("item_rating") or {}).get("rating_star") or 0), 1),
            "賣家地區": location,
            "出貨地": classify_shipping(location),
            "商品連結": f"https://shopee.tw/product/{shopid}/{itemid}" if shopid and itemid else "",
        })
    return items


@st.cache_data(ttl=600, show_spinner=False)  # 快取 10 分鐘；快取命中不消耗守門員額度
def fetch_shopee_items(keyword: str, limit: int = 50, cookie: str = "") -> dict:
    """呼叫蝦皮搜尋 API。永不拋出例外：失敗時回傳 ok=False 與原因，由主程式切換備援資料。"""
    out = {"ok": False, "items": [], "status": None, "reason": ""}

    denied = _guard_acquire("shopee")
    if denied:
        out["reason"] = f"蝦皮{denied}"
        return out

    try:
        time.sleep(random.uniform(1.0, 2.0))  # 模擬人類操作的隨機延遲
        session = _build_shopee_session(keyword, cookie)
        params = {
            "by": "relevancy", "keyword": keyword, "limit": limit, "newest": 0,
            "order": "desc", "page_type": "search", "scenario": "PAGE_GLOBAL_SEARCH", "version": 2,
        }
        resp = session.get(SHOPEE_SEARCH_URL, params=params, timeout=(5, 10))
        out["status"] = resp.status_code

        if resp.status_code in (403, 429):
            _guard_trip("shopee", str(resp.status_code))
            out["reason"] = f"蝦皮拒絕存取（HTTP {resp.status_code}）"
            return out
        resp.raise_for_status()

        payload = resp.json()
        if payload.get("error"):  # 被風控時常回 200 但帶 error 代碼
            _guard_trip("shopee", f"錯誤代碼 {payload.get('error')}")
            out["reason"] = f"蝦皮回傳錯誤代碼 {payload.get('error')}（觸發風控驗證）"
            return out

        items = _parse_shopee_items(payload)
        if not items:
            out["reason"] = "查無商品或 API 回傳結構已變動"
            return out
        out.update(ok=True, items=items)
    except requests.exceptions.Timeout:
        out["reason"] = "蝦皮連線逾時"
    except requests.exceptions.ConnectionError:
        out["reason"] = "網路連線異常，無法連上蝦皮"
    except requests.exceptions.RequestException as exc:
        out["reason"] = f"蝦皮請求失敗：{type(exc).__name__}"
    except ValueError:
        out["reason"] = "蝦皮回傳內容不是有效的 JSON"
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"非預期錯誤：{type(exc).__name__}"
    return out


def generate_mock_items(brand: str, item_tw: str, reference_twd: float, n: int = 40) -> list[dict]:
    """
    蝦皮離線備援資料：結構與真實資料一致，標題一定含品牌名。
    以「品牌＋品項」雜湊當亂數種子，同一組合每次產生相同資料，錄影穩定。
    價格以日本實際台幣成本為錨點，加成 1.05~1.6 倍，模擬代購溢價。
    """
    seed = int(hashlib.md5(f"{brand}|{item_tw}".encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    anchor = reference_twd if reference_twd > 0 else 600.0
    market_center = anchor * rng.uniform(1.05, 1.6)

    brand_label = brand.split()[0]
    tw_cities = ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "新竹市"]
    shop_names = ["日本代購小舖", "東京直送", "日系選物", "大阪連線", "正品代購", "日貨專賣"]
    tags = ["現貨", "日本連線", "正品", "預購", "男女款", "多色可選"]

    items = []
    for i in range(n):
        is_tw = rng.random() < 0.6
        factor = rng.lognormal(mean=0.0, sigma=0.15) * (1.0 if is_tw else 0.9)
        items.append({
            "商品名稱": f"【{rng.choice(shop_names)}】{brand_label} {item_tw} {rng.choice(tags)} #{i + 1:02d}",
            "價格": max(round(market_center * factor, 0), 1),
            "累積銷量": int(rng.pareto(1.3) * 50),
            "評分": round(float(rng.uniform(4.3, 5.0)), 1),
            "賣家地區": str(rng.choice(tw_cities)) if is_tw else str(rng.choice(["日本", "日本", "中國大陸"])),
            "出貨地": SHIP_TW if is_tw else SHIP_OVERSEAS,
            "商品連結": f"https://shopee.tw/search?keyword={quote(brand_label + ' ' + item_tw)}",
        })
    return items


# ============================================================================
# 6. 分析邏輯
# ============================================================================

def calc_overseas_cost(jpy_price: float, rate: float, fee_pct: float, refund_pct: float) -> float:
    """日本實際台幣成本 = 日幣標價 ×（1 − 退稅率）× 匯率 ×（1 + 刷卡手續費率）"""
    return jpy_price * (1 - refund_pct / 100) * rate * (1 + fee_pct / 100)


def remove_outliers_iqr(df: pd.DataFrame, col: str = "價格") -> pd.DataFrame:
    """以 IQR 法排除極端值（例如搜尋到配件、福袋、整批出清）。"""
    if len(df) < 8:
        return df
    q1, q3 = df[col].quantile([0.25, 0.75])
    iqr = q3 - q1
    return df[df[col].between(q1 - 1.5 * iqr, q3 + 1.5 * iqr)]


def decision_advice(spread: float, ratio: float, qty: int) -> tuple[str, str, str]:
    """依價差比例回傳 (訊息等級, 標籤, 說明)。ratio = (蝦皮中位數 − 日本成本) / 蝦皮中位數"""
    total = spread * qty
    if ratio > 0.20:
        return "success", f"🔥 日本必買！每件現省 NT${spread:,.0f}", \
            f"比蝦皮中位數便宜 {ratio:.0%}，買 {qty} 件共省 NT${total:,.0f}，值得塞進行李箱。"
    if ratio >= 0.10:
        return "info", f"👍 可以順手買，每件省 NT${spread:,.0f}", \
            f"便宜 {ratio:.0%}，行李還有空間就帶；買 {qty} 件共省 NT${total:,.0f}。"
    if ratio >= 0:
        return "warning", "📦 直接蝦皮買，省行李空間", \
            f"價差僅 {ratio:.0%}（每件 NT${spread:,.0f}），衣服佔空間，不值得扛回台灣。"
    return "error", "🚫 日本反而比較貴", \
        f"日本成本比蝦皮中位數高 NT${-spread:,.0f}（{-ratio:.0%}），建議回台灣在蝦皮購買。"


# ============================================================================
# 7. Streamlit 介面
# ============================================================================

def render_sidebar(bot: dict) -> dict:
    """側邊欄：品牌、品項、官網價格、匯率、費用與蝦皮設定。"""
    sb = st.sidebar
    sb.header("👕 日系服飾比價設定")

    # --- 品牌與品項 ---
    brand = sb.selectbox("品牌", list(BRANDS))
    brand_cfg = BRANDS[brand]
    item_name = sb.selectbox("品項", list(brand_cfg["items"]) + [CUSTOM_ITEM])
    jp_default, tw_default = brand_cfg["items"].get(item_name, ("", ""))
    auto_site = brand in OFFICIAL_SITES

    if auto_site:
        jp_kw = sb.text_input("🇯🇵 官網搜尋關鍵字（日文）", value=jp_default,
                              key=f"jpkw_{brand}_{item_name}",
                              help="UNIQLO / GU 日本官網需用日文搜尋，例如：ヒートテック")
    else:
        jp_kw = ""
    tw_item = sb.text_input("🇹🇼 蝦皮搜尋品項（中文）", value=tw_default,
                            key=f"twkw_{brand}_{item_name}")
    brand_label = brand.split()[0]
    shopee_kw = f"{brand_label} {tw_item}".strip()
    sb.caption(f"蝦皮實際搜尋：**{shopee_kw}**")

    # --- 日本官網價格 ---
    sb.divider()
    sb.subheader("🇯🇵 日本官網價格")
    official = {"ok": False, "items": [], "reason": ""}
    selected = None
    if auto_site and jp_kw.strip():
        with st.spinner(f"搜尋 {brand} 日本官網…"):
            official = fetch_official_items(brand, jp_kw.strip())

    if official["ok"]:
        options = official["items"]

        def _fmt(i: int) -> str:
            it = options[i]
            sale = f"→特價¥{it['特價(JPY)']:,.0f}" if it["特價(JPY)"] else ""
            return f"{it['官網商品名稱']}｜¥{it['定價(JPY)']:,.0f}{sale}"

        idx = sb.selectbox("選擇官網商品", range(len(options)), format_func=_fmt,
                           key=f"pick_{brand}_{jp_kw}")
        selected = options[idx]
        use_sale = sb.checkbox("使用官網特價（若有）", value=True)
        auto_jpy = selected["特價(JPY)"] if (use_sale and selected["特價(JPY)"]) else selected["定價(JPY)"]
        sb.caption(f"✅ 已自動帶入官網價格 ¥{auto_jpy:,.0f}（含稅），可手動調整")
    else:
        auto_jpy = DEFAULT_MANUAL_JPY
        msg = official["reason"] or ("此品牌需手動輸入" if not auto_site else "請輸入官網搜尋關鍵字")
        sb.caption(f"✍️ {msg}。請到日本官網或門市確認含稅價後輸入。")

    jpy_price = sb.number_input(
        "日本含稅標價（JPY）", min_value=0.0, value=float(auto_jpy), step=10.0,
        key=f"jpy_{brand}_{item_name}_{selected['商品編號'] if selected else 'manual'}_{auto_jpy}",
    )
    qty = sb.number_input("預計購買件數", min_value=1, value=1, step=1)

    # --- 匯率與費用 ---
    sb.divider()
    if bot["ok"]:
        auto_rate = bot["rate"]
        sb.caption(f"🏦 台銀日圓{bot['rate_type']}：**{auto_rate}**｜更新：{bot['updated_at']}")
    else:
        auto_rate = FALLBACK_JPY_RATE
        sb.caption(f"⚠️ {bot.get('error') or '無即時匯率'}，暫用參考匯率 {auto_rate}，請手動確認。")
    rate = sb.number_input("使用匯率（1 JPY = ? TWD，可手動微調）", min_value=0.0001,
                           value=float(auto_rate), step=0.0001, format="%.4f")
    fee_pct = sb.number_input("💳 海外刷卡手續費率（%）", min_value=0.0, max_value=10.0, value=1.5, step=0.1)
    refund_pct = sb.number_input(
        "🧾 免稅 / 退稅折抵（%）", min_value=0.0, max_value=30.0, value=0.0, step=0.5,
        help="日本含稅價內含 10% 消費稅，符合免稅條件時約可折抵 9.09%（10/110）。"
             "免稅門檻與退稅方式請以購買當下店家規定為準。",
    )

    # --- 蝦皮資料來源 ---
    sb.divider()
    mode = sb.radio("🛒 蝦皮資料來源", [MODE_AUTO, MODE_OFFLINE, MODE_IMPORT],
                    help="錄影或展示時建議選「僅離線備援」，完全不連蝦皮，零被擋風險。")
    uploaded = None
    if mode == MODE_IMPORT:
        uploaded = sb.file_uploader("上傳蝦皮搜尋結果 JSON", type="json")
        sb.caption("在自己的瀏覽器開啟蝦皮搜尋頁 → F12 → Network → 篩選 search_items → "
                   "Response 全選複製，存成 .json。")
    elif mode == MODE_AUTO:
        gs = guard_status("shopee")
        if gs["cooldown"] > 0:
            sb.warning(f"⏸️ 蝦皮連線冷卻中（{gs['reason']}），剩約 {gs['cooldown'] / 60:.0f} 分鐘")
        else:
            sb.caption(f"🛡️ 本小時已連線蝦皮 {gs['used']}/{gs['cap']} 次")

    with sb.expander("⚙️ 進階設定"):
        limit = st.slider("蝦皮搜尋筆數", 20, 60, 50, step=10)
        brand_only = st.checkbox("只保留標題含品牌名的商品", value=True,
                                 help="過濾掉「類 UNIQLO 款」等非該品牌商品")
        exclude_outliers = st.checkbox("排除極端價格（IQR 法）", value=True)
        cookie = st.text_input("蝦皮 Cookie（選填，不建議在雲端使用）", type="password")
        if st.button("🔄 清除快取並重新查詢"):
            st.cache_data.clear()
            st.rerun()

    return dict(brand=brand, item_name=item_name, tw_item=tw_item.strip(), shopee_kw=shopee_kw,
                jp_kw=jp_kw.strip(), official=official, selected=selected, jpy_price=jpy_price,
                qty=int(qty), rate=rate, fee_pct=fee_pct, refund_pct=refund_pct, mode=mode,
                uploaded=uploaded, limit=limit, brand_only=brand_only,
                exclude_outliers=exclude_outliers, cookie=cookie.strip())


def render_official(cfg: dict) -> None:
    """顯示日本官網搜尋結果，讓使用者確認抓到的是正確商品。"""
    with st.expander(f"🇯🇵 {cfg['brand']} 日本官網搜尋結果", expanded=False):
        if cfg["official"]["ok"]:
            st.dataframe(
                pd.DataFrame(cfg["official"]["items"]), width="stretch", hide_index=True,
                column_config={
                    "定價(JPY)": st.column_config.NumberColumn(format="¥%d"),
                    "特價(JPY)": st.column_config.NumberColumn(format="¥%d"),
                    "官網連結": st.column_config.LinkColumn(display_text="開啟"),
                },
            )
        else:
            st.write(cfg["official"]["reason"] or "此品牌使用手動輸入價格。")


def render_charts(df: pd.DataFrame, overseas_twd: float) -> None:
    """Plotly 視覺化：價格分佈（直方圖 + 箱型圖）與 價格 vs 銷量 散布圖。"""
    color_map = {SHIP_TW: "#2E86DE", SHIP_OVERSEAS: "#F39C12"}
    left, right = st.columns(2)

    with left:
        st.markdown("**① 蝦皮賣家價格分佈**")
        fig = px.histogram(
            df, x="價格", color="出貨地", marginal="box", nbins=25,
            barmode="overlay", opacity=0.75, color_discrete_map=color_map,
            labels={"價格": "蝦皮售價（TWD）"},
        )
        # 紅色垂直線：日本實際台幣成本（直方圖與箱型圖共用 x 軸）
        fig.add_vline(x=overseas_twd, line_color="red", line_dash="dash", line_width=3)
        fig.add_annotation(x=overseas_twd, y=1.02, xref="x", yref="paper", showarrow=False,
                           text=f"日本成本 NT${overseas_twd:,.0f}", font=dict(color="red", size=13))
        fig.update_layout(height=430, margin=dict(t=40, b=10), legend_title_text="", yaxis_title="賣家數")
        st.plotly_chart(fig, width="stretch")

    with right:
        st.markdown("**② 價格 vs. 累積銷量**")
        use_log = st.checkbox("銷量使用對數座標", value=df["累積銷量"].max() > 1000,
                              help="對數座標下銷量為 0 的商品不會顯示")
        fig2 = px.scatter(
            df, x="累積銷量", y="價格", color="出貨地", color_discrete_map=color_map,
            hover_name="商品名稱", hover_data={"賣家地區": True, "評分": True},
            log_x=use_log, opacity=0.8, labels={"價格": "蝦皮售價（TWD）"},
        )
        fig2.add_hline(y=overseas_twd, line_color="red", line_dash="dash", line_width=2,
                       annotation_text="日本成本", annotation_font_color="red")
        fig2.update_traces(marker=dict(size=10, line=dict(width=0.5, color="white")))
        fig2.update_layout(height=400, margin=dict(t=20, b=10), legend_title_text="")
        st.plotly_chart(fig2, width="stretch")


def render_table(df: pd.DataFrame, overseas_twd: float, file_tag: str) -> None:
    """商品明細：出貨地篩選、價差計算、CSV 下載。"""
    st.subheader("📋 蝦皮商品明細")
    ship_filter = st.multiselect("出貨地篩選", [SHIP_TW, SHIP_OVERSEAS], default=[SHIP_TW, SHIP_OVERSEAS])
    view = df[df["出貨地"].isin(ship_filter)].copy()

    # 價差 > 0 表示蝦皮比日本貴（= 在日本買可省下的金額）
    view["價差(元)"] = (view["價格"] - overseas_twd).round(0)
    view["價差(%)"] = ((view["價格"] - overseas_twd) / view["價格"] * 100).round(1)
    view = view.sort_values("價格").reset_index(drop=True)

    cols = ["商品名稱", "價格", "價差(元)", "價差(%)", "累積銷量", "評分", "出貨地", "賣家地區", "商品連結"]
    st.dataframe(
        view[cols], width="stretch", hide_index=True, height=380,
        column_config={
            "價格": st.column_config.NumberColumn("蝦皮售價", format="NT$%d"),
            "價差(元)": st.column_config.NumberColumn("價差(元)", format="%d", help="蝦皮售價 − 日本成本"),
            "價差(%)": st.column_config.NumberColumn("價差(%)", format="%.1f%%"),
            "商品連結": st.column_config.LinkColumn("連結", display_text="開啟"),
        },
    )
    st.caption(f"共 {len(view)} 筆　｜　價差為正：在日本買比較省")
    st.download_button(
        "⬇️ 下載 CSV", data=view[cols].to_csv(index=False).encode("utf-8-sig"), mime="text/csv",
        file_name=f"price_compare_{file_tag}_{datetime.now(TW_TZ):%Y%m%d_%H%M}.csv",
    )


def load_shopee(cfg: dict) -> tuple[dict, str]:
    """依模式取得蝦皮資料；任何失敗都回傳 ok=False，由呼叫端改用備援資料。"""
    shopee = {"ok": False, "items": [], "reason": ""}
    if cfg["mode"] == MODE_OFFLINE:
        shopee["reason"] = "使用者選擇離線模式，未連線蝦皮"
        return shopee, "即時"
    if cfg["mode"] == MODE_IMPORT:
        if cfg["uploaded"] is None:
            shopee["reason"] = "尚未上傳 JSON 檔"
            return shopee, "匯入檔案"
        try:
            items = _parse_shopee_items(json.load(cfg["uploaded"]))
            return {"ok": bool(items), "items": items,
                    "reason": "" if items else "JSON 中找不到商品資料"}, "匯入檔案"
        except Exception:  # noqa: BLE001
            shopee["reason"] = "JSON 檔格式無法解析"
            return shopee, "匯入檔案"
    with st.spinner(f"正在搜尋蝦皮「{cfg['shopee_kw']}」…"):
        return fetch_shopee_items(cfg["shopee_kw"], cfg["limit"], cfg["cookie"]), "即時"


def main() -> None:
    st.set_page_config(page_title="日系服飾比價精算機", page_icon="👕", layout="wide")
    st.title("👕 國際旅遊外幣購物預算精算機｜日系服飾版")
    st.caption("日本官網 vs 台灣蝦皮：同品牌服飾，在日本買到底省多少？")

    with st.spinner("讀取台灣銀行即時匯率…"):
        bot = fetch_bot_jpy_rate()
    cfg = render_sidebar(bot)

    if not cfg["tw_item"]:
        st.warning("請在左側輸入蝦皮搜尋品項。")
        st.stop()
    if cfg["jpy_price"] <= 0:
        st.warning("請輸入大於 0 的日本含稅標價。")
        st.stop()

    overseas_twd = calc_overseas_cost(cfg["jpy_price"], cfg["rate"], cfg["fee_pct"], cfg["refund_pct"])

    # ---------- 官網狀態 ----------
    if cfg["official"]["ok"]:
        st.success(f"✅ 已取得 {cfg['brand']} 日本官網價格：{cfg['selected']['官網商品名稱']}"
                   f"（¥{cfg['jpy_price']:,.0f}）", icon="🇯🇵")
    else:
        st.warning(f"日本價格使用手動輸入值 ¥{cfg['jpy_price']:,.0f}"
                   f"（{cfg['official']['reason'] or '此品牌無官網介面'}）", icon="✍️")

    # ---------- 蝦皮資料（失敗自動備援） ----------
    shopee, source_label = load_shopee(cfg)
    if shopee["ok"]:
        df = pd.DataFrame(shopee["items"])
        st.success(f"✅ 已取得蝦皮{source_label}資料 {len(df)} 筆", icon="🛒")
    else:
        df = pd.DataFrame(generate_mock_items(cfg["brand"], cfg["tw_item"], overseas_twd))
        st.info(f"已切換至離線備援資料模式（原因：{shopee['reason'] or '未知'}）。"
                "以下為模擬資料，分析流程與真實資料完全相同。", icon="🛟")

    # ---------- 資料清洗 ----------
    df["價格"] = pd.to_numeric(df["價格"], errors="coerce")
    df["累積銷量"] = pd.to_numeric(df["累積銷量"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["價格"])
    df = df[df["價格"] > 0]
    notes = []
    if cfg["brand_only"]:
        before = len(df)
        df = df[df["商品名稱"].apply(lambda t: title_matches_brand(t, BRANDS[cfg["brand"]]["aliases"]))]
        if before - len(df):
            notes.append(f"排除 {before - len(df)} 筆標題不含品牌名的商品")
    if cfg["exclude_outliers"]:
        before = len(df)
        df = remove_outliers_iqr(df)
        if before - len(df):
            notes.append(f"排除 {before - len(df)} 筆極端價格")
    if df.empty:
        st.error("篩選後沒有可分析的蝦皮商品，請換個品項，或在進階設定關閉「只保留標題含品牌名」。")
        st.stop()
    if notes:
        st.caption("已" + "、".join(notes) + "。")

    # ---------- 統計 ----------
    p_min, p_med, p_max = df["價格"].min(), df["價格"].median(), df["價格"].max()
    spread = p_med - overseas_twd
    ratio = spread / p_med if p_med else 0
    max_spread = p_max - overseas_twd
    percentile = (df["價格"] < overseas_twd).mean()

    # ---------- KPI ----------
    st.subheader("📊 關鍵指標")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("台銀匯率（JPY）", f"{cfg['rate']:.4f}")
    k2.metric("日本實際台幣成本", f"NT${overseas_twd:,.0f}",
              delta=f"{-spread:+,.0f} vs 中位數", delta_color="inverse",
              help=f"¥{cfg['jpy_price']:,.0f} ×（1−退稅）× 匯率 ×（1+手續費）")
    k3.metric("蝦皮市場最低價", f"NT${p_min:,.0f}")
    k4.metric("市場中位數價", f"NT${p_med:,.0f}")
    k5.metric("最大價差", f"NT${max_spread:,.0f}", help="蝦皮最高價 − 日本成本")

    # ---------- 決策 ----------
    st.subheader("🧠 智慧購買決策建議")
    level, title, detail = decision_advice(spread, ratio, cfg["qty"])
    getattr(st, level)(f"### {title}\n{detail}")
    st.caption(f"日本成本落在蝦皮價格第 **{percentile:.0%}** 百分位"
               f"（有 {percentile:.0%} 的蝦皮賣家比日本便宜）。")

    with st.expander("🗂️ 4W1H 分析摘要"):
        n_tw = int((df["出貨地"] == SHIP_TW).sum())
        st.markdown(
            f"- **What｜買什麼**：{cfg['brand']} {cfg['item_name'] if cfg['item_name'] != CUSTOM_ITEM else cfg['tw_item']}"
            f"（{cfg['qty']} 件）\n"
            f"- **Where｜在哪買**：日本 NT${overseas_twd:,.0f}（¥{cfg['jpy_price']:,.0f}） vs 蝦皮中位數 NT${p_med:,.0f}\n"
            f"- **When｜何時的價格**：匯率 {bot.get('updated_at') or '參考值'}；"
            f"日本價格 {'官網即時' if cfg['official']['ok'] else '手動輸入'}；"
            f"蝦皮 {source_label if shopee['ok'] else '離線備援'}\n"
            f"- **Who｜誰在賣**：台灣現貨 {n_tw} 家、海外出貨 {len(df) - n_tw} 家\n"
            f"- **How｜怎麼買最划算**：{title}"
        )

    render_official(cfg)
    st.subheader("📈 視覺化分析")
    render_charts(df, overseas_twd)
    render_table(df, overseas_twd, f"{cfg['brand'].split()[0]}_{cfg['tw_item']}")

    st.divider()
    st.caption("資料來源：臺灣銀行牌告匯率、UNIQLO／GU 日本官網、蝦皮購物。價格僅供參考，實際以結帳金額為準。")


if __name__ == "__main__":
    main()
