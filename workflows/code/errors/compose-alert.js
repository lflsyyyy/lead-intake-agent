const d = $('Extract payload').first().json;
const dl = $('Save dead letter').first().json;
const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const text = [
  `⚠️ LIA failed at <b>${esc(d.node)}</b>: ${esc(d.error.slice(0, 300))}`,
  `Workflow: ${esc(d.workflow)} · execution ${esc(d.execution_id)}`,
  d.lead_id ? `Lead: ${esc(d.lead_id)}` : '',
  `Dead letter #${dl.id}` + (d.replayable ? ': nothing is lost, run “LIA · Replay dead letters” to retry.' : '.'),
].filter(Boolean).join('\n');
return [{ json: { text } }];
