const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const rows = $('Mark follow-up sent').all().map(i => i.json);
const lines = rows.map(r => `• #${r.step} → ${esc(r.name || r.email)}${r.company ? ' (' + esc(r.company) + ')' : ''}: ${r.note === 'emailed' ? 'sent' : 'saved to outbox'}`);
return [{ json: { text: `↩️ <b>Follow-ups: ${rows.length}</b>\n` + lines.join('\n') } }];
