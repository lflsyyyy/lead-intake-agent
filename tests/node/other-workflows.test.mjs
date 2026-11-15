import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createDecipheriv, scryptSync } from 'node:crypto';
import { join } from 'node:path';
import { one, runNode, ROOT } from './harness.mjs';

describe('follow-ups: still waiting for a reply?', () => {
  const due = (o = {}) => ({ id: 11, lead_id: 'lia_1', contact_id: '501', email: 'a@b.io', step: 1, expected_status: 'OPEN',
                             createdAt: '2026-11-10T10:00:00.000Z', subject: 's', body: 'b', ...o });
  const check = async (contacts, dues) => (await runNode('follow-ups/still-waiting-for-a-reply.js', {
    input: contacts, nodes: { 'Get due follow-ups': dues } })).map(i => i.json);
  test('unchanged status and no reply -> send', async () => {
    const [r] = await check([{ id: '501', properties: { hs_lead_status: 'OPEN' } }], [due()]);
    assert.equal(r.send, true);
    assert.equal(r.stop_reason, '');
    assert.equal(r.followup_id, '11');
  });
  test('status changed in HubSpot -> stop with the reason', async () => {
    const [r] = await check([{ id: '501', properties: { hs_lead_status: 'CONNECTED' } }], [due()]);
    assert.equal(r.send, false);
    assert.equal(r.stop_reason, 'lead status changed to CONNECTED');
  });
  test('status cleared -> stop', async () => {
    assert.equal((await check([{ id: '501', properties: {} }], [due()]))[0].stop_reason, 'lead status changed to empty');
  });
  test('the lead replied after we queued it -> stop', async () => {
    const [r] = await check([{ id: '501', properties: { hs_lead_status: 'OPEN', hs_sales_email_last_replied: '2026-11-12T09:00:00Z' } }], [due()]);
    assert.equal(r.stop_reason, 'lead replied');
  });
  test('a reply from before the follow-up was queued does not count', async () => {
    const [r] = await check([{ id: '501', properties: { hs_lead_status: 'OPEN', hs_sales_email_last_replied: '2026-11-01T09:00:00Z' } }], [due()]);
    assert.equal(r.send, true);
  });
  test('contact gone from HubSpot -> stop', async () => {
    assert.equal((await check([{}], [due()]))[0].stop_reason, 'contact not found in HubSpot');
  });
  test('several follow-ups keep their order', async () => {
    const out = await check([{ id: '1', properties: { hs_lead_status: 'OPEN' } }, { id: '2', properties: { hs_lead_status: 'NEW' } }],
                            [due({ id: 1 }), due({ id: 2 })]);
    assert.deepEqual(out.map(r => [r.followup_id, r.send]), [['1', true], ['2', false]]);
  });
});

describe('follow-ups: email and alert', () => {
  test('write follow-up email', async () => {
    const out = (await runNode('follow-ups/write-follow-up-email.js', { input: [
      { lead_id: 'l', followup_id: '9', contact_id: '5', step: 2, email: 'a@b.io', subject: 'S', body: 'B' }] })).map(i => i.json);
    assert.deepEqual(out, [{ lead_id: 'l', followup_id: '9', contact_id: '5', kind: 'follow-up #2', to: 'a@b.io', subject: 'S', body: 'B' }]);
  });
  test('one alert for the whole batch', async () => {
    const a = await one('follow-ups/compose-follow-up-alert.js', { nodes: { 'Mark follow-up sent': [
      { step: 1, name: 'Tom', company: 'Becker & Sons', note: 'emailed' }, { step: 2, email: 'x@y.io', note: 'outbox' }] } });
    assert.match(a.text, /^↩️ <b>Follow-ups: 2<\/b>/);
    assert.match(a.text, /#1 → Tom \(Becker &amp; Sons\): sent/);
    assert.match(a.text, /#2 → x@y.io: saved to outbox/);
  });
});

describe('form adapter', () => {
  test('drops form metadata, keeps fields, tags the source', async () => {
    const r = await one('form/to-intake-payload.js', { input: { submittedAt: 'x', formMode: 'production', Name: 'A', Email: 'a@b.io', hp: '' } });
    assert.deepEqual(r, { channel: 'form', payload: { Name: 'A', Email: 'a@b.io', hp: '', source: 'website form' } });
  });
  test('the honeypot field travels to intake', async () => {
    const r = await one('form/to-intake-payload.js', { input: { Name: 'Bot', hp: 'http://spam.example' } });
    assert.equal(r.payload.hp, 'http://spam.example');
  });
});

describe('errors: describe failure', () => {
  const describeFail = (e) => one('errors/describe-failure.js', { nodes: { 'Error trigger': e } });
  test('takes node, message and ids from the execution', async () => {
    const d = await describeFail({ workflow: { id: 'w1', name: 'LIA · Intake' },
      execution: { id: 77, url: 'http://n8n/x', lastNodeExecuted: 'LLM · fallback', error: { message: 'timeout' } } });
    assert.deepEqual(d, { workflow: 'LIA · Intake', workflow_id: 'w1', node: 'LLM · fallback', error: 'timeout',
                          execution_id: '77', execution_url: 'http://n8n/x' });
  });
  test('trigger errors (no execution) still describe something', async () => {
    const d = await describeFail({ workflow: { name: 'W' }, trigger: { error: { message: 'boom', node: { name: 'Lead form' } } } });
    assert.equal(d.node, 'Lead form');
    assert.equal(d.error, 'boom');
  });
  test('long errors are cut to 1000 characters', async () => {
    const d = await describeFail({ execution: { error: { message: 'x'.repeat(5000) } } });
    assert.equal(d.error.length, 1000);
    assert.equal(d.node, 'trigger');
  });
});

describe('errors: extract payload (dead letter)', () => {
  const f = { workflow: 'LIA · Intake', node: 'LLM · fallback', error: 'e', execution_id: '1' };
  const runData = (nodes) => ({ data: { resultData: { runData: Object.fromEntries(Object.entries(nodes).map(([k, v]) =>
    [k, [{ data: { main: [[{ json: v }]] } }]])) } } });
  const extract = (ex) => one('errors/extract-payload.js', { input: ex, nodes: { 'Describe failure': f } });
  test('webhook body plus UTM from the query', async () => {
    const d = await extract(runData({ 'Lead webhook': { body: { email: 'a@b.io' }, query: { utm_source: 'g', other: 'x' } },
                                       'Normalize & validate': { lead_id: 'lia_9' } }));
    assert.deepEqual(JSON.parse(d.payload), { email: 'a@b.io', utm_source: 'g' });
    assert.equal(d.lead_id, 'lia_9');
    assert.equal(d.replayable, true);
  });
  test('a body field wins over the same UTM in the query', async () => {
    const d = await extract(runData({ 'Lead webhook': { body: { utm_source: 'body' }, query: { utm_source: 'query' } } }));
    assert.equal(JSON.parse(d.payload).utm_source, 'body');
  });
  test('internal trigger payload (form or replay)', async () => {
    const d = await extract(runData({ 'Internal trigger': { channel: 'form', payload: { Email: 'a@b.io' } } }));
    assert.deepEqual(JSON.parse(d.payload), { Email: 'a@b.io' });
  });
  test('form fields without form metadata', async () => {
    const d = await extract(runData({ 'Lead form': { submittedAt: 't', formMode: 'p', Email: 'a@b.io' } }));
    assert.deepEqual(JSON.parse(d.payload), { Email: 'a@b.io' });
  });
  test('nothing to replay when the execution could not be read', async () => {
    const d = await extract({});
    assert.equal(d.payload, '');
    assert.equal(d.replayable, false);
    assert.equal(d.lead_id, '');
  });
});

describe('errors: alert and replay', () => {
  test('alert names node, error, workflow and dead letter', async () => {
    const a = await one('errors/compose-alert.js', { nodes: {
      'Extract payload': { node: 'HubSpot · upsert contact', error: '<429> too many', workflow: 'LIA · Intake', execution_id: '5',
                           lead_id: 'lia_1', replayable: true }, 'Save dead letter': { id: 3 } } });
    assert.match(a.text, /^⚠️ LIA failed at <b>HubSpot · upsert contact<\/b>: &lt;429&gt; too many/);
    assert.match(a.text, /Dead letter #3: nothing is lost/);
    assert.match(a.text, /Lead: lia_1/);
  });
  test('alert for a non-replayable failure', async () => {
    const a = await one('errors/compose-alert.js', { nodes: {
      'Extract payload': { node: 'x', error: 'y', workflow: 'W', execution_id: '5', lead_id: '', replayable: false }, 'Save dead letter': { id: 4 } } });
    assert.match(a.text, /Dead letter #4\.$/);
    assert.ok(!a.text.includes('Lead:'));
  });
  test('prepare replay skips broken rows and keeps attempts', async () => {
    const out = (await runNode('replay-dead-letters/prepare-replay.js', { input: [
      { id: 1, payload: '{"email":"a@b.io"}', attempts: 2 }, { id: 2, payload: 'not json' }, { id: 3, payload: '' }, {}] })).map(i => i.json);
    assert.deepEqual(out, [{ payload: { email: 'a@b.io' }, channel: 'replay', replay_of: 1, attempts: 2 }]);
  });
  test('prepare replay with nothing to do returns no items', async () => {
    assert.deepEqual(await runNode('replay-dead-letters/prepare-replay.js', { input: [{}] }), []);
  });
});

describe('sealing the owner chat id (tools/seal.cjs)', () => {
  const { seal } = createRequire(import.meta.url)(join(ROOT, 'tools', 'seal.cjs'));
  // the decrypt side, written from n8n's CryptoV2 node: [0x01][salt16][iv12][tag16][ciphertext]
  const unseal = (b64, pass) => {
    const p = Buffer.from(b64, 'base64');
    assert.equal(p[0], 1);
    const key = scryptSync(pass, p.subarray(1, 17), 32, { N: 65536, r: 8, p: 1, maxmem: 128 * 1024 * 1024 });
    const d = createDecipheriv('aes-256-gcm', key, p.subarray(17, 29));
    d.setAuthTag(p.subarray(29, 45));
    return Buffer.concat([d.update(p.subarray(45)), d.final()]).toString('utf8');
  };
  test('round trip', () => {
    assert.equal(unseal(seal('pass', '123456789'), 'pass'), '123456789');
  });
  test('a wrong passphrase fails authentication', () => {
    assert.throws(() => unseal(seal('pass', '123'), 'other'));
  });
  test('every seal is different (random salt and iv), plain text never shows', () => {
    const a = seal('pass', '424242'), b = seal('pass', '424242');
    assert.notEqual(a, b);
    assert.ok(!Buffer.from(a, 'base64').toString('latin1').includes('424242'));
  });
});
