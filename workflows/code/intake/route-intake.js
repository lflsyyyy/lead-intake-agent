// Decide what happens to this submission, in this order:
//   401 bad signature -> 429 rate limit -> 422 invalid -> 200 duplicate -> 202 spam (silently) -> 202 accepted.
// Input: every lia_leads row received inside the larger of the two windows ("Recent submissions").
const lead = $('Normalize & validate').first().json;
const cfg = $('Config').first().json;
const now = Date.now();
const minutes = (v, d) => (Number(v) > 0 ? Number(v) : d) * 60000;
const dedupeMs = minutes(cfg.dedupe_window_min, 10);
const rateMs = minutes(cfg.rate_limit_window_min, 10);
const rateMax = Number(cfg.rate_limit_max) > 0 ? Number(cfg.rate_limit_max) : 5;
const at = r => new Date(r.received_at || r.createdAt).getTime();
const recent = $input.all().map(i => i.json).filter(r => r && r.id != null && Number.isFinite(at(r)));
const replay = lead.channel === 'replay';             // replays skip the duplicate check and the rate limit

const dup = replay ? null
  : recent.find(r => r.fingerprint === lead.fingerprint && r.status !== 'invalid' && now - at(r) < dedupeMs);
const sameSender = r => (lead.email && r.email === lead.email) || (lead.client_ip && r.client_ip === lead.client_ip);
const hits = replay ? [] : recent.filter(r => now - at(r) < rateMs && sameSender(r));

let status = 'accepted', http_code = 202, response = { status: 'accepted', lead_id: lead.lead_id };
if (lead.auth_error) {
  status = 'unauthorized'; http_code = 401; response = { status: 'unauthorized', error: lead.auth_error };
} else if (hits.length >= rateMax) {
  const oldest = Math.min(...hits.map(at));
  status = 'rate_limited'; http_code = 429;
  response = { status: 'rate_limited', retry_after_s: Math.max(1, Math.ceil((oldest + rateMs - now) / 1000)) };
} else if (lead.errors.length) {
  status = 'invalid'; http_code = 422; response = { status: 'rejected', errors: lead.errors };
} else if (dup) {
  status = 'duplicate'; http_code = 200; response = { status: 'duplicate', lead_id: dup.lead_id };
} else if (lead.spam_reason) status = 'spam';             // bots get the same 202 as humans

const note = status === 'spam' ? 'rule: ' + lead.spam_reason
  : status === 'duplicate' ? 'duplicate of ' + dup.lead_id
  : status === 'invalid' ? lead.errors.join('; ')
  : lead.replay_of ? 'replay of dead letter #' + lead.replay_of : '';
// unauthorized and rate-limited requests get an answer but are not stored: a flood must not fill the table
const log = status !== 'unauthorized' && status !== 'rate_limited';
return [{ json: { ...lead, status, http_code, response, log, fit: status === 'spam' ? 'spam' : '', note,
  action: status === 'accepted' ? 'processing' : 'logged' } }];
