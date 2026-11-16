# 爬蟲程式（資料收集）

本專案的價格資料庫（`data/official_snapshot.csv`、`data/shopee_snapshot.csv`）由以下程式收集。Streamlit App 只讀取資料庫，不需要執行爬蟲。

## 整體流程

```
日本官網 ──(collect.py official)──────────────────────────┐
                                                          ├─→ data/*.csv ─→ app.py
蝦皮 ──(shopee_crawler.py 或 shopee_capture.js)─→ raw/shopee/*.json ──(collect.py shopee)──┘
```

| 步驟 | 程式 | 說明 |
|---|---|---|
| 1 | `collect.py official` | 呼叫 UNIQLO、GU 日本官網公開的商品搜尋介面（`/jp/api/commerce/v5/ja/products`），7 個類別、11 個日文關鍵字 |
| 2 | `crawler/browser_console/shopee_api.js`（建議） | **直接呼叫蝦皮搜尋 API**：在已登入的 shopee.tw 主控台執行，程式主動對 `/api/v4/search/search_items` 發出 14 次請求，下載回傳的 JSON |
| 2 | `crawler/shopee_api.py` | 同上的 Python 版（只用標準庫 urllib），需貼上自己的蝦皮 Cookie |
| 2 | `crawler/shopee_crawler.py` | 用 Playwright 開啟真正的瀏覽器，搜尋 14 組「品牌 + 類別」，存下網頁本身載入的搜尋結果 JSON |
| 2' | `crawler/browser_console/shopee_capture.js` | 同步驟 2，但改成貼在瀏覽器開發者工具的主控台（Console）執行，不需要安裝 Python 套件 |
| 3 | `collect.py shopee` | 讀取 `raw/shopee/*.json`，排除廣告，寫入資料庫 |
| 補充 | `crawler/browser_console/official_fetch.js` | 步驟 1 的主控台版本，適合無法執行 Python 的電腦 |

`catalog.py` 是 App 和爬蟲共用的設定，內容包括：
- 品牌、類別、日文和中文關鍵字
- 類別排除規則
- 解析函式：同時支援蝦皮舊版的 `item_basic` 格式，和 2026 年新版的 `item_card_displayed_asset` 加 `item_data` 格式

## 執行方式

```bash
# 日本官網（只用 Python 標準庫）
python collect.py official

# 蝦皮：直接呼叫 API（建議）
#   打開 shopee.tw 並登入 → F12 → Console → 貼上 browser_console/shopee_api.js
#   或：python crawler/shopee_api.py（需貼上自己的 Cookie）
#   把 JSON 放進 raw/shopee/，再執行 python collect.py shopee

# 蝦皮：方法 A（Playwright）
pip install -r crawler/requirements.txt
python -m playwright install chromium
python crawler/shopee_crawler.py      # 會開啟瀏覽器，請先自行登入蝦皮
python collect.py shopee

# 蝦皮：方法 B（瀏覽器主控台）
#   打開 shopee.tw 並登入 → F12 → Console → 貼上 shopee_capture.js
#   把下載的 JSON 放進 raw/shopee/，再執行：
python collect.py shopee
```

## 為什麼這樣設計（研究方法與倫理）

1. **蝦皮不直接呼叫 API，而是攔截網頁本身的回應。** 蝦皮的搜尋 API 要求網頁 JavaScript 即時產生加密簽章，而且需要登入。本專案不偽造簽章、不繞過驗證，所以由使用者在真正的瀏覽器裡登入，程式只記錄網頁本身載入的搜尋結果。
2. **控制請求量。** 遇到 403、429 或蝦皮的風控錯誤代碼時，程式會立即停止。

   | 資料來源 | 請求次數 | 每次間隔 |
   |---|---|---|
   | 蝦皮 | 14 次搜尋 | 8～12 秒 |
   | 日本官網 | 22 次查詢 | 2～3 秒 |

3. **遇到驗證就交給人。** 出現登入頁或流量驗證頁時，程式會暫停，請使用者自己完成，不自動破解驗證碼。
4. **只收集公開的商品資訊。** 內容包括商品名稱、價格、累積銷量、賣家所在地區和商品連結，不包含任何個人資料。
5. **資料清洗**，分兩個階段：
   - `collect.py` 寫入資料庫時：
     - 排除廣告商品（`adsid` 不為空）
     - 日本官網套用類別排除規則，例如「襯衫」排除 T 恤，「短褲」排除裙子和內搭褲
   - App 分析時：
     - 只保留標題含品牌名的商品，並排除仿款（「類 UNIQLO」）
     - 排除二手和轉賣商品
     - 用 IQR 法排除極端價格
6. **不使用模擬資料。** App 只使用實際收集的資料。資料庫沒有的商品會直接顯示「沒有資料」。

## 本次資料收集紀錄

| 來源 | 收集方式 | 收集時間 | 筆數 |
|---|---|---|---|
| 蝦皮（UNIQLO、GU × 7 類別） | `shopee_api.js` **直接呼叫蝦皮搜尋 API**，共 14 次請求 | 2026-11-17 01:50 | 826 筆（14 次 × 60 筆，已排除 14 筆廣告；API 皆無錯誤代碼） |
| 日本官網（UNIQLO、GU × 7 類別） | 官網公開商品搜尋 API，共 22 次查詢 | 2026-11-17 00:10 | 291 筆 |
