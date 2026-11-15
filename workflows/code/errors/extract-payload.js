// Pull the original trigger payload out of the failed execution so it can be replayed later.
const f = $('Describe failure').first().json;
const ex = $input.first().json || {};
const run = ex?.data?.resultData?.runData || {};
const out = n => run[n]?.[0]?.data?.main?.[0]?.[0]?.json;
let payload = null;
if (out('Internal trigger')) payload = out('Internal trigger').payload ?? null;
else if (out('Lead webhook')) {
  const w = out('Lead webhook');
  payload = { ...(w.body || {}) };
  for (const [k, v] of Object.entries(w.query || {})) if (k.startsWith('utm_') && !(k in payload)) payload[k] = v;
} else if (out('Lead form')) {
  const { submittedAt, formMode, ...fields } = out('Lead form');
  payload = fields;
}
const lead_id = out('Normalize & validate')?.lead_id || '';
return [{ json: { ...f, lead_id, payload: payload ? JSON.stringify(payload) : '', replayable: !!payload } }];
