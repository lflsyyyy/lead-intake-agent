const out = [];
for (const it of $input.all()) {
  const d = it.json;
  if (d.id == null || !d.payload) continue;
  let payload;
  try { payload = JSON.parse(d.payload); } catch (e) { continue; }
  out.push({ json: { payload, channel: 'replay', replay_of: d.id, attempts: Number(d.attempts || 0) } });
}
return out;
