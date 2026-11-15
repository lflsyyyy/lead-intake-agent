// The signature covers the exact bytes the client sent, so read the raw body (the Webhook node keeps it as
// binary because "Raw Body" is on) instead of re-serialising the parsed JSON.
// Signed string: "<X-Lead-Timestamp>.<raw body>"; the HMAC itself is computed by the next (Crypto) node,
// whose secret lives in the "LIA · Webhook secret" credential.
const item = $input.first();
const h = item.json.headers || {};
const header = name => String(h[name] ?? '').trim();
let raw = '';
if (item.binary && item.binary.data) {
  try { raw = (await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8'); } catch (e) { raw = ''; }
}
const timestamp = header('x-lead-timestamp');
return [{ json: {
  timestamp,
  signature: header('x-signature'),
  signed_payload: `${timestamp}.${raw}`,
  body_bytes: raw.length,
} }];
