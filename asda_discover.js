/**
 * ASDA API Discovery Tool
 *
 * Opens a headed browser, lets you log in and browse ASDA groceries,
 * and captures all XHR/fetch API calls made by the site.
 *
 * Usage:
 *   node asda_discover.js
 *
 * Then in the browser:
 *   1. Log in to your ASDA account
 *   2. Search for a few products
 *   3. Add an item to your basket
 *   4. Visit Favourites and Past Orders
 *   Press Enter in this terminal when done.
 *
 * Output: asda_api_calls.json
 */

const { chromium } = require('playwright');
const fs = require('fs');

const OUTPUT_FILE = 'asda_api_calls.json';
const MAX_BODY_BYTES = 50_000; // truncate large responses to keep output readable

function tryParseJson(str) {
  try { return JSON.parse(str); } catch { return str; }
}

function summarisePath(url) {
  try {
    const u = new URL(url);
    return `${u.pathname}${u.search ? '?' + [...u.searchParams.keys()].join('&') : ''}`;
  } catch { return url; }
}

(async () => {
  // Use your real Chrome profile so Cloudflare sees a trusted browser with existing cookies/history.
  // Chrome must be fully closed before running this — two instances can't share a profile.
  const context = await chromium.launchPersistentContext(
    'C:/Users/Rythm/AppData/Local/Microsoft/Edge/User Data',
    {
      headless: false,
      channel: 'msedge',
      args: ['--profile-directory=Default'],
    }
  );
  const page = await context.newPage();

  const calls = [];
  const requestMap = new Map(); // Playwright request object → calls entry

  page.on('request', (req) => {
    const url = req.url();
    const type = req.resourceType();
    const CAPTURE_DOMAINS = ['asda.com', 'algolia.net', 'algolianet.com'];
    if ((type === 'fetch' || type === 'xhr') && CAPTURE_DOMAINS.some(d => url.includes(d))) {
      let parsedUrl;
      try { parsedUrl = new URL(url); } catch { return; }

      const entry = {
        timestamp: new Date().toISOString(),
        method: req.method(),
        url,
        path: parsedUrl.pathname,
        query: Object.fromEntries(parsedUrl.searchParams),
        requestHeaders: req.headers(),
        postData: req.postData() ? tryParseJson(req.postData()) : null,
        status: null,
        responseHeaders: null,
        responseBody: null,
        error: null,
      };
      requestMap.set(req, entry);
      calls.push(entry);
    }
  });

  page.on('response', async (res) => {
    const entry = requestMap.get(res.request());
    if (!entry) return;
    entry.status = res.status();
    entry.responseHeaders = res.headers();
    try {
      const buf = await res.body();
      if (buf.length > MAX_BODY_BYTES) {
        entry.responseBody = `[truncated — ${buf.length} bytes]`;
      } else {
        const text = buf.toString('utf8');
        const trimmed = text.trim();
        entry.responseBody = (trimmed.startsWith('{') || trimmed.startsWith('['))
          ? tryParseJson(text)
          : text;
      }
    } catch (e) {
      entry.error = e.message;
    }
  });

  page.on('close', () => {
    save(calls);
    process.exit(0);
  });

  await page.goto('https://groceries.asda.com', { waitUntil: 'domcontentloaded' });

  console.log('\n=== ASDA API Discovery ===');
  console.log('Browser is open. Please work through each of these steps:');
  console.log('  1. Log in to your ASDA account (if not already)');
  console.log('  2. Search for a product (e.g. "milk")');
  console.log('  3. Open a product page');
  console.log('  4. Add an item to your basket');
  console.log('  5. View your basket');
  console.log('  6. Visit Favourites');
  console.log('  7. Visit Past Orders');
  console.log('\nPress Enter in this terminal when done...\n');

  await new Promise(resolve => process.stdin.once('data', resolve));
  await context.close();
  save(calls);
})();

function save(calls) {
  let existing = [];
  if (fs.existsSync(OUTPUT_FILE)) {
    try { existing = JSON.parse(fs.readFileSync(OUTPUT_FILE, 'utf8')); } catch {}
  }
  const merged = [...existing, ...calls];
  fs.writeFileSync(OUTPUT_FILE, JSON.stringify(merged, null, 2));

  const unique = [...new Set(merged.map(c => `${c.method.padEnd(6)} ${summarisePath(c.url)}`))].sort();
  console.log(`\nCaptured ${calls.length} new API calls, ${existing.length} existing — ${merged.length} total (${unique.length} unique endpoints)`);
  console.log('\nEndpoints discovered:');
  unique.forEach(u => console.log('  ' + u));
  console.log(`\nFull detail saved to: ${OUTPUT_FILE}`);
}
