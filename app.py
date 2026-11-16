"""
國際旅遊外幣購物預算精算機｜日系服飾版（UNIQLO × GU）
=======================================================
用途：比較 UNIQLO、GU 在「日本官網」與「台灣蝦皮」的價格（長袖、短袖、外套、襯衫、長褲、短褲、配件），
      把日幣標價換算成實際台幣成本（含刷卡手續費、退稅），給出「日本買 or 蝦皮買」的建議。

資料來源（皆有防護與備援，任何失敗都不會中斷畫面）：
  1. 台灣銀行即時牌告匯率 CSV（日圓現金賣出）
  2. 日本官網價格：即時查詢官網公開商品介面，或讀取資料庫快照 data/official_snapshot.csv
  3. 蝦皮價格：資料庫快照 data/shopee_snapshot.csv、即時 API、匯入 JSON、離線模擬資料

資料庫由 collect.py 建立，詳見 README.md。
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
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import streamlit as st

from catalog import (BRANDS, CATEGORIES, SHIP_OVERSEAS, SHIP_TW, clean_official_rows,
                     is_used_item, parse_official_items, parse_shopee_payload, suggest_same_item_pattern,
                     title_matches_brand, title_matches_pattern, to_float)

# ============================================================================
# 0. 全域常數
# ============================================================================

TW_TZ = timezone(timedelta(hours=8))
BOT_CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
SHOPEE_SEARCH_URL = "https://shopee.tw/api/v4/search/search_items"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

DATA_DIR = Path(__file__).resolve().parent / "data"
OFFICIAL_CSV = DATA_DIR / "official_snapshot.csv"
SHOPEE_CSV = DATA_DIR / "shopee_snapshot.csv"

FALLBACK_JPY_RATE = 0.2150    # 台銀抓取失敗時的參考值（請手動確認）
DEFAULT_MANUAL_JPY = 2990.0   # 完全沒有官網資料時的預設值（請手動確認）

# 資料來源模式
MODE_DB = "📦 資料庫快照（推薦）"
MODE_LIVE = "🌐 即時查詢（失敗改用資料庫）"
MODE_IMPORT = "📂 匯入蝦皮 JSON"
MODE_OFFLINE = "🛟 蝦皮離線模擬資料"

# 守門員：每個外部網站各自節流、限量、被擋就冷卻
GUARD_RULES = {
    "shopee": {"min_interval": 6.0, "hourly_cap": 20, "cooldown": 30 * 60},
    "official": {"min_interval": 3.0, "hourly_cap": 40, "cooldown": 15 * 60},
}


# ============================================================================
# 1. 守門員（節流 / 每小時上限 / 被擋冷卻）
# ============================================================================

@st.cache_resource
def _guards() -> dict:
    """全域共用（對外部網站來說所有使用者都是同一個 IP）；『清除快取』不會重置冷卻。"""
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
    """取得一次請求額度；不允許時回傳原因。允許時先等到節流間隔已過。"""
    g, rule = _guards()[site], GUARD_RULES[site]
    with g["lock"]:
        now = time.time()
        if now < g["blocked_until"]:
            return f"先前被擋下（{g['block_reason']}），冷卻中，約 {(g['blocked_until'] - now) / 60:.0f} 分鐘後再連線"
        while g["history"] and now - g["history"][0] > 3600:
            g["history"].popleft()
        if len(g["history"]) >= rule["hourly_cap"]:
            return f"已達每小時 {rule['hourly_cap']} 次的自我限制"
        wait = rule["min_interval"] - (now - g["last"])
        if wait > 0:
            time.sleep(wait)
        g["last"] = time.time()
        g["history"].append(g["last"])
    return None


def _guard_trip(site: str, reason: str) -> None:
    g = _guards()[site]
    with g["lock"]:
        g["blocked_until"] = time.time() + GUARD_RULES[site]["cooldown"]
        g["block_reason"] = reason


# ============================================================================
# 2. 台灣銀行匯率
# ============================================================================

def parse_bot_csv(text: str) -> dict[str, dict]:
    """台銀 CSV 欄名重複，改以每列的「本行賣出」標記定位：其後第 1 欄現金、第 2 欄即期。"""
    df = pd.read_csv(io.StringIO(text), header=None, skiprows=1, dtype=str, keep_default_na=False)
    rates: dict[str, dict] = {}
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row.tolist()]
        if not cells or not cells[0] or "本行賣出" not in cells:
            continue
        i = cells.index("本行賣出")
        rates[cells[0].upper()] = {
            "cash_sell": to_float(cells[i + 1]) if i + 1 < len(cells) else None,
            "spot_sell": to_float(cells[i + 2]) if i + 2 < len(cells) else None,
        }
    return rates


@st.cache_data(ttl=600, show_spinner=False)
def fetch_bot_jpy_rate() -> dict:
    """台銀日圓匯率（優先現金賣出）。永不拋出例外。"""
    result = {"ok": False, "rate": None, "rate_type": "", "updated_at": None, "error": None}
    try:
        resp = requests.get(BOT_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        text = None
        for enc in ("utf-8-sig", "big5", "cp950"):
            try:
                text = resp.content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("無法辨識 CSV 編碼")
        info = parse_bot_csv(text).get("JPY", {})
        if info.get("cash_sell"):
            result.update(rate=info["cash_sell"], rate_type="現金賣出")
        elif info.get("spot_sell"):
            result.update(rate=info["spot_sell"], rate_type="即期賣出")
        else:
            raise ValueError("CSV 中找不到日圓匯率")
        m = re.search(r"@(\d{12})", resp.headers.get("Content-Disposition", ""))
        result["updated_at"] = (datetime.strptime(m.group(1), "%Y%m%d%H%M").strftime("%Y/%m/%d %H:%M") + "（台銀牌告）"
                                if m else datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M") + "（抓取時間）")
        result["ok"] = True
    except requests.exceptions.Timeout:
        result["error"] = "台銀匯率連線逾時"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"台銀匯率連線失敗：{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"台銀匯率解析失敗：{exc}"
    return result


# ============================================================================
# 3. 資料庫快照（collect.py 產生的 CSV）
# ============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def load_snapshot(path_str: str) -> pd.DataFrame:
    """讀取快照 CSV；檔案不存在或損毀時回傳空表，不中斷程式。"""
    try:
        df = pd.read_csv(path_str, encoding="utf-8-sig", dtype={"商品編號": str})
        df["date"] = df["collected_at"].astype(str).str[:10]
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def snapshot_rows(path: Path, brand: str, category: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """回傳 (該品牌類別全部歷史, 最新一天資料, 最新收集時間)。"""
    df = load_snapshot(str(path))
    if df.empty:
        return df, df, ""
    sub = df[(df["brand"] == brand) & (df["category"] == category)]
    if sub.empty:
        return sub, sub, ""
    latest_date = sub["date"].max()
    latest = sub[sub["date"] == latest_date]
    return sub, latest, str(latest["collected_at"].max())


# ============================================================================
# 4. 日本官網（即時）
# ============================================================================

@st.cache_data(ttl=600, show_spinner=False)
def fetch_official_live(brand: str, category: str) -> dict:
    """即時查詢官網（該類別的所有日文關鍵字合併去重）。永不拋出例外。"""
    out = {"ok": False, "items": [], "reason": ""}
    site = BRANDS[brand]
    rows: list[dict] = []
    for kw in CATEGORIES[category]["jp"]:
        denied = _guard_acquire("official")
        if denied:
            out["reason"] = f"官網{denied}"
            break
        try:
            time.sleep(random.uniform(1.0, 2.0))
            resp = requests.get(
                site["api"], params={"q": kw, "limit": 36, "offset": 0, "httpFailure": "true"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                         "Accept-Language": "ja-JP,ja;q=0.9,zh-TW;q=0.8",
                         "Referer": site["search_page"].format(q=quote(kw))},
                timeout=(5, 10),
            )
            if resp.status_code in (403, 429):
                _guard_trip("official", str(resp.status_code))
                out["reason"] = f"官網拒絕存取（HTTP {resp.status_code}）"
                break
            resp.raise_for_status()
            rows += parse_official_items(resp.json(), brand)
        except requests.exceptions.Timeout:
            out["reason"] = "官網連線逾時"
        except requests.exceptions.RequestException as exc:
            out["reason"] = f"官網連線失敗：{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"官網資料解析失敗：{type(exc).__name__}"
    rows = clean_official_rows(rows, category)
    if rows:
        out.update(ok=True, items=rows, reason="")
    elif not out["reason"]:
        out["reason"] = "官網查無此類別商品"
    return out


def get_official(brand: str, category: str, mode: str) -> dict:
    """
    依模式取得官網價格：
      資料庫模式：快照 → (沒有) 即時
      其他模式  ：即時 → (失敗) 快照
    回傳 {"ok", "items", "source", "reason"}
    """
    _, latest, collected = snapshot_rows(OFFICIAL_CSV, brand, category)
    snap_items = []
    if not latest.empty:
        snap_items = [{
            "商品編號": r["商品編號"], "官網商品名稱": r["官網商品名稱"],
            "定價(JPY)": to_float(r["定價(JPY)"]), "特價(JPY)": to_float(r["特價(JPY)"]),
            "官網連結": r["官網連結"],
        } for _, r in latest.iterrows() if to_float(r["定價(JPY)"])]

    if mode == MODE_DB and snap_items:
        return {"ok": True, "items": snap_items, "source": f"資料庫（{collected}）", "reason": ""}
    live = fetch_official_live(brand, category)
    if live["ok"]:
        return {"ok": True, "items": live["items"], "source": "官網即時", "reason": ""}
    if snap_items:
        return {"ok": True, "items": snap_items, "source": f"資料庫（{collected}，即時查詢失敗改用）",
                "reason": live["reason"]}
    return {"ok": False, "items": [], "source": "手動輸入", "reason": live["reason"]}


def effective_jpy(item: dict, use_sale: bool) -> float:
    return item["特價(JPY)"] if (use_sale and item.get("特價(JPY)")) else item["定價(JPY)"]


# ============================================================================
# 5. 蝦皮（即時 API / 備援模擬）
# ============================================================================

@st.cache_data(ttl=600, show_spinner=False)
def fetch_shopee_live(keyword: str, limit: int = 50, cookie: str = "") -> dict:
    """蝦皮搜尋 API。永不拋出例外；失敗回傳 ok=False 與原因。"""
    out = {"ok": False, "items": [], "reason": ""}
    denied = _guard_acquire("shopee")
    if denied:
        out["reason"] = f"蝦皮{denied}"
        return out
    try:
        time.sleep(random.uniform(1.0, 2.0))  # 模擬人類操作
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
            "Referer": f"https://shopee.tw/search?keyword={quote(keyword)}",
            "X-Requested-With": "XMLHttpRequest", "X-API-SOURCE": "pc", "X-Shopee-Language": "zh-Hant",
        })
        if cookie:
            session.headers["Cookie"] = cookie
            csrf = re.search(r"csrftoken=([^;]+)", cookie)
            if csrf:
                session.headers["X-CSRFToken"] = csrf.group(1)
        resp = session.get(SHOPEE_SEARCH_URL, timeout=(5, 10), params={
            "by": "relevancy", "keyword": keyword, "limit": limit, "newest": 0, "order": "desc",
            "page_type": "search", "scenario": "PAGE_GLOBAL_SEARCH", "version": 2})
        if resp.status_code in (403, 429):
            _guard_trip("shopee", str(resp.status_code))
            out["reason"] = f"蝦皮拒絕存取（HTTP {resp.status_code}）"
            return out
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            _guard_trip("shopee", f"錯誤代碼 {payload.get('error')}")
            out["reason"] = f"蝦皮回傳錯誤代碼 {payload.get('error')}（觸發風控）"
            return out
        items = parse_shopee_payload(payload)
        if items:
            out.update(ok=True, items=items)
        else:
            out["reason"] = "查無商品或回傳結構已變動"
    except requests.exceptions.Timeout:
        out["reason"] = "蝦皮連線逾時"
    except requests.exceptions.RequestException as exc:
        out["reason"] = f"蝦皮連線失敗：{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"蝦皮非預期錯誤：{type(exc).__name__}"
    return out


def generate_mock_items(brand: str, category: str, reference_twd: float, n: int = 40) -> list[dict]:
    """蝦皮離線模擬資料：品牌＋類別當亂數種子（每次相同，錄影穩定），價格為日本成本的 1.05~1.6 倍。"""
    rng = np.random.default_rng(int(hashlib.md5(f"{brand}|{category}".encode()).hexdigest()[:8], 16))
    center = (reference_twd if reference_twd > 0 else 600.0) * rng.uniform(1.05, 1.6)
    tw_item = CATEGORIES[category]["tw"]
    shops = ["日本代購小舖", "東京直送", "日系選物", "大阪連線", "正品代購", "日貨專賣"]
    tags = ["現貨", "日本連線", "正品", "預購", "男女款", "多色可選"]
    cities = ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市"]
    rows = []
    for i in range(n):
        is_tw = rng.random() < 0.6
        rows.append({
            "商品名稱": f"【{rng.choice(shops)}】{brand} {tw_item} {rng.choice(tags)} #{i + 1:02d}",
            "價格": max(round(center * rng.lognormal(0, 0.15) * (1.0 if is_tw else 0.9)), 1),
            "累積銷量": int(rng.pareto(1.3) * 50),
            "評分": round(float(rng.uniform(4.3, 5.0)), 1),
            "賣家地區": str(rng.choice(cities)) if is_tw else str(rng.choice(["日本", "日本", "中國大陸"])),
            "出貨地": SHIP_TW if is_tw else SHIP_OVERSEAS,
            "商品連結": f"https://shopee.tw/search?keyword={quote(brand + ' ' + tw_item)}",
        })
    return rows


def get_shopee(cfg: dict) -> tuple[pd.DataFrame | None, str, str]:
    """依模式取得蝦皮資料，回傳 (DataFrame 或 None, 來源說明, 失敗原因)。None 代表改用模擬資料。"""
    _, latest, collected = snapshot_rows(SHOPEE_CSV, cfg["brand"], cfg["category"])
    snap = latest.drop(columns=["collected_at", "brand", "category", "date"], errors="ignore")

    if cfg["mode"] == MODE_DB:
        if not snap.empty:
            return snap, f"資料庫（{collected}）", ""
        return None, "", "資料庫中還沒有此品牌類別的蝦皮資料（請用 collect.py 匯入）"
    if cfg["mode"] == MODE_LIVE:
        with st.spinner(f"搜尋蝦皮「{cfg['shopee_kw']}」…"):
            live = fetch_shopee_live(cfg["shopee_kw"], cfg["limit"], cfg["cookie"])
        if live["ok"]:
            return pd.DataFrame(live["items"]), "蝦皮即時", ""
        if not snap.empty:
            return snap, f"資料庫（{collected}，即時查詢失敗改用）", live["reason"]
        return None, "", live["reason"]
    if cfg["mode"] == MODE_IMPORT:
        if cfg["uploaded"] is None:
            return None, "", "尚未上傳 JSON 檔"
        try:
            items = parse_shopee_payload(json.load(cfg["uploaded"]))
            return (pd.DataFrame(items), "匯入檔案", "") if items else (None, "", "JSON 中找不到商品資料")
        except Exception:  # noqa: BLE001
            return None, "", "JSON 檔格式無法解析"
    return None, "", "使用者選擇離線模擬資料"


# ============================================================================
# 6. 分析邏輯
# ============================================================================

def calc_overseas_cost(jpy: float, rate: float, fee_pct: float, refund_pct: float) -> float:
    """日本實際台幣成本 = 日幣含稅價 ×（1 − 退稅率）× 匯率 ×（1 + 刷卡手續費率）"""
    return jpy * (1 - refund_pct / 100) * rate * (1 + fee_pct / 100)


def remove_outliers_iqr(df: pd.DataFrame, col: str = "價格") -> pd.DataFrame:
    if len(df) < 8:
        return df
    q1, q3 = df[col].quantile([0.25, 0.75])
    iqr = q3 - q1
    return df[df[col].between(q1 - 1.5 * iqr, q3 + 1.5 * iqr)]


def decision_advice(spread: float, ratio: float, qty: int) -> tuple[str, str, str]:
    """ratio = (蝦皮中位數 − 日本成本) / 蝦皮中位數"""
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
# 7. 介面
# ============================================================================

def render_sidebar(bot: dict) -> dict:
    sb = st.sidebar
    sb.header("👕 UNIQLO × GU 比價設定")
    brand = sb.selectbox("品牌", list(BRANDS))
    category = sb.radio("類別", list(CATEGORIES), horizontal=True)
    mode = sb.radio("資料來源", [MODE_DB, MODE_LIVE, MODE_IMPORT, MODE_OFFLINE],
                    help="錄影展示建議用「資料庫快照」：真實資料、畫面穩定、不會被擋。")

    # --- 日本官網價格 ---
    sb.divider()
    sb.subheader("🇯🇵 日本價格")
    with st.spinner("讀取日本官網價格…"):
        official = get_official(brand, category, mode)
    use_sale = sb.checkbox("使用官網特價（若有）", value=True)
    picked = None
    if official["ok"]:
        items = official["items"]
        median_jpy = float(np.median([effective_jpy(i, use_sale) for i in items]))
        labels = [f"📊 類別中位數（{len(items)} 件商品）｜¥{median_jpy:,.0f}"] + [
            f"{i['官網商品名稱']}｜¥{effective_jpy(i, use_sale):,.0f}{'（特價）' if use_sale and i['特價(JPY)'] else ''}"
            for i in items]
        choice = sb.selectbox("比較對象", range(len(labels)), format_func=lambda k: labels[k],
                              key=f"pick_{brand}_{category}")
        if choice == 0:
            auto_jpy = median_jpy
        else:
            picked = items[choice - 1]
            auto_jpy = effective_jpy(picked, use_sale)
        sb.caption(f"來源：{official['source']}")
    else:
        auto_jpy = DEFAULT_MANUAL_JPY
        sb.caption(f"✍️ 無官網資料（{official['reason']}），請手動輸入含稅價。")
    # --- 同款比對：選了單一官網商品時，蝦皮只保留同款 ---
    same_item = ""
    if picked:
        same_item = sb.text_input(
            "🔎 蝦皮同款關鍵字（用 | 分隔）",
            value=suggest_same_item_pattern(picked["官網商品名稱"], picked["商品編號"]),
            key=f"same_{picked['商品編號']}",
            help="已依官網貨號與系列名稱自動帶入，例如 482314|heattech|發熱衣。清空則改為整個類別比較。",
        ).strip()

    jpy_price = sb.number_input("日本含稅價（JPY，可手動調整）", min_value=0.0, value=float(round(auto_jpy)),
                                step=10.0, key=f"jpy_{brand}_{category}_{use_sale}_{round(auto_jpy)}")
    qty = sb.number_input("預計購買件數", min_value=1, value=1, step=1)

    # --- 匯率與費用 ---
    sb.divider()
    if bot["ok"]:
        auto_rate = bot["rate"]
        sb.caption(f"🏦 台銀日圓{bot['rate_type']}：**{auto_rate}**｜{bot['updated_at']}")
    else:
        auto_rate = FALLBACK_JPY_RATE
        sb.caption(f"⚠️ {bot['error']}，暫用參考匯率 {auto_rate}，請手動確認。")
    rate = sb.number_input("使用匯率（1 JPY = ? TWD）", min_value=0.0001, value=float(auto_rate),
                           step=0.0001, format="%.4f")
    fee_pct = sb.number_input("💳 海外刷卡手續費率（%）", 0.0, 10.0, 1.5, 0.1)
    refund_pct = sb.number_input("🧾 免稅 / 退稅折抵（%）", 0.0, 30.0, 0.0, 0.5,
                                 help="日本含稅價內含 10% 消費稅，符合免稅條件時約可折抵 9.09%（10/110）。"
                                      "免稅門檻與退稅方式請以購買當下店家規定為準。")

    # --- 蝦皮設定 ---
    uploaded = None
    if mode == MODE_IMPORT:
        sb.divider()
        uploaded = sb.file_uploader("上傳蝦皮搜尋結果 JSON", type="json")
    elif mode == MODE_LIVE:
        gs = guard_status("shopee")
        if gs["cooldown"] > 0:
            sb.warning(f"⏸️ 蝦皮連線冷卻中（{gs['reason']}），剩約 {gs['cooldown'] / 60:.0f} 分鐘")
        else:
            sb.caption(f"🛡️ 本小時已連線蝦皮 {gs['used']}/{gs['cap']} 次")

    with sb.expander("⚙️ 進階設定"):
        shopee_kw = st.text_input("蝦皮即時搜尋字串", value=f"{brand} {CATEGORIES[category]['tw']}",
                                  key=f"kw_{brand}_{category}")
        limit = st.slider("蝦皮即時搜尋筆數", 20, 60, 50, step=10)
        brand_only = st.checkbox("只保留標題含品牌名的商品", value=True)
        exclude_used = st.checkbox("排除二手 / 轉賣商品", value=True,
                                   help="標題含「二手」「九成新」「近全新」「轉賣」等字樣的商品，價格不代表新品行情")
        exclude_outliers = st.checkbox("排除極端價格（IQR 法）", value=True)
        cookie = st.text_input("蝦皮 Cookie（選填，不建議在雲端使用）", type="password")
        if st.button("🔄 清除快取並重新讀取"):
            st.cache_data.clear()
            st.rerun()

    return dict(brand=brand, category=category, mode=mode, official=official, picked=picked, same_item=same_item,
                use_sale=use_sale, jpy_price=jpy_price, qty=int(qty), rate=rate, fee_pct=fee_pct,
                refund_pct=refund_pct, uploaded=uploaded, shopee_kw=shopee_kw.strip(), limit=limit,
                brand_only=brand_only, exclude_used=exclude_used, exclude_outliers=exclude_outliers, cookie=cookie.strip())


def render_table(df: pd.DataFrame, overseas_twd: float, tag: str) -> None:
    st.subheader("📋 蝦皮商品明細")
    ship_filter = st.multiselect("出貨地篩選", [SHIP_TW, SHIP_OVERSEAS], default=[SHIP_TW, SHIP_OVERSEAS])
    view = df[df["出貨地"].isin(ship_filter)].copy()
    view["價差(元)"] = (view["價格"] - overseas_twd).round(0)
    view["價差(%)"] = ((view["價格"] - overseas_twd) / view["價格"] * 100).round(1)
    view = view.sort_values("價格").reset_index(drop=True)
    cols = ["商品名稱", "價格", "價差(元)", "價差(%)", "累積銷量", "評分", "出貨地", "賣家地區", "商品連結"]
    st.dataframe(view[cols], width="stretch", hide_index=True, height=380, column_config={
        "價格": st.column_config.NumberColumn("蝦皮售價", format="NT$%d"),
        "價差(元)": st.column_config.NumberColumn(format="%d", help="蝦皮售價 − 日本成本"),
        "價差(%)": st.column_config.NumberColumn(format="%.1f%%"),
        "商品連結": st.column_config.LinkColumn("連結", display_text="開啟"),
    })
    st.caption(f"共 {len(view)} 筆　｜　價差為正：在日本買比較省")
    st.download_button("⬇️ 下載 CSV", data=view[cols].to_csv(index=False).encode("utf-8-sig"), mime="text/csv",
                       file_name=f"price_compare_{tag}_{datetime.now(TW_TZ):%Y%m%d_%H%M}.csv")


def main() -> None:
    st.set_page_config(page_title="UNIQLO × GU 日本 vs 蝦皮 比價", page_icon="👕", layout="wide")
    st.title("👕 國際旅遊外幣購物預算精算機｜UNIQLO × GU")
    st.caption("日本官網 vs 台灣蝦皮：同品牌服飾，在日本買到底省多少？")

    with st.spinner("讀取台灣銀行即時匯率…"):
        bot = fetch_bot_jpy_rate()
    cfg = render_sidebar(bot)
    if cfg["jpy_price"] <= 0:
        st.warning("請輸入大於 0 的日本含稅價。")
        st.stop()
    overseas_twd = calc_overseas_cost(cfg["jpy_price"], cfg["rate"], cfg["fee_pct"], cfg["refund_pct"])
    compare_name = cfg["picked"]["官網商品名稱"] if cfg["picked"] else f"{cfg['category']}類中位數"

    # ---------- 資料狀態 ----------
    if cfg["official"]["ok"]:
        st.success(f"日本價格：{cfg['brand']}｜{compare_name}｜¥{cfg['jpy_price']:,.0f}"
                   f"（來源：{cfg['official']['source']}）", icon="🇯🇵")
    else:
        st.warning(f"日本價格使用手動輸入值 ¥{cfg['jpy_price']:,.0f}（{cfg['official']['reason']}）", icon="✍️")

    df, shopee_source, shopee_reason = get_shopee(cfg)
    if df is not None:
        st.success(f"蝦皮資料：{len(df)} 筆（來源：{shopee_source}）", icon="🛒")
    else:
        df = pd.DataFrame(generate_mock_items(cfg["brand"], cfg["category"], overseas_twd))
        shopee_source = "離線模擬"
        st.info(f"已切換至離線備援資料模式（原因：{shopee_reason}）。以下為模擬資料，分析流程與真實資料相同。",
                icon="🛟")

    # ---------- 清洗 ----------
    df["價格"] = pd.to_numeric(df["價格"], errors="coerce")
    df["累積銷量"] = pd.to_numeric(df["累積銷量"], errors="coerce").fillna(0).astype(int)
    df["評分"] = pd.to_numeric(df["評分"], errors="coerce").fillna(0)
    df = df.dropna(subset=["價格"])
    df = df[df["價格"] > 0]
    notes = []
    if cfg["brand_only"]:
        n0 = len(df)
        df = df[df["商品名稱"].apply(lambda t: title_matches_brand(t, BRANDS[cfg["brand"]]["aliases"]))]
        if n0 - len(df):
            notes.append(f"排除 {n0 - len(df)} 筆標題不含品牌名的商品")
    if cfg["same_item"]:
        matched = df[df["商品名稱"].apply(lambda t: title_matches_pattern(t, cfg["same_item"]))]
        if len(matched) >= 3:
            st.info(f"同款比對：蝦皮有 {len(matched)} 筆標題符合「{cfg['same_item']}」，以下只用這些商品比較。", icon="🔎")
            df = matched
        else:
            st.warning(f"同款比對：蝦皮只有 {len(matched)} 筆符合「{cfg['same_item']}」，樣本太少，"
                       "改用整個類別比較。可在側邊欄調整關鍵字。", icon="🔎")
    if cfg["exclude_used"]:
        n0 = len(df)
        df = df[~df["商品名稱"].apply(is_used_item)]
        if n0 - len(df):
            notes.append(f"排除 {n0 - len(df)} 筆二手 / 轉賣商品")
    if cfg["exclude_outliers"]:
        n0 = len(df)
        df = remove_outliers_iqr(df)
        if n0 - len(df):
            notes.append(f"排除 {n0 - len(df)} 筆極端價格")
    if df.empty:
        st.error("篩選後沒有可分析的蝦皮商品，請在進階設定關閉「只保留標題含品牌名」或換個類別。")
        st.stop()
    if notes:
        st.caption("已" + "、".join(notes) + "。")

    # ---------- 統計與 KPI ----------
    p_min, p_med, p_max = df["價格"].min(), df["價格"].median(), df["價格"].max()
    spread = p_med - overseas_twd
    ratio = spread / p_med if p_med else 0
    percentile = (df["價格"] < overseas_twd).mean()

    st.subheader("📊 關鍵指標")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("台銀匯率（JPY）", f"{cfg['rate']:.4f}")
    k2.metric("日本實際台幣成本", f"NT${overseas_twd:,.0f}", delta=f"{-spread:+,.0f} vs 中位數",
              delta_color="inverse", help=f"¥{cfg['jpy_price']:,.0f} ×（1−退稅）× 匯率 ×（1+手續費）")
    k3.metric("蝦皮市場最低價", f"NT${p_min:,.0f}")
    k4.metric("市場中位數價", f"NT${p_med:,.0f}")
    k5.metric("最大價差", f"NT${p_max - overseas_twd:,.0f}", help="蝦皮最高價 − 日本成本")

    st.subheader("🧠 智慧購買決策建議")
    level, title, detail = decision_advice(spread, ratio, cfg["qty"])
    getattr(st, level)(f"### {title}\n{detail}")
    st.caption(f"日本成本落在蝦皮價格第 **{percentile:.0%}** 百分位（有 {percentile:.0%} 的蝦皮賣家比日本便宜）。")

    with st.expander("🗂️ 4W1H 分析摘要"):
        n_tw = int((df["出貨地"] == SHIP_TW).sum())
        st.markdown(
            f"- **What｜買什麼**：{cfg['brand']} {cfg['category']}｜{compare_name}（{cfg['qty']} 件）\n"
            f"- **Where｜在哪買**：日本 NT${overseas_twd:,.0f}（¥{cfg['jpy_price']:,.0f}） vs 蝦皮中位數 NT${p_med:,.0f}\n"
            f"- **When｜資料時間**：匯率 {bot.get('updated_at') or '參考值'}；日本 {cfg['official']['source']}；"
            f"蝦皮 {shopee_source}\n"
            f"- **Who｜誰在賣**：台灣現貨 {n_tw} 家、海外出貨 {len(df) - n_tw} 家\n"
            f"- **How｜怎麼買最划算**：{title}"
        )

    # ---------- 日本官網每件商品 vs 蝦皮中位數 ----------
    if cfg["official"]["ok"]:
        st.subheader(f"🇯🇵 {cfg['brand']} {cfg['category']}類：日本官網每件商品 vs 蝦皮中位數")
        rows = []
        for it in cfg["official"]["items"]:
            jpy = effective_jpy(it, cfg["use_sale"])
            twd = calc_overseas_cost(jpy, cfg["rate"], cfg["fee_pct"], cfg["refund_pct"])
            rows.append({"官網商品名稱": it["官網商品名稱"], "日本價(JPY)": jpy, "日本成本(TWD)": round(twd),
                         "蝦皮中位數(TWD)": round(p_med), "日本買省(元)": round(p_med - twd),
                         "日本買省(%)": round((p_med - twd) / p_med * 100, 1), "官網連結": it["官網連結"]})
        cmp_df = pd.DataFrame(rows).sort_values("日本買省(元)", ascending=False)
        st.dataframe(cmp_df, width="stretch", hide_index=True, column_config={
            "日本價(JPY)": st.column_config.NumberColumn(format="¥%d"),
            "日本成本(TWD)": st.column_config.NumberColumn(format="NT$%d"),
            "蝦皮中位數(TWD)": st.column_config.NumberColumn(format="NT$%d"),
            "日本買省(%)": st.column_config.NumberColumn(format="%.1f%%"),
            "官網連結": st.column_config.LinkColumn("連結", display_text="開啟"),
        })
        st.caption(f"日本成本已含匯率、刷卡手續費 {cfg['fee_pct']}%、退稅 {cfg['refund_pct']}%；"
                   "「日本買省」為負數代表日本比較貴。")

    render_table(df, overseas_twd, f"{cfg['brand']}_{cfg['category']}")

    st.divider()
    st.caption("資料來源：臺灣銀行牌告匯率、UNIQLO／GU 日本官網、蝦皮購物。價格僅供參考，實際以結帳金額為準。")


if __name__ == "__main__":
    main()
