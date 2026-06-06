/**
 * asda_enrich_regulars.js
 *
 * Pulls your full ASDA order history, extracts every product ordered,
 * and merges anything new into data/asda_regulars.json.
 *
 * Token-efficient: Edge is open only long enough to capture session cookies.
 * All heavy lifting (order fetching, Algolia lookups) is done via direct HTTP.
 *
 * Usage:  node asda_enrich_regulars.js
 * Prereq: Close Edge before running.
 */

const { chromium } = require('playwright');
const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const crypto = require('crypto');

const REGULARS_FILE = path.join(__dirname, 'data', 'asda_regulars.json');
const SESSION_FILE  = path.join(__dirname, 'data', 'asda_session.json');
const OCP_KEY       = 'bc042eff107c4bca87dccb19ae707d16';
const ORDER_LIMIT   = 6;    // max older orders the API accepts

const ALGOLIA_APP   = '8I6WSKCCNV';
const ALGOLIA_KEY   = '03e4272048dd17f771da37b57ff8a75e';
const ALGOLIA_INDEX = 'ASDA_PRODUCTS';
const STORE_ID      = '4383';

// ── Helpers ───────────────────────────────────────────────────────────────────

function uuid() { return crypto.randomUUID(); }

function httpsGet(url, headers) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    https.get({ hostname: u.hostname, path: u.pathname + u.search, headers }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        try { resolve(JSON.parse(d)); } catch { reject(new Error(`Bad JSON from ${url}: ${d.slice(0,200)}`)); }
      });
    }).on('error', reject);
  });
}

function httpsPost(hostname, path, headers, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      { hostname, path, method: 'POST', headers: { ...headers, 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } },
      res => {
        let d = '';
        res.on('data', c => d += c);
        res.on('end', () => { try { resolve(JSON.parse(d)); } catch { reject(new Error(`Bad JSON: ${d.slice(0,200)}`)); } });
      }
    );
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

function cookieHeader(cookies) {
  return cookies
    .filter(c => c.name && c.value)
    .filter(c => !/[\x00-\x1F\x7F,;]/.test(c.name + '=' + c.value))
    .map(c => `${c.name}=${c.value}`)
    .join('; ');
}

function cleanName(name) {
  // Strip common ASDA brand prefixes from order item names
  return name
    .replace(/^ASDA\s+(The Bakery\s+)?/i, '')
    .replace(/^Exceptional by ASDA\s+/i, '')
    .replace(/^The Bakery at\s+ASDA\s+/i, '')
    .replace(/^Exceptional\s+/i, '')
    .trim();
}

// ── Phase 1: Load session saved by asda_discover.js ──────────────────────────

function loadSession() {
  if (!fs.existsSync(SESSION_FILE)) {
    throw new Error(
      'No session file found.\nRun  node asda_discover.js  first — it saves your session when you press Enter.'
    );
  }
  const session = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
  const ageH = (Date.now() - new Date(session.captured)) / 3600000;
  if (ageH > 48) {
    console.warn(`[WARN] Session is ${ageH.toFixed(0)}h old — cookies may have expired. Re-run asda_discover.js if requests fail.`);
  }
  console.log(`\n[1/4] Loaded session from asda_session.json (${session.cookies.length} cookies, ${ageH.toFixed(1)}h old)`);
  return session;
}

// ── Phase 2: Navigate to past orders page and intercept API responses ─────────

async function fetchOrders() {
  console.log('\n[2/4] Opening Edge to scrape order history (fully automatic)…\n');

  const context = await chromium.launchPersistentContext(
    'C:/Users/Rythm/AppData/Local/Microsoft/Edge/User Data',
    { headless: false, channel: 'msedge', args: [
        '--profile-directory=Default',
        '--disable-blink-features=AutomationControlled',
    ]}
  );
  const page = await context.newPage();

  // Intercept the order list response to get order IDs
  let orderListResolve;
  const orderListPromise = new Promise(r => { orderListResolve = r; });
  page.on('response', async res => {
    if (res.url().includes('order/v1/list')) {
      try { orderListResolve(await res.json()); } catch { orderListResolve(null); }
    }
  });

  await page.goto('https://www.asda.com/groceries/my-account/past-orders', {
    waitUntil: 'domcontentloaded', timeout: 30000,
  });

  // Wait for the list API response (max 15s)
  const orderList = await Promise.race([
    orderListPromise,
    new Promise(r => setTimeout(() => r(null), 15000)),
  ]);

  if (!orderList) throw new Error('Order list did not load — are you logged in to ASDA in Edge?');

  const allOrders = [
    ...(orderList.recentOrders?.orders || []),
    ...(orderList.olderOrders?.orders  || []),
  ];
  console.log(`      Found ${allOrders.length} orders — fetching details via browser…`);

  // Fetch each order detail using the browser's own session (bypasses Cloudflare auth)
  const allItems = {};
  for (const [i, order] of allOrders.entries()) {
    const orderId = order.orderNumber;
    process.stdout.write(`      Order ${i + 1}/${allOrders.length} (${orderId})…\r`);
    try {
      const detail = await page.evaluate(async ({ orderId, ocpKey }) => {
        const r = await fetch(
          `https://api2.asda.com/external/ghs/order/v1/detail/${orderId}?sellingChannel=ASDA_GROCERIES&orgId=ASDA`,
          { headers: { 'ocp-apim-subscription-key': ocpKey }, credentials: 'include' }
        );
        return r.json();
      }, { orderId, ocpKey: OCP_KEY });

      for (const dept of (detail.items || [])) {
        const category = dept.departmentName && dept.departmentName !== 'Substitutes & unavailable'
          ? dept.departmentName : null;
        for (const item of (dept.items || [])) {
          if (!item.productId || item.unavailable) continue;
          const pid = String(item.productId);
          if (!allItems[pid]) allItems[pid] = { name: cleanName(item.name), totalQty: 0, orderCount: 0, category: null };
          allItems[pid].totalQty   += (item.quantity || 1);
          allItems[pid].orderCount += 1;
          if (category && !allItems[pid].category) allItems[pid].category = category;
        }
      }
    } catch (e) {
      console.log(`\n      [WARN] Skipping ${orderId}: ${e.message}`);
    }
    await page.waitForTimeout(400);
  }

  await context.close();
  console.log(`\n      Done — ${Object.keys(allItems).length} unique products extracted`);
  return allItems;
}

// ── Phase 3: Resolve any names still missing via Algolia ──────────────────────

async function algoliaLookup(productIds, label) {
  if (!productIds.length) return {};
  console.log(`\n[3/4] Algolia lookup for ${productIds.length} items (${label})…`);
  const BATCH = 500;
  const byId = {};
  for (let i = 0; i < productIds.length; i += BATCH) {
    const batch  = productIds.slice(i, i + BATCH);
    const filter = batch.map(id => `CIN:${id}`).join(' OR ');
    const result = await httpsPost(
      `${ALGOLIA_APP.toLowerCase()}-dsn.algolia.net`,
      '/1/indexes/*/queries',
      { 'x-algolia-application-id': ALGOLIA_APP, 'x-algolia-api-key': ALGOLIA_KEY },
      { requests: [{ indexName: ALGOLIA_INDEX, query: '',
          params: [
            `hitsPerPage=${batch.length}`,
            `attributesToRetrieve=["CIN","NAME","PRIMARY_TAXONOMY"]`,
            `filters=(${filter}) AND (STATUS:A OR STATUS:I)`,
          ].join('&') }] }
    );
    for (const hit of (result.results?.[0]?.hits || [])) {
      if (!hit.CIN) continue;
      const pid  = String(hit.CIN);
      const aisle = hit.PRIMARY_TAXONOMY?.AISLE_NAME;
      byId[pid] = {
        name:     hit.NAME || null,
        category: (typeof aisle === 'object' ? aisle?.value : aisle) || null,
      };
    }
  }
  console.log(`      Got data for ${Object.keys(byId).length}/${productIds.length} items`);
  return byId;
}

// ── Phase 4: Merge into asda_regulars.json ────────────────────────────────────

function merge(orderItems) {
  console.log('\n[4/4] Merging into asda_regulars.json…');

  const existing = JSON.parse(fs.readFileSync(REGULARS_FILE, 'utf8'));
  const existingIds = new Set(existing.map(r => r.product_id));

  const newIds = Object.keys(orderItems).filter(pid => !existingIds.has(pid));
  console.log(`      ${Object.keys(orderItems).length} products from history, ${newIds.length} not already in regulars`);

  // Entries already in regulars: update usual_qty from order history if it's higher
  for (const reg of existing) {
    const hist = orderItems[reg.product_id];
    if (hist) {
      const avgQty = Math.round(hist.totalQty / hist.orderCount);
      if (avgQty > reg.usual_qty) reg.usual_qty = avgQty;
    }
  }

  // New entries: need names
  const needsName = newIds.filter(pid => !orderItems[pid].name);
  const algoliaNames = {}; // will be filled if needed

  return { existing, newIds, orderItems, algoliaNames };
}

// ── Main ──────────────────────────────────────────────────────────────────────

(async () => {
  try {
    loadSession(); // validates session file exists, warns if stale
    const orderItems = await fetchOrders();
    const existing   = JSON.parse(fs.readFileSync(REGULARS_FILE, 'utf8'));
    const existingIds = new Set(existing.map(r => r.product_id));

    const newIds = Object.keys(orderItems).filter(pid => !existingIds.has(pid));

    // Algolia lookup: new items needing names, plus ALL items needing categories
    const needsName     = newIds.filter(pid => !orderItems[pid].name);
    const needsCategory = [...existing.map(r => r.product_id), ...newIds]
      .filter(pid => !(orderItems[pid]?.category) && !(existing.find(r => r.product_id === pid)?.category));
    const algoliaIds = [...new Set([...needsName, ...needsCategory])];

    let algoliaData = {};
    if (algoliaIds.length) {
      algoliaData = await algoliaLookup(algoliaIds, 'names + categories');
    } else {
      console.log('\n[3/4] All data resolved from order history — skipping Algolia');
    }

    console.log('\n[4/4] Merging into asda_regulars.json…');

    // Update usual_qty and category on existing regulars
    let updated = 0;
    for (const reg of existing) {
      const hist = orderItems[reg.product_id];
      if (hist) {
        const avgQty = Math.round(hist.totalQty / hist.orderCount);
        if (avgQty > reg.usual_qty) { reg.usual_qty = avgQty; updated++; }
        if (!reg.category) reg.category = hist.category || algoliaData[reg.product_id]?.category || null;
      } else if (!reg.category) {
        reg.category = algoliaData[reg.product_id]?.category || null;
      }
    }

    // Add new items
    let added = 0;
    let skipped = 0;
    for (const pid of newIds) {
      const name = orderItems[pid].name || algoliaData[pid]?.name;
      if (!name) { skipped++; continue; }
      const avgQty  = Math.round(orderItems[pid].totalQty / orderItems[pid].orderCount) || 1;
      const category = orderItems[pid].category || algoliaData[pid]?.category || null;
      existing.push({ product_id: pid, name, usual_qty: avgQty, category });
      added++;
    }

    // De-dupe by name (case-insensitive) — keep the entry with the higher order frequency,
    // so if ASDA changes a product ID the newer one wins
    const freq = pid => orderItems[pid]?.orderCount || 0;
    const seenNames = new Map(); // normalised name → index in deduped array
    const deduped = [];
    for (const item of existing) {
      const key = item.name.toLowerCase().trim();
      if (seenNames.has(key)) {
        const existingIdx = seenNames.get(key);
        if (freq(item.product_id) > freq(deduped[existingIdx].product_id)) {
          deduped[existingIdx] = item; // replace with higher-frequency entry
        }
      } else {
        seenNames.set(key, deduped.length);
        deduped.push(item);
      }
    }
    if (deduped.length < existing.length) {
      console.log(`      Removed ${existing.length - deduped.length} name duplicate(s)`);
    }

    deduped.sort((a, b) => freq(b.product_id) - freq(a.product_id) || a.name.localeCompare(b.name));

    fs.writeFileSync(REGULARS_FILE, JSON.stringify(deduped, null, 2));

    console.log(`\n✅ Done!`);
    console.log(`   ${added} new items added`);
    console.log(`   ${updated} items had usual_qty updated`);
    console.log(`   ${skipped} items skipped (name unresolvable)`);
    console.log(`   ${existing.length} total items in regulars list`);
    console.log(`\n   File: ${REGULARS_FILE}`);

  } catch (e) {
    console.error('\n❌ Error:', e.message);
    process.exit(1);
  }
})();
