const https = require('https');
const fs    = require('fs');
const path  = require('path');

const REGULARS_FILE = path.join(__dirname, 'data', 'asda_regulars.json');
const regulars = JSON.parse(fs.readFileSync(REGULARS_FILE, 'utf8'));
const missing  = regulars.filter(r => !r.category).map(r => r.product_id);
console.log(`${regulars.length} items total, ${missing.length} missing category`);

function post(body) {
  return new Promise((res, rej) => {
    const payload = JSON.stringify(body);
    const req = https.request({
      hostname: '8i6wskccnv-dsn.algolia.net',
      path: '/1/indexes/*/queries',
      method: 'POST',
      headers: {
        'x-algolia-application-id': '8I6WSKCCNV',
        'x-algolia-api-key': '03e4272048dd17f771da37b57ff8a75e',
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(payload),
      }
    }, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch { rej(d.slice(0,200)); } }); });
    req.on('error', rej);
    req.write(payload);
    req.end();
  });
}

(async () => {
  const byId = {};
  const BATCH = 400;
  for (let i = 0; i < missing.length; i += BATCH) {
    const batch  = missing.slice(i, i + BATCH);
    const filter = batch.map(id => `CIN:${id}`).join(' OR ');
    const result = await post({
      requests: [{
        indexName: 'ASDA_PRODUCTS',
        query: '',
        params: [
          `hitsPerPage=${batch.length}`,
          `attributesToRetrieve=["CIN","PRIMARY_TAXONOMY"]`,
          `filters=(${filter}) AND (STATUS:A OR STATUS:I)`,
        ].join('&'),
      }]
    });
    for (const hit of (result.results?.[0]?.hits || [])) {
      if (!hit.CIN) continue;
      const aisle = hit.PRIMARY_TAXONOMY?.AISLE_NAME;
      if (aisle) byId[String(hit.CIN)] = typeof aisle === 'object' ? aisle.value : aisle;
    }
    process.stdout.write(`  Batch ${Math.floor(i / BATCH) + 1}/${Math.ceil(missing.length / BATCH)} done\r`);
  }

  let filled = 0;
  for (const r of regulars) {
    if (!r.category && byId[r.product_id]) { r.category = byId[r.product_id]; filled++; }
  }
  fs.writeFileSync(REGULARS_FILE, JSON.stringify(regulars, null, 2));
  console.log(`\nDone — ${filled} categories filled, ${missing.length - filled} still unknown (discontinued/unlisted)`);
})();
