"""
catalog.py — 品牌 × 類別設定與共用解析函式
=============================================
app.py（Streamlit 介面）與 collect.py（資料收集工具）共用這份設定。
本檔只使用 Python 標準庫，確保在限制較多的 Windows 電腦上也能執行 collect.py。
"""

from __future__ import annotations

import re

# ============================================================================
# 品牌設定：UNIQLO 與 GU 同屬迅銷集團，日本官網使用相同的公開商品介面
# ============================================================================

BRANDS: dict[str, dict] = {
    "UNIQLO": {
        "aliases": ["uniqlo", "優衣庫", "ユニクロ"],
        "api": "https://www.uniqlo.com/jp/api/commerce/v5/ja/products",
        "search_page": "https://www.uniqlo.com/jp/ja/search?q={q}",
        "product_page": "https://www.uniqlo.com/jp/ja/products/{pid}/00",
    },
    "GU": {
        "aliases": ["gu", "ジーユー"],
        "api": "https://www.gu-global.com/jp/api/commerce/v5/ja/products",
        "search_page": "https://www.gu-global.com/jp/ja/search?q={q}",
        "product_page": "https://www.gu-global.com/jp/ja/products/{pid}/00",
    },
}

# ============================================================================
# 類別設定
#   jp         : 日本官網搜尋關鍵字（可多個，結果合併去重）
#   jp_exclude : 官網商品名稱若符合此正規表示式就排除（避免「シャツ」搜到 Tシャツ 等）
#   tw         : 蝦皮搜尋用的中文品項（實際搜尋字串 = 品牌 + 空白 + tw）
# ============================================================================

CATEGORIES: dict[str, dict] = {
    "長袖": {"jp": ["長袖T"], "jp_exclude": r"半袖|ノースリーブ", "tw": "長袖上衣"},
    "短袖": {"jp": ["半袖T"], "jp_exclude": r"長袖", "tw": "短袖T恤"},
    "外套": {"jp": ["ジャケット", "アウター"], "jp_exclude": r"", "tw": "外套"},
    "襯衫": {"jp": ["シャツ"], "jp_exclude": r"Tシャツ|ＵＴ|(?<![A-Za-z])UT(?![A-Za-z])|ポロ|スウェット", "tw": "襯衫"},
    "長褲": {"jp": ["パンツ"],
             "jp_exclude": r"ショート|ハーフ|ボクサー|トランクス|ショーツ|ブリーフ|レギンス|インナー",
             "tw": "長褲"},
    "短褲": {"jp": ["ショートパンツ", "ハーフパンツ"], "jp_exclude": r"ボクサー|トランクス|インナー", "tw": "短褲"},
    "配件": {"jp": ["バッグ", "キャップ", "ソックス"], "jp_exclude": r"", "tw": "配件"},
}

# 台灣縣市：判斷蝦皮賣家是「台灣現貨」還是「海外出貨」
TW_LOCATIONS = [
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "苗栗", "彰化",
    "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "澎湖", "金門", "連江", "馬祖",
]
SHIP_TW = "台灣現貨"
SHIP_OVERSEAS = "海外出貨"


# ============================================================================
# 共用工具
# ============================================================================

def to_float(value) -> float | None:
    """安全轉換為正數 float；空值、0、無法轉換時回傳 None。"""
    try:
        num = float(str(value).strip().replace(",", ""))
        return num if num > 0 else None
    except (TypeError, ValueError):
        return None


def classify_shipping(location: str) -> str:
    """依賣家所在地判斷出貨類型（『臺』統一轉為『台』再比對）。"""
    loc = str(location or "").replace("臺", "台").strip()
    return SHIP_TW if any(city in loc for city in TW_LOCATIONS) else SHIP_OVERSEAS


def title_matches_brand(title: str, aliases: list[str]) -> bool:
    """
    蝦皮商品標題是否真的是該品牌：
    - ASCII 別名用單字邊界比對（GU 不會配到 GUESS）
    - 仿款標題（類 UNIQLO、UNIQLO 風、仿 UNIQLO）不算
    """
    text = str(title or "").lower()
    for alias in aliases:
        a = alias.lower()
        if re.search(rf"(類|仿|似)\s*{re.escape(a)}|{re.escape(a)}\s*(風|感|款式?平替)", text):
            return False
        if a.isascii():
            if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text):
                return True
        elif a in text:
            return True
    return False


# ============================================================================
# 解析函式
# ============================================================================

def parse_official_items(payload: dict, brand: str) -> list[dict]:
    """解析迅銷集團官網商品介面回傳的 JSON，欄位缺漏的商品略過。"""
    site = BRANDS[brand]
    rows = []
    for it in (payload.get("result") or {}).get("items") or []:
        prices = it.get("prices") or {}
        base = to_float((prices.get("base") or {}).get("value"))
        promo = to_float((prices.get("promo") or {}).get("value"))
        if not base:
            continue
        pid = it.get("productId") or ""
        rows.append({
            "商品編號": pid,
            "官網商品名稱": it.get("name") or "（未命名）",
            "定價(JPY)": base,
            "特價(JPY)": promo if promo and promo < base else None,
            "官網連結": site["product_page"].format(pid=pid) if pid else "",
        })
    return rows


def clean_official_rows(rows: list[dict], category: str) -> list[dict]:
    """依類別排除規則過濾官網商品，並以商品編號去重。"""
    pattern = CATEGORIES[category].get("jp_exclude") or ""
    seen, out = set(), []
    for r in rows:
        if pattern and re.search(pattern, r["官網商品名稱"]):
            continue
        key = r["商品編號"] or r["官網商品名稱"]
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def parse_shopee_payload(payload: dict) -> list[dict]:
    """解析蝦皮搜尋結果 JSON（API 或從瀏覽器存下的回應皆可）；價格單位為『元 × 100000』。"""
    raw_items = payload.get("items") or (payload.get("data") or {}).get("items") or []
    rows = []
    for raw in raw_items:
        basic = raw.get("item_basic") or raw
        price_raw = basic.get("price") or basic.get("price_min")
        price = (price_raw or 0) / 100000
        if price <= 0:
            continue
        shopid, itemid = basic.get("shopid"), basic.get("itemid")
        location = basic.get("shop_location") or "未知"
        rows.append({
            "商品名稱": basic.get("name", "（未命名商品）"),
            "價格": round(price),
            "累積銷量": int(basic.get("historical_sold") or basic.get("sold") or 0),
            "評分": round(float((basic.get("item_rating") or {}).get("rating_star") or 0), 1),
            "賣家地區": location,
            "出貨地": classify_shipping(location),
            "商品連結": f"https://shopee.tw/product/{shopid}/{itemid}" if shopid and itemid else "",
        })
    return rows
