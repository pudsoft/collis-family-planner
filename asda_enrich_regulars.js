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

const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const crypto = require('crypto');

const REGULARS_FILE = path.join(__dirname, 'data', 'asda_regulars.json');
const SESSION_FILE  = path.join(__dirname, 'data', 'asda_session.json');
const OCP_KEY       = 'bc042eff107c4bca87dccb19ae707d16';
const ORDER_LIMIT   = 20;   // older orders to fetch (recent + this many)

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
  return cookies.map(c => `${c.name}=${c.value}`).join('; ');
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

// ── Phase 2: Fetch order list + all order details via direct HTTP ─────────────

async function fetchOrders(cookies, sessionId) {
  console.log('\n[2/4] Fetching order history…');

  const baseHeaders = {
    'ocp-apim-subscription-key': OCP_KEY,
    'cookie': cookieHeader(cookies),
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
    'x-correlation-id': uuid(),
    ...(sessionId ? { 'x-apisession-id': sessionId } : {}),
  };

  const listUrl = `https://api2.asda.com/external/ghs/order/v1/list?olderOrderLimit=${ORDER_LIMIT}`;
  const listData = await httpsGet(listUrl, baseHeaders);

  const recent = listData.recentOrders?.orders || [];
  const older  = listData.olderOrders?.orders  || [];
  const allOrders = [...recent, ...older];
  console.log(`      Found ${allOrders.length} orders`);

  const allItems = {}; // productId → { name, totalQty, orderCount }

  for (const [i, order] of allOrders.entries()) {
    const orderId = order.orderNumber;
    process.stdout.write(`      Fetching order ${i + 1}/${allOrders.length} (${orderId})…\r`);

    const detailUrl = `https://api2.asda.com/external/ghs/order/v1/detail/${orderId}?sellingChannel=ASDA_GROCERIES&orgId=ASDA`;
    let detail;
    try {
      detail = await httpsGet(detailUrl, { ...baseHeaders, 'x-correlation-id': uuid() });
    } catch (e) {
      console.log(`\n      [WARN] Could not fetch order ${orderId}: ${e.message}`);
      continue;
    }

    for (const dept of (detail.items || [])) {
      for (const item of (dept.items || [])) {
        if (!item.productId || item.unavailable) continue;
        const pid = String(item.productId);
        if (!allItems[pid]) {
          allItems[pid] = { name: cleanName(item.name), totalQty: 0, orderCount: 0 };
        }
        allItems[pid].totalQty  += (item.quantity || 1);
        allItems[pid].orderCount += 1;
      }
    }

    // Polite delay between requests
    await new Promise(r => setTimeout(r, 300));
  }

  console.log(`\n      Extracted ${Object.keys(allItems).length} unique products from order history`);
  return allItems;
}

// ── Phase 3: Resolve any names still missing via Algolia ──────────────────────

async function resolveNamesAlgolia(productIds) {
  if (!productIds.length) return {};
  console.log(`\n[3/4] Resolving ${productIds.length} product names via Algolia…`);

  const now   = Math.floor(Date.now() / 1000);
  const filter = productIds.map(id => `CIN:${id}`).join(' OR ');
  const result = await httpsPost(
    `${ALGOLIA_APP.toLowerCase()}-dsn.algolia.net`,
    '/1/indexes/*/queries',
    { 'x-algolia-application-id': ALGOLIA_APP, 'x-algolia-api-key': ALGOLIA_KEY },
    {
      requests: [{
        indexName: ALGOLIA_INDEX,
        query: '',
        params: [
          `hitsPerPage=${productIds.length}`,
          `attributesToRetrieve=["CIN","NAME","PACK_SIZE"]`,
          `filters=(${filter}) AND (STATUS:A OR STATUS:I) AND STOCK.${STORE_ID}>0`,
        ].join('&'),
      }],
    }
  );

  const byId = {};
  for (const hit of (result.results?.[0]?.hits || [])) {
    if (hit.CIN) byId[String(hit.CIN)] = hit.NAME || null;
  }
  console.log(`      Resolved ${Object.keys(byId).length}/${productIds.length} names`);
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
    const { cookies, sessionId } = loadSession();
    const orderItems = await fetchOrders(cookies, sessionId);
    const existing   = JSON.parse(fs.readFileSync(REGULARS_FILE, 'utf8'));
    const existingIds = new Set(existing.map(r => r.product_id));

    const newIds = Object.keys(orderItems).filter(pid => !existingIds.has(pid));

    // Resolve names for new items that didn't come with a name
    const needsAlgolia = newIds.filter(pid => !orderItems[pid].name);
    let algoliaNames = {};
    if (needsAlgolia.length) {
      algoliaNames = await resolveNamesAlgolia(needsAlgolia);
    } else {
      console.log('\n[3/4] All new item names resolved from order data — skipping Algolia');
    }

    console.log('\n[4/4] Merging into asda_regulars.json…');

    // Update usual_qty on existing regulars from order history
    let updated = 0;
    for (const reg of existing) {
      const hist = orderItems[reg.product_id];
      if (hist) {
        const avgQty = Math.round(hist.totalQty / hist.orderCount);
        if (avgQty > reg.usual_qty) { reg.usual_qty = avgQty; updated++; }
      }
    }

    // Add new items
    let added = 0;
    let skipped = 0;
    for (const pid of newIds) {
      const name = orderItems[pid].name || algoliaNames[pid];
      if (!name) { skipped++; continue; }
      const avgQty = Math.round(orderItems[pid].totalQty / orderItems[pid].orderCount) || 1;
      existing.push({ product_id: pid, name, usual_qty: avgQty });
      added++;
    }

    // Sort: existing order history frequency descending, then alphabetical
    const freq = pid => orderItems[pid]?.orderCount || 0;
    existing.sort((a, b) => freq(b.product_id) - freq(a.product_id) || a.name.localeCompare(b.name));

    fs.writeFileSync(REGULARS_FILE, JSON.stringify(existing, null, 2));

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
