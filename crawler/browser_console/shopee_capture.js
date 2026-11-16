/*
 * shopee_capture.js — 在瀏覽器主控台執行的蝦皮搜尋結果收集程式
 * ================================================================
 * 本專案資料庫實際使用的方法：在已登入蝦皮的瀏覽器中，
 * 攔截「網頁本身」載入的搜尋結果，不另外呼叫 API、不偽造任何簽章。
 *
 * 使用方式：
 *   1. 用瀏覽器打開 https://shopee.tw 並登入
 *   2. 按 F12 → Console，貼上本檔全部內容後按 Enter
 *   3. 腳本會自動依序搜尋 14 組關鍵字（每次間隔約 8 秒），
 *      每組結果下載成「品牌_類別.json」
 *   4. 把下載的 JSON 放進專案的 raw/shopee/，執行 python collect.py shopee
 *
 * 也可以只執行步驟 2 的「攔截」部分，然後自己手動在搜尋框搜尋，
 * 每搜尋一次就會自動下載一個檔案。
 */
(async () => {
  // 與 catalog.py 的 CATEGORIES 對應：蝦皮搜尋字 → 類別
  const CATS = { '長袖上衣': '長袖', '短袖T恤': '短袖', '外套': '外套', '襯衫': '襯衫',
                 '長褲': '長褲', '短褲': '短褲', '配件': '配件' };
  const BRANDS = ['UNIQLO', 'GU'];
  const WAIT_MS = () => 8000 + Math.random() * 4000;   // 每次搜尋間隔 8～12 秒
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ---- 1. 攔截網頁自己發出的 search_items 回應（fetch 與 XHR 都攔） ----
  const captured = {};
  const onResponse = (url, data) => {
    if (!url.includes('/api/v4/search/search_items') || !url.includes('newest=0')) return;
    const kw = new URL(url, location.href).searchParams.get('keyword') || '';
    captured[kw.toLowerCase()] = data;
    const [brand, ...rest] = kw.split(' ');
    const cat = CATS[rest.join(' ')] || CATS[Object.keys(CATS).find(k => k.toLowerCase() === rest.join(' ').toLowerCase())];
    const name = cat ? `${brand.toUpperCase()}_${cat}.json` : `shopee_${kw}.json`;
    const blob = new Blob([JSON.stringify(data)], { type: 'application/json' });
    const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: name });
    a.click();
    const items = data.items || [];
    console.log(`✅ ${kw}：${items.length} 筆（廣告 ${items.filter(i => i.adsid).length} 筆）→ ${name}`);
  };
  if (!window.__shopeeHooked) {
    window.__shopeeHooked = true;
    const origFetch = window.fetch;
    window.fetch = async function (...args) {
      const res = await origFetch.apply(this, args);
      try {
        const url = (args[0] && args[0].url) || String(args[0]);
        if (url.includes('search_items')) res.clone().json().then(d => onResponse(url, d));
      } catch (e) { /* 不影響網頁 */ }
      return res;
    };
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) { this.__url = String(u); return origOpen.apply(this, arguments); };
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      this.addEventListener('load', () => {
        try { if (this.__url.includes('search_items')) onResponse(this.__url, JSON.parse(this.responseText)); } catch (e) {}
      });
      return origSend.apply(this, arguments);
    };
  }

  // ---- 2. 用搜尋框依序搜尋（與一般人操作相同，網頁不會整頁重新載入） ----
  const box = document.querySelector('input.shopee-searchbar-input__input');
  if (!box) { console.warn('找不到搜尋框，請手動搜尋，結果仍會自動下載。'); return; }
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (const brand of BRANDS) {
    for (const tw of Object.keys(CATS)) {
      const kw = `${brand} ${tw}`;
      const input = document.querySelector('input.shopee-searchbar-input__input');
      setValue.call(input, kw);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      const btn = document.querySelector('button.shopee-searchbar__search-button');
      if (btn) btn.click();
      else input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
      console.log(`🔍 搜尋「${kw}」`);
      await sleep(WAIT_MS());
      const data = captured[kw.toLowerCase()];
      if (data && data.error) { console.error(`⛔ 蝦皮回傳錯誤代碼 ${data.error}，停止收集`); return; }
      if (!data) console.warn(`⚠️ 「${kw}」沒有攔截到結果，可稍後手動再搜一次`);
    }
  }
  console.log('完成。把下載的 JSON 放進 raw/shopee/，再執行 python collect.py shopee');
})();
