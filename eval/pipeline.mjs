// The workflow's own Code nodes, run outside n8n for the evaluation (same files the workflow uses).
//   node eval/pipeline.mjs prepare --model M [--effort low] < leads.jsonl > prepared.jsonl
//   node eval/pipeline.mjs parse < answered.jsonl > parsed.jsonl
// prepare: Normalize & validate -> Route intake -> Build LLM request (status != accepted means no model call)
// parse:   Check primary answer -> Parse qualification (schema check + output gate)
import { createInterface } from 'node:readline';
import { one, CONFIG } from '../tests/node/harness.mjs';

const arg = (name, d = '') => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : d; };
const cfg = { ...CONFIG, business_profile: arg('--profile', process.env.BUSINESS_PROFILE || '') || CONFIG.business_profile,
              llm_model: arg('--model', CONFIG.llm_model), llm_reasoning_effort: arg('--effort', '') };

async function prepare(rec) {
  const nodes = { 'Config': cfg, 'Lead webhook': { headers: {}, params: {}, query: {}, body: rec.lead },
                  'Verify signature': { auth: 'hmac', auth_error: '' } };
  const lead = await one('intake/normalize-validate.js', { nodes });
  const routed = await one('intake/route-intake.js', { input: [], nodes: { ...nodes, 'Normalize & validate': lead } });
  if (routed.status !== 'accepted') return { id: rec.id, status: routed.status, routed };
  const req = await one('intake/build-llm-request.js', { nodes: { 'Config': cfg, 'Route intake': routed } });
  return { id: rec.id, status: 'accepted', routed, request: req.primary };
}

async function parse(rec) {
  const check = await one('intake/check-primary-answer.js', { input: rec.response || {} });
  const answer = check.ok ? check : { ok: false, content: check.content, model: check.model };
  const q = await one('intake/parse-qualification.js', {
    input: answer, nodes: { 'Config': cfg, 'Route intake': rec.routed, 'Log lead': { id: 1 } } });
  const keep = ['fit', 'model_fit', 'score', 'needs_review', 'review_reasons', 'reply_draft', 'summary', 'urgency', 'service_interest'];
  return { id: rec.id, valid_json: check.ok, ...Object.fromEntries(keep.map(k => [k, q[k]])) };
}

const mode = process.argv[2];
const fn = { prepare, parse }[mode];
if (!fn) { console.error('usage: pipeline.mjs prepare|parse'); process.exit(2); }
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  process.stdout.write(JSON.stringify(await fn(JSON.parse(line))) + '\n');
}
