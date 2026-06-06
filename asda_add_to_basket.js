/**
 * asda_add_to_basket.js
 *
 * Reads a shopping list JSON, opens Edge, and adds each ASDA item to the basket
 * with human-like random delays. Leaves the browser open on the basket page
 * so you can review and checkout manually.
 *
 * Usage:
 *   node asda_add_to_basket.js [shopping-list.json]
 *
 * If no file is given it reads data/shopping_list.json.
 * Generate the file from the Family Planner shopping page → "Export for ASDA".
 *
 * Prereq: Close Edge before running.
 */

const { chromium } = require('playwright');
const fs   = require('fs');
const path = require('path');

const LIST_FILE = process.argv[2] || path.join(__dirname, 'data', 'shopping_list.json');

const SFCC_ORG    = 'f_ecom_bjgs_prd';
const SFCC_SITE   = 'ASDA_GROCERIES';
const SFCC_PREFIX = `/mobify/proxy/ghs-api/checkout/shopper-baskets/v1/organizations/${SFCC_ORG}`;

// ── Delay helpers ─────────────────────────────────────────────────────────────

function humanDelay() {
  const r = Math.random();
  let ms;
  if (r < 0.70) ms = 2000  + Math.random() * 3000;   // 70%: 2–5 s
  else if (r < 0.90) ms = 5000  + Math.random() * 5000;   // 20%: 5–10 s
  else               ms = 15000 + Math.random() * 15000;  // 10%: 15–30 s
  return Math.round(ms);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function fmt(ms) {
  return ms >= 10000 ? `${(ms/1000).toFixed(0)}s` : `${(ms/1000).toFixed(1)}s`;
}

// ── Main ──────────────────────────────────────────────────────────────────────

(async () => {
  // 1. Load shopping list
  if (!fs.existsSync(LIST_FILE)) {
    console.error(`\n❌ Shopping list not found: ${LIST_FILE}`);
    console.error('   Export it from the Family Planner shopping page → "Export for ASDA"');
    process.exit(1);
  }

  const allItems = JSON.parse(fs.readFileSync(LIST_FILE, 'utf8'));
  const asdaItems   = allItems.filter(i => i.product_id && !i.manual);
  const manualItems = allItems.filter(i => !i.product_id || i.manual);

  console.log(`\n📋 Shopping list: ${allItems.length} items`);
  console.log(`   ✅ ${asdaItems.length} ASDA items to add automatically`);
  console.log(`   ✏️  ${manualItems.length} manual items (listed at end)\n`);

  if (!asdaItems.length) {
    console.log('No ASDA items to add. Done.');
    if (manualItems.length) printManual(manualItems);
    return;
  }

  // 2. Open Edge
  console.log('[1/3] Opening Edge — waiting for ASDA session to load…');
  const context = await chromium.launchPersistentContext(
    'C:/Users/Rythm/AppData/Local/Microsoft/Edge/User Data',
    { headless: false, channel: 'msedge', args: ['--profile-directory=Default'] }
  );

  const page = await context.newPage();

  // 3. Intercept JWT and basket ID
  let jwt      = null;
  let basketId = null;

  context.on('response', async res => {
    const url = res.url();

    // Capture JWT from any SFCC bearer-auth request
    if (!jwt && url.includes('mobify/proxy/ghs-api')) {
      const auth = res.request().headers()['authorization'] || '';
      const match = auth.match(/^Bearer (.+)$/i);
      if (match) jwt = match[1];
    }

    // Capture basket ID from customer baskets response
    if (!basketId && url.includes('/customers/') && url.includes('/baskets')) {
      try {
        const body = await res.json();
        const bid  = body?.baskets?.[0]?.basketId;
        if (bid) basketId = bid;
      } catch {}
    }
  });

  // Navigate to ASDA — this triggers auth + basket calls in the background
  await page.goto('https://www.asda.com/groceries', { waitUntil: 'domcontentloaded', timeout: 30000 });

  // Wait until we have both JWT and basket ID (up to 20s)
  console.log('[1/3] Waiting for session tokens…');
  for (let i = 0; i < 40; i++) {
    if (jwt && basketId) break;
    await sleep(500);
    // If not found yet, scroll to trigger any lazy background calls
    if (i === 10) await page.evaluate(() => window.scrollBy(0, 200));
  }

  if (!jwt || !basketId) {
    // Try navigating to the basket page itself — always triggers auth calls
    console.log('      Tokens not found on homepage — trying basket page…');
    await page.goto('https://www.asda.com/groceries/checkout/basket', {
      waitUntil: 'domcontentloaded', timeout: 20000
    });
    for (let i = 0; i < 20; i++) {
      if (jwt && basketId) break;
      await sleep(500);
    }
  }

  if (!jwt)      { console.error('\n❌ Could not capture JWT — are you logged in to ASDA?'); await context.close(); process.exit(1); }
  if (!basketId) { console.error('\n❌ Could not capture basket ID'); await context.close(); process.exit(1); }

  console.log(`[1/3] ✅ Session captured (basket: ${basketId.slice(0,12)}…)\n`);

  // 4. Add items to basket
  console.log('[2/3] Adding items to basket…\n');

  const added   = [];
  const failed  = [];

  for (let i = 0; i < asdaItems.length; i++) {
    const item = asdaItems[i];
    const qty  = item.qty || 1;

    process.stdout.write(`  (${i + 1}/${asdaItems.length}) ${item.name} × ${qty} … `);

    const url  = `https://www.asda.com${SFCC_PREFIX}/baskets/${basketId}/items?siteId=${SFCC_SITE}`;
    const body = JSON.stringify([{ productId: item.product_id, quantity: qty }]);

    try {
      const result = await page.evaluate(async ({ url, body, jwt }) => {
        const res = await fetch(url, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            'authorization': `Bearer ${jwt}`,
          },
          body,
        });
        return { status: res.status, ok: res.ok };
      }, { url, body, jwt });

      if (result.ok) {
        console.log('✅');
        added.push(item);
      } else {
        console.log(`❌ (HTTP ${result.status})`);
        failed.push({ ...item, reason: `HTTP ${result.status}` });
      }
    } catch (err) {
      console.log(`❌ (${err.message.split('\n')[0]})`);
      failed.push({ ...item, reason: err.message.split('\n')[0] });
    }

    // Human-like delay between items (skip after last item)
    if (i < asdaItems.length - 1) {
      const delay = humanDelay();
      process.stdout.write(`       ⏱  waiting ${fmt(delay)}…\r`);
      await sleep(delay);
      process.stdout.write(' '.repeat(40) + '\r'); // clear the wait line
    }
  }

  // 5. Navigate to basket for review
  console.log('\n[3/3] Opening basket page for review…');
  await page.goto('https://www.asda.com/groceries/checkout/basket', {
    waitUntil: 'domcontentloaded', timeout: 20000
  });

  // 6. Summary
  console.log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log(`✅ Added:  ${added.length} / ${asdaItems.length} ASDA items`);
  if (failed.length) {
    console.log(`\n❌ Failed (${failed.length}):`);
    failed.forEach(i => console.log(`   • ${i.name} — ${i.reason}`));
  }
  if (manualItems.length) {
    printManual(manualItems);
  }
  console.log('\n🛒 Browser is open on the basket — review and checkout when ready.');
  console.log('   Close the browser window when done.\n');

  // Keep alive until browser is closed
  await context.waitForEvent('close', { timeout: 0 }).catch(() => {});
})();

function printManual(items) {
  console.log(`\n✏️  Manual items — add these yourself in the browser (${items.length}):`);
  items.forEach(i => console.log(`   • ${i.name}${i.qty && i.qty > 1 ? ` × ${i.qty}` : ''}`));
}
