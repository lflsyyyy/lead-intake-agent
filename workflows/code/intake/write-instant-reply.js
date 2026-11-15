const p = $input.first().json;
return [{ json: { lead_id: p.lead_id, followup_id: '', contact_id: p.contact_id, kind: 'instant reply',
  to: p.email, subject: `Re: your ${p.service_interest.toLowerCase()} enquiry`, body: p.reply_draft } }];
