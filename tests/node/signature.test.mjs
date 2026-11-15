import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { one, CONFIG } from './harness.mjs';

const SECRET = 'test-secret';
const NOW = Date.UTC(2026, 10, 15, 12, 0, 0);
const TS = String(Math.floor(NOW / 1000));
const hmac = (s) => createHmac('sha256', SECRET).update(s, 'utf8').digest('hex');

async function readRaw(body, headers) {
  return one('intake/read-raw-body.js', { input: { headers, body: {} }, binary: body === null ? undefined : Buffer.from(body, 'utf8') });
}

// Read raw body -> (Crypto node, simulated with node:crypto) -> Verify signature
async function verify({ body = '{"email":"a@b.io"}', ts = TS, sig, mode = 'hmac', now = NOW, tolerance, webhook = true, secret = SECRET } = {}) {
  const headers = {};
  if (ts !== null) headers['x-lead-timestamp'] = ts;
  const read = await readRaw(body, headers);
  const expected = createHmac('sha256', SECRET).update(read.signed_payload, 'utf8').digest('hex');
  if (sig === undefined) sig = 'sha256=' + createHmac('sha256', secret).update(`${ts}.${body}`, 'utf8').digest('hex');
  if (sig !== null) read.signature = sig;
  const cfg = { ...CONFIG, webhook_auth: mode, ...(tolerance ? { signature_tolerance_s: tolerance } : {}) };
  const nodes = { 'Config': cfg, 'Read raw body': read, 'Sign request (HMAC)': { ...read, expected_signature: expected } };
  if (webhook) nodes['Lead webhook'] = { headers };
  return one('intake/verify-signature.js', { nodes, now });
}

describe('read raw body', () => {
  test('builds "<timestamp>.<raw body>" from the exact bytes', async () => {
    const r = await readRaw('{"a": 1,  "b":"é"}', { 'x-lead-timestamp': '1700000000', 'x-signature': 'sha256=ab' });
    assert.equal(r.signed_payload, '1700000000.{"a": 1,  "b":"é"}');
    assert.equal(r.timestamp, '1700000000');
    assert.equal(r.signature, 'sha256=ab');
  });
  test('missing headers give empty strings', async () => {
    const r = await readRaw('{}', {});
    assert.equal(r.timestamp, '');
    assert.equal(r.signature, '');
    assert.equal(r.signed_payload, '.{}');
  });
  test('no binary (empty body) still works', async () => {
    const r = await readRaw(null, { 'x-lead-timestamp': '1' });
    assert.equal(r.signed_payload, '1.');
    assert.equal(r.body_bytes, 0);
  });
  test('header values are trimmed', async () => {
    const r = await readRaw('{}', { 'x-lead-timestamp': ' 123 ', 'x-signature': ' sha256=ff ' });
    assert.equal(r.timestamp, '123');
    assert.equal(r.signature, 'sha256=ff');
  });
});

describe('verify signature', () => {
  test('a correct signature passes', async () => {
    assert.equal((await verify()).auth_error, '');
  });
  test('upper-case hex is accepted', async () => {
    const body = '{"x":1}';
    const sig = 'sha256=' + hmac(`${TS}.${body}`).toUpperCase();
    assert.equal((await verify({ body, sig })).auth_error, '');
  });
  test('missing signature', async () => {
    assert.equal((await verify({ sig: null })).auth_error, 'missing X-Signature header');
  });
  test('malformed signature', async () => {
    assert.equal((await verify({ sig: 'md5=abc' })).auth_error, 'malformed X-Signature header');
    assert.equal((await verify({ sig: 'sha256=xyz' })).auth_error, 'malformed X-Signature header');
  });
  test('wrong secret', async () => {
    assert.equal((await verify({ secret: 'other' })).auth_error, 'signature mismatch');
  });
  test('body changed after signing', async () => {
    const sig = 'sha256=' + hmac(`${TS}.{"email":"a@b.io"}`);
    assert.equal((await verify({ body: '{"email":"evil@b.io"}', sig })).auth_error, 'signature mismatch');
  });
  test('timestamp is part of the signature', async () => {
    const sig = 'sha256=' + hmac(`${Number(TS) - 1}.{"email":"a@b.io"}`);
    assert.equal((await verify({ sig })).auth_error, 'signature mismatch');
  });
  test('missing or non-numeric timestamp', async () => {
    assert.equal((await verify({ ts: null, sig: 'sha256=' + 'a'.repeat(64) })).auth_error, 'missing or invalid X-Lead-Timestamp header');
    assert.equal((await verify({ ts: 'yesterday' })).auth_error, 'missing or invalid X-Lead-Timestamp header');
  });
  test('timestamp older than 5 minutes is rejected', async () => {
    const ts = String(Number(TS) - 301);
    assert.equal((await verify({ ts })).auth_error, 'timestamp outside the replay window');
  });
  test('timestamp from the future is rejected too', async () => {
    assert.equal((await verify({ ts: String(Number(TS) + 400) })).auth_error, 'timestamp outside the replay window');
  });
  test('inside the window passes (299 s)', async () => {
    assert.equal((await verify({ ts: String(Number(TS) - 299) })).auth_error, '');
  });
  test('the tolerance comes from Config', async () => {
    assert.equal((await verify({ ts: String(Number(TS) - 100), tolerance: 60 })).auth_error, 'timestamp outside the replay window');
  });
  test('token and none modes leave it to the Webhook node', async () => {
    assert.deepEqual(await verify({ mode: 'token', sig: null }), { auth: 'token', auth_error: '' });
    assert.deepEqual(await verify({ mode: 'none', sig: null }), { auth: 'none', auth_error: '' });
  });
  test('internal trigger is not checked', async () => {
    assert.deepEqual(await verify({ webhook: false, sig: null }), { auth: 'internal', auth_error: '' });
  });
});
