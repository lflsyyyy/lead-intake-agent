// Collect the CRM ids and pick the action. The HubSpot portal for deep links comes from the HubSpot API
// (account info, read with the HubSpot credential), so no portal id is stored in the workflow.
const p = $('Parse qualification').first().json;
const contact_id = String($('HubSpot · upsert contact').first().json.results[0].id);
let deal_id = '';
if ($('HubSpot · create deal').isExecuted) deal_id = String($('HubSpot · create deal').first().json.id);
else if ($('HubSpot · update deal').isExecuted) deal_id = String($('HubSpot · update deal').first().json.id);
const acct = $('HubSpot · account info').isExecuted ? $('HubSpot · account info').first().json : {};
const ui = String(acct.uiDomain || 'app.hubspot.com').replace(/^https?:\/\//, '').replace(/\/+$/, '');
const base = acct.portalId ? `https://${ui}/contacts/${acct.portalId}/record` : '';
return [{ json: { ...p, contact_id, deal_id,
  contact_url: base ? `${base}/0-1/${contact_id}` : '', deal_url: base && deal_id ? `${base}/0-3/${deal_id}` : '',
  action: p.needs_review ? 'log' : p.fit === 'hot' ? 'reply' : p.fit === 'warm' ? 'followup' : 'log' } }];
