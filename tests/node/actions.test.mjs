import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { one, runNode, CONFIG } from './harness.mjs';

const NOW = Date.UTC(2026, 10, 15, 12, 0, 0);
const parsed = (o = {}) => ({ lead_id: 'lia_1', row_id: 42, email: 'priya@fernhill.example', name: 'Priya Natarajan',
  first_name: 'Priya', company: 'Fernhill Dental', fit: 'hot', model_fit: 'hot', score: 9, needs_review: false,
  review_reasons: [], service_interest: 'Website and Booking', summary: 'Dental group wants a site.', budget: '$15k',
  budget_estimate: null, urgency: 'high', source: 'website', utm_text: 'source=google', disqualify_reason: null,
  reply_draft: 'Hi Priya, thanks!', lead_status: 'IN_PROGRESS', llm_provider: 'primary', ...o });

describe('check primary answer', () => {
  const check = (r) => one('intake/check-primary-answer.js', { input: r });
  const resp = (content, model = 'm1') => ({ choices: [{ message: { content } }], model });
  test('valid JSON with the core fields is ok', async () => {
    const c = await check(resp('{"score":9,"fit":"hot","summary":"x"}'));
    assert.equal(c.ok, true);
    assert.equal(c.model, 'm1');
  });
  test('fenced JSON is ok', async () => {
    assert.equal((await check(resp('```json\n{"score":1,"fit":"spam","summary":"x"}\n```'))).ok, true);
  });
  test('missing core fields is not ok (fallback is tried)', async () => {
    assert.equal((await check(resp('{"score":9}'))).ok, false);
  });
  test('prose is not ok', async () => {
    assert.equal((await check(resp('I think this lead is hot.'))).ok, false);
  });
  test('an error body without choices is not ok', async () => {
    assert.equal((await check({ error: { message: 'rate limited' } })).ok, false);
  });
});

describe('plan next step', () => {
  const plan = (p = {}, nodes = {}) => one('intake/plan-next-step.js', { nodes: {
    'Parse qualification': parsed(p), 'HubSpot · upsert contact': { results: [{ id: 501 }] },
    'HubSpot · account info': { portalId: 777, uiDomain: 'app-eu1.hubspot.com' }, ...nodes } });
  test('hot -> reply, warm -> followup, cold -> log', async () => {
    assert.equal((await plan()).action, 'reply');
    assert.equal((await plan({ fit: 'warm' })).action, 'followup');
    assert.equal((await plan({ fit: 'cold' })).action, 'log');
  });
  test('a held lead is only logged, whatever the fit', async () => {
    assert.equal((await plan({ fit: 'hot', needs_review: true })).action, 'log');
  });
  test('deep links use the portal from the HubSpot API', async () => {
    const p = await plan({}, { 'HubSpot · create deal': { id: 900 } });
    assert.equal(p.contact_url, 'https://app-eu1.hubspot.com/contacts/777/record/0-1/501');
    assert.equal(p.deal_url, 'https://app-eu1.hubspot.com/contacts/777/record/0-3/900');
    assert.equal(p.contact_id, '501');
    assert.equal(p.deal_id, '900');
  });
  test('an updated (existing) deal is picked up too', async () => {
    assert.equal((await plan({}, { 'HubSpot · update deal': { id: 901 } })).deal_id, '901');
  });
  test('no deal for cold leads', async () => {
    const p = await plan({ fit: 'cold' });
    assert.equal(p.deal_id, '');
    assert.equal(p.deal_url, '');
  });
  test('without account info there are no links but nothing breaks', async () => {
    const p = await one('intake/plan-next-step.js', { nodes: {
      'Parse qualification': parsed(), 'HubSpot · upsert contact': { results: [{ id: 5 }] } } });
    assert.equal(p.contact_url, '');
  });
});

describe('write instant reply', () => {
  test('addresses the lead with the draft and a subject from the service', async () => {
    const r = await one('intake/write-instant-reply.js', { input: { ...parsed(), contact_id: '501' } });
    assert.deepEqual(r, { lead_id: 'lia_1', followup_id: '', contact_id: '501', kind: 'instant reply',
      to: 'priya@fernhill.example', subject: 'Re: your website and booking enquiry', body: 'Hi Priya, thanks!' });
  });
});

describe('plan follow-ups', () => {
  const plan = (cfg = {}, p = {}) => runNode('intake/plan-follow-ups.js', {
    input: { ...parsed({ fit: 'warm', lead_status: 'OPEN', ...p }), contact_id: '501' }, now: NOW, nodes: { 'Config': { ...CONFIG, ...cfg } } });
  test('two follow-ups, day 2 and day 5', async () => {
    const out = (await plan()).map(i => i.json);
    assert.equal(out.length, 2);
    assert.equal(out[0].due_at, new Date(NOW + 2 * 86400000).toISOString());
    assert.equal(out[1].due_at, new Date(NOW + 5 * 86400000).toISOString());
    assert.deepEqual(out.map(f => f.step), [1, 2]);
  });
  test('the first sends the AI draft, the second a short nudge', async () => {
    const [a, b] = (await plan()).map(i => i.json);
    assert.equal(a.body, 'Hi Priya, thanks!');
    assert.match(b.body, /^Hi Priya,/);
    assert.match(b.body, /Alex, Brightline Studio$/);
    assert.equal(a.subject, 'Re: your website and booking enquiry');
    assert.equal(b.subject, 'Following up: website and booking');
  });
  test('they remember the HubSpot status they were planned against', async () => {
    for (const f of (await plan()).map(i => i.json)) {
      assert.equal(f.expected_status, 'OPEN');
      assert.equal(f.status, 'pending');
    }
  });
  test('DEMO_FAST: minutes instead of days', async () => {
    const out = (await plan({ followup_unit_ms: 60000 })).map(i => i.json);
    assert.equal(out[0].due_at, new Date(NOW + 2 * 60000).toISOString());
  });
  test('custom schedule and junk values are ignored', async () => {
    const out = (await plan({ followup_days: '1, x, 0, 3, 7' })).map(i => i.json);
    assert.deepEqual(out.map(f => f.step), [1, 2, 3]);
  });
  test('nudge without a first name says "there"', async () => {
    const [, b] = (await plan({}, { first_name: '' })).map(i => i.json);
    assert.match(b.body, /^Hi there,/);
  });
});

describe('compose owner alert', () => {
  const compose = (p = {}, nodes = {}, cfg = {}) => one('intake/compose-owner-alert.js', { nodes: {
    'Config': { ...CONFIG, ...cfg }, 'Plan next step': { ...parsed(p), contact_url: 'https://hs/c/1', deal_url: p.deal_url ?? 'https://hs/d/2',
      action: p.action || 'reply' }, ...nodes } });
  test('hot + delivered', async () => {
    const a = await compose({}, { 'Mark delivered': { id: 1 } });
    assert.match(a.text, /^<b>🔥 HOT lead · 9\/10<\/b>/);
    assert.match(a.text, /Instant reply sent to priya@fernhill.example/);
    assert.equal(a.action_result, 'emailed');
  });
  test('hot kept in the outbox by the safety rule', async () => {
    const a = await compose();
    assert.equal(a.action_result, 'outbox');
    assert.match(a.text, /not a whitelisted inbox/);
  });
  test('warm shows the follow-up schedule', async () => {
    const a = await compose({ fit: 'warm', action: 'followup', score: 5 });
    assert.equal(a.action_result, 'follow-ups queued');
    assert.match(a.text, /after 2 and 5 days/);
    assert.match(a.text, /🟡 WARM/);
  });
  test('warm in DEMO_FAST mode talks about minutes', async () => {
    assert.match((await compose({ fit: 'warm', action: 'followup' }, {}, { followup_unit_ms: 60000 })).text, /2 and 5 min/);
  });
  test('cold is logged only', async () => {
    const a = await compose({ fit: 'cold', action: 'log', disqualify_reason: 'student project', deal_url: '' });
    assert.equal(a.action_result, 'logged');
    assert.match(a.text, /Why cold: student project/);
    assert.ok(!a.text.includes('Deal</a>'));
  });
  test('a held lead says so and why, and what the model wanted', async () => {
    const a = await compose({ fit: 'cold', model_fit: 'hot', needs_review: true, action: 'log',
                              review_reasons: ['prompt injection suspected (score-steering)'] });
    assert.equal(a.action_result, 'held for review');
    assert.match(a.text, /needs review/);
    assert.match(a.text, /Held for review, nothing was sent: prompt injection suspected/);
    assert.match(a.text, /Model said: hot/);
  });
  test('escapes HTML from the lead', async () => {
    const a = await compose({ name: '<b>Evil</b>', summary: 'x < y & z' });
    assert.ok(a.text.includes('&lt;b&gt;Evil&lt;/b&gt;'));
    assert.ok(a.text.includes('x &lt; y &amp; z'));
  });
  test('shows budget, urgency, source and UTM', async () => {
    const t = (await compose()).text;
    assert.match(t, /Budget: \$15k · Urgency: high/);
    assert.match(t, /Source: website \(source=google\)/);
  });
  test('no double blank lines and links at the end', async () => {
    const t = (await compose({ disqualify_reason: null })).text;
    assert.ok(!t.includes('\n\n\n'));
    assert.match(t, /<a href="https:\/\/hs\/c\/1">HubSpot contact<\/a> · <a href="https:\/\/hs\/d\/2">Deal<\/a>$/);
  });
  test('never contains the chat id or any address of ours', async () => {
    const t = (await compose()).text;
    assert.ok(!/chat_id|noreply@/.test(t));
  });
});
