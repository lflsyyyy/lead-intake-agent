const e = $('Error trigger').first().json;
return [{ json: {
  workflow: e.workflow?.name || '', workflow_id: String(e.workflow?.id || ''),
  node: e.execution?.lastNodeExecuted || e.execution?.error?.node?.name || e.trigger?.error?.node?.name || 'trigger',
  error: String(e.execution?.error?.message || e.trigger?.error?.message || 'unknown error').slice(0, 1000),
  execution_id: String(e.execution?.id || ''), execution_url: e.execution?.url || '',
} }];
