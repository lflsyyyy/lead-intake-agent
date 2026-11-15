// Send only if nobody touched the lead: same HubSpot lead status as when we queued it, and no reply logged.
const due = $('Get due follow-ups').all();
return $input.all().map((it, i) => {
  const fu = due[i].json;
  const props = it.json.properties || {};
  const replied = props.hs_sales_email_last_replied && new Date(props.hs_sales_email_last_replied) > new Date(fu.createdAt);
  const status = props.hs_lead_status || '';
  const stop_reason = !it.json.id ? 'contact not found in HubSpot'
    : replied ? 'lead replied'
    : status !== fu.expected_status ? `lead status changed to ${status || 'empty'}` : '';
  return { json: { ...fu, followup_id: String(fu.id), send: !stop_reason, stop_reason } };
});
