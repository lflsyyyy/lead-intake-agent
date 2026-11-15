import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { one, CONFIG } from './harness.mjs';

const NOW = Date.UTC(2026, 10, 15, 12, 0, 0);
const ago = (min) => new Date(NOW - min * 60000).toISOString();
const lead = (o = {}) => ({ lead_id: 'lia_new', channel: 'webhook', email: 'a@b.io', fingerprint: 'fp1', errors: [],
                            spam_reason: '', auth_error: '', client_ip: '', replay_of: '', injection_flags: [], ...o });
const row = (o = {}) => ({ id: Math.floor(Math.random() * 1e6), lead_id: 'lia_old', email: 'x@y.io', fingerprint: 'other',
                           status: 'processed', client_ip: '', received_at: ago(1), ...o });
const route = (l, rows = [], cfg = {}) => one('intake/route-intake.js', {
  input: rows.length ? rows : [{}], now: NOW, nodes: { 'Config': { ...CONFIG, ...cfg }, 'Normalize & validate': lead(l) } });

describe('route: status and http code', () => {
  test('a new valid lead is accepted with 202', async () => {
    const r = await route();
    assert.equal(r.status, 'accepted');
    assert.equal(r.http_code, 202);
    assert.deepEqual(r.response, { status: 'accepted', lead_id: 'lia_new' });
    assert.equal(r.action, 'processing');
    assert.equal(r.log, true);
  });
  test('validation errors give 422 with the list', async () => {
    const r = await route({ errors: ['email is missing or invalid'] });
    assert.equal(r.http_code, 422);
    assert.deepEqual(r.response, { status: 'rejected', errors: ['email is missing or invalid'] });
    assert.equal(r.status, 'invalid');
  });
  test('spam gets the same 202 as a real lead', async () => {
    const r = await route({ spam_reason: 'honeypot field was filled' });
    assert.equal(r.status, 'spam');
    assert.equal(r.http_code, 202);
    assert.equal(r.response.status, 'accepted');
    assert.equal(r.fit, 'spam');
    assert.equal(r.note, 'rule: honeypot field was filled');
  });
  test('bad signature gives 401 and is not logged', async () => {
    const r = await route({ auth_error: 'signature mismatch' });
    assert.equal(r.http_code, 401);
    assert.equal(r.status, 'unauthorized');
    assert.equal(r.response.error, 'signature mismatch');
    assert.equal(r.log, false);
  });
  test('401 wins over validation errors', async () => {
    assert.equal((await route({ auth_error: 'x', errors: ['message is required'] })).http_code, 401);
  });
  test('a replay notes the dead letter', async () => {
    assert.equal((await route({ channel: 'replay', replay_of: '7' })).note, 'replay of dead letter #7');
  });
  test('an empty "Recent submissions" result (alwaysOutputData item) is ignored', async () => {
    assert.equal((await route({}, [{}])).status, 'accepted');
  });
});

describe('route: duplicates', () => {
  test('same fingerprint within the window is a duplicate (200)', async () => {
    const r = await route({}, [row({ fingerprint: 'fp1', lead_id: 'lia_first', received_at: ago(3) })]);
    assert.equal(r.status, 'duplicate');
    assert.equal(r.http_code, 200);
    assert.deepEqual(r.response, { status: 'duplicate', lead_id: 'lia_first' });
    assert.equal(r.note, 'duplicate of lia_first');
  });
  test('outside the dedupe window it is new', async () => {
    assert.equal((await route({}, [row({ fingerprint: 'fp1', received_at: ago(11) })])).status, 'accepted');
  });
  test('the window comes from Config', async () => {
    assert.equal((await route({}, [row({ fingerprint: 'fp1', received_at: ago(11) })], { dedupe_window_min: 30, rate_limit_window_min: 30 })).status, 'duplicate');
  });
  test('an earlier invalid submission does not count as the original', async () => {
    assert.equal((await route({}, [row({ fingerprint: 'fp1', status: 'invalid' })])).status, 'accepted');
  });
  test('replays skip the duplicate check', async () => {
    assert.equal((await route({ channel: 'replay' }, [row({ fingerprint: 'fp1' })])).status, 'accepted');
  });
  test('createdAt is used when received_at is missing', async () => {
    const r = row({ fingerprint: 'fp1' });
    delete r.received_at;
    r.createdAt = ago(2);
    assert.equal((await route({}, [r])).status, 'duplicate');
  });
});

describe('route: rate limit', () => {
  const burst = (n, o) => Array.from({ length: n }, (_, i) => row({ fingerprint: 'f' + i, ...o }));
  test('5 earlier submissions from the same email -> 429', async () => {
    const r = await route({}, burst(5, { email: 'a@b.io', received_at: ago(2) }));
    assert.equal(r.http_code, 429);
    assert.equal(r.status, 'rate_limited');
    assert.equal(r.log, false);
  });
  test('4 earlier submissions are still fine', async () => {
    assert.equal((await route({}, burst(4, { email: 'a@b.io' }))).status, 'accepted');
  });
  test('same client ip with different emails -> 429', async () => {
    const r = await route({ client_ip: '203.0.113.9' }, burst(5, { client_ip: '203.0.113.9' }));
    assert.equal(r.http_code, 429);
  });
  test('an empty client ip never matches other empty ips', async () => {
    assert.equal((await route({ client_ip: '' }, burst(8, { client_ip: '' }))).status, 'accepted');
  });
  test('old submissions fall out of the window', async () => {
    assert.equal((await route({}, burst(6, { email: 'a@b.io', received_at: ago(11) }))).status, 'accepted');
  });
  test('retry_after_s is when the oldest hit leaves the window', async () => {
    const rows = [row({ email: 'a@b.io', received_at: ago(7) }), ...burst(4, { email: 'a@b.io', received_at: ago(1) })];
    const r = await route({}, rows);
    assert.equal(r.response.retry_after_s, 180);
  });
  test('limit and window come from Config', async () => {
    assert.equal((await route({}, burst(2, { email: 'a@b.io' }), { rate_limit_max: 2 })).http_code, 429);
  });
  test('429 wins over a duplicate and over validation errors', async () => {
    const rows = burst(5, { email: 'a@b.io', fingerprint: 'fp1' });
    assert.equal((await route({ errors: ['message is required'] }, rows)).http_code, 429);
  });
  test('replays are not rate limited', async () => {
    assert.equal((await route({ channel: 'replay' }, burst(9, { email: 'a@b.io' }))).status, 'accepted');
  });
});
