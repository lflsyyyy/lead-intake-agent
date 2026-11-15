// Seal a value for n8n's Crypto node (action "decrypt", mode "symmetric", cipher aes-256-gcm).
// stdin: {"passphrase": "...", "value": "..."}   stdout: base64 ciphertext
// Layout matches n8n 2.x CryptoV2: [0x01][salt:16][iv:12][tag:16][ciphertext], key = scrypt(N=65536, r=8, p=1).
// Used by tools/build.py so that private values (the owner's Telegram chat id) never sit in a workflow in plain text.
const { randomBytes, scryptSync, createCipheriv } = require('node:crypto');

function seal(passphrase, value) {
  const salt = randomBytes(16);
  const iv = randomBytes(12);
  const key = scryptSync(passphrase, salt, 32, { N: 65536, r: 8, p: 1, maxmem: 128 * 1024 * 1024 });
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([cipher.update(String(value), 'utf8'), cipher.final()]);
  return Buffer.concat([Buffer.from([1]), salt, iv, cipher.getAuthTag(), ct]).toString('base64');
}

if (typeof module !== 'undefined') module.exports = { seal };

if (require.main === module || !module.parent) {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (d) => { input += d; });
  process.stdin.on('end', () => {
    const { passphrase, value } = JSON.parse(input);
    if (!passphrase) { process.stderr.write('passphrase missing\n'); process.exit(2); }
    process.stdout.write(seal(passphrase, value));
  });
}
