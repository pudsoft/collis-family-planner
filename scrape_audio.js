const { chromium } = require('playwright');
const https = require('https');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const TARGET_URL = process.argv[2];
if (!TARGET_URL) {
  console.error('Usage: node scrape_audio.js <url>');
  process.exit(1);
}
const AUDIO_EXTS = ['.mp3', '.m4a', '.ogg', '.wav', '.aac', '.flac', '.opus', '.weba'];

function isAudioUrl(url) {
  try {
    const parsed = new URL(url);
    const ext = path.extname(parsed.pathname).toLowerCase();
    return AUDIO_EXTS.includes(ext) || parsed.pathname.includes('/audio/') || url.includes('audio');
  } catch { return false; }
}

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith('https') ? https : http;
    const file = fs.createWriteStream(dest);
    proto.get(url, (res) => {
      if (res.statusCode === 301 || res.statusCode === 302) {
        file.close();
        return download(res.headers.location, dest).then(resolve).catch(reject);
      }
      res.pipe(file);
      file.on('finish', () => file.close(resolve));
    }).on('error', (err) => {
      fs.unlink(dest, () => {});
      reject(err);
    });
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const audioUrls = new Set();

  // Intercept all network requests
  page.on('request', (req) => {
    const url = req.url();
    const type = req.resourceType();
    if (type === 'media' || isAudioUrl(url)) {
      audioUrls.add(url);
    }
  });

  page.on('response', async (res) => {
    const url = res.url();
    const ct = res.headers()['content-type'] || '';
    if (ct.includes('audio') || ct.includes('mpeg') || ct.includes('ogg') || ct.includes('mp4')) {
      audioUrls.add(url);
    }
  });

  console.log(`Loading: ${TARGET_URL}`);
  await page.goto(TARGET_URL, { waitUntil: 'networkidle', timeout: 30000 });

  // Try clicking a play button if present
  try {
    const playBtn = await page.$('button[aria-label*="play" i], .play-button, [class*="play"], audio');
    if (playBtn) {
      console.log('Found play button, clicking...');
      await playBtn.click();
      await page.waitForTimeout(3000);
    }
  } catch {}

  // Also check audio element src attributes directly in the DOM
  const domAudio = await page.evaluate(() => {
    const sources = [];
    document.querySelectorAll('audio, source, [src]').forEach(el => {
      if (el.src) sources.push(el.src);
      if (el.currentSrc) sources.push(el.currentSrc);
    });
    return sources;
  });
  domAudio.forEach(u => { if (u && !u.startsWith('data:')) audioUrls.add(u); });

  await browser.close();

  if (audioUrls.size === 0) {
    console.log('No audio URLs detected. The player may use DRM or a streaming protocol.');
    return;
  }

  console.log(`\nFound ${audioUrls.size} audio URL(s):`);
  const urls = [...audioUrls];
  urls.forEach((u, i) => console.log(`  [${i + 1}] ${u}`));

  // Download the first one
  const audioUrl = urls[0];
  const ext = path.extname(new URL(audioUrl).pathname) || '.mp3';
  const outFile = path.join(process.cwd(), `bedtime_story${ext}`);
  console.log(`\nDownloading to: ${outFile}`);
  await download(audioUrl, outFile);
  console.log('Done!');
})();
