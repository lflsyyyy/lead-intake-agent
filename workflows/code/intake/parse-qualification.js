// Validate the qualification against the schema, then run the output gate. Nothing downstream trusts raw
// model output: a broken answer, an injection attempt, or an answer that tries something the automation may not
// do is downgraded to "cold + needs review". Those leads still reach the CRM and the owner, but never get an
// automatic email.
const cfg = $('Config').first().json;
const lead = $('Route intake').first().json;
const row_id = $('Log lead').first().json.id;
const j = $input.first().json;
const fromFallback = Array.isArray(j.choices);
const content = fromFallback ? (j.choices[0]?.message?.content || '') : String(j.content || '');
const model = fromFallback ? (j.model || cfg.llm_fallback_model) : (j.model || cfg.llm_model);

let q = null;
try { q = JSON.parse(String(content).trim().replace(/^```(?:json)?\s*|\s*```$/g, '')); } catch (e) { q = null; }
let broken = false;
if (!q || typeof q !== 'object' || Array.isArray(q)) { q = {}; broken = true; }
const s = (v, n) => (v == null ? '' : String(v)).trim().slice(0, n);

let score = Math.round(Number(q.score));
if (q.score === null || q.score === '' || typeof q.score === 'boolean' || !Number.isFinite(score)) { score = 5; broken = true; }
score = Math.min(10, Math.max(1, score));
let fit = String(q.fit || '').toLowerCase().trim();
if (!['hot', 'warm', 'cold', 'spam'].includes(fit)) { fit = score >= 7 ? 'hot' : score >= 4 ? 'warm' : 'cold'; broken = true; }
const urgency = ['high', 'medium', 'low'].includes(String(q.urgency).toLowerCase()) ? String(q.urgency).toLowerCase() : 'medium';
const qual = {
  score, fit, urgency,
  intent: s(q.intent, 200),
  service_interest: s(q.service_interest, 60) || 'General enquiry',
  business_type: s(q.business_type, 80),
  budget_estimate: q.budget_estimate == null || q.budget_estimate === '' ? null : s(q.budget_estimate, 60),
  summary: s(q.summary, 300) || 'No summary: qualification needs review.',
  reply_draft: fit === 'spam' ? '' : s(q.reply_draft, 1500),
  disqualify_reason: q.disqualify_reason == null || q.disqualify_reason === '' ? null : s(q.disqualify_reason, 200),
};

// ---------------------------------------------------------------- output gate
const review = [];
if (broken) review.push('model output failed the schema check');
if ((lead.injection_flags || []).length) review.push('prompt injection suspected (' + lead.injection_flags.join(', ') + ')');
if (q.injection_suspected === true) review.push('model flagged an injection attempt');
const KEYS = ['score', 'fit', 'intent', 'service_interest', 'business_type', 'budget_estimate', 'urgency', 'summary',
              'reply_draft', 'disqualify_reason', 'injection_suspected'];
const extra = Object.keys(q).filter(k => !KEYS.includes(k));
if (extra.length) review.push('unexpected fields in model output: ' + extra.slice(0, 3).join(', '));

const reply = qual.reply_draft;
const said = [reply, qual.summary, qual.intent].join('\n');
const others = (said.match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g) || []).map(e => e.toLowerCase()).filter(e => e !== lead.email);
if (others.length) review.push('reply mentions another email address');

const hostOf = u => String(u).trim().toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '').split(/[\/?#:\s]/)[0];
const allowed = [lead.website, ...String(cfg.link_allowlist || '').split(',')].map(hostOf).filter(Boolean);
const links = reply.match(/\b(?:https?:\/\/|www\.)[^\s<>"')\]]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\/[^\s<>"')\]]*/gi) || [];
const offList = links.map(hostOf).filter(h => h && !allowed.some(a => h === a || h.endsWith('.' + a)));
if (offList.length) review.push('reply links to a site not on the allowlist (' + offList[0] + ')');

// prices and discounts: only what the lead said themselves, or terms the business allows in config
const MONEY = /[$€£¥₹]\s?\d[\d,.]*\s?[km]?\b|\b\d[\d,.]*\s?k?\s?(?:usd|eur|gbp|dollars?|euros?|pounds?)\b|\b\d{1,3}\s?%\s?(?:off|discount)\b|\b(?:discount|coupon|promo code|voucher|free of charge|money[- ]back|refund)\b/gi;
const squash = t => String(t || '').toLowerCase().replace(/[\s,]/g, '');
// every amount a text mentions, as a number: "$4,000" -> 4000, "4 grand" -> 4000, "$15k-$25k" -> 15000, 25000
const amounts = t => {
  const n = String(t || '').toLowerCase().replace(/(\d)[.,](\d{3})(?!\d)/g, '$1$2').replace(/(\d)[.,](\d{3})(?!\d)/g, '$1$2');
  const mult = { k: 1e3, grand: 1e3, thousand: 1e3, m: 1e6, million: 1e6 };
  return [...n.matchAll(/(\d+(?:\.\d+)?)\s*(?:(k|m|grand|thousand|million)(?![a-z]))?/g)].map(x => Math.round(parseFloat(x[1]) * (mult[x[2]] || 1)));
};
const theirText = lead.message + ' ' + lead.budget;
const theirs = squash(theirText);
const theirAmounts = new Set(amounts(theirText));
const terms = String(cfg.allowed_price_terms || '').split(',').map(squash).filter(Boolean);
const echoes = m => { const a = amounts(m); return a.length > 0 && a.every(v => theirAmounts.has(v)); };
const offers = (reply.match(MONEY) || []).filter(m => !theirs.includes(squash(m)) && !echoes(m)
  && !terms.some(t => t.includes(squash(m)) || squash(m).includes(t)));
if (offers.length) review.push('reply mentions a price or discount (' + offers[0].trim() + ')');

if (/\b(lead score|scored (you|this|your)|marked (you|this|your)|(as )?a hot lead|priority lead|qualified you)\b/i.test(said))
  review.push('reply talks about internal scoring');
if (/qualify inbound leads|return only a json|injection_suspected|<\/?lead>/i.test(said)) review.push('reply leaks the instructions');
if (['hot', 'warm'].includes(qual.fit) && !reply) review.push('no reply draft');

const needs_review = review.length > 0;
const model_fit = qual.fit;
if (needs_review && ['hot', 'warm'].includes(qual.fit)) qual.fit = 'cold';

const lead_status = { hot: 'IN_PROGRESS', warm: 'OPEN', cold: 'NEW', spam: 'UNQUALIFIED' }[qual.fit];
const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const contact_properties = {
  email: lead.email, firstname: lead.first_name, lastname: lead.last_name, company: lead.company,
  website: lead.website, message: lead.message.slice(0, 2000), hs_lead_status: lead_status,
};
for (const k of Object.keys(contact_properties)) if (!contact_properties[k]) delete contact_properties[k];

const li = (k, v) => v ? `<li><strong>${k}:</strong> ${esc(v)}</li>` : '';
const note_html = [
  `<p><strong>Lead qualification</strong> · score ${qual.score}/10 · ${qual.fit.toUpperCase()}${needs_review ? ' · needs review' : ''}</p>`,
  `<p>${esc(qual.summary)}</p><ul>`,
  li('Intent', qual.intent), li('Service', qual.service_interest), li('Business type', qual.business_type),
  li('Budget', lead.budget ? lead.budget + ' (stated)' : qual.budget_estimate ? qual.budget_estimate + ' (estimate)' : ''),
  li('Urgency', qual.urgency), li('Source', lead.source + (lead.utm_text ? ' · ' + lead.utm_text : '')),
  li('Why not a fit', qual.disqualify_reason), li('Held for review', review.join('; ')),
  li('Model said', needs_review && model_fit !== qual.fit ? model_fit : ''), li('Model', model), '</ul>',
  `<p><strong>Message</strong><br>${esc(lead.message).replace(/\n/g, '<br>')}</p>`,
  qual.reply_draft ? `<p><strong>Suggested reply</strong><br>${esc(qual.reply_draft).replace(/\n/g, '<br>')}</p>` : '',
].join('');

return [{ json: {
  ...lead, ...qual, model_fit, needs_review, review_reasons: review, row_id,
  llm_provider: fromFallback ? 'fallback' : 'primary', llm_model: model,
  lead_status, contact_properties, note_html,
  deal_name: `${lead.company || lead.name || lead.email} — ${qual.service_interest}`.slice(0, 200),
  deal_stage: qual.score >= 8 ? 'qualifiedtobuy' : 'appointmentscheduled',
  amount: lead.budget_usd || null,
} }];
