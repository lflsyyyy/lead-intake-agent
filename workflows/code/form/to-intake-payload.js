// n8n forms cannot share a workflow with "Respond to Webhook", so the form is a thin adapter into Intake.
const { submittedAt, formMode, ...fields } = $input.first().json;
return [{ json: { channel: 'form', payload: { ...fields, source: 'website form' } } }];
