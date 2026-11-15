// One raw submission in, one clean lead out. Never throws on bad input: problems go to `errors`.
const raw = (() => {
  if ($('Internal trigger').isExecuted) {                 // from LIA · Form or LIA · Replay dead letters
    const r = $('Internal trigger').first().json;
    return { channel: r.channel || 'replay', src: r.payload || {}, query: {}, headers: {}, replay_of: String(r.replay_of ?? '') };
  }
  const w = $('Lead webhook').first().json;
  return { channel: 'webhook', src: (w.body && typeof w.body === 'object' && !Array.isArray(w.body)) ? w.body : {},
           query: w.query || {}, headers: w.headers || {}, replay_of: '' };
})();
const auth_error = $('Verify signature').isExecuted ? String($('Verify signature').first().json.auth_error || '') : '';

const flat = {};
for (const [k, v] of Object.entries(raw.src || {}))
  flat[String(k).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')] = v;
const str = v => (v == null || typeof v === 'object' ? '' : String(v)).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '').trim();
const pick = (...keys) => { for (const k of keys) { const s = str(flat[k]); if (s) return s; } return ''; };

const email = pick('email', 'work_email', 'e_mail').toLowerCase();
let name = pick('name', 'full_name', 'your_name');
if (!name) name = [pick('first_name', 'firstname'), pick('last_name', 'lastname')].filter(Boolean).join(' ');
name = name.replace(/\s+/g, ' ').slice(0, 120);
const [first_name = '', ...rest] = name.split(' ');
const company = pick('company', 'company_name', 'organization', 'business').replace(/\s+/g, ' ').slice(0, 120);
let website = pick('website', 'site', 'url', 'company_website').toLowerCase()
  .replace(/^https?:\/\//, '').replace(/^www\./, '').split(/[\/?#\s]/)[0];
website = /^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$/.test(website) ? 'https://' + website : '';
const message = pick('message', 'details', 'project', 'how_can_we_help', 'comments')
  .replace(/\r\n/g, '\n').replace(/\n{3,}/g, '\n\n').slice(0, 4000);
const budget = pick('budget', 'budget_range', 'estimated_budget').slice(0, 60);
// lower bound of a stated budget: "$5k-10k" -> 5000, "10.000 €" -> 10000, "around 4 grand" -> 4000
let budget_usd = null;
const bm = budget.toLowerCase().replace(/(\d)[.,](\d{3})(?!\d)/g, '$1$2').replace(/(\d)[.,](\d{3})(?!\d)/g, '$1$2')
  .match(/(\d+(?:\.\d+)?)\s*(?:(k|m|grand|thousand|million)(?![a-z]))?/);
if (bm) budget_usd = Math.round(parseFloat(bm[1]) * ({ k: 1e3, grand: 1e3, thousand: 1e3, m: 1e6, million: 1e6 }[bm[2] || ''] || 1));

const utmIn = (raw.src && typeof raw.src.utm === 'object' && raw.src.utm) || {};
const utm = {};
for (const k of ['source', 'medium', 'campaign', 'term', 'content']) {
  const v = str(utmIn[k] ?? utmIn['utm_' + k] ?? flat['utm_' + k] ?? raw.query['utm_' + k]);
  if (v) utm[k] = v.slice(0, 100);
}
const source = pick('source', 'lead_source').slice(0, 60)
  || { form: 'website form', webhook: 'website', replay: 'replay' }[raw.channel] || raw.channel;

// client address for the rate limit, as set by the reverse proxy in front of n8n (Cloudflare, nginx, Caddy)
const hdr = n => String(raw.headers[n] ?? '').trim();
const client_ip = (hdr('cf-connecting-ip') || hdr('x-real-ip') || hdr('x-forwarded-for').split(',')[0]).trim().slice(0, 64);

const errors = [];
if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) errors.push('email is missing or invalid');
if (message.length < 3) errors.push('message is required');

// cheap spam checks before spending an LLM call
const honeypot = pick('hp', 'honeypot', 'fax', 'nickname');           // hidden field: humans leave it empty
const links = (message.match(/https?:\/\/|www\./gi) || []).length;
const pitch = /(seo (services|ranking|audit)|backlinks?|guest post|link building|casino|crypto (investment|trading)|forex signals|first page of google)/i;
let spam_reason = '';
if (honeypot) spam_reason = 'honeypot field was filled';
else if (links >= 3) spam_reason = `${links} links in the message`;
else if (pitch.test(message) && links >= 1) spam_reason = 'sales pitch keywords + link';

// Prompt-injection pre-filter: obvious attempts to steer the qualifier. A hit never drops the lead,
// it only takes automation off the table: the lead is held for a human.
const INJECTION = [
  ['ignore-instructions', /\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the|these|system)\b[^.\n]{0,25}\b(instructions?|prompts?|rules|directions|guidelines)\b/i],
  ['role-override', /\b(you are now|act as (an?|the|my)|pretend (to be|you are)|new (system )?instructions|system prompt|developer (message|mode)|jailbreak|DAN mode)\b/i],
  ['score-steering', /\b(mark|classify|set|score|rate|label|flag|treat|tag)\b[^.\n]{0,30}\b(me|this( lead)?|us|it|my (lead|request|enquiry|inquiry))\b[^.\n]{0,25}\b(as )?(hot|10\/10|10 out of 10|score (of )?10|high[- ]priority|top priority|qualified)\b/i],
  ['send-elsewhere', /\b(send|forward|email|e-mail|cc|bcc|copy|deliver)\b[^\n]{0,60}\b(to|at)\b[^\n]{0,40}[\w.+-]+@[\w-]+(\.[\w-]+)+/i],
  ['delimiter', /<\/?\s*(lead|system|assistant|instructions?|prompt)\s*>|\[\/?(INST|SYSTEM)\]|^\s*(system|assistant|developer)\s*:/im],
  ['prompt-leak', /\b(reveal|print|show|repeat|output)\b[^.\n]{0,30}\b(your|the|system)\b[^.\n]{0,15}\b(prompt|instructions|rules)\b/i],
  ['foreign-ignore', /(ignora|ignorez|ignoriere|игнорируй|забудь)[^.\n]{0,40}(instrucciones|instructions|anweisungen|инструкци)/i],
];
const scan = [message, name, company, budget].join('\n');
const injection_flags = INJECTION.filter(([, re]) => re.test(scan)).map(([id]) => id);

const cyrb53 = (s, seed = 0) => {
  let h1 = 0xdeadbeef ^ seed, h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < s.length; i++) { const ch = s.charCodeAt(i); h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677); }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
};
// any script counts as text, so two different messages in Cyrillic or Farsi never collide
const fingerprint = cyrb53(email + '|' + message.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim());

return [{ json: {
  lead_id: 'lia_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
  received_at: new Date().toISOString(), channel: raw.channel, replay_of: raw.replay_of,
  email, name, first_name, last_name: rest.join(' '), company, website, message, budget, budget_usd,
  source, utm, utm_text: Object.entries(utm).map(([k, v]) => `${k}=${v}`).join('&'),
  fingerprint, errors, spam_reason, injection_flags, client_ip, auth_error,
} }];
