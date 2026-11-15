const p = $('Plan next step').first().json;
const cfg = $('Config').first().json;
const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const badge = { hot: '🔥 HOT', warm: '🟡 WARM', cold: '🧊 COLD' }[p.fit] || p.fit;
let action_result = 'logged', action = '📝 Logged only, no automatic email.';
if (p.needs_review) {
  action_result = 'held for review';
  action = `👀 Held for review, nothing was sent: ${esc((p.review_reasons || []).join('; '))}`;
} else if (p.action === 'reply') {
  const sent = $('Mark delivered').isExecuted;
  action_result = sent ? 'emailed' : 'outbox';
  action = sent ? `✉️ Instant reply sent to ${esc(p.email)}`
                : `✉️ Reply saved to outbox (${esc(p.email)} is not a whitelisted inbox)`;
} else if (p.action === 'followup') {
  const unit = Number(cfg.followup_unit_ms) === 60000 ? 'min' : 'days';
  const steps = String(cfg.followup_days).split(',').map(s => s.trim()).join(' and ');
  action_result = 'follow-ups queued';
  action = `⏰ Follow-ups queued: after ${steps} ${unit} unless you change the lead status in HubSpot`;
}
const links = [p.contact_url ? `<a href="${p.contact_url}">HubSpot contact</a>` : '',
               p.deal_url ? `<a href="${p.deal_url}">Deal</a>` : ''].filter(Boolean).join(' · ');
const lines = [
  `<b>${badge} lead · ${p.score}/10</b>${p.needs_review ? ' · ⚠️ needs review' : ''}`,
  `<b>${esc(p.name || p.email)}</b>${p.company ? ' — ' + esc(p.company) : ''}`,
  '',
  esc(p.summary),
  '',
  `Service: ${esc(p.service_interest)}`,
  `Budget: ${esc(p.budget || p.budget_estimate || 'not stated')} · Urgency: ${esc(p.urgency)}`,
  `Source: ${esc(p.source)}${p.utm_text ? ' (' + esc(p.utm_text) + ')' : ''}`,
  p.disqualify_reason ? `Why ${p.fit}: ${esc(p.disqualify_reason)}` : '',
  p.needs_review && p.model_fit && p.model_fit !== p.fit ? `Model said: ${esc(p.model_fit)}` : '',
  '',
  action,
  links,
].filter((l, i, a) => !(l === '' && a[i - 1] === '') && !(l === '' && i === a.length - 1));
return [{ json: { text: lines.join('\n').trim(), action_result } }];
