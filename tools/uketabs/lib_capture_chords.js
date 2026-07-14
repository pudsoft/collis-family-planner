/**
 * Captures chord fingering diagrams and strumming pattern from a UG print page.
 * Requires a logged-in browser context (see login_ug.js).
 *
 * XPaths confirmed on: /tab/print?...&is_ukulele=1&...
 *   Chord tabs:    /html/body/div/div/div[1]/div[1]/div/div[1]/section
 *   Strum pattern: /html/body/div/div/div[1]/div[1]/div/div[1]/div[3]/section
 */

const CHORD_XPATH = '/html/body/div/div/div[1]/div[1]/div/div[1]/section';
const STRUM_XPATH = '/html/body/div/div/div[1]/div[1]/div/div[1]/div[3]/section';

async function screenshotXPath(page, xpath) {
  try {
    const el = await page.$(`xpath=${xpath}`);
    if (!el) return null;
    const box = await el.boundingBox();
    if (!box || box.width < 10 || box.height < 10) return null;
    return el.screenshot({ type: 'png' });
  } catch {
    return null;
  }
}

/**
 * Open a new page in `context`, navigate to the UG print URL for `tabId`,
 * and capture chord + strum sections.
 * Returns { chordBuf: Buffer|null, strumBuf: Buffer|null }
 */
async function captureFromPrintPage(context, tabId) {
  const printUrl = `https://tabs.ultimate-guitar.com/tab/print?app_utm_campaign=Print&flats=0&font_size=0&id=${tabId}&is_ukulele=1&simplified=0&transpose=0`;

  const page = await context.newPage();
  await page.setViewportSize({ width: 1280, height: 900 });

  try {
    await page.goto(printUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(2500);

    const onPrint = page.url().includes('/print');
    if (!onPrint) {
      await page.close();
      return { chordBuf: null, strumBuf: null, redirected: true };
    }

    const chordBuf = await screenshotXPath(page, CHORD_XPATH);
    const strumBuf = await screenshotXPath(page, STRUM_XPATH);
    return { chordBuf, strumBuf, redirected: false };
  } finally {
    await page.close();
  }
}

module.exports = { captureFromPrintPage, CHORD_XPATH, STRUM_XPATH };
