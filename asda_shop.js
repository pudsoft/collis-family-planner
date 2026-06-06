/**
 * asda_shop.js
 *
 * Reads the exported shopping list JSON and adds all ASDA items to your basket.
 * Manual items (no product_id) are listed separately at the end.
 *
 * Usage:
 *   node asda_shop.js                          (looks for asda-shopping-list.json in Downloads)
 *   node asda_shop.js path\to\list.json        (explicit path)
 *
 * Prereq: Close Edge before running.
 */

const { chromium } = require('playwright');
const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const os     = require('os');

const ORG_ID     = 'f_ecom_bjgs_prd';
const SITE_ID    = 'ASDA_GROCERIES';
const BASE_URL   = 'https://www.asda.com';
const ALGOLIA_APP = '8I6WSKCCNV';
const ALGOLIA_KEY = '03e4272048dd17f771da37b57ff8a75e';
const STORE_ID    = '4383';

// ── Find the shopping list file ───────────────────────────────────────────────

function findListFile() {
  const explicit = process.argv[2];
  if (explicit) {
    if (!fs.existsSync(explicit)) throw new Error(`File not found: ${explicit}`);
    return explicit;
  }
  const downloads = path.join(os.homedir(), 'Downloads', 'asda-shopping-list.json');
  if (fs.existsSync(downloads)) return downloads;
  throw new Error(
    'Could not find asda-shopping-list.json in Downloads.\n' +
    'Export your list from the Family Planner shopping page first,\n' +
    'or pass the path as an argument: node asda_shop.js path\\to\\list.json'
  );
}

// ── Algolia price lookup ──────────────────────────────────────────────────────

function httpsPost(hostname, urlPath, headers, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      { hostname, path: urlPath, method: 'POST',
        headers: { ...headers, 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } },
      res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { try { resolve(JSON.parse(d)); } catch { reject(new Error(d.slice(0,200))); } }); }
    );
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

async function lookupPrices(productIds) {
  if (!productIds.length) return {};
  const filter = productIds.map(id => `CIN:${id}`).join(' OR ');
  const result = await httpsPost(
    `${ALGOLIA_APP.toLowerCase()}-dsn.algolia.net`,
    '/1/indexes/*/queries',
    { 'x-algolia-application-id': ALGOLIA_APP, 'x-algolia-api-key': ALGOLIA_KEY },
    { requests: [{ indexName: 'ASDA_PRODUCTS', query: '',
        params: `hitsPerPage=${productIds.length}&attributesToRetrieve=["CIN","PRICES.EN"]&filters=(${filter}) AND STOCK.${STORE_ID}>0` }] }
  );
  const prices = {};
  for (const hit of (result.results?.[0]?.hits || [])) {
    const p = hit['PRICES.EN'];
    if (hit.CIN && p != null) prices[String(hit.CIN)] = typeof p === 'object' ? Object.values(p)[0] : p;
  }
  return prices;
}

// ── Browser session: grab JWT + basket ID ─────────────────────────────────────

async function grabSession() {
  console.log('\nOpening Edge to authenticate (will close automatically)…');
  const context = await chromium.launchPersistentContext(
    'C:/Users/Rythm/AppData/Local/Microsoft/Edge/User Data',
    { headless: false, channel: 'msedge', args: ['--profile-directory=Default'] }
  );
  const page = await context.newPage();

  let jwt = null, basketId = null, customerId = null;

  page.on('request', req => {
    const auth = req.headers()['authorization'];
    if (auth?.startsWith('Bearer ') && !jwt) jwt = auth.slice(7);
  });

  page.on('response', async res => {
    if (res.url().includes('/customers/') && res.url().includes('/baskets')) {
      try {
        const data = await res.json();
        if (data.baskets?.[0]?.basketId) {
          basketId   = data.baskets[0].basketId;
          customerId = data.baskets[0].customerInfo?.customerId;
        }
      } catch {}
    }
  });

  await page.goto(`${BASE_URL}/groceries`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  // Wait for background SFCC calls to fire
  await page.waitForTimeout(5000);
  await context.close();

  if (!jwt)      throw new Error('Could not capture Bearer token — are you logged in to ASDA in Edge?');
  if (!basketId) throw new Error('Could not find basket ID');
  console.log(`  Authenticated. Basket: ${basketId}`);
  return { jwt, basketId };
}

// ── Add items to basket ───────────────────────────────────────────────────────

async function addToBasket(jwt, basketId, items) {
  const url = new URL(
    `${BASE_URL}/mobify/proxy/ghs-api/checkout/shopper-baskets/v1/organizations/${ORG_ID}/baskets/${basketId}/items?siteId=${SITE_ID}`
  );
  const payload = JSON.stringify(items);
  return new Promise((resolve, reject) => {
    const req = https.request(
      { hostname: url.hostname, path: url.pathname + url.search, method: 'POST',
        headers: { authorization: `Bearer ${jwt}`, 'content-type': 'application/json',
                   'content-length': Buffer.byteLength(payload) } },
      res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { try { resolve(JSON.parse(d)); } catch { reject(new Error(d.slice(0,300))); } }); }
    );
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

// ── Main ──────────────────────────────────────────────────────────────────────

(async () => {
  try {
    const listFile = findListFile();
    const allItems = JSON.parse(fs.readFileSync(listFile, 'utf8'));
    console.log(`\nLoaded ${allItems.length} items from ${path.basename(listFile)}`);

    const asdaItems   = allItems.filter(i => i.product_id && !i.manual);
    const manualItems = allItems.filter(i => i.manual || !i.product_id);

    console.log(`  ${asdaItems.length} ASDA items  |  ${manualItems.length} manual`);

    if (!asdaItems.length) {
      console.log('\nNo ASDA items to add. Done.');
      if (manualItems.length) {
        console.log('\nManual items to add yourself:');
        manualItems.forEach(i => console.log(`  • ${i.name}${i.qty > 1 ? ` ×${i.qty}` : ''}`));
      }
      return;
    }

    // Look up current prices from Algolia
    console.log('\nLooking up current prices…');
    const prices = await lookupPrices(asdaItems.map(i => i.product_id));

    const basketPayload = asdaItems.map(i => ({
      productId: i.product_id,
      quantity:  parseInt(i.qty) || 1,
      price:     prices[i.product_id] ?? 0,
    }));

    // Authenticate via Edge
    const { jwt, basketId } = await grabSession();

    // Add everything in one request
    console.log(`\nAdding ${basketPayload.length} items to basket…`);
    const result = await addToBasket(jwt, basketId, basketPayload);

    if (result.fault || result.error) {
      throw new Error(JSON.stringify(result.fault || result.error));
    }

    const addedCount = result.productItems?.length ?? basketPayload.length;
    console.log(`\n✅ Done! ${addedCount} item(s) added to your ASDA basket.`);
    console.log('   Open https://www.asda.com/groceries to review and checkout.\n');

    if (manualItems.length) {
      console.log('⚠️  These items need to be added manually (no ASDA product ID):');
      manualItems.forEach(i => console.log(`   • ${i.name}${i.qty > 1 ? ` ×${i.qty}` : ''}`));
    }

  } catch (e) {
    console.error('\n❌ Error:', e.message);
    process.exit(1);
  }
})();
