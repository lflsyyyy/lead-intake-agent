# Threat model

Short and specific to this pipeline: a public endpoint takes text from strangers, an LLM reads it, and the
result can send email from the business's domain and write to its CRM.

## Assets

| Asset | Why it matters |
|---|---|
| Sending reputation of the business domain (Resend) | One abused auto-reply campaign gets the domain blocklisted |
| CRM data (HubSpot contacts, notes, deals) | Customer PII; junk records waste the owner's time |
| API keys: LLM providers, HubSpot, Resend, Telegram bot, n8n API | Direct cost and data access if leaked |
| Owner's Telegram chat | Alert channel; spam there trains the owner to ignore it |
| LLM budget | A flood of submissions turns into a bill |
| Lead data in n8n (`lia_*` tables, executions) | PII of every submitter |

## Trust boundaries

1. Internet → `POST /webhook/leads` (untrusted bytes).
2. Internet → n8n hosted form `/form/lia-contact` (untrusted, unauthenticated by design).
3. Lead text → LLM (untrusted text next to our instructions).
4. LLM output → automation (email, CRM, follow-ups).
5. Workflow definitions → exports and screenshots (must not leak private values).

## Threats and mitigations

| # | Threat | Mitigation | Where |
|---|---|---|---|
| T1 | Forged submissions to the webhook (spam, CRM pollution, LLM cost) | HMAC-SHA256 over `"<X-Lead-Timestamp>.<raw body>"`, secret in the `LIA · Webhook secret` credential (n8n Crypto node); constant-time compare; 401 on any mismatch, not stored. `WEBHOOK_AUTH=token` switches to an `X-Lead-Token` header checked by n8n's own Header Auth | `Read raw body` → `Sign request (HMAC)` → `Verify signature` |
| T2 | Replay of a captured signed request | Timestamp must be within ±300 s; inside that window an identical request is a duplicate (fingerprint) and gets 200 without side effects | `Verify signature`, `Route intake` |
| T3 | Flooding (valid or invalid payloads, form spam) | Max 5 submissions per email or client IP in 10 min, then 429; rate-limited and unauthorized requests are answered without writing a row, so a flood cannot fill the table. Bots: honeypot field (`hp`, also on the hosted form), link-count and pitch rules before any LLM call | `Route intake`, `Normalize & validate` |
| T4 | Prompt injection: the lead tells the model to score it hot, email someone, add links or discounts, or reveal the prompt | (a) the lead goes in as JSON inside `<lead>…</lead>`, with `<` and `>` escaped so it cannot close the block; it is never concatenated into instructions. (b) The system prompt says the block is untrusted data and to ignore instructions in it, and asks the model to set `injection_suspected`. (c) A regex pre-filter flags obvious attempts (ignore-instructions, role override, score steering, send-elsewhere, delimiter tricks, prompt leak, Spanish/Russian variants). (d) An output gate refuses automation when the answer has unexpected fields, another email address, a link off the allowlist, a price or discount the lead did not mention and config does not allow, talk about internal scoring, or leaked instructions. Any hit: *held for review* (cold, no email, no follow-ups, owner alerted with the reasons) | `Build LLM request`, `Normalize & validate`, `Parse qualification` |
| T5 | Broken or adversarial model output (not JSON, wrong types, huge strings) | Strict schema check, clamping and truncation; a broken answer is downgraded and never automated; fallback provider when the primary answer is unusable | `Check primary answer`, `Parse qualification` |
| T6 | Auto-reply to an address the business must not email | Email safety rule: only addresses matching `REAL_INBOX_REGEX` are sent; everything else stays in `lia_outbox`. The recipient is always the submitter's own address, never one taken from model output | outbox chain |
| T7 | HTML/markup injection into Telegram alerts or HubSpot notes | Every lead-supplied value is HTML-escaped before it goes into the alert or the note | `Compose owner alert`, `Parse qualification` |
| T8 | Secrets or personal data leaking through exports, screenshots or the editor | Secrets only in n8n credentials (encrypted at rest). The Config node holds no private values: the owner's chat id is sealed (AES-256-GCM, passphrase in the `LIA · Private settings` credential) and unsealed by a Crypto node right before Telegram; the real sender address is merged into the Resend request from the `LIA · Resend` credential; the HubSpot portal for deep links is read from the HubSpot API at run time. `build.py --export` replaces credential and workflow ids and the Config values with public placeholders. CI runs gitleaks and tests that fail on key patterns or private domains in tracked files | `tools/build.py`, `tests/python/test_workflow_json.py`, CI |
| T9 | Losing leads on failures (which attackers can cause, e.g. by exhausting LLM quota) | Row written before the 202; every external call retries; failed runs go to a dead-letter table with a replay workflow | `LIA · Errors`, `LIA · Replay dead letters` |

## Residual risks

* **Client IP comes from proxy headers** (`CF-Connecting-IP`, `X-Real-IP`, `X-Forwarded-For`). If n8n is exposed
  without a reverse proxy that overwrites them, a sender can rotate the header and only the per-email limit holds.
  Put n8n behind a proxy that sets them; the per-email limit and HMAC still apply either way.
* **The hosted form is unauthenticated** (it is n8n's own form and cannot be signed). It has the honeypot, the
  per-email rate limit and the spam rules, but no per-IP limit (n8n's form trigger does not expose the client IP).
  Add a captcha or put it behind the proxy's rate limiter if it gets abused.
* **The HMAC secret is shared** with whatever backend signs the requests. A browser-only website cannot sign
  safely; post through a small backend or use the hosted form.
* **Injection detection is not complete.** The regex filter is a cheap first line, not a classifier; the main
  protection is structural (no model output decides recipients, and anything unusual is held, not sent). A
  sufficiently subtle injection can still bias the score of a lead, which at worst puts it in front of the owner
  sooner. It cannot make the pipeline email a third party.
* **The output gate's price detection is pattern based.** A price written in words ("two thousand dollars") passes.
  The prompt also forbids prices, and replies only ever go to the submitter.
* **Rate-limit counting uses the `lia_leads` table**, so it is per instance and eventually consistent under bursts
  of concurrent executions (a few extra requests can slip through in the same second).
* **Execution data in n8n keeps lead PII and model output**, as n8n does for every workflow. Restrict editor
  access and set execution pruning (`EXECUTIONS_DATA_MAX_AGE`) to the retention you need.
* **Sealed values are only as safe as the n8n instance.** Anyone with editor access can run the unseal node.
  The point is to keep private values out of exports, screenshots and the canvas, not to hide them from admins.
