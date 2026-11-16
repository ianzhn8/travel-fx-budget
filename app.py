"""
國際旅遊外幣購物預算精算機
=================================
用途：出國旅遊時，把「國外當地外幣標價」換算成「實際台幣成本（含刷卡手續費、退稅）」，
      再與蝦皮台灣市場價格比較，給出「國外買 or 蝦皮買」的購買建議。

資料來源：
  1. 台灣銀行即時牌告匯率 CSV（外部公開資料）
  2. 蝦皮購物搜尋 API（核心資料，含快取、隨機延遲、Session 標頭、Cookie、離線備援五道防護）

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

# 支援的旅遊國家 / 幣別。fallback_rate 僅在台銀匯率抓取失敗時作為「參考值」使用。
CURRENCIES: dict[str, dict] = {
    "JPY": {"label": "🇯🇵 日本（日圓 JPY）", "country": "日本", "fallback_rate": 0.2150, "default_price": 1500.0},
    "KRW": {"label": "🇰🇷 韓國（韓元 KRW）", "country": "韓國", "fallback_rate": 0.0245, "default_price": 15000.0},
    "USD": {"label": "🇺🇸 美國（美元 USD）", "country": "美國", "fallback_rate": 33.00, "default_price": 25.0},
    "THB": {"label": "🇹🇭 泰國（泰銖 THB）", "country": "泰國", "fallback_rate": 1.000, "default_price": 350.0},
    "EUR": {"label": "🇪🇺 歐洲（歐元 EUR）", "country": "歐洲", "fallback_rate": 36.50, "default_price": 20.0},
}

# 台灣縣市清單：用於判斷蝦皮賣家是「台灣現貨」還是「海外出貨」
TW_LOCATIONS = [
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "苗栗", "彰化",
    "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "澎湖", "金門", "連江", "馬祖",
]

SHIP_TW = "台灣現貨"
SHIP_OVERSEAS = "海外出貨"

# ---- 蝦皮防鎖參數：寧可少查，也不要讓 IP 或帳號被風控 ----
SHOPEE_MIN_INTERVAL = 6.0      # 兩次真實請求的最短間隔（秒）
SHOPEE_HOURLY_CAP = 20         # 每小時真實請求上限
SHOPEE_COOLDOWN_SEC = 30 * 60  # 被擋（403/429/風控代碼）後，暫停連線的時間

# 資料來源模式
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


# ============================================================================
# 2. 資料來源 A：台灣銀行即時牌告匯率
# ============================================================================

def parse_bot_csv(text: str) -> dict[str, dict]:
    """
    解析台銀匯率 CSV 文字，回傳 {幣別: {"cash_sell": 現金賣出, "spot_sell": 即期賣出}}。

    台銀 CSV 的欄位標題有重複（買入 / 賣出兩組都叫「現金」「即期」），
    因此不依賴欄名，而是在每一列中尋找「本行賣出」這個標記，其後第 1 欄為現金、第 2 欄為即期。
    """
    df = pd.read_csv(io.StringIO(text), header=None, skiprows=1, dtype=str, keep_default_na=False)
    rates: dict[str, dict] = {}
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row.tolist()]
        if not cells or not cells[0]:
            continue
        code = cells[0].upper()
        try:
            sell_idx = cells.index("本行賣出")
        except ValueError:
            continue  # 該列格式異常，略過
        cash_sell = _to_float(cells[sell_idx + 1]) if sell_idx + 1 < len(cells) else None
        spot_sell = _to_float(cells[sell_idx + 2]) if sell_idx + 2 < len(cells) else None
        rates[code] = {"cash_sell": cash_sell, "spot_sell": spot_sell}
    return rates


@st.cache_data(ttl=600, show_spinner=False)
def fetch_bot_rates() -> dict:
    """
    下載並解析台灣銀行當日牌告匯率。

    回傳結構：
      {
        "ok": bool,                 # 是否成功取得即時匯率
        "rates": {幣別: 匯率},      # 只保留本專案支援的幣別，優先使用『現金賣出』
        "rate_type": {幣別: str},   # 使用的匯率種類（現金賣出 / 即期賣出 / 參考值）
        "updated_at": str,          # 資料更新時間
        "error": str | None,
      }
    任何錯誤都不會拋出例外，而是回傳 ok=False 並由呼叫端改用參考匯率。
    """
    result = {"ok": False, "rates": {}, "rate_type": {}, "updated_at": None, "error": None}
    try:
        resp = requests.get(BOT_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()

        # --- 編碼處理：台銀 CSV 通常為 UTF-8 (含 BOM)，保險起見依序嘗試 ---
        text = None
        for enc in ("utf-8-sig", "big5", "cp950"):
            try:
                text = resp.content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("無法辨識 CSV 編碼")

        parsed = parse_bot_csv(text)
        for code in CURRENCIES:
            info = parsed.get(code, {})
            if info.get("cash_sell"):
                result["rates"][code] = info["cash_sell"]
                result["rate_type"][code] = "現金賣出"
            elif info.get("spot_sell"):
                result["rates"][code] = info["spot_sell"]
                result["rate_type"][code] = "即期賣出（無現金報價）"

        if not result["rates"]:
            raise ValueError("CSV 中找不到任何目標幣別")

        # --- 更新時間：檔名格式為 ExchangeRate@YYYYMMDDHHMM.csv ---
        disposition = resp.headers.get("Content-Disposition", "")
        match = re.search(r"@(\d{12})", disposition)
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
    except Exception as exc:  # noqa: BLE001  CSV 格式變動等任何非預期錯誤
        result["error"] = f"台銀匯率解析失敗：{exc}"
    return result


# ============================================================================
# 3. 資料來源 B：蝦皮購物 API（五道防護）
# ============================================================================

def _build_shopee_session(keyword: str, cookie: str) -> requests.Session:
    """【防護 3】建立帶完整瀏覽器標頭的 Session；【防護 4】若有 Cookie 則一併帶入。"""
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
        # 蝦皮部分請求需要 csrftoken 與 Cookie 中的值一致
        csrf = re.search(r"csrftoken=([^;]+)", cookie)
        if csrf:
            session.headers["X-CSRFToken"] = csrf.group(1)
    return session


def _parse_shopee_items(payload: dict) -> list[dict]:
    """把蝦皮 API 回傳的 JSON 轉成乾淨的商品清單；價格單位為『元 × 100000』。"""
    items = []
    for raw in payload.get("items") or []:
        basic = raw.get("item_basic") or raw  # 新舊版 API 結構相容
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


@st.cache_resource
def shopee_guard() -> dict:
    """
    【防護 6】全域請求守門員（跨所有使用者共用，因為對蝦皮來說都是同一個 IP）。
    - 節流：兩次真實請求至少間隔 SHOPEE_MIN_INTERVAL 秒
    - 限量：每小時最多 SHOPEE_HOURLY_CAP 次
    - 斷路器：一旦被擋，SHOPEE_COOLDOWN_SEC 內完全不再連線，避免越擋越嚴重或帳號被鎖
    注意：『清除快取』按鈕只清 cache_data，不會重置這裡的冷卻狀態。
    """
    return {"lock": threading.Lock(), "last": 0.0, "history": deque(), "blocked_until": 0.0, "block_reason": ""}


def guard_status() -> dict:
    """給側邊欄顯示用：冷卻剩餘秒數、本小時已用次數。"""
    g = shopee_guard()
    now = time.time()
    with g["lock"]:
        while g["history"] and now - g["history"][0] > 3600:
            g["history"].popleft()
        return {"cooldown": max(0, g["blocked_until"] - now), "used": len(g["history"]),
                "reason": g["block_reason"]}


def _guard_acquire() -> str | None:
    """取得一次請求額度；不允許時回傳原因字串。允許時會先等到節流間隔已過。"""
    g = shopee_guard()
    with g["lock"]:
        now = time.time()
        if now < g["blocked_until"]:
            mins = (g["blocked_until"] - now) / 60
            return f"先前被蝦皮擋下（{g['block_reason']}），冷卻中，約 {mins:.0f} 分鐘後才會再連線"
        while g["history"] and now - g["history"][0] > 3600:
            g["history"].popleft()
        if len(g["history"]) >= SHOPEE_HOURLY_CAP:
            return f"已達每小時 {SHOPEE_HOURLY_CAP} 次的自我限制，暫停連線"
        wait = SHOPEE_MIN_INTERVAL - (now - g["last"])
        if wait > 0:
            time.sleep(wait)
        g["last"] = time.time()
        g["history"].append(g["last"])
    return None


def _guard_trip(reason: str) -> None:
    """被擋時觸發斷路器。"""
    g = shopee_guard()
    with g["lock"]:
        g["blocked_until"] = time.time() + SHOPEE_COOLDOWN_SEC
        g["block_reason"] = reason


@st.cache_data(ttl=600, show_spinner=False)  # 【防護 1】快取 10 分鐘，同關鍵字不重複請求
def fetch_shopee_items(keyword: str, limit: int = 50, cookie: str = "") -> dict:
    """
    呼叫蝦皮搜尋 API。此函式**永不拋出例外**：失敗時回傳 ok=False 與原因，
    由主程式切換到離線備援資料（防護 5）。
    快取命中時不會執行函式本體，因此不會消耗守門員的額度。

    回傳結構：{"ok": bool, "items": list[dict], "status": int|None, "reason": str}
    """
    out = {"ok": False, "items": [], "status": None, "reason": ""}

    # 【防護 6】守門員：冷卻中／超過上限就直接不連線
    denied = _guard_acquire()
    if denied:
        out["reason"] = denied
        return out

    try:
        # 【防護 2】模擬人類操作的隨機延遲
        time.sleep(random.uniform(1.0, 2.0))

        session = _build_shopee_session(keyword, cookie)
        params = {
            "by": "relevancy",
            "keyword": keyword,
            "limit": limit,
            "newest": 0,
            "order": "desc",
            "page_type": "search",
            "scenario": "PAGE_GLOBAL_SEARCH",
            "version": 2,
        }
        resp = session.get(SHOPEE_SEARCH_URL, params=params, timeout=(5, 10))
        out["status"] = resp.status_code

        if resp.status_code == 403:
            out["reason"] = "蝦皮拒絕存取（403 Forbidden）"
            _guard_trip("403")
            return out
        if resp.status_code == 429:
            out["reason"] = "請求過於頻繁（429 Too Many Requests）"
            _guard_trip("429")
            return out
        resp.raise_for_status()

        payload = resp.json()
        # 蝦皮被風控時常回 200 但內容帶 error 代碼
        if payload.get("error"):
            out["reason"] = f"蝦皮回傳錯誤代碼 {payload.get('error')}（觸發風控驗證）"
            _guard_trip(f"錯誤代碼 {payload.get('error')}")
            return out

        items = _parse_shopee_items(payload)
        if not items:
            out["reason"] = "查無商品或 API 回傳結構已變動"
            return out

        out["items"] = items
        out["ok"] = True
    except requests.exceptions.Timeout:
        out["reason"] = "蝦皮連線逾時"
    except requests.exceptions.ConnectionError:
        out["reason"] = "網路連線異常，無法連上蝦皮"
    except requests.exceptions.RequestException as exc:
        out["reason"] = f"蝦皮請求失敗：{type(exc).__name__}"
    except ValueError:
        out["reason"] = "蝦皮回傳內容不是有效的 JSON"
    except Exception as exc:  # noqa: BLE001  最後一道保險，確保畫面不中斷
        out["reason"] = f"非預期錯誤：{type(exc).__name__}"
    return out


def generate_mock_items(keyword: str, reference_twd: float, currency: str, n: int = 40) -> list[dict]:
    """
    【防護 5】產生結構與真實 API 完全一致的模擬備援資料。
    - 以關鍵字雜湊當亂數種子：同一關鍵字每次產生相同資料，錄影 / 展示穩定。
    - 價格以「海外台幣成本」為錨點，隨機加成 0.95~1.45 倍，模擬台灣代購溢價。
    """
    seed = int(hashlib.md5(keyword.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)

    anchor = reference_twd if reference_twd > 0 else 500.0
    market_center = anchor * rng.uniform(0.95, 1.45)

    tw_cities = ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "新竹市"]
    overseas_locs = ["中國大陸", CURRENCIES.get(currency, {}).get("country", "海外"), "海外"]
    shop_prefix = ["小資", "好物", "藥妝", "代購", "日常", "旅人", "嚴選", "質感"]
    shop_suffix = ["代購小舖", "專賣店", "生活館", "批發", "選物", "直送"]

    items = []
    for i in range(n):
        is_tw = rng.random() < 0.65
        # 台灣現貨通常較貴（含運費與時效溢價），海外出貨便宜但等待久
        factor = rng.lognormal(mean=0.0, sigma=0.18) * (1.0 if is_tw else 0.85)
        price = max(round(market_center * factor, 0), 1)
        location = rng.choice(tw_cities) if is_tw else rng.choice(overseas_locs)
        sold = int(rng.pareto(1.3) * 60)
        items.append({
            "商品名稱": f"【{rng.choice(shop_prefix)}{rng.choice(shop_suffix)}】{keyword} 正品 #{i + 1:02d}",
            "價格": price,
            "累積銷量": sold,
            "評分": round(float(rng.uniform(4.2, 5.0)), 1),
            "賣家地區": str(location),
            "出貨地": SHIP_TW if is_tw else SHIP_OVERSEAS,
            "商品連結": f"https://shopee.tw/search?keyword={quote(keyword)}",
        })
    return items


# ============================================================================
# 4. 分析邏輯
# ============================================================================

def calc_overseas_cost(foreign_price: float, rate: float, fee_pct: float, refund_pct: float) -> float:
    """海外實際台幣成本 = 外幣標價 ×（1 − 退稅率）× 匯率 ×（1 + 刷卡手續費率）"""
    return foreign_price * (1 - refund_pct / 100) * rate * (1 + fee_pct / 100)


def remove_outliers_iqr(df: pd.DataFrame, col: str = "價格") -> pd.DataFrame:
    """以 IQR 法排除極端值（例如搜尋到的配件、整箱批發）。"""
    if len(df) < 8:
        return df
    q1, q3 = df[col].quantile([0.25, 0.75])
    iqr = q3 - q1
    return df[df[col].between(q1 - 1.5 * iqr, q3 + 1.5 * iqr)]


def decision_advice(spread: float, ratio: float, qty: int) -> tuple[str, str, str]:
    """依價差比例回傳 (訊息等級, 標籤, 說明)。ratio = (蝦皮中位數 − 海外成本) / 蝦皮中位數"""
    total = spread * qty
    if ratio > 0.20:
        return "success", f"🔥 國外必買！每件現省 NT${spread:,.0f}", \
            f"比蝦皮中位數便宜 {ratio:.0%}，買 {qty} 件共省 NT${total:,.0f}，值得佔行李空間。"
    if ratio >= 0.10:
        return "info", f"👍 可以順手買，每件省 NT${spread:,.0f}", \
            f"便宜 {ratio:.0%}，若行李還有空間就帶；買 {qty} 件共省 NT${total:,.0f}。"
    if ratio >= 0:
        return "warning", "📦 直接蝦皮買，省行李空間", \
            f"價差僅 {ratio:.0%}（每件 NT${spread:,.0f}），不值得扛回台灣。"
    return "error", "🚫 國外反而比較貴", \
        f"海外成本比蝦皮中位數高 NT${-spread:,.0f}（{-ratio:.0%}），建議回台灣在蝦皮購買。"


# ============================================================================
# 5. Streamlit 介面
# ============================================================================

def render_sidebar(bot: dict) -> dict:
    """側邊欄：所有輸入參數，回傳設定 dict。"""
    sb = st.sidebar
    sb.header("✈️ 旅遊購物設定")

    code = sb.selectbox("旅遊國家 / 幣別", list(CURRENCIES), format_func=lambda c: CURRENCIES[c]["label"])

    # --- 匯率：自動帶入台銀，允許手動微調 ---
    if bot["ok"] and code in bot["rates"]:
        auto_rate = bot["rates"][code]
        sb.caption(f"🏦 台銀{bot['rate_type'][code]}：**{auto_rate}**　｜　更新：{bot['updated_at']}")
    else:
        auto_rate = CURRENCIES[code]["fallback_rate"]
        sb.caption(f"⚠️ {bot.get('error') or '無即時匯率'}，暫用參考匯率 {auto_rate}，請手動確認。")

    rate = sb.number_input(
        f"使用匯率（1 {code} = ? TWD，可手動微調）",
        min_value=0.0001, value=float(auto_rate), step=0.0001, format="%.4f", key=f"rate_{code}",
    )

    sb.divider()
    keyword = sb.text_input("🔍 商品關鍵字", value="合利他命", help="例如：合利他命、DHC 護唇膏、吹風機")
    foreign_price = sb.number_input(
        f"💴 國外當地標價（{code}）", min_value=0.0,
        value=float(CURRENCIES[code]["default_price"]), step=1.0, key=f"price_{code}",
    )
    qty = sb.number_input("預計購買數量", min_value=1, value=1, step=1)

    sb.divider()
    fee_pct = sb.number_input("💳 海外刷卡手續費率（%）", min_value=0.0, max_value=10.0, value=1.5, step=0.1)
    refund_pct = sb.number_input(
        "🧾 退稅折抵（%）", min_value=0.0, max_value=30.0, value=0.0, step=0.5,
        help="例如日本含稅價 10% 消費稅，免稅約可折抵 9.09%；韓國依金額級距退稅。",
    )

    sb.divider()
    mode = sb.radio("🛒 蝦皮資料來源", [MODE_AUTO, MODE_OFFLINE, MODE_IMPORT],
                    help="錄影或展示時建議選「僅離線備援」，完全不連蝦皮，零被擋風險。")
    uploaded = None
    if mode == MODE_IMPORT:
        uploaded = sb.file_uploader("上傳蝦皮搜尋結果 JSON", type="json")
        sb.caption("在自己的瀏覽器開啟蝦皮搜尋頁 → F12 開發者工具 → Network → "
                   "找到 search_items 請求 → 在 Response 上按右鍵另存成 .json。")
    elif mode == MODE_AUTO:
        gs = guard_status()
        if gs["cooldown"] > 0:
            sb.warning(f"⏸️ 蝦皮連線冷卻中（{gs['reason']}），剩約 {gs['cooldown'] / 60:.0f} 分鐘")
        else:
            sb.caption(f"🛡️ 本小時已連線 {gs['used']}/{SHOPEE_HOURLY_CAP} 次")

    with sb.expander("⚙️ 進階設定"):
        limit = st.slider("蝦皮搜尋筆數", 20, 60, 50, step=10)
        exclude_outliers = st.checkbox("排除極端價格（IQR 法）", value=True,
                                       help="過濾配件、整箱批發等明顯偏離的價格")
        cookie = st.text_input("蝦皮 Cookie（選填）", type="password",
                               help="遇到 403 時，可從瀏覽器登入蝦皮後複製 Cookie 貼上。僅存在本次記憶體中，不會寫入檔案。")
        if st.button("🔄 清除快取並重新查詢"):
            st.cache_data.clear()
            st.rerun()

    return dict(code=code, rate=rate, keyword=keyword.strip(), foreign_price=foreign_price, qty=int(qty),
                fee_pct=fee_pct, refund_pct=refund_pct, limit=limit,
                exclude_outliers=exclude_outliers, cookie=cookie.strip(),
                mode=mode, uploaded=uploaded)


def render_charts(df: pd.DataFrame, overseas_twd: float) -> None:
    """Plotly 視覺化：價格分佈（直方圖 + 箱型圖）與 價格 vs 銷量 散布圖。"""
    color_map = {SHIP_TW: "#2E86DE", SHIP_OVERSEAS: "#F39C12"}
    left, right = st.columns(2)

    with left:
        st.markdown("**① 蝦皮賣家價格分佈**")
        fig = px.histogram(
            df, x="價格", color="出貨地", marginal="box", nbins=25,
            barmode="overlay", opacity=0.75, color_discrete_map=color_map,
            labels={"價格": "蝦皮售價（TWD）", "count": "賣家數"},
        )
        # 紅色垂直線：國外當地台幣換算價（直方圖與箱型圖共用 x 軸，一起標註）
        fig.add_vline(x=overseas_twd, line_color="red", line_dash="dash", line_width=3)
        fig.add_annotation(
            x=overseas_twd, y=1.02, xref="x", yref="paper", showarrow=False,
            text=f"海外成本 NT${overseas_twd:,.0f}", font=dict(color="red", size=13),
        )
        fig.update_layout(height=430, margin=dict(t=40, b=10), legend_title_text="", yaxis_title="賣家數")
        st.plotly_chart(fig, width="stretch")

    with right:
        st.markdown("**② 價格 vs. 累積銷量**")
        use_log = st.checkbox("銷量使用對數座標", value=df["累積銷量"].max() > 1000,
                              help="對數座標下銷量為 0 的商品不會顯示")
        fig2 = px.scatter(
            df, x="累積銷量", y="價格", color="出貨地", color_discrete_map=color_map,
            hover_name="商品名稱", hover_data={"賣家地區": True, "評分": True},
            log_x=use_log, opacity=0.8,
            labels={"價格": "蝦皮售價（TWD）"},
        )
        fig2.add_hline(y=overseas_twd, line_color="red", line_dash="dash", line_width=2,
                       annotation_text="海外成本", annotation_font_color="red")
        fig2.update_traces(marker=dict(size=10, line=dict(width=0.5, color="white")))
        fig2.update_layout(height=400, margin=dict(t=20, b=10), legend_title_text="")
        st.plotly_chart(fig2, width="stretch")


def render_table(df: pd.DataFrame, overseas_twd: float, keyword: str) -> None:
    """商品明細：出貨地篩選、價差計算、CSV 下載。"""
    st.subheader("📋 商品明細")
    ship_filter = st.multiselect("出貨地篩選", [SHIP_TW, SHIP_OVERSEAS], default=[SHIP_TW, SHIP_OVERSEAS])
    view = df[df["出貨地"].isin(ship_filter)].copy()

    # 價差 > 0 表示蝦皮比海外貴（= 在國外買可省下的金額）
    view["價差(元)"] = (view["價格"] - overseas_twd).round(0)
    view["價差(%)"] = ((view["價格"] - overseas_twd) / view["價格"] * 100).round(1)
    view = view.sort_values("價格").reset_index(drop=True)

    cols = ["商品名稱", "價格", "價差(元)", "價差(%)", "累積銷量", "評分", "出貨地", "賣家地區", "商品連結"]
    st.dataframe(
        view[cols], width="stretch", hide_index=True, height=380,
        column_config={
            "價格": st.column_config.NumberColumn("蝦皮售價", format="NT$%d"),
            "價差(元)": st.column_config.NumberColumn("價差(元)", format="%d", help="蝦皮售價 − 海外成本"),
            "價差(%)": st.column_config.NumberColumn("價差(%)", format="%.1f%%"),
            "商品連結": st.column_config.LinkColumn("連結", display_text="開啟"),
        },
    )
    st.caption(f"共 {len(view)} 筆　｜　價差為正：在國外買比較省")

    csv_bytes = view[cols].to_csv(index=False).encode("utf-8-sig")  # utf-8-sig：Excel 開啟不亂碼
    st.download_button(
        "⬇️ 下載 CSV", data=csv_bytes, mime="text/csv",
        file_name=f"price_compare_{keyword}_{datetime.now(TW_TZ):%Y%m%d_%H%M}.csv",
    )


def main() -> None:
    st.set_page_config(page_title="國際旅遊外幣購物預算精算機", page_icon="✈️", layout="wide")
    st.title("✈️ 國際旅遊外幣購物預算精算機")
    st.caption("國外買還是回台灣蝦皮買？輸入當地標價，一鍵算出真實成本與價差。")

    # ---------- 讀取匯率 + 側邊欄 ----------
    with st.spinner("讀取台灣銀行即時匯率…"):
        bot = fetch_bot_rates()
    cfg = render_sidebar(bot)

    if not cfg["keyword"]:
        st.warning("請在左側輸入商品關鍵字。")
        st.stop()
    if cfg["foreign_price"] <= 0:
        st.warning("請輸入大於 0 的國外當地標價。")
        st.stop()

    overseas_twd = calc_overseas_cost(cfg["foreign_price"], cfg["rate"], cfg["fee_pct"], cfg["refund_pct"])

    # ---------- 蝦皮資料（依模式取得；任何失敗都自動切換備援） ----------
    shopee = {"ok": False, "items": [], "reason": ""}
    source_label = "即時"
    if cfg["mode"] == MODE_OFFLINE:
        shopee["reason"] = "使用者選擇離線模式，未連線蝦皮"
    elif cfg["mode"] == MODE_IMPORT:
        source_label = "匯入檔案"
        if cfg["uploaded"] is None:
            shopee["reason"] = "尚未上傳 JSON 檔"
        else:
            try:
                items = _parse_shopee_items(json.load(cfg["uploaded"]))
                shopee = {"ok": bool(items), "items": items,
                          "reason": "" if items else "JSON 中找不到商品資料"}
            except Exception:  # noqa: BLE001  檔案格式錯誤也不中斷
                shopee["reason"] = "JSON 檔格式無法解析"
    else:
        with st.spinner(f"正在搜尋蝦皮「{cfg['keyword']}」…"):
            shopee = fetch_shopee_items(cfg["keyword"], cfg["limit"], cfg["cookie"])

    if shopee["ok"]:
        df = pd.DataFrame(shopee["items"])
        st.success(f"✅ 已取得蝦皮{source_label}資料 {len(df)} 筆", icon="🛒")
    else:
        df = pd.DataFrame(generate_mock_items(cfg["keyword"], overseas_twd, cfg["code"]))
        st.info(
            f"已切換至離線備援資料模式（原因：{shopee['reason'] or '未知'}）。"
            "以下為模擬資料，分析流程與真實資料完全相同；可於「進階設定」填入 Cookie 後重試。",
            icon="🛟",
        )

    # 資料清洗：確保數值型態並移除無效列
    df["價格"] = pd.to_numeric(df["價格"], errors="coerce")
    df["累積銷量"] = pd.to_numeric(df["累積銷量"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["價格"])
    df = df[df["價格"] > 0]
    raw_count = len(df)
    if cfg["exclude_outliers"]:
        df = remove_outliers_iqr(df)
    if df.empty:
        st.error("沒有可分析的商品資料，請換個關鍵字試試。")
        st.stop()
    if len(df) < raw_count:
        st.caption(f"已排除 {raw_count - len(df)} 筆極端價格商品。")

    # ---------- 統計 ----------
    p_min, p_med, p_max = df["價格"].min(), df["價格"].median(), df["價格"].max()
    spread = p_med - overseas_twd                    # 以中位數衡量「一般市場價」的價差
    ratio = spread / p_med if p_med else 0
    max_spread = p_max - overseas_twd                # 最大價差：與蝦皮最高價相比
    percentile = (df["價格"] < overseas_twd).mean()  # 海外成本在蝦皮價格中的百分位

    # ---------- KPI 指標 ----------
    st.subheader("📊 關鍵指標")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric(f"台銀匯率（{cfg['code']}）", f"{cfg['rate']:.4f}")
    k2.metric("海外實際台幣成本", f"NT${overseas_twd:,.0f}",
              delta=f"{-spread:+,.0f} vs 中位數", delta_color="inverse",
              help="外幣標價 ×（1−退稅）× 匯率 ×（1+手續費）")
    k3.metric("蝦皮市場最低價", f"NT${p_min:,.0f}")
    k4.metric("市場中位數價", f"NT${p_med:,.0f}")
    k5.metric("最大價差", f"NT${max_spread:,.0f}", help="蝦皮最高價 − 海外成本")

    # ---------- 智慧購買決策 ----------
    st.subheader("🧠 智慧購買決策建議")
    level, title, detail = decision_advice(spread, ratio, cfg["qty"])
    getattr(st, level)(f"### {title}\n{detail}")
    st.caption(f"海外成本落在蝦皮價格第 **{percentile:.0%}** 百分位"
               f"（有 {percentile:.0%} 的蝦皮賣家比國外便宜）。")

    # ---------- 4W1H 摘要 ----------
    with st.expander("🗂️ 4W1H 分析摘要", expanded=False):
        n_tw = int((df["出貨地"] == SHIP_TW).sum())
        n_os = len(df) - n_tw
        st.markdown(
            f"- **What｜買什麼**：{cfg['keyword']}（{cfg['qty']} 件）\n"
            f"- **Where｜在哪買**：{CURRENCIES[cfg['code']]['country']}當地 NT${overseas_twd:,.0f} "
            f"vs 蝦皮中位數 NT${p_med:,.0f}\n"
            f"- **When｜何時的價格**：匯率 {bot.get('updated_at') or '參考值'}；"
            f"蝦皮資料 {source_label if shopee['ok'] else '離線備援'}\n"
            f"- **Who｜誰在賣**：台灣現貨 {n_tw} 家、海外出貨 {n_os} 家\n"
            f"- **How｜怎麼買最划算**：{title}"
        )

    # ---------- 圖表 + 明細 ----------
    st.subheader("📈 視覺化分析")
    render_charts(df, overseas_twd)
    render_table(df, overseas_twd, cfg["keyword"])

    st.divider()
    st.caption("資料來源：臺灣銀行牌告匯率、蝦皮購物搜尋結果。價格僅供參考，實際以結帳金額為準。")


if __name__ == "__main__":
    main()
