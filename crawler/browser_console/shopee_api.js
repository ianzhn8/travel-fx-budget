/*
 * shopee_api.js — 直接呼叫蝦皮搜尋 API 的收集程式（在瀏覽器主控台執行）
 * =====================================================================
 * 由程式主動對蝦皮 API 端點發出請求：
 *     GET https://shopee.tw/api/v4/search/search_items?keyword=...&limit=60&newest=0...
 * 回傳的 JSON 原樣下載成「品牌_類別.json」，再用 collect.py shopee 清洗寫入資料庫。
 *
 * 為什麼在主控台執行：
 *   蝦皮 API 需要登入狀態（Cookie）。在已登入的 shopee.tw 頁面執行，
 *   瀏覽器會自動帶上你自己的登入狀態，程式不需要、也不會讀取或保存你的 Cookie。
 *
 * 使用方式：
 *   1. 用瀏覽器打開 https://shopee.tw 並登入
 *   2. 按 F12 → Console，貼上本檔全部內容後按 Enter
 *   3. 每 8～12 秒呼叫一次 API，共 14 次（約 2～3 分鐘），每次下載一個 JSON 檔
 *   4. 把 JSON 放進專案的 raw/shopee/，執行 python collect.py shopee
 *
 * 防護設計：
 *   - 依序執行、每次間隔 8～12 秒，不平行請求
 *   - HTTP 403 / 429 或 API 回傳 error 代碼（風控）時立即停止，避免帳號被鎖
 *   - 不偽造任何簽章、不繞過驗證；若蝦皮拒絕直接呼叫，程式會顯示錯誤並停止
 */
(async () => {
  // 與 catalog.py 的 BRANDS / CATEGORIES 對應
  const BRANDS = ['UNIQLO', 'GU'];
  const CATS = { '長袖': '長袖上衣', '短袖': '短袖T恤', '外套': '外套', '襯衫': '襯衫',
                 '長褲': '長褲', '短褲': '短褲', '配件': '配件' };
  const API = '/api/v4/search/search_items';
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const download = (name, data) => {
    const blob = new Blob([JSON.stringify(data)], { type: 'application/json' });
    Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: name }).click();
  };

  if (!location.hostname.endsWith('shopee.tw')) {
    console.error('請在已登入的 https://shopee.tw 頁面執行本程式');
    return;
  }

  const jobs = BRANDS.flatMap(b => Object.entries(CATS).map(([cat, tw]) => [b, cat, `${b} ${tw}`]));
  console.log(`=== 蝦皮 API 收集：共 ${jobs.length} 次請求 ===`);

  for (const [i, [brand, cat, keyword]] of jobs.entries()) {
    const params = new URLSearchParams({
      by: 'relevancy', keyword, limit: 60, newest: 0, order: 'desc',
      page_type: 'search', scenario: 'PAGE_GLOBAL_SEARCH', version: 2,
    });
    let res, data;
    try {
      res = await fetch(`${API}?${params}`, {
        method: 'GET', credentials: 'include',
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest',
                   'X-API-SOURCE': 'pc', 'X-Shopee-Language': 'zh-Hant' },
      });
      data = await res.json();
    } catch (e) {
      console.error(`[${i + 1}/${jobs.length}] ${keyword}：請求失敗（${e.message}），停止`);
      return;
    }

    if (res.status === 403 || res.status === 429) {
      console.error(`⛔ HTTP ${res.status}：蝦皮拒絕這次 API 請求，停止收集`);
      return;
    }
    if (data.error) {
      console.error(`⛔ API 回傳錯誤代碼 ${data.error}（${data.error_msg || '風控驗證'}）：`
        + '蝦皮不接受這種直接呼叫，停止收集');
      return;
    }

    const items = data.items || [];
    const ads = items.filter(x => x.adsid).length;
    download(`${brand}_${cat}.json`, data);
    console.log(`✅ [${i + 1}/${jobs.length}] ${keyword}：${items.length} 筆（廣告 ${ads} 筆）→ ${brand}_${cat}.json`);

    if (i < jobs.length - 1) await sleep(8000 + Math.random() * 4000);   // 間隔 8～12 秒
  }
  console.log('完成。把下載的 JSON 放進 raw/shopee/，再執行 python collect.py shopee');
})();
