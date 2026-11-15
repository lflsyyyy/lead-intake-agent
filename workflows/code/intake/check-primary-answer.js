// Is the primary model's answer usable JSON with the fields we need? If not, the fallback endpoint is tried.
const r = $input.first().json;
const content = r?.choices?.[0]?.message?.content || '';
let ok = false;
try {
  const o = JSON.parse(content.replace(/^```(?:json)?\s*|\s*```$/g, ''));
  ok = !!o && typeof o === 'object' && 'score' in o && 'fit' in o && 'summary' in o;
} catch (e) { ok = false; }
return [{ json: { ok, content, model: r?.model || '' } }];
