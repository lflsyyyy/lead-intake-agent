// Run one n8n Code node file (workflows/code/<workflow>/<node>.js) outside n8n.
//
//   const out = await runNode('intake/route-intake.js', {
//     input: [{ id: 1, ... }],                       // $input items (plain json or {json, binary})
//     nodes: { 'Config': { ... }, 'Normalize & validate': { ... } },   // what $('Name') returns
//     binary: Buffer, now: Date | number,
//   });
//   out -> array of { json } exactly as the node returned it
//
// The Code node runs "once for all items": top-level `return`, `await`, `$input`, `$json`, `$()`, `$now`,
// `this.helpers.getBinaryDataBuffer`. A node that is not in `nodes` behaves like an unexecuted n8n node:
// `.isExecuted` is false and `.first()` throws.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
export const CODE = join(ROOT, 'workflows', 'code');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const cache = new Map();

const asItem = (x) => (x && typeof x === 'object' && 'json' in x && Object.keys(x).every(k => ['json', 'binary', 'pairedItem'].includes(k)) ? x : { json: x });
const asItems = (v) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map(asItem));

export function source(file) {
  if (!cache.has(file)) cache.set(file, readFileSync(join(CODE, file), 'utf8'));
  return cache.get(file);
}

function nodeProxy(name, nodes) {
  const has = Object.prototype.hasOwnProperty.call(nodes, name);
  const items = has ? asItems(nodes[name]) : [];
  const fail = () => { throw new Error(`Node '${name}' hasn't been executed`); };
  return {
    isExecuted: has,
    first: () => (has ? items[0] : fail()),
    last: () => (has ? items[items.length - 1] : fail()),
    all: () => (has ? items : fail()),
    get item() { return has ? items[0] : fail(); },
  };
}

export async function runNode(file, { input, nodes = {}, binary, now } = {}) {
  const items = asItems(input === undefined ? {} : input);
  if (binary !== undefined) items[0].binary = { data: { data: Buffer.from(binary).toString('base64'), mimeType: 'application/json' } };
  const $input = { all: () => items, first: () => items[0], last: () => items[items.length - 1], item: items[0] };
  const $ = (name) => nodeProxy(name, nodes);
  const helpers = {
    async getBinaryDataBuffer(i, prop) {
      const b = items[i]?.binary?.[prop];
      if (!b) throw new Error(`no binary data '${prop}' on item ${i}`);
      return Buffer.from(b.data, 'base64');
    },
  };
  const fn = new AsyncFunction('$input', '$json', '$', '$now', '$env', source(file));
  const RealDate = Date;
  if (now !== undefined) {
    const fixed = new RealDate(now).getTime();
    globalThis.Date = class extends RealDate {
      constructor(...a) { super(...(a.length ? a : [fixed])); }
      static now() { return fixed; }
    };
  }
  try {
    const envProxy = new Proxy({}, { get() { throw new Error('access to env vars denied'); } });
    const out = await fn.call({ helpers }, $input, items[0]?.json, $, new Date(), envProxy);
    if (!Array.isArray(out)) throw new Error(`${file} must return an array of items`);
    return out;
  } finally {
    globalThis.Date = RealDate;
  }
}

export const one = async (file, ctx) => (await runNode(file, ctx))[0].json;

// shared defaults: the Config node of a typical deployment
export const CONFIG = {
  llm_base_url: 'https://api.groq.com/openai/v1', llm_model: 'openai/gpt-oss-120b', llm_reasoning_effort: 'low',
  llm_fallback_base_url: 'https://api.openai.com/v1', llm_fallback_model: 'gpt-4o-mini',
  hubspot_base_url: 'https://api.hubapi.com', resend_base_url: 'https://api.resend.com',
  mail_from: 'Brightline Studio <noreply@example.com>', mail_reply_to: '',
  real_inbox_regex: '^delivered(\\+[a-z0-9._-]+)?@resend\\.dev$',
  business_name: 'Brightline Studio', business_profile: 'A small web and automation studio.',
  sender_name: 'Alex, Brightline Studio', dedupe_window_min: 10, rate_limit_max: 5, rate_limit_window_min: 10,
  webhook_auth: 'hmac', signature_tolerance_s: 300, link_allowlist: '', allowed_price_terms: '',
  followup_days: '2,5', followup_unit_ms: 86400000, n8n_api_url: 'http://127.0.0.1:5678/api/v1',
  owner_chat_sealed: 'sealed',
};
