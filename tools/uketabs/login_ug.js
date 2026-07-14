/**
 * Log in to Ultimate Guitar and capture chord/strum images for all songs.
 * Everything happens in one browser session to avoid Cloudflare.
 *
 * Usage: node tools/uketabs/login_ug.js
 *
 * A UG print page opens. If you're not logged in, UG will ask you to log in.
 * Once the chord diagrams appear on screen, capture starts automatically.
 */
const { chromium } = require('playwright');
const fs   = require('fs');
const path = require('path');

const DATA_ROOT   = path.join(__dirname, '..', '..', 'data', 'ukulele_songs');
const SONGS_DIR   = path.join(DATA_ROOT, 'songs');
const SONGS_INDEX = path.join(DATA_ROOT, 'songs.json');
const CHORD_XPATH = '/html/body/div/div/div[1]/div[1]/div/div[1]/section';
const STRUM_XPATH = '/html/body/div/div/div[1]/div[1]/div/div[1]/div[3]/section';

async function screenshotXPath(page, xpath) {
  try {
    const el = await page.$(`xpath=${xpath}`);
    if (!el) return null;
    const box = await el.boundingBox();
    if (!box || box.width < 10 || box.height < 10) return null;
    return el.screenshot({ type: 'png' });
  } catch { return null; }
}

function printUrl(tabId) {
  return `https://tabs.ultimate-guitar.com/tab/print?app_utm_campaign=Print&flats=0&font_size=0&id=${tabId}&is_ukulele=1&simplified=0&transpose=0`;
}

(async () => {
  const index = JSON.parse(fs.readFileSync(SONGS_INDEX, 'utf8'));
  const todo  = index.filter(e => e.ugTabId);

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();

  // ── Step 1: wait for user to be logged in ────────────────────
  const firstId   = todo[0]?.ugTabId || '4835888';
  const loginPage = await context.newPage();
  await loginPage.setViewportSize({ width: 1280, height: 900 });

  console.log('\nOpening UG print page — log in if prompted.');
  console.log('Waiting for chord diagrams to appear (up to 5 minutes)...\n');

  await loginPage.goto(printUrl(firstId), { waitUntil: 'domcontentloaded', timeout: 60000 });

  // Wait until the chord section XPath becomes visible (user is logged in & page rendered)
  try {
    await loginPage.waitForSelector(`xpath=${CHORD_XPATH}`, { timeout: 300000 });
  } catch {
    console.log('Timed out waiting for chord section. Exiting.');
    await browser.close();
    return;
  }

  console.log('✓ Chord section detected — starting capture.\n');

  // ── Step 2: capture all songs ────────────────────────────────
  console.log(`Capturing ${todo.length} songs...\n`);

  for (let i = 0; i < todo.length; i++) {
    const entry    = todo[i];
    const songPath = path.join(SONGS_DIR, entry.file);
    if (!fs.existsSync(songPath)) continue;

    const song = JSON.parse(fs.readFileSync(songPath, 'utf8'));
    const isFirst = (entry.ugTabId === firstId);
    console.log(`[${i + 1}/${todo.length}] ${entry.artist} — ${entry.title}`);

    // Remove stale images
    for (const field of ['chordImageFile', 'strumImageFile']) {
      if (song[field]) {
        const p = path.join(SONGS_DIR, song[field]);
        if (fs.existsSync(p)) fs.unlinkSync(p);
        delete song[field];
      }
    }

    const page = isFirst ? loginPage : await context.newPage();
    if (!isFirst) await page.setViewportSize({ width: 1280, height: 900 });

    try {
      if (!isFirst) {
        await page.goto(printUrl(entry.ugTabId), { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForSelector(`xpath=${CHORD_XPATH}`, { timeout: 15000 });
      }

      const chordBuf = await screenshotXPath(page, CHORD_XPATH);
      const strumBuf = await screenshotXPath(page, STRUM_XPATH);

      if (chordBuf) {
        const f = `${entry.id}-chords.png`;
        fs.writeFileSync(path.join(SONGS_DIR, f), chordBuf);
        song.chordImageFile = f;
        console.log(`  ✓ chords`);
      } else {
        console.log(`  –  no chord section`);
      }

      if (strumBuf) {
        const f = `${entry.id}-strum.png`;
        fs.writeFileSync(path.join(SONGS_DIR, f), strumBuf);
        song.strumImageFile = f;
        console.log(`  ✓ strum`);
      } else {
        console.log(`  –  no strum section`);
      }
    } catch (e) {
      console.log(`  ✗ ${e.message}`);
    }

    fs.writeFileSync(songPath, JSON.stringify(song, null, 2));
    if (!isFirst) await page.close();
  }

  await context.storageState({ path: path.join(__dirname, 'ug_session.json') });
  await browser.close();
  console.log('\nDone. Refresh the songbook to see chord diagrams.');
})();
