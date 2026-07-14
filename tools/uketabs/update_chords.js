/**
 * (Re)captures chord diagram + strumming pattern images for all songs
 * from the UG print page using the correct XPath selectors.
 *
 * Requires a saved UG session — run node tools/uketabs/login_ug.js first if needed.
 * Usage: node tools/uketabs/update_chords.js
 */
const { chromium } = require('playwright');
const { captureFromPrintPage } = require('./lib_capture_chords');
const fs   = require('fs');
const path = require('path');

const DATA_ROOT    = path.join(__dirname, '..', '..', 'data', 'ukulele_songs');
const SONGS_DIR    = path.join(DATA_ROOT, 'songs');
const SONGS_INDEX  = path.join(DATA_ROOT, 'songs.json');
const SESSION_FILE = path.join(__dirname, 'ug_session.json');

(async () => {
  if (!fs.existsSync(SESSION_FILE)) {
    console.error('No UG session found. Run: node tools/uketabs/login_ug.js first.');
    process.exit(1);
  }

  const index = JSON.parse(fs.readFileSync(SONGS_INDEX, 'utf8'));
  const todo  = index.filter(e => e.ugTabId);

  console.log(`Capturing chord/strum images for ${todo.length} songs...\n`);

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ storageState: SESSION_FILE });

  let sessionExpired = false;

  for (let i = 0; i < todo.length; i++) {
    if (sessionExpired) break;

    const entry    = todo[i];
    const songPath = path.join(SONGS_DIR, entry.file);
    if (!fs.existsSync(songPath)) continue;

    const song = JSON.parse(fs.readFileSync(songPath, 'utf8'));
    console.log(`[${i + 1}/${todo.length}] ${entry.artist} — ${entry.title}`);

    // Remove old PNG files before recapturing
    for (const field of ['chordImageFile', 'strumImageFile']) {
      if (song[field]) {
        const old = path.join(SONGS_DIR, song[field]);
        if (fs.existsSync(old)) fs.unlinkSync(old);
        delete song[field];
      }
    }

    const { chordBuf, strumBuf, redirected } = await captureFromPrintPage(context, entry.ugTabId);

    if (redirected) {
      sessionExpired = true;
      console.log('  ⚠ Session expired — stopping. Run node tools/uketabs/login_ug.js then retry.');
      fs.writeFileSync(songPath, JSON.stringify(song, null, 2));
      break;
    }

    if (chordBuf) {
      const f = `${entry.id}-chords.png`;
      fs.writeFileSync(path.join(SONGS_DIR, f), chordBuf);
      song.chordImageFile = f;
      console.log(`  ✓ chords → ${f}`);
    } else {
      console.log(`  –  no chord section`);
    }

    if (strumBuf) {
      const f = `${entry.id}-strum.png`;
      fs.writeFileSync(path.join(SONGS_DIR, f), strumBuf);
      song.strumImageFile = f;
      console.log(`  ✓ strum  → ${f}`);
    } else {
      console.log(`  –  no strum section`);
    }

    fs.writeFileSync(songPath, JSON.stringify(song, null, 2));
  }

  await browser.close();
  console.log('\nDone.');
})();
