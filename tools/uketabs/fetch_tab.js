/**
 * Fetch an Ultimate Guitar tab and save it into the /ukulele songbook.
 * Usage: node tools/uketabs/fetch_tab.js <ug-url> [--pdf]
 */
const { chromium } = require('playwright');
const { captureFromPrintPage } = require('./lib_capture_chords');

const fs   = require('fs');
const path = require('path');

const DATA_ROOT    = path.join(__dirname, '..', '..', 'data', 'ukulele_songs');
const SESSION_FILE = path.join(__dirname, 'ug_session.json');
const SONGS_DIR    = path.join(DATA_ROOT, 'songs');
const SONGS_INDEX  = path.join(DATA_ROOT, 'songs.json');

const url        = process.argv.find(a => a.startsWith('http'));
const generatePdf = process.argv.includes('--pdf');

if (!url) {
  console.error('Usage: node tools/uketabs/fetch_tab.js <ug-url> [--pdf]');
  process.exit(1);
}

// Duplicate check — bail early if this tab ID is already in the index
const incomingTabId = new URL(url).searchParams.get('id');
if (incomingTabId && fs.existsSync(SONGS_INDEX)) {
  const existing = JSON.parse(fs.readFileSync(SONGS_INDEX, 'utf8'));
  const dupe = existing.find(s => s.ugTabId === incomingTabId);
  if (dupe) {
    console.log(`Already in songbook: ${dupe.artist} — ${dupe.title} (tab ID ${incomingTabId})`);
    process.exit(0);
  }
}

function slugify(str) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

(async () => {
  const browser = await chromium.launch({ headless: false });
  const page    = await browser.newPage();
  await page.setViewportSize({ width: 1440, height: 1100 });

  console.log('Loading page...');
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });

  // Dismiss cookie consent if present
  try {
    await page.locator('button:has-text("Accept"), button:has-text("Agree"), button:has-text("OK")')
      .first().click({ timeout: 4000 });
    await page.waitForTimeout(1000);
  } catch {}

  // Wait for tab content — <pre> is the stable selector across UG deployments
  console.log('Waiting for tab content...');
  await page.waitForSelector('pre', { timeout: 30000 });

  const data = await page.evaluate(() => {
    const pre = document.querySelector('pre');
    const raw = pre ? pre.innerText : '';
    const lines = raw.split('\n');

    const getLine = prefix => {
      const l = lines.find(l => new RegExp('^' + prefix + ':', 'i').test(l));
      return l ? l.replace(new RegExp('^' + prefix + ':\\s*', 'i'), '').trim() : null;
    };

    // 1) Prefer explicit header lines inside the tab text (e.g. "Artist: Eagles")
    const inlineArtist = getLine('Artist');
    const inlineSong   = getLine('Song');
    if (inlineArtist && inlineSong) {
      return { artist: inlineArtist, title: inlineSong, content: raw };
    }

    // 2) Parse UG page title: "SONG NAME CHORDS by Artist @ Ultimate-Guitar.Com"
    // Use last " by " so version strings like "VER 2" or "(VER 2)" don't trip us up.
    const pageTitle = document.title.replace(/@.*$/, '').trim();
    const byIdx = pageTitle.search(/\sby\s(?!.*\sby\s)/i);
    if (byIdx > 0) {
      const artist = pageTitle.slice(byIdx).replace(/^\s*by\s*/i, '').trim();
      const rawSong = pageTitle.slice(0, byIdx);
      const title = rawSong
        .replace(/\s+(?:UKULELE|CHORDS?|TABS?|GUITAR|VER\.?\s*\d+|\(\s*VER\.?\s*\d+\s*\))\s*/gi, ' ')
        .replace(/\s+/g, ' ')
        .replace(/\s+(?:CHORDS?|TABS?|VER\.?\s*\d+)$/i, '') // strip any trailing noise
        .trim();
      return { artist, title: title || rawSong.trim(), content: raw };
    }

    // 3) Last resort
    return { artist: 'Unknown', title: pageTitle, content: raw };
  });

  if (!data.content) {
    console.error('No tab content found.');
    await browser.close();
    process.exit(1);
  }

  if (!fs.existsSync(SONGS_DIR)) fs.mkdirSync(SONGS_DIR, { recursive: true });

  const id       = `${slugify(data.artist)}-${slugify(data.title)}`;
  const filename = `${id}.json`;
  const songData = {
    id,
    title:     data.title,
    artist:    data.artist,
    ugUrl:     url,
    content:   data.content,
    fetchedAt: new Date().toISOString(),
  };

  // Capture chord diagrams + strum pattern from the print page using saved UG session
  if (incomingTabId) {
    if (!fs.existsSync(SESSION_FILE)) {
      console.log('Skipping chord capture — no UG session. Run: node tools/uketabs/login_ug.js');
    } else {
      console.log('Capturing chord/strum from print page...');
      const chordContext = await browser.newContext({ storageState: SESSION_FILE });
      try {
        const { chordBuf, strumBuf } = await captureFromPrintPage(chordContext, incomingTabId);
        if (chordBuf) {
          const f = `${id}-chords.png`;
          fs.writeFileSync(path.join(SONGS_DIR, f), chordBuf);
          songData.chordImageFile = f;
          console.log(`Chord image: songs/${f}`);
        }
        if (strumBuf) {
          const f = `${id}-strum.png`;
          fs.writeFileSync(path.join(SONGS_DIR, f), strumBuf);
          songData.strumImageFile = f;
          console.log(`Strum image: songs/${f}`);
        }
        if (!chordBuf && !strumBuf) console.log('No chord/strum found — session may have expired.');
      } catch (e) {
        console.log(`Chord capture failed: ${e.message}`);
      }
      await chordContext.close();
    }
  }

  fs.writeFileSync(path.join(SONGS_DIR, filename), JSON.stringify(songData, null, 2));
  console.log(`Saved: songs/${filename}`);

  // Update songs index
  let index = [];
  try { index = JSON.parse(fs.readFileSync(SONGS_INDEX, 'utf8')); } catch {}
  const entry = { id, title: data.title, artist: data.artist, file: filename, ugTabId: incomingTabId };
  const existing = index.findIndex(s => s.id === id);
  if (existing >= 0) index[existing] = entry; else index.push(entry);
  index.sort((a, b) => a.artist.localeCompare(b.artist) || a.title.localeCompare(b.title));
  fs.writeFileSync(SONGS_INDEX, JSON.stringify(index, null, 2));
  console.log(`Songs index updated (${index.length} song${index.length !== 1 ? 's' : ''})`);

  if (generatePdf) {
    const pdfPath = path.join(__dirname, `${id}.pdf`);
    const escaped = data.content
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    await page.setContent(`<!DOCTYPE html><html><head><meta charset="utf-8">
<title>${data.artist} — ${data.title}</title>
<style>
  @page { size: A4; margin: 15mm 12mm; }
  body { font-family: 'Courier New', monospace; font-size: 12px; color:#000; margin:0; }
  h1 { font-family: sans-serif; font-size: 14px; margin-bottom: 12px; }
  pre { white-space: pre-wrap; line-height: 1.5; }
</style></head><body>
<h1>${data.artist} — ${data.title}</h1>
<pre>${escaped}</pre></body></html>`);
    await page.pdf({ path: pdfPath, format: 'A4', printBackground: false });
    console.log(`PDF saved: ${id}.pdf`);
  }

  await browser.close();
})();
