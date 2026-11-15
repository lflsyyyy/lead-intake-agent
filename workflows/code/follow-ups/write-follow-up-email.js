return $input.all().map(it => ({ json: { lead_id: it.json.lead_id, followup_id: it.json.followup_id,
  contact_id: it.json.contact_id, kind: `follow-up #${it.json.step}`, to: it.json.email,
  subject: it.json.subject, body: it.json.body } }));
