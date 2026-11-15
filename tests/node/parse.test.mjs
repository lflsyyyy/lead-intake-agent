import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { one, CONFIG } from './harness.mjs';

const lead = (o = {}) => ({ lead_id: 'lia_1', email: 'priya@fernhill.example', name: 'Priya Natarajan', first_name: 'Priya',
  last_name: 'Natarajan', company: 'Fernhill Dental', website: 'https://fernhill.example', budget: '$15k-$25k', budget_usd: 15000,
  message: 'We need a booking website, budget $15k-$25k.', source: 'website', utm_text: '', injection_flags: [], ...o });
const good = (o = {}) => ({ score: 9, fit: 'hot', intent: 'new website', service_interest: 'website and booking',
  business_type: 'Dental clinic', budget_estimate: '$15k-$25k', urgency: 'high', summary: 'Dental group wants a site.',
  reply_draft: 'Hi Priya,\n\nThanks for reaching out about the new site. Which practice software do you use?\n\nAlex',
  disqualify_reason: null, injection_suspected: false, ...o });
const primary = (q, model = 'openai/gpt-oss-120b') => ({ ok: true, content: typeof q === 'string' ? q : JSON.stringify(q), model });
const fallback = (q, model = 'gpt-4o-mini') => ({ choices: [{ message: { content: typeof q === 'string' ? q : JSON.stringify(q) } }], model });
const parse = (answer, l = {}, cfg = {}) => one('intake/parse-qualification.js', {
  input: answer, nodes: { 'Config': { ...CONFIG, ...cfg }, 'Route intake': lead(l), 'Log lead': { id: 42 } } });

describe('parse: schema', () => {
  test('a valid answer passes through', async () => {
    const p = await parse(primary(good()));
    assert.equal(p.fit, 'hot');
    assert.equal(p.score, 9);
    assert.equal(p.needs_review, false);
    assert.deepEqual(p.review_reasons, []);
    assert.equal(p.row_id, 42);
  });
  test('records which endpoint answered', async () => {
    assert.equal((await parse(primary(good()))).llm_provider, 'primary');
    const f = await parse(fallback(good()));
    assert.equal(f.llm_provider, 'fallback');
    assert.equal(f.llm_model, 'gpt-4o-mini');
  });
  test('accepts JSON wrapped in a ```json fence', async () => {
    assert.equal((await parse(primary('```json\n' + JSON.stringify(good()) + '\n```'))).needs_review, false);
  });
  test('score is clamped to 1-10 and rounded', async () => {
    assert.equal((await parse(primary(good({ score: 14 })))).score, 10);
    assert.equal((await parse(primary(good({ score: -3, fit: 'spam' })))).score, 1);
    assert.equal((await parse(primary(good({ score: '8.6' })))).score, 9);
  });
  test('non-numeric score means review', async () => {
    const p = await parse(primary(good({ score: 'high' })));
    assert.equal(p.needs_review, true);
    assert.equal(p.fit, 'cold');
    assert.equal(p.model_fit, 'hot');
  });
  test('unknown fit is derived from the score but flagged', async () => {
    const p = await parse(primary(good({ fit: 'very hot', score: 8 })));
    assert.equal(p.needs_review, true);
    assert.equal(p.fit, 'cold');
    assert.ok(p.review_reasons.includes('model output failed the schema check'));
  });
  test('fit is case-insensitive', async () => {
    assert.equal((await parse(primary(good({ fit: 'HOT' })))).fit, 'hot');
  });
  test('unknown urgency becomes medium', async () => {
    assert.equal((await parse(primary(good({ urgency: 'asap' })))).urgency, 'medium');
  });
  test('strings are truncated', async () => {
    const p = await parse(primary(good({ intent: 'x'.repeat(500), business_type: 'b'.repeat(200) })));
    assert.equal(p.intent.length, 200);
    assert.equal(p.business_type.length, 80);
  });
  test('missing service_interest and summary get defaults', async () => {
    const p = await parse(primary(good({ service_interest: '', summary: '' })));
    assert.equal(p.service_interest, 'General enquiry');
    assert.match(p.summary, /needs review/);
  });
  test('spam never keeps a reply draft', async () => {
    assert.equal((await parse(primary(good({ fit: 'spam', score: 1, reply_draft: 'Hi there' })))).reply_draft, '');
  });
  test('empty budget_estimate and disqualify_reason become null', async () => {
    const p = await parse(primary(good({ budget_estimate: '', disqualify_reason: '' })));
    assert.equal(p.budget_estimate, null);
    assert.equal(p.disqualify_reason, null);
  });
});

describe('parse: broken output is downgraded, never automated', () => {
  for (const [name, content] of [['not JSON', 'Sure! Here is the lead...'], ['empty', ''], ['a JSON array', '[1,2]'],
                                 ['JSON null', 'null'], ['truncated JSON', '{"score": 9, "fit": "hot", "summ']]) {
    test(name, async () => {
      const p = await parse(primary(content));
      assert.equal(p.needs_review, true);
      assert.notEqual(p.fit, 'hot');
      assert.notEqual(p.fit, 'warm');
      assert.ok(p.review_reasons.includes('model output failed the schema check'));
    });
  }
  test('hot without a reply draft is held', async () => {
    const p = await parse(primary(good({ reply_draft: '' })));
    assert.equal(p.fit, 'cold');
    assert.ok(p.review_reasons.includes('no reply draft'));
  });
  test('a warm lead downgraded keeps its score for the owner', async () => {
    const p = await parse(primary(good({ fit: 'warm', score: 5, reply_draft: '' })));
    assert.equal(p.score, 5);
    assert.equal(p.model_fit, 'warm');
  });
});

describe('parse: output gate', () => {
  const reply = (text) => primary(good({ reply_draft: `Hi Priya,\n\n${text}\n\nAlex` }));
  test('injection flagged by the pre-filter holds the lead', async () => {
    const p = await parse(primary(good()), { injection_flags: ['score-steering'] });
    assert.equal(p.fit, 'cold');
    assert.equal(p.model_fit, 'hot');
    assert.match(p.review_reasons[0], /prompt injection suspected \(score-steering\)/);
  });
  test('injection_suspected from the model holds the lead', async () => {
    const p = await parse(primary(good({ injection_suspected: true })));
    assert.equal(p.needs_review, true);
    assert.ok(p.review_reasons.includes('model flagged an injection attempt'));
  });
  test('another email address in the reply is refused', async () => {
    const p = await parse(reply('I have copied friend@other.example on this.'));
    assert.ok(p.review_reasons.includes('reply mentions another email address'));
  });
  test("the lead's own address is fine", async () => {
    assert.equal((await parse(reply('We will write to priya@fernhill.example.'))).needs_review, false);
  });
  test('a link off the allowlist is refused', async () => {
    const p = await parse(reply('Pay here: http://pay-here.example/invoice'));
    assert.ok(p.review_reasons.some(r => r.startsWith('reply links to a site not on the allowlist (pay-here.example)')));
  });
  test('bare domain with a path counts as a link', async () => {
    assert.equal((await parse(reply('See bit.ly/abc for details.'))).needs_review, true);
  });
  test("the lead's own website is allowed", async () => {
    assert.equal((await parse(reply('I had a look at https://www.fernhill.example/about already.'))).needs_review, false);
  });
  test('allowlisted domains and their subdomains pass', async () => {
    const cfg = { link_allowlist: 'brightline.example, cal.com' };
    assert.equal((await parse(reply('Book at https://cal.com/alex or https://docs.brightline.example/start'), {}, cfg)).needs_review, false);
  });
  test('a price the lead did not mention is refused', async () => {
    const p = await parse(reply('We can do it for $4,500.'));
    assert.ok(p.review_reasons.some(r => r.startsWith('reply mentions a price or discount')));
  });
  test("echoing the lead's own budget is fine", async () => {
    assert.equal((await parse(reply('A $15k-$25k budget fits this scope well.'))).needs_review, false);
  });
  test('discount words are refused', async () => {
    for (const t of ['We offer a 20% discount this month.', 'Use promo code SAVE10.', 'There is a money-back guarantee.', 'We can do 30 % off.']) {
      assert.equal((await parse(reply(t))).needs_review, true, t);
    }
  });
  test('"100% committed" is not a discount', async () => {
    assert.equal((await parse(reply('We are 100% focused on clinics like yours.'))).needs_review, false);
  });
  test('prices in other currencies are refused', async () => {
    assert.equal((await parse(reply('Projects start at 3000 EUR.'))).needs_review, true);
    assert.equal((await parse(reply('Typical cost is €2k.'))).needs_review, true);
  });
  test('terms allowed in Config pass', async () => {
    assert.equal((await parse(reply('We offer a free of charge 30-minute call.'), {}, { allowed_price_terms: 'free of charge' })).needs_review, false);
  });
  test('talking about internal scoring is refused', async () => {
    assert.equal((await parse(reply('Great news, you are marked as a hot lead!'))).needs_review, true);
  });
  test('leaking the instructions is refused', async () => {
    assert.ok((await parse(primary(good({ summary: 'You qualify inbound leads for us' })))).review_reasons.includes('reply leaks the instructions'));
  });
  test('extra keys in the model output are refused', async () => {
    const p = await parse(primary(good({ send_to: 'boss@evil.example', actions: ['email'] })));
    assert.ok(p.review_reasons.some(r => r.startsWith('unexpected fields in model output: send_to, actions')));
  });
  test('several reasons are all reported', async () => {
    const p = await parse(reply('Email me at x@evil.example for 50% off via http://evil.example/'), { injection_flags: ['ignore-instructions'] });
    assert.ok(p.review_reasons.length >= 4);
  });
  test('a held spam answer stays spam', async () => {
    const p = await parse(primary(good({ fit: 'spam', score: 1, reply_draft: '' })), { injection_flags: ['role-override'] });
    assert.equal(p.fit, 'spam');
    assert.equal(p.needs_review, true);
  });
});

describe('parse: CRM payloads', () => {
  test('lead status follows the fit', async () => {
    const st = async (q) => (await parse(primary(good(q)))).lead_status;
    assert.equal(await st({}), 'IN_PROGRESS');
    assert.equal(await st({ fit: 'warm', score: 5 }), 'OPEN');
    assert.equal(await st({ fit: 'cold', score: 3 }), 'NEW');
    assert.equal(await st({ fit: 'spam', score: 1 }), 'UNQUALIFIED');
  });
  test('held leads go to HubSpot as NEW', async () => {
    assert.equal((await parse(primary(good()), { injection_flags: ['x'] })).contact_properties.hs_lead_status, 'NEW');
  });
  test('contact properties drop empty values', async () => {
    const p = await parse(primary(good()), { company: '', website: '' });
    assert.ok(!('company' in p.contact_properties));
    assert.ok(!('website' in p.contact_properties));
    assert.equal(p.contact_properties.email, 'priya@fernhill.example');
  });
  test('the note escapes HTML from the lead', async () => {
    const p = await parse(primary(good()), { message: '<script>alert(1)</script>' });
    assert.ok(!p.note_html.includes('<script>'));
    assert.ok(p.note_html.includes('&lt;script&gt;'));
  });
  test('the note shows why a lead was held', async () => {
    const p = await parse(primary(good()), { injection_flags: ['delimiter'] });
    assert.match(p.note_html, /Held for review/);
    assert.match(p.note_html, /needs review/);
  });
  test('deal name, stage and amount', async () => {
    const p = await parse(primary(good()));
    assert.equal(p.deal_name, 'Fernhill Dental — website and booking');
    assert.equal(p.deal_stage, 'qualifiedtobuy');
    assert.equal(p.amount, 15000);
    const w = await parse(primary(good({ fit: 'warm', score: 6 })), { budget_usd: null, company: '' });
    assert.equal(w.deal_stage, 'appointmentscheduled');
    assert.equal(w.amount, null);
    assert.equal(w.deal_name, 'Priya Natarajan — website and booking');
  });
});

describe('parse: the lead\'s own amounts in other spellings', () => {
  const reply = (text) => primary(good({ reply_draft: `Hi Brett,\n\n${text}\n\nAlex` }));
  test('"4 grand" in the lead allows "$4,000" in the reply', async () => {
    assert.equal((await parse(reply('A budget of around $4,000 works for a rebuild.'), { budget: 'around 4 grand', message: 'site rebuild' })).needs_review, false);
  });
  test('"$15k-$25k" allows "$25,000"', async () => {
    assert.equal((await parse(reply('Up to $25,000 is plenty.'))).needs_review, false);
  });
  test('a different amount is still refused', async () => {
    assert.equal((await parse(reply('We usually start at $3,000.'), { budget: 'around 4 grand', message: 'site rebuild' })).needs_review, true);
  });
  test('"in 3 months" in the lead does not allow "$3 million"', async () => {
    assert.equal((await parse(reply('That would be $3m.'), { budget: '', message: 'we want to launch in 3 months' })).needs_review, true);
  });
  test('a discount word never counts as an echo', async () => {
    assert.equal((await parse(reply('We can give you a discount.'), { message: 'is there a discount for nonprofits?' })).needs_review, false);
    assert.equal((await parse(reply('We can give you a discount.'), { message: 'site please' })).needs_review, true);
  });
});
