// Warm leads: the owner has a head start. If nobody touches the lead in HubSpot, the AI reply goes out
// on day 2 and a short nudge on day 5. Changing the lead status in HubSpot cancels them.
const p = $input.first().json;
const cfg = $('Config').first().json;
const unit = Number(cfg.followup_unit_ms) || 86400000;
const days = String(cfg.followup_days || '2,5').split(',').map(Number).filter(n => n > 0);
const topic = p.service_interest.toLowerCase();
const nudge = `Hi ${p.first_name || 'there'},\n\nJust bringing this back to the top of your inbox. Happy to help with ${topic} whenever the timing suits you. If it is easier, reply with two or three times that work for a short call.\n\n${cfg.sender_name}`;
return days.map((d, i) => ({ json: {
  lead_id: p.lead_id, contact_id: p.contact_id, email: p.email, name: p.name, company: p.company,
  step: i + 1, due_at: new Date(Date.now() + d * unit).toISOString(), status: 'pending',
  expected_status: p.lead_status,
  subject: i === 0 ? `Re: your ${topic} enquiry` : `Following up: ${topic}`,
  body: i === 0 ? p.reply_draft : nudge,
} }));
