/**
 * extract_edge_cookies.js
 * Reads cookies directly from Edge's on-disk SQLite database.
 * Uses Windows DPAPI (via PowerShell) to decrypt the encryption key,
 * then AES-256-GCM to decrypt each cookie value.
 *
 * Usage: node extract_edge_cookies.js
 * Outputs: data/asda_session.json
 * Note: Edge must be closed (or at least not holding the Cookies DB exclusively).
 */

const { execSync } = require('child_process');
const Database = require('better-sqlite3');
const crypto   = require('crypto');
const fs       = require('fs');
const path     = require('path');

const EDGE_DIR    = process.env.LOCALAPPDATA + '\\Microsoft\\Edge\\User Data';
const COOKIES_SRC = path.join(EDGE_DIR, 'Default', 'Network', 'Cookies');
const LOCAL_STATE = path.join(EDGE_DIR, 'Local State');
const COOKIES_TMP = path.join(require('os').tmpdir(), 'edge_cookies_tmp.db');
const SESSION_OUT = path.join(__dirname, 'data', 'asda_session.json');

const TARGET_DOMAINS = ['.asda.com', 'asda.com', '.api2.asda.com', 'api2.asda.com'];

function getEncryptionKey() {
  const state   = JSON.parse(fs.readFileSync(LOCAL_STATE, 'utf8'));
  const b64Key  = state.os_crypt?.encrypted_key;
  if (!b64Key) throw new Error('No encrypted_key in Edge Local State');

  // Strip the DPAPI prefix ("DPAPI" as 5 bytes) and base64-decode
  const encKeyBuf = Buffer.from(b64Key, 'base64').slice(5);

  // Use PowerShell to call Windows DPAPI CryptUnprotectData
  const psCmd = `
    Add-Type -AssemblyName System.Security
    $enc = [Convert]::FromBase64String('${encKeyBuf.toString('base64')}')
    $dec = [System.Security.Cryptography.ProtectedData]::Unprotect($enc, $null, 'CurrentUser')
    [Convert]::ToBase64String($dec)
  `.trim().replace(/\n/g, '; ');

  const result = execSync(`powershell -NoProfile -Command "${psCmd}"`, { encoding: 'utf8' }).trim();
  return Buffer.from(result, 'base64');
}

function decryptCookieValue(key, encryptedValue) {
  // Chromium v10+ format: "v10" prefix + 12-byte nonce + ciphertext + 16-byte tag
  const buf = Buffer.from(encryptedValue);
  if (buf.length < 3) return '';
  const prefix = buf.slice(0, 3).toString();
  if (prefix !== 'v10' && prefix !== 'v11') return buf.toString(); // old unencrypted format

  const nonce      = buf.slice(3, 15);
  const ciphertext = buf.slice(15, buf.length - 16);
  const tag        = buf.slice(buf.length - 16);

  try {
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce);
    decipher.setAuthTag(tag);
    return decipher.update(ciphertext, undefined, 'utf8') + decipher.final('utf8');
  } catch {
    return '';
  }
}

function run() {
  console.log('Reading Edge encryption key via DPAPI…');
  const key = getEncryptionKey();
  console.log('Key decrypted successfully');

  // Copy DB so we don't hold a lock on Edge's live file
  fs.copyFileSync(COOKIES_SRC, COOKIES_TMP);
  const db = new Database(COOKIES_TMP, { readonly: true });

  const rows = db.prepare(
    `SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly
     FROM cookies WHERE host_key IN (${TARGET_DOMAINS.map(() => '?').join(',')})
     OR host_key LIKE '%.asda.com'`
  ).all(...TARGET_DOMAINS);

  db.close();
  fs.unlinkSync(COOKIES_TMP);

  console.log(`Found ${rows.length} ASDA cookies`);

  const cookies = rows
    .map(r => {
      const value = decryptCookieValue(key, r.encrypted_value);
      if (!value) return null;
      return {
        name:     r.name,
        value,
        domain:   r.host_key,
        path:     r.path,
        secure:   !!r.is_secure,
        httpOnly: !!r.is_httponly,
        expires:  r.expires_utc
          ? new Date((r.expires_utc / 1000000) - 11644473600000).toISOString()
          : undefined,
        sameSite: 'None',
      };
    })
    .filter(Boolean);

  const session = {
    cookies,
    sessionId: null,
    captured:  new Date().toISOString(),
  };

  fs.writeFileSync(SESSION_OUT, JSON.stringify(session, null, 2));
  console.log(`\n✅ Saved ${cookies.length} cookies to data/asda_session.json`);
  console.log('   Now run: node asda_enrich_regulars.js');
}

run();
