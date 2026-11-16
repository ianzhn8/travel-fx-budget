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
    "長袖": {"jp": ["長袖T"], "jp_exclude": r"半袖|ノースリーブ|ジャケット|ブルゾン|コート|カーディガン|パーカ|セット", "tw": "長袖上衣"},
    "短袖": {"jp": ["半袖T"], "jp_exclude": r"長袖|12分袖|ヒートテック|ウォーム|ワンピース|ブラ", "tw": "短袖T恤"},
    "外套": {"jp": ["ジャケット", "アウター"], "jp_exclude": r"", "tw": "外套"},
    "襯衫": {"jp": ["シャツ"], "jp_exclude": r"Tシャツ|ＵＴ|(?<![A-Za-z])UT(?![A-Za-z])|ポロ|スウェット|ワンピース|ジャケット", "tw": "襯衫"},
    "長褲": {"jp": ["パンツ"],
             "jp_exclude": r"ショート|ハーフ|ボクサー|トランクス|ショーツ|ブリーフ|レギンス|インナー",
             "tw": "長褲"},
    "短褲": {"jp": ["ショートパンツ", "ハーフパンツ"],
             "jp_exclude": r"ボクサー|トランクス|インナー|スコート|スカート|ペチコート|ステテコ|サロペット|ウォーム|ウォーマー|ラウンジ|GIRLS", "tw": "短褲"},
    "配件": {"jp": ["バッグ", "キャップ", "ソックス"], "jp_exclude": r"リユーザブル|収納|チャーム", "tw": "配件"},
}

# 台灣縣市：判斷蝦皮賣家是「台灣現貨」還是「海外出貨」
TW_LOCATIONS = [
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "苗栗", "彰化",
    "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "澎湖", "金門", "連江", "馬祖",
]
# 二手 / 轉賣商品的標題關鍵字（價格不代表新品市場價，比價時預設排除）
USED_PATTERN = (r"二手|中古|古著|近全新|近新|[0-9９]成新|[一二三四五六七八九]成新|9\.?[0-9]?成|"
                r"轉賣|僅試穿|少穿|試穿|有使用痕跡|已下水")

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


# 日文系列 / 款式名稱 → 蝦皮台灣標題常見寫法（正規表示式，不分大小寫）
JP_TW_TERMS: list[tuple[str, str]] = [
    (r"ヒートテック", r"heattech|發熱衣"),
    (r"エアリズム", r"airism|涼感"),
    (r"ウルトラライトダウン", r"極輕羽絨|輕羽絨"),
    (r"パフテック", r"pufftech|輕暖"),
    (r"感動", r"感動"),
    (r"ウルトラストレッチ", r"特級彈性|ultra ?stretch"),
    (r"ノンアイロン", r"免燙|抗皺|non.?iron"),
    (r"オックスフォード", r"牛津|oxford"),
    (r"フランネル", r"法蘭絨|flannel"),
    (r"コーデュロイ", r"燈芯絨|燈心絨|燈蕊絨|corduroy"),
    (r"フリース", r"搖粒絨|刷毛|fleece"),
    (r"ボア|ファーリー|フラッフィー", r"毛毛|泰迪|羊羔|絨毛|毛茸茸"),
    (r"スウェット", r"大學t|衛衣|sweat"),
    (r"メリノ", r"美麗諾|merino|羊毛"),
    (r"カシミヤ", r"喀什米爾|cashmere"),
    (r"デニム|ジーンズ", r"牛仔|丹寧|denim|jeans"),
    (r"ジョーツ", r"jorts"),
    (r"バレル", r"barrel|錐形|繭型"),
    (r"カーゴ", r"工裝|cargo"),
    (r"ジョガー", r"束口|jogger"),
    (r"(?<!セミ)ワイド", r"寬褲|寬版|wide"),
    (r"テーラード", r"西裝外套"),
    (r"ブルゾン", r"布勞森|夾克"),
    (r"MA-1", r"ma-?1|飛行"),
    (r"パデッド", r"夾層|鋪棉|padded"),
    (r"ギア", r"geared|機能"),
    (r"(?<![A-Za-z])UT(?![A-Za-z])", r"(?<![a-z])ut(?![a-z])|聯名"),
    (r"カウズ", r"kaws"),
    (r"ピーナッツ", r"peanuts|史努比|snoopy"),
    (r"ちいかわ|CHIIKAWA", r"吉伊卡哇|chiikawa"),
    (r"ミッフィー", r"米菲|miffy"),
    (r"ジェイソン・ポラン", r"jason ?polan"),
    (r"バスキア", r"basquiat|巴斯奇亞"),
    (r"ENGINEERED GARMENTS", r"engineered ?garments"),
    (r"ラウンドミニショルダー", r"圓弧|迷你肩背|mini ?shoulder"),
    (r"ショルダーバッグ", r"肩背包|斜背包|斜挎"),
    (r"バックパック|ナップサック", r"後背包|背包"),
    (r"ボストン", r"波士頓"),
    (r"キャップ", r"帽"),
    (r"ソックス", r"襪"),
]


# 中文查詢詞 → (日本官網商品名稱比對用的日文, 蝦皮標題同義詞)
# 第一欄用來判斷使用者輸入的詞屬於哪一組（正規表示式，不分大小寫）
CN_JP_TERMS: list[tuple[str, str, str]] = [
    (r"發熱|heattech|保暖衣|衛生衣", r"ヒートテック", r"發熱衣|heattech"),
    (r"涼感|airism|透氣", r"エアリズム", r"airism|涼感"),
    (r"羽絨|down", r"ダウン", r"羽絨|down"),
    (r"輕暖|pufftech", r"パフテック", r"pufftech|輕暖"),
    (r"牛仔|丹寧|denim|jeans", r"ジーンズ|デニム|ジョーツ", r"牛仔|丹寧|denim|jeans"),
    (r"襯衫|shirt", r"シャツ|ブラウス", r"襯衫|shirt"),
    (r"外套|夾克|jacket", r"ジャケット|ブルゾン|コート|アウター|パーカ", r"外套|夾克|jacket"),
    (r"大衣|coat", r"コート", r"大衣|coat"),
    (r"大學t|衛衣|帽t|sweat", r"スウェット|パーカ", r"大學t|衛衣|帽t|sweat"),
    (r"連帽|帽踢|parka|hoodie", r"パーカ|フーデッド", r"連帽|帽踢|parka|hoodie"),
    (r"毛衣|針織|knit", r"ニット|セーター|カーディガン", r"毛衣|針織|knit"),
    (r"開襟衫|罩衫|cardigan", r"カーディガン", r"開襟|罩衫|cardigan"),
    (r"燈芯絨|燈心絨|燈蕊絨|corduroy", r"コーデュロイ", r"燈芯絨|燈心絨|燈蕊絨|corduroy"),
    (r"刷毛|搖粒絨|fleece", r"フリース", r"刷毛|搖粒絨|fleece"),
    (r"毛毛|泰迪|羊羔|絨毛|毛茸茸", r"ボア|ファーリー|フラッフィー", r"毛毛|泰迪|羊羔|絨毛|毛茸茸"),
    (r"寬褲|寬版|wide", r"(?<!セミ)ワイド", r"寬褲|寬版|wide"),
    (r"束口|jogger", r"ジョガー", r"束口|jogger"),
    (r"錐形|繭型|barrel", r"バレル|テーパード", r"錐形|繭型|barrel"),
    (r"工裝|cargo", r"カーゴ|ワーク|ユーティリティ", r"工裝|cargo"),
    (r"長褲|褲子|pants", r"パンツ|ジーンズ", r"長褲|褲|pants"),
    (r"短褲|shorts", r"ショーツ|ショートパンツ|ハーフパンツ|ジョーツ", r"短褲|shorts"),
    (r"t恤|短t|素t|tee", r"T(?![A-Za-z])|Tシャツ", r"t恤|短t|素t|tee|t-shirt"),
    (r"褲", r"パンツ|ジーンズ|ショーツ|ジョーツ", r"褲|pants|jeans"),
    (r"西裝", r"テーラード|スーツ", r"西裝"),
    (r"牛津|oxford", r"オックスフォード", r"牛津|oxford"),
    (r"免燙|抗皺", r"ノンアイロン", r"免燙|抗皺"),
    (r"法蘭絨|flannel", r"フランネル", r"法蘭絨|flannel"),
    (r"喀什米爾|cashmere", r"カシミヤ", r"喀什米爾|cashmere"),
    (r"羊毛|美麗諾|merino|wool", r"メリノ|ウール", r"羊毛|美麗諾|merino|wool"),
    (r"彈性|stretch", r"ストレッチ", r"彈性|stretch"),
    (r"聯名|ut", r"(?<![A-Za-z])UT(?![A-Za-z])", r"聯名|(?<![a-z])ut(?![a-z])"),
    (r"kaws", r"カウズ", r"kaws"),
    (r"史努比|snoopy|peanuts", r"ピーナッツ", r"史努比|snoopy|peanuts"),
    (r"吉伊卡哇|chiikawa", r"ちいかわ|CHIIKAWA", r"吉伊卡哇|chiikawa"),
    (r"米菲|miffy", r"ミッフィー", r"米菲|miffy"),
    (r"包|bag", r"バッグ|ポーチ|バックパック|ナップサック", r"包|bag"),
    (r"(?<![肩斜])背包|後背包|backpack", r"バックパック|ナップサック", r"背包|backpack"),
    (r"肩背|斜背|斜挎", r"ショルダー", r"肩背|斜背|斜挎"),
    (r"托特|tote", r"トート", r"托特|tote"),
    (r"帽|cap", r"キャップ|ハット|帽", r"帽|cap"),
    (r"襪", r"ソックス", r"襪"),
    (r"皮帶|腰帶|belt", r"ベルト", r"皮帶|腰帶|belt"),
    (r"長袖", r"長袖|ロングスリーブ", r"長袖"),
    (r"短袖|半袖", r"半袖|5分袖", r"短袖|半袖"),
]


def cn_query_terms(query: str) -> list[dict]:
    """
    把中文查詢拆成詞（以空白分隔），每個詞找出對應的日文與蝦皮同義詞。
    回傳 [{"token", "jp": [日文正規表示式...], "tw": [蝦皮正規表示式...]}]
    同一個詞對到多組時「全部都要符合」，例如「肩背包」= 肩背 且 包、「牛仔褲」= 牛仔 且 褲。
    - 查不到對照的英數詞（如 MA-1）：官網與蝦皮都直接用原字比對
    - 查不到對照的中文詞：只用於蝦皮，官網略過（會在畫面上提示）
    """
    terms = []
    for token in str(query or "").split():
        matched = []
        for cn, jp, tw in CN_JP_TERMS:
            m = re.search(cn, token, flags=re.IGNORECASE)
            if m:
                matched.append((len(m.group(0)), jp, tw))
        matched.sort(key=lambda x: -x[0])  # 對到越長的字越具體，排在前面（即時查詢用第一組）
        jp_parts = [jp for _, jp, _ in matched]
        tw_parts = [f"{re.escape(token)}|{tw}" for _, _, tw in matched]
        if not jp_parts and token.isascii():
            jp_parts.append(re.escape(token))
        if not tw_parts:
            tw_parts.append(re.escape(token))
        terms.append({"token": token, "jp": jp_parts, "tw": tw_parts})
    return terms


def match_all_terms(text: str, terms: list[dict], key: str) -> bool:
    """文字是否符合每一個詞的每一組表示式（AND）；該詞沒有對應表示式時略過。"""
    for t in terms:
        for pattern in t[key]:
            if not re.search(pattern, str(text or ""), flags=re.IGNORECASE):
                return False
    return True


def same_item_terms(jp_name: str) -> list[str]:
    """官網商品名稱對到的每一組蝦皮系列詞（逐項對照時分開比對，挑最具體的那組）。"""
    return [tw for jp, tw in JP_TW_TERMS if re.search(jp, jp_name or "", flags=re.IGNORECASE)]


def suggest_same_item_pattern(jp_name: str, product_id: str) -> str:
    """
    依官網商品名稱與編號，產生蝦皮同款比對用的正規表示式（以 | 串接）。
    - 官網編號 E482314-000 → 蝦皮標題常見的 6 碼貨號 482314
    - 日文系列名稱 → 台灣常見寫法（JP_TW_TERMS）
    """
    terms: list[str] = []
    m = re.search(r"(\d{6})", product_id or "")
    if m:
        terms.append(m.group(1))
    for jp, tw in JP_TW_TERMS:
        if re.search(jp, jp_name or "", flags=re.IGNORECASE) and tw not in terms:
            terms.append(tw)
    return "|".join(terms)


def title_matches_pattern(title: str, pattern: str) -> bool:
    """蝦皮標題是否符合同款比對條件；條件格式錯誤時視為不篩選。"""
    if not pattern:
        return True
    try:
        return bool(re.search(pattern, str(title or ""), flags=re.IGNORECASE))
    except re.error:
        return True


def is_used_item(title: str) -> bool:
    """標題看起來是二手或轉賣商品。"""
    return bool(re.search(USED_PATTERN, str(title or "")))


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
        if raw.get("adsid"):  # 跳過廣告商品
            continue
        # 舊版格式：item_basic；2026 新版格式：item_card_displayed_asset + item_data
        basic = raw.get("item_basic") or {}
        asset = raw.get("item_card_displayed_asset") or {}
        data = raw.get("item_data") or {}
        name = asset.get("name") or basic.get("name") or ""
        price_raw = ((data.get("item_card_display_price") or {}).get("price")
                     or (asset.get("display_price") or {}).get("price")
                     or basic.get("price") or basic.get("price_min"))
        price = (price_raw or 0) / 100000
        if price <= 0 or not name:
            continue
        sold = ((data.get("item_card_display_sold_count") or {}).get("historical_sold_count")
                or basic.get("historical_sold") or basic.get("sold") or 0)
        rating = (data.get("item_rating") or basic.get("item_rating") or {}).get("rating_star") or 0
        shopid = raw.get("shopid") or data.get("shopid") or basic.get("shopid")
        itemid = raw.get("itemid") or data.get("itemid") or basic.get("itemid")
        location = asset.get("shop_location") or basic.get("shop_location") or "未知"
        rows.append({
            "商品名稱": name,
            "價格": round(price),
            "累積銷量": int(sold),
            "評分": round(float(rating), 1),
            "賣家地區": location,
            "出貨地": classify_shipping(location),
            "商品連結": f"https://shopee.tw/product/{shopid}/{itemid}" if shopid and itemid else "",
        })
    return rows
