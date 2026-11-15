// Webhook authentication. Modes (Config.webhook_auth):
//   hmac  - X-Signature: sha256=<hex HMAC-SHA256(secret, "<X-Lead-Timestamp>.<raw body>")>, timestamp within
//           signature_tolerance_s (default 300 s) of now. A replay inside the window is caught by the duplicate check.
//   token - X-Lead-Token is checked by the Webhook node itself (Header Auth credential); nothing to do here.
//   none  - local development only.
// The internal trigger (LIA · Form, replays) is only reachable from inside n8n and is not signed.
const cfg = $('Config').first().json;
if (!$('Lead webhook').isExecuted) return [{ json: { auth: 'internal', auth_error: '' } }];
const mode = String(cfg.webhook_auth || 'hmac').toLowerCase();
if (mode !== 'hmac') return [{ json: { auth: mode, auth_error: '' } }];

const req = $('Read raw body').first().json;
const expected = String($('Sign request (HMAC)').first().json.expected_signature || '').toLowerCase();
const given = String(req.signature || '').replace(/^sha256=/i, '').trim().toLowerCase();
const tolerance = Number(cfg.signature_tolerance_s) || 300;

// constant-time comparison: no early exit on the first differing character
const safeEqual = (a, b) => {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
};

let auth_error = '';
if (!req.signature) auth_error = 'missing X-Signature header';
else if (!/^sha256=[0-9a-f]{64}$/i.test(String(req.signature))) auth_error = 'malformed X-Signature header';
else if (!/^\d{9,11}$/.test(String(req.timestamp || ''))) auth_error = 'missing or invalid X-Lead-Timestamp header';
else if (Math.abs(Date.now() / 1000 - Number(req.timestamp)) > tolerance) auth_error = 'timestamp outside the replay window';
else if (!safeEqual(given, expected)) auth_error = 'signature mismatch';
return [{ json: { auth: 'hmac', auth_error } }];
