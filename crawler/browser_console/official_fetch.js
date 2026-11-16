/*
 * official_fetch.js — 在 UNIQLO / GU 日本官網主控台執行的價格收集程式
 * ======================================================================
 * 與 collect.py official 做的事情相同（呼叫官網公開的商品搜尋介面），
 * 差別是在官網頁面裡執行（同網域請求），適合在無法執行 Python 的電腦上使用。
 *
 * 使用方式：
 *   1. 打開 https://www.uniqlo.com/jp/ja/ 或 https://www.gu-global.com/jp/ja/
 *   2. 按 F12 → Console，貼上本檔全部內容後按 Enter
 *   3. 約 30～40 秒後自動下載 official_品牌.csv（欄位與 data/official_snapshot.csv 相同）
 *   4. 把內容合併進 data/official_snapshot.csv
 */
(async () => {
  const BRAND = location.hostname.includes('gu-global') ? 'GU' : 'UNIQLO';
  const PRODUCT_PAGE = BRAND === 'GU'
    ? 'https://www.gu-global.com/jp/ja/products/{pid}/00'
    : 'https://www.uniqlo.com/jp/ja/products/{pid}/00';
  // 與 catalog.py 的 CATEGORIES 相同：日文搜尋字、排除規則
  const CATS = {
    '長袖': [['長袖T'], /半袖|ノースリーブ|ジャケット|ブルゾン|コート|カーディガン|パーカ|セット/],
    '短袖': [['半袖T'], /長袖|12分袖|ヒートテック|ウォーム|ワンピース|ブラ/],
    '外套': [['ジャケット', 'アウター'], null],
    '襯衫': [['シャツ'], /Tシャツ|ＵＴ|(?<![A-Za-z])UT(?![A-Za-z])|ポロ|スウェット|ワンピース|ジャケット/],
    '長褲': [['パンツ'], /ショート|ハーフ|ボクサー|トランクス|ショーツ|ブリーフ|レギンス|インナー/],
    '短褲': [['ショートパンツ', 'ハーフパンツ'], /ボクサー|トランクス|インナー|スコート|スカート|ペチコート|ステテコ|サロペット|ウォーム|ウォーマー|ラウンジ|GIRLS/],
    '配件': [['バッグ', 'キャップ', 'ソックス'], /リユーザブル|収納|チャーム/],
  };
  const MAX_PER_CAT = 25;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const now = new Date();
  const stamp = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} `
              + `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const esc = v => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));

  const rows = [['collected_at', 'brand', 'category', '商品編號', '官網商品名稱', '定價(JPY)', '特價(JPY)', '官網連結']];
  for (const [cat, [keywords, exclude]] of Object.entries(CATS)) {
    const seen = new Set();
    let n = 0;
    for (const kw of keywords) {
      await sleep(2000 + Math.random() * 1000);   // 每次查詢間隔 2～3 秒
      const res = await fetch('/jp/api/commerce/v5/ja/products?' +
        new URLSearchParams({ q: kw, limit: 36, offset: 0, httpFailure: 'true' }), { headers: { Accept: 'application/json' } });
      if (res.status === 403 || res.status === 429) { console.error(`⛔ 官網拒絕存取（HTTP ${res.status}），停止`); return; }
      if (!res.ok) { console.warn(`⚠️ ${cat}（${kw}）HTTP ${res.status}`); continue; }
      const items = ((await res.json()).result || {}).items || [];
      for (const it of items) {
        const base = ((it.prices || {}).base || {}).value;
        const promo = ((it.prices || {}).promo || {}).value;
        const name = it.name || '';
        if (!base || (exclude && exclude.test(name)) || seen.has(it.productId) || n >= MAX_PER_CAT) continue;
        seen.add(it.productId); n++;
        rows.push([stamp, BRAND, cat, it.productId, name, base, promo && promo < base ? promo : '',
                   PRODUCT_PAGE.replace('{pid}', it.productId)]);
      }
      console.log(`${BRAND} ${cat}（${kw}）：${items.length} 件`);
    }
  }
  const csv = '﻿' + rows.map(r => r.map(esc).join(',')).join('\n');
  const a = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(new Blob([csv], { type: 'text/csv' })), download: `official_${BRAND}.csv` });
  a.click();
  console.log(`✅ 完成：${rows.length - 1} 筆 → official_${BRAND}.csv`);
})();
