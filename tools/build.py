"""Build (or rebuild) the Lead Intake Agent in n8n through its public API.

    python tools/build.py                    # live target (.env): tables + credentials + 5 workflows, activate
    python tools/build.py --target docker    # the Docker stack (docker.env): mocks, Mailpit, no real keys
    python tools/build.py --export           # also write sanitized workflow JSON to workflows/
    python tools/build.py --update-credentials   # re-send credential secrets that are available locally

Idempotent: workflows are matched by name and updated in place (webhook URLs keep working),
data tables get missing columns added, credentials are created once and then reused.
Secrets are read from the env file only to create n8n credentials; workflows reference credentials by id.
The JavaScript of every Code node lives in workflows/code/<workflow>/<node>.js and is injected here.
"""
import copy
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, N8n, env, load_env, secret  # noqa: E402

n8n = None          # set in main(); everything above main() works offline (tests import this module)

PREFIX = "LIA · "
WF = {"intake": PREFIX + "Intake", "followups": PREFIX + "Follow-ups",
      "errors": PREFIX + "Errors", "replay": PREFIX + "Replay dead letters", "form": PREFIX + "Form"}
CODE_DIR = os.path.join(ROOT, "workflows", "code")
PUBLIC_MAIL_FROM = "Brightline Studio <noreply@example.com>"
SEALED_PLACEHOLDER = "REPLACE_WITH_SEALED_CHAT_ID"

# ---------------------------------------------------------------- data tables
TABLES = {
    "lia_leads": [
        ("lead_id", "string"), ("received_at", "date"), ("channel", "string"), ("status", "string"),
        ("email", "string"), ("name", "string"), ("company", "string"), ("website", "string"),
        ("message", "string"), ("budget", "string"), ("budget_usd", "number"), ("source", "string"),
        ("utm", "string"), ("fingerprint", "string"), ("score", "number"), ("fit", "string"),
        ("summary", "string"), ("service_interest", "string"), ("llm_provider", "string"),
        ("contact_id", "string"), ("deal_id", "string"), ("telegram_message_id", "string"),
        ("action", "string"), ("note", "string"), ("client_ip", "string"), ("review_reason", "string")],
    "lia_followups": [
        ("lead_id", "string"), ("contact_id", "string"), ("email", "string"), ("name", "string"),
        ("company", "string"), ("step", "number"), ("due_at", "date"), ("status", "string"),
        ("expected_status", "string"), ("subject", "string"), ("body", "string"), ("sent_at", "date"),
        ("note", "string")],
    "lia_outbox": [
        ("lead_id", "string"), ("followup_id", "string"), ("contact_id", "string"), ("kind", "string"),
        ("to", "string"), ("subject", "string"), ("body", "string"), ("delivered", "boolean"),
        ("provider_id", "string"), ("created_at", "date")],
    "lia_dead_letter": [
        ("workflow", "string"), ("workflow_id", "string"), ("node", "string"), ("error", "string"),
        ("execution_id", "string"), ("execution_url", "string"), ("lead_id", "string"),
        ("payload", "string"), ("status", "string"), ("attempts", "number"), ("replayed_at", "date")],
}
COLTYPE = {t: dict(cols) for t, cols in TABLES.items()}


def ensure_tables():
    existing = {t["name"]: t for t in n8n.all("/data-tables")}
    for name, cols in TABLES.items():
        if name not in existing:
            n8n("POST", "/data-tables", {"name": name, "columns": [{"name": c, "type": t} for c, t in cols]})
            print("table created  ", name)
            continue
        have = {c["name"] for c in n8n("GET", "/data-tables/%s/columns" % existing[name]["id"])}
        for c, t in cols:
            if c not in have:
                n8n("POST", "/data-tables/%s/columns" % existing[name]["id"], {"name": c, "type": t})
                print("column added   ", name, c)
        print("table ok       ", name)


# ---------------------------------------------------------------- local secrets
def local_secret(name, default_file):
    """A secret that build.py may generate: NAME, NAME_FILE, or a random value saved to default_file."""
    v = secret(name, required=False)
    if v:
        return v
    p = env(name + "_FILE") or default_file
    p = p if os.path.isabs(p) else os.path.join(ROOT, p)
    if os.path.exists(p):
        return open(p, encoding="utf8").read().strip()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    v = pysecrets.token_urlsafe(32)
    with open(p, "w", encoding="utf8") as f:
        f.write(v)
    print("secret created ", os.path.relpath(p, ROOT))
    return v


def webhook_secret():
    return local_secret("WEBHOOK_SECRET", "secrets/webhook.key")


def private_passphrase():
    return local_secret("PRIVATE_SETTINGS_PASSPHRASE", "secrets/private-settings.key")


def webhook_auth_mode():
    mode = env("WEBHOOK_AUTH", "hmac").lower()
    if mode not in ("hmac", "token", "none"):
        raise SystemExit("WEBHOOK_AUTH must be hmac, token or none")
    return mode


# ---------------------------------------------------------------- credentials
def resend_auth_json():
    """Resend auth header, plus the real sender when MAIL_FROM is set: n8n merges a Custom Auth body into the
    request body, so the live From address lives only in this (encrypted) credential, never in a workflow."""
    data = {"headers": {"Authorization": "Bearer " + secret("RESEND_API_KEY")}}
    if env("MAIL_FROM") and env("MAIL_FROM") != PUBLIC_MAIL_FROM:
        data["body"] = {"from": env("MAIL_FROM")}
    return json.dumps(data)


def credential_specs():
    """name -> (type, data factory). Factories are only called when a credential is created or updated."""
    specs = {
        "llm": (PREFIX + "LLM primary", "httpHeaderAuth",
                lambda: {"name": "Authorization", "value": "Bearer " + secret("LLM_API_KEY")}),
        "hubspot": (PREFIX + "HubSpot", "hubspotAppToken", lambda: {"appToken": secret("HUBSPOT_TOKEN")}),
        "telegram": (PREFIX + "Telegram", "telegramApi",
                     lambda: {"accessToken": secret("TELEGRAM_BOT_TOKEN"),
                              "baseUrl": env("TELEGRAM_BASE_URL", "https://api.telegram.org").rstrip("/")}),
        "n8n": (PREFIX + "n8n API", "n8nApi",
                lambda: {"apiKey": secret("N8N_API_KEY"), "baseUrl": n8n_api_url_for_n8n()}),
        "resend": (PREFIX + "Resend", "httpCustomAuth", lambda: {"json": resend_auth_json()}),
        "webhook_hmac": (PREFIX + "Webhook secret", "crypto", lambda: {"hmacSecret": webhook_secret()}),
        "private": (PREFIX + "Private settings", "crypto",
                    lambda: {"encryptionPassphrase": private_passphrase()}),
    }
    if env("LLM_FALLBACK_BASE_URL"):
        specs["llm_fallback"] = (PREFIX + "LLM fallback", "httpHeaderAuth",
                                 lambda: {"name": "Authorization",
                                          "value": "Bearer " + (secret("LLM_FALLBACK_API_KEY", False) or "none")})
    if webhook_auth_mode() == "token":
        specs["webhook_token"] = (PREFIX + "Webhook token", "httpHeaderAuth",
                                  lambda: {"name": "X-Lead-Token", "value": webhook_secret()})
    return specs


def ensure_credentials(update=False):
    have = {c["name"]: c for c in n8n.all("/credentials")}
    out = {}
    for key, (name, ctype, data_fn) in credential_specs().items():
        if name in have and have[name]["type"] == ctype:
            out[key] = {"id": have[name]["id"], "name": name}
            if update:
                try:
                    n8n("PATCH", "/credentials/%s" % have[name]["id"], {"name": name, "type": ctype, "data": data_fn()})
                    print("credential upd ", name)
                except SystemExit as e:              # a secret that is not available locally: keep the stored one
                    print("credential kept", name, "(%s)" % e)
            continue
        if name in have:                              # same name, different type (older build): replace it
            n8n("DELETE", "/credentials/%s" % have[name]["id"])
        r = n8n("POST", "/credentials", {"name": name, "type": ctype, "data": data_fn()})
        print("credential new ", name)
        out[key] = {"id": r["id"], "name": name}
    return out


def n8n_api_url_for_n8n():
    """The n8n API as seen from inside n8n (in Docker that is not the host's localhost URL)."""
    return env("N8N_INTERNAL_URL", env("N8N_URL", "http://127.0.0.1:5678")).rstrip("/") + "/api/v1"


# ---------------------------------------------------------------- sealing (owner chat id)
SEAL_JS = os.path.join(ROOT, "tools", "seal.cjs")


def seal(value):
    """Encrypt value in the format of n8n's Crypto node (AES-256-GCM, scrypt key from the passphrase stored in the
    LIA · Private settings credential). The Config node then holds only ciphertext; a Crypto node unseals it."""
    if not value:
        return ""
    payload = json.dumps({"passphrase": private_passphrase(), "value": str(value)})
    node = shutil.which("node")
    if node:
        cmd = [node, SEAL_JS]
    elif env("LIA_TARGET") == "docker":                # no local Node: use the one inside the n8n container
        cmd = ["docker", "compose", "exec", "-T", "n8n", "node", "-e", open(SEAL_JS, encoding="utf8").read()]
    else:
        raise SystemExit("node is required to seal private settings (or set TELEGRAM_CHAT_ID empty)")
    r = subprocess.run(cmd, input=payload, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("seal failed: " + r.stderr.strip()[:300])
    return r.stdout.strip()


# ---------------------------------------------------------------- config node
def public_config():
    """Placeholder set used in exports and as defaults. No personal data, no ids, no secrets."""
    return {
        "llm_base_url": "https://api.groq.com/openai/v1", "llm_model": "openai/gpt-oss-120b",
        "llm_reasoning_effort": "low",
        "llm_fallback_base_url": "https://api.openai.com/v1", "llm_fallback_model": "gpt-4o-mini",
        "hubspot_base_url": "https://api.hubapi.com", "resend_base_url": "https://api.resend.com",
        "mail_from": PUBLIC_MAIL_FROM, "mail_reply_to": "",
        "real_inbox_regex": r"^delivered(\+[a-z0-9._-]+)?@resend\.dev$",
        "business_name": "Brightline Studio",
        "business_profile": DEFAULT_PROFILE, "sender_name": "Alex, Brightline Studio",
        "dedupe_window_min": 10, "rate_limit_max": 5, "rate_limit_window_min": 10,
        "webhook_auth": "hmac", "signature_tolerance_s": 300,
        "link_allowlist": "", "allowed_price_terms": "",
        "followup_days": "2,5", "followup_unit_ms": 86400000,
        "n8n_api_url": "http://127.0.0.1:5678/api/v1",
        "owner_chat_sealed": SEALED_PLACEHOLDER,
    }


DEFAULT_PROFILE = ("A small web and automation studio. We build marketing websites, Shopify stores, and workflow "
                   "automation (n8n, CRM integrations) for small and mid-sized businesses. Typical projects are "
                   "$3k-$30k; we do not do SEO link building, app store apps, or staff outsourcing.")


def config_values():
    """Values for the Config node of the target instance. Private values never land here in plain text:
    the owner's chat id is sealed, the real sender is in the Resend credential, the HubSpot portal is read
    from the HubSpot API at run time."""
    pub = public_config()
    fast = env("DEMO_FAST", "0") == "1"
    return {
        "llm_base_url": env("LLM_BASE_URL", pub["llm_base_url"]).rstrip("/"),
        "llm_model": env("LLM_MODEL", pub["llm_model"]),
        "llm_reasoning_effort": env("LLM_REASONING_EFFORT"),
        "llm_fallback_base_url": env("LLM_FALLBACK_BASE_URL").rstrip("/"),
        "llm_fallback_model": env("LLM_FALLBACK_MODEL"),
        "hubspot_base_url": env("HUBSPOT_BASE_URL", pub["hubspot_base_url"]).rstrip("/"),
        "resend_base_url": env("RESEND_BASE_URL", pub["resend_base_url"]).rstrip("/"),
        "mail_from": env("MAIL_FROM_PUBLIC", PUBLIC_MAIL_FROM), "mail_reply_to": env("MAIL_REPLY_TO"),
        "real_inbox_regex": env("REAL_INBOX_REGEX", pub["real_inbox_regex"]),
        "business_name": env("BUSINESS_NAME", pub["business_name"]),
        "business_profile": env("BUSINESS_PROFILE", DEFAULT_PROFILE),
        "sender_name": env("SENDER_NAME", pub["sender_name"]),
        "dedupe_window_min": int(env("DEDUPE_WINDOW_MIN", "10")),
        "rate_limit_max": int(env("RATE_LIMIT_MAX", "5")),
        "rate_limit_window_min": int(env("RATE_LIMIT_WINDOW_MIN", "10")),
        "webhook_auth": webhook_auth_mode(),
        "signature_tolerance_s": int(env("SIGNATURE_TOLERANCE_S", "300")),
        "link_allowlist": env("LINK_ALLOWLIST"), "allowed_price_terms": env("ALLOWED_PRICE_TERMS"),
        "followup_days": env("FOLLOWUP_DAYS", "2,5"),
        "followup_unit_ms": 60000 if fast else 86400000,
        "n8n_api_url": n8n_api_url_for_n8n(),
        "owner_chat_sealed": seal(env("TELEGRAM_CHAT_ID")),
    }


# ---------------------------------------------------------------- node helpers
_WF_SCOPE = [""]      # the workflow being built, for deterministic node ids (clean diffs between builds)


def node(name, type_, ver, params, pos, **extra):
    d = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "lia/%s/%s" % (_WF_SCOPE[0], name))), "name": name,
         "type": type_, "typeVersion": ver, "position": pos, "parameters": params}
    d.update(extra)
    return d


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def code_path(workflow, name):
    return os.path.join(CODE_DIR, workflow, slug(name) + ".js")


def read_code(workflow, name):
    p = code_path(workflow, name)
    if not os.path.exists(p):
        raise SystemExit("missing Code node source: %s" % os.path.relpath(p, ROOT))
    return open(p, encoding="utf8").read().strip()


def code(workflow, name, pos, **extra):
    """A Code node whose JavaScript is the single source of truth in workflows/code/<workflow>/<name>.js"""
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": read_code(workflow, name)}, pos, **extra)


def config_node(values, pos):
    kind = lambda v: "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "string"
    return node("Config", "n8n-nodes-base.set", 3.4, {
        "mode": "manual",
        "assignments": {"assignments": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "lia/config/" + k)), "name": k,
                                         "value": v, "type": kind(v)} for k, v in values.items()]},
        "includeOtherFields": False, "options": {}}, pos)


def _schema(table, cols):
    return [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True,
             "type": COLTYPE[table][c], "canBeUsedToMatch": True, "removed": False} for c in cols]


def _table_ref(table):
    return {"__rl": True, "mode": "name", "value": table}


def dt_insert(name, table, values, pos, **extra):
    return node(name, "n8n-nodes-base.dataTable", 1.1, {
        "resource": "row", "operation": "insert", "dataTableId": _table_ref(table),
        "columns": {"mappingMode": "defineBelow", "value": values, "matchingColumns": [],
                    "schema": _schema(table, values), "attemptToConvertTypes": False,
                    "convertFieldsToString": False},
        "options": {}}, pos, **extra)


def _filters(conds):
    return {"conditions": [{"keyName": k, "condition": c, "keyValue": v} for k, c, v in conds]}


def dt_update(name, table, conds, values, pos, **extra):
    return node(name, "n8n-nodes-base.dataTable", 1.1, {
        "resource": "row", "operation": "update", "dataTableId": _table_ref(table),
        "matchType": "allConditions", "filters": _filters(conds),
        "columns": {"mappingMode": "defineBelow", "value": values, "matchingColumns": [],
                    "schema": _schema(table, values), "attemptToConvertTypes": False,
                    "convertFieldsToString": False},
        "options": {}}, pos, **extra)


def dt_get(name, table, conds, pos, **extra):
    return node(name, "n8n-nodes-base.dataTable", 1.1, {
        "resource": "row", "operation": "get", "dataTableId": _table_ref(table),
        "matchType": "allConditions", "filters": _filters(conds), "returnAll": True, "options": {}},
        pos, **extra)


def _cond(left, op, right=""):
    typ, operation = op.split(":")
    c = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "lia/cond/%s/%s/%s" % (left, op, right))), "leftValue": left,
         "rightValue": right, "operator": {"type": typ, "operation": operation}}
    if operation in ("true", "false", "empty", "notEmpty"):
        c["operator"]["singleValue"] = True
    return c


def if_node(name, left, op, right, pos):
    return node(name, "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": False, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [_cond(left, op, right)], "combinator": "and"},
        "options": {}}, pos)


def switch_node(name, left, cases, pos):
    rules = [{"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict",
                                         "version": 2},
                             "conditions": [_cond(left, "string:equals", value)], "combinator": "and"},
              "renameOutput": True, "outputKey": label} for value, label in cases]
    return node(name, "n8n-nodes-base.switch", 3.2, {"rules": {"values": rules}, "options": {}}, pos)


RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000}


def http_node(name, method, url, pos, cred, body=None, timeout=30000, retry=RETRY, **extra):
    """cred = ('header'|'custom', credref) | ('hubspotAppToken', credref) | ('n8nApi', credref)"""
    kind, ref = cred
    p = {"method": method, "url": url, "options": {"timeout": timeout}}
    if kind in ("header", "custom"):
        gtype = "httpHeaderAuth" if kind == "header" else "httpCustomAuth"
        p.update({"authentication": "genericCredentialType", "genericAuthType": gtype})
        creds = {gtype: ref}
    else:
        p.update({"authentication": "predefinedCredentialType", "nodeCredentialType": kind})
        creds = {kind: ref}
    if body is not None:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    return node(name, "n8n-nodes-base.httpRequest", 4.2, p, pos, credentials=creds, **dict(retry or {}), **extra)


def unseal_node(pos, cred):
    """Crypto node that turns the sealed owner chat id from Config into plain text for the Telegram node."""
    return node("Unseal owner chat", "n8n-nodes-base.crypto", 2, {
        "action": "decrypt", "mode": "symmetric", "cipher": "aes-256-gcm",
        "value": "={{ $('Config').first().json.owner_chat_sealed }}", "dataPropertyName": "owner_chat_id"},
        pos, credentials={"crypto": cred})


def telegram(name, pos, cred):
    return node(name, "n8n-nodes-base.telegram", 1.2, {
        "resource": "message", "operation": "sendMessage",
        "chatId": "={{ $json.owner_chat_id }}", "text": "={{ $json.text }}",
        "additionalFields": {"appendAttribution": False, "parse_mode": "HTML", "disable_web_page_preview": True}},
        pos, credentials={"telegramApi": cred}, **RETRY)


def sticky(text, pos, w, h, color=7):
    return node("Note · " + text.split("\n")[0].strip("# ")[:40], "n8n-nodes-base.stickyNote", 1,
                {"content": text, "width": w, "height": h, "color": color}, pos)


def wire(*pairs):
    """(src, dst) or (src, dst, output_index) or (src, dst, output_index, input_index)"""
    c = {}
    for p in pairs:
        src, dst, out = p[0], p[1], (p[2] if len(p) > 2 else 0)
        inp = p[3] if len(p) > 3 else 0
        lst = c.setdefault(src, {"main": []})["main"]
        while len(lst) <= out:
            lst.append([])
        lst[out].append({"node": dst, "type": "main", "index": inp})
    return c


def settings(err_id=None):
    s = {"executionOrder": "v1", "timezone": "UTC", "saveDataErrorExecution": "all",
         "saveDataSuccessExecution": "all", "saveManualExecutions": True, "saveExecutionProgress": True,
         "callerPolicy": "workflowsFromSameOwner"}
    if err_id:
        s["errorWorkflow"] = err_id
    return s


def upsert_workflow(wf):
    for w in n8n.all("/workflows"):
        if w["name"] == wf["name"]:
            if w.get("active"):
                n8n("POST", "/workflows/%s/deactivate" % w["id"])
            n8n("PUT", "/workflows/%s" % w["id"], wf)
            print("workflow upd   ", w["id"], wf["name"])
            return w["id"]
    r = n8n("POST", "/workflows", wf)
    print("workflow new   ", r["id"], wf["name"])
    return r["id"]


# shared email sub-chain: outbox row -> safety gate -> Resend -> mark delivered
def outbox_chain(x, y, cred_resend):
    nodes = [
        dt_insert("Save to lia_outbox", "lia_outbox", {
            "lead_id": "={{ $json.lead_id }}", "followup_id": "={{ $json.followup_id }}",
            "contact_id": "={{ $json.contact_id }}", "kind": "={{ $json.kind }}", "to": "={{ $json.to }}",
            "subject": "={{ $json.subject }}", "body": "={{ $json.body }}", "delivered": False,
            "created_at": "={{ $now.toISO() }}"}, [x, y]),
        if_node("Real inbox? (safety rule)", "={{ $json.to }}", "string:regex",
                "={{ $('Config').first().json.real_inbox_regex }}", [x + 240, y]),
        http_node("Resend · send email", "POST", "={{ $('Config').first().json.resend_base_url }}/emails",
                  [x + 480, y - 120], ("custom", cred_resend),
                  body="={{ JSON.stringify({ from: $('Config').first().json.mail_from, to: [$json.to], "
                       "subject: $json.subject, text: $json.body, "
                       "reply_to: $('Config').first().json.mail_reply_to || undefined }) }}", timeout=20000),
        dt_update("Mark delivered", "lia_outbox",
                  [("id", "eq", "={{ $('Save to lia_outbox').item.json.id }}")],
                  {"delivered": True, "provider_id": "={{ $json.id }}"}, [x + 720, y - 120]),
    ]
    cons = [("Save to lia_outbox", "Real inbox? (safety rule)"),
            ("Real inbox? (safety rule)", "Resend · send email", 0),
            ("Resend · send email", "Mark delivered")]
    return nodes, cons


def hs_url(path):
    return "={{ $('Config').first().json.hubspot_base_url }}" + path


# ================================================================ 1. INTAKE
def build_intake(c, cfg, err_id):
    _WF_SCOPE[0] = "intake"
    I = "intake"
    mode = cfg.get("webhook_auth", "hmac")
    hook_params = {"httpMethod": "POST", "path": env("WEBHOOK_PATH", "leads"), "responseMode": "responseNode",
                   "options": {"allowedOrigins": "*", "rawBody": True}}
    hook_extra = {"webhookId": str(uuid.uuid5(uuid.NAMESPACE_URL, "lia-webhook"))}
    if mode == "token":
        hook_params["authentication"] = "headerAuth"
        hook_extra["credentials"] = {"httpHeaderAuth": c["webhook_token"]}
    hook = node("Lead webhook", "n8n-nodes-base.webhook", 2, hook_params, [0, 0], **hook_extra)

    ob_nodes, ob_cons = outbox_chain(480, 1400, c["resend"])
    hs = ("hubspotAppToken", c["hubspot"])
    contact_id = "$('HubSpot · upsert contact').first().json.results[0].id"
    PQ = "$('Parse qualification').first().json"
    PN = "$('Plan next step').first().json"
    R2, R3 = 700, 1400            # canvas rows: intake | AI + CRM | actions + alert
    nodes = [
        hook,
        code(I, "Read raw body", [220, 0]),
        node("Sign request (HMAC)", "n8n-nodes-base.crypto", 2, {
            "action": "hmac", "type": "SHA256", "value": "={{ $json.signed_payload }}",
            "dataPropertyName": "expected_signature", "encoding": "hex"}, [440, 0],
            credentials={"crypto": c["webhook_hmac"]}),
        node("Internal trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"},
             [440, 220]),
        config_node(cfg, [660, 100]),
        code(I, "Verify signature", [880, 100]),
        code(I, "Normalize & validate", [1100, 100]),
        dt_get("Recent submissions", "lia_leads",
               [("received_at", "gte", "={{ $now.minus(Math.max(Number($('Config').first().json.dedupe_window_min) || 10, "
                                       "Number($('Config').first().json.rate_limit_window_min) || 10), 'minutes').toISO() }}")],
               [1320, 100], alwaysOutputData=True),
        code(I, "Route intake", [1540, 100]),
        if_node("Log it?", "={{ $json.log }}", "boolean:true", "", [1760, 100]),
        dt_insert("Log lead", "lia_leads", {
            "lead_id": "={{ $json.lead_id }}", "received_at": "={{ $json.received_at }}",
            "channel": "={{ $json.channel }}", "status": "={{ $json.status }}", "email": "={{ $json.email }}",
            "name": "={{ $json.name }}", "company": "={{ $json.company }}", "website": "={{ $json.website }}",
            "message": "={{ $json.message }}", "budget": "={{ $json.budget }}",
            "budget_usd": "={{ $json.budget_usd }}", "source": "={{ $json.source }}",
            "utm": "={{ $json.utm_text }}", "fingerprint": "={{ $json.fingerprint }}", "fit": "={{ $json.fit }}",
            "action": "={{ $json.action }}", "note": "={{ $json.note }}", "client_ip": "={{ $json.client_ip }}"},
            [1980, 0]),
        if_node("Came from webhook?", "={{ $('Route intake').first().json.channel }}", "string:equals", "webhook",
                [2200, 100]),
        node("Respond 202 / 200 / 401 / 422 / 429", "n8n-nodes-base.respondToWebhook", 1.1, {
            "respondWith": "json",
            "responseBody": "={{ JSON.stringify($('Route intake').first().json.response) }}",
            "options": {"responseCode": "={{ $('Route intake').first().json.http_code }}"}}, [2420, 0]),
        if_node("Needs qualification?", "={{ $('Route intake').first().json.status }}", "string:equals", "accepted",
                [2640, 100]),
        # ---- row 2: AI + CRM
        code(I, "Build LLM request", [0, R2]),
        http_node("LLM · primary", "POST", "={{ $('Config').first().json.llm_base_url }}/chat/completions",
                  [240, R2], ("header", c["llm"]), body="={{ JSON.stringify($json.primary) }}", timeout=60000,
                  onError="continueErrorOutput"),
        code(I, "Check primary answer", [480, R2 - 100]),
        if_node("Primary answer valid?", "={{ $json.ok }}", "boolean:true", "", [720, R2 - 100]),
        http_node("LLM · fallback", "POST", "={{ $('Config').first().json.llm_fallback_base_url }}/chat/completions",
                  [960, R2 + 120], ("header", c.get("llm_fallback", c["llm"])),
                  body="={{ JSON.stringify($('Build LLM request').first().json.fallback) }}", timeout=120000,
                  retry={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000}),
        code(I, "Parse qualification", [1200, R2]),
        if_node("Spam?", "={{ $json.fit }}", "string:equals", "spam", [1440, R2]),
        dt_update("Mark as spam", "lia_leads", [("id", "eq", "={{ $json.row_id }}")], {
            "status": "spam", "fit": "spam", "score": "={{ $json.score }}", "summary": "={{ $json.summary }}",
            "llm_provider": "={{ $json.llm_provider }}", "action": "logged",
            "review_reason": "={{ $json.review_reasons.join('; ') }}",
            "note": "={{ 'llm: ' + ($json.disqualify_reason || 'spam') }}"}, [1680, R2 + 180]),
        http_node("HubSpot · upsert contact", "POST", hs_url("/crm/v3/objects/contacts/batch/upsert"),
                  [1680, R2 - 100], hs, body="={{ JSON.stringify({ inputs: [{ idProperty: 'email', id: $json.email, "
                                              "properties: $json.contact_properties }] }) }}"),
        http_node("HubSpot · add note", "POST", hs_url("/crm/v3/objects/notes"), [1920, R2 - 100], hs,
                  body="={{ JSON.stringify({ properties: { hs_timestamp: $now.toISO(), "
                       "hs_note_body: %s.note_html }, "
                       "associations: [{ to: { id: %s }, types: [{ associationCategory: 'HUBSPOT_DEFINED', "
                       "associationTypeId: 202 }] }] }) }}" % (PQ, contact_id)),
        http_node("HubSpot · account info", "GET", hs_url("/account-info/v3/details"), [2160, R2 - 100], hs),
        if_node("Hot or warm?", "={{ %s.fit }}" % PQ, "string:regex", "^(hot|warm)$", [2400, R2 - 100]),
        http_node("HubSpot · find deal", "POST", hs_url("/crm/v3/objects/deals/search"), [2640, R2 - 200],
                  hs, body="={{ JSON.stringify({ filterGroups: [{ filters: ["
                           "{ propertyName: 'associations.contact', operator: 'EQ', value: %s }, "
                           "{ propertyName: 'dealname', operator: 'EQ', value: %s.deal_name }"
                           "] }], properties: ['dealname', 'dealstage'], limit: 1 }) }}" % (contact_id, PQ)),
        if_node("Deal exists?", "={{ $json.total }}", "number:gt", 0, [2880, R2 - 200]),
        http_node("HubSpot · update deal", "PATCH", hs_url("/crm/v3/objects/deals/{{ $json.results[0].id }}"),
                  [3120, R2 - 300], hs,
                  body="={{ JSON.stringify({ properties: { dealstage: %s.deal_stage, "
                       "amount: %s.amount ?? undefined, description: %s.summary } }) }}" % (PQ, PQ, PQ)),
        http_node("HubSpot · create deal", "POST", hs_url("/crm/v3/objects/deals"), [3120, R2 - 100], hs,
                  body="={{ JSON.stringify({ properties: { dealname: %s.deal_name, "
                       "pipeline: 'default', dealstage: %s.deal_stage, amount: %s.amount ?? undefined, "
                       "description: %s.summary }, "
                       "associations: [{ to: { id: %s }, types: [{ associationCategory: 'HUBSPOT_DEFINED', "
                       "associationTypeId: 3 }] }] }) }}" % (PQ, PQ, PQ, PQ, contact_id)),
        code(I, "Plan next step", [3360, R2]),
        # ---- row 3: actions + alert
        switch_node("Next step by fit", "={{ $json.action }}",
                    [("reply", "hot: reply now"), ("followup", "warm: follow-ups"), ("log", "cold / review: log only")],
                    [0, R3]),
        code(I, "Write instant reply", [240, R3 - 100]),
        code(I, "Plan follow-ups", [240, R3 + 220]),
        dt_insert("Queue follow-ups", "lia_followups", {
            k: "={{ $json.%s }}" % k for k in ("lead_id", "contact_id", "email", "name", "company", "step",
                                               "due_at", "status", "expected_status", "subject", "body")},
            [480, R3 + 220]),
        code(I, "Compose owner alert", [1440, R3]),
        unseal_node([1680, R3], c["private"]),
        telegram("Telegram · notify owner", [1920, R3], c["telegram"]),
        dt_update("Update lead row", "lia_leads", [("id", "eq", "={{ %s.row_id }}" % PN)], {
            "status": "processed", "fit": "={{ %s.fit }}" % PN, "score": "={{ %s.score }}" % PN,
            "summary": "={{ %s.summary }}" % PN, "service_interest": "={{ %s.service_interest }}" % PN,
            "llm_provider": "={{ %s.llm_provider }}" % PN, "contact_id": "={{ %s.contact_id }}" % PN,
            "deal_id": "={{ %s.deal_id }}" % PN, "review_reason": "={{ %s.review_reasons.join('; ') }}" % PN,
            "telegram_message_id": "={{ String($json.result?.message_id ?? $json.message_id ?? '') }}",
            "action": "={{ $('Compose owner alert').first().json.action_result }}"}, [2160, R3]),
    ] + [dict(n, position=[n["position"][0], n["position"][1] - 1400 + R3]) for n in ob_nodes] + [
        sticky("## LIA · Intake\nWebhook `POST /webhook/leads` (HMAC-signed), or the internal trigger (LIA · Form, "
               "replays) → signature, rate limit, spam and duplicate checks → **202 in milliseconds** → LLM "
               "qualification (primary, fallback) → output gate → HubSpot contact + note (+ deal for hot/warm) → "
               "hot: instant reply, warm: follow-ups, cold or review: log → Telegram to the owner.\n"
               "Every external call retries 3×; any failure goes to **LIA · Errors** (dead letter + alert).",
               [-40, -320], 820, 220, 4),
        sticky("### LLM with fallback, then an output gate\nLead text goes in as quoted data, never as instructions. "
               "A pre-filter flags injection attempts; the gate refuses automation when the answer is broken or "
               "tries something it may not (other addresses, links off the allowlist, prices). Those leads are "
               "*held for review*, never auto-emailed.", [-40, R2 - 330], 940, 170, 6),
        sticky("### Email safety rule\nOnly addresses matching `real_inbox_regex` are really emailed. Everything else is "
               "written to `lia_outbox` only.", [440, R3 - 330], 620, 130, 3),
    ]
    con = wire(
        ("Lead webhook", "Read raw body"), ("Read raw body", "Sign request (HMAC)"), ("Sign request (HMAC)", "Config"),
        ("Internal trigger", "Config"), ("Config", "Verify signature"), ("Verify signature", "Normalize & validate"),
        ("Normalize & validate", "Recent submissions"), ("Recent submissions", "Route intake"),
        ("Route intake", "Log it?"), ("Log it?", "Log lead", 0), ("Log it?", "Came from webhook?", 1),
        ("Log lead", "Came from webhook?"),
        ("Came from webhook?", "Respond 202 / 200 / 401 / 422 / 429", 0), ("Came from webhook?", "Needs qualification?", 1),
        ("Respond 202 / 200 / 401 / 422 / 429", "Needs qualification?"),
        ("Needs qualification?", "Build LLM request", 0), ("Build LLM request", "LLM · primary"),
        ("LLM · primary", "Check primary answer", 0), ("LLM · primary", "LLM · fallback", 1),
        ("Check primary answer", "Primary answer valid?"),
        ("Primary answer valid?", "Parse qualification", 0), ("Primary answer valid?", "LLM · fallback", 1),
        ("LLM · fallback", "Parse qualification"),
        ("Parse qualification", "Spam?"), ("Spam?", "Mark as spam", 0), ("Spam?", "HubSpot · upsert contact", 1),
        ("HubSpot · upsert contact", "HubSpot · add note"), ("HubSpot · add note", "HubSpot · account info"),
        ("HubSpot · account info", "Hot or warm?"),
        ("Hot or warm?", "HubSpot · find deal", 0), ("Hot or warm?", "Plan next step", 1),
        ("HubSpot · find deal", "Deal exists?"),
        ("Deal exists?", "HubSpot · update deal", 0), ("Deal exists?", "HubSpot · create deal", 1),
        ("HubSpot · update deal", "Plan next step"), ("HubSpot · create deal", "Plan next step"),
        ("Plan next step", "Next step by fit"),
        ("Next step by fit", "Write instant reply", 0), ("Next step by fit", "Plan follow-ups", 1),
        ("Next step by fit", "Compose owner alert", 2),
        ("Write instant reply", "Save to lia_outbox"),
        *ob_cons,
        ("Real inbox? (safety rule)", "Compose owner alert", 1), ("Mark delivered", "Compose owner alert"),
        ("Plan follow-ups", "Queue follow-ups"), ("Queue follow-ups", "Compose owner alert"),
        ("Compose owner alert", "Unseal owner chat"), ("Unseal owner chat", "Telegram · notify owner"),
        ("Telegram · notify owner", "Update lead row"),
    )
    return {"name": WF["intake"], "nodes": nodes, "connections": con, "settings": settings(err_id)}


# ================================================================ 2. FOLLOW-UPS
def build_followups(c, cfg, err_id):
    _WF_SCOPE[0] = "follow-ups"
    F = "follow-ups"
    fast = cfg["followup_unit_ms"] == 60000
    hs = ("hubspotAppToken", c["hubspot"])
    ob_nodes, ob_cons = outbox_chain(1680, 0, c["resend"])
    sched = "Every minute (DEMO_FAST)" if fast else "Every 15 minutes"
    nodes = [
        node(sched, "n8n-nodes-base.scheduleTrigger", 1.2,
             {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1 if fast else 15}]}}, [0, 0]),
        node("Run now", "n8n-nodes-base.manualTrigger", 1, {}, [0, 200]),
        config_node(cfg, [240, 100]),
        dt_get("Get due follow-ups", "lia_followups",
               [("status", "eq", "pending"), ("due_at", "lte", "={{ $now.toISO() }}")], [480, 100]),
        http_node("HubSpot · read contact", "GET",
                  hs_url("/crm/v3/objects/contacts/{{ $json.contact_id }}?properties=hs_lead_status,"
                         "hs_sales_email_last_replied"), [720, 100], hs, onError="continueRegularOutput"),
        code(F, "Still waiting for a reply?", [960, 100]),
        if_node("Send it?", "={{ $json.send }}", "boolean:true", "", [1200, 100]),
        code(F, "Write follow-up email", [1440, 0]),
        dt_update("Mark follow-up stopped", "lia_followups", [("id", "eq", "={{ $json.followup_id }}")],
                  {"status": "stopped", "note": "={{ $json.stop_reason }}"}, [1440, 300]),
    ] + ob_nodes + [
        node("Sent + outbox", "n8n-nodes-base.merge", 3, {"mode": "append"}, [2640, 100]),
        dt_update("Mark follow-up sent", "lia_followups", [("id", "eq", "={{ $json.followup_id }}")],
                  {"status": "sent", "sent_at": "={{ $now.toISO() }}",
                   "note": "={{ $json.delivered ? 'emailed' : 'outbox' }}"}, [2880, 100]),
        http_node("HubSpot · log follow-up", "POST", hs_url("/crm/v3/objects/notes"), [3120, 100], hs,
                  body="={{ JSON.stringify({ properties: { hs_timestamp: $now.toISO(), hs_note_body: "
                       "'<p><strong>Automatic follow-up #' + $json.step + '</strong> ' + ($json.note === 'emailed' ? 'sent' : 'saved to outbox') + "
                       "': ' + $json.subject + '</p>' }, associations: [{ to: { id: $json.contact_id }, "
                       "types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }] }] }) }}"),
        code(F, "Compose follow-up alert", [3360, 100]),
        unseal_node([3600, 100], c["private"]),
        telegram("Telegram · follow-ups sent", [3840, 100], c["telegram"]),
        sticky("## LIA · Follow-ups\nEvery 15 min: pending follow-ups that are due → check the contact in HubSpot → "
               "if the lead status is unchanged and they have not replied, send (safety rule applies) and log a note; "
               "otherwise mark the follow-up stopped.\n`DEMO_FAST=1`: days become minutes, runs every minute.",
               [-40, -300], 700, 190, 5),
    ]
    con = wire(
        (sched, "Config"), ("Run now", "Config"), ("Config", "Get due follow-ups"),
        ("Get due follow-ups", "HubSpot · read contact"), ("HubSpot · read contact", "Still waiting for a reply?"),
        ("Still waiting for a reply?", "Send it?"),
        ("Send it?", "Write follow-up email", 0), ("Send it?", "Mark follow-up stopped", 1),
        ("Write follow-up email", "Save to lia_outbox"), *ob_cons,
        ("Mark delivered", "Sent + outbox", 0, 0), ("Real inbox? (safety rule)", "Sent + outbox", 1, 1),
        ("Sent + outbox", "Mark follow-up sent"),
        ("Mark follow-up sent", "HubSpot · log follow-up"), ("HubSpot · log follow-up", "Compose follow-up alert"),
        ("Compose follow-up alert", "Unseal owner chat"), ("Unseal owner chat", "Telegram · follow-ups sent"),
    )
    return {"name": WF["followups"], "nodes": nodes, "connections": con, "settings": settings(err_id)}


# ================================================================ 3. FORM
def build_form(intake_id, err_id):
    _WF_SCOPE[0] = "form"
    form = node("Lead form", "n8n-nodes-base.formTrigger", 2.2, {
        "formTitle": "Tell us about your project",
        "formDescription": "We usually reply within one business hour.",
        "formFields": {"values": [
            {"fieldLabel": "Name", "requiredField": True},
            {"fieldLabel": "Email", "fieldType": "email", "requiredField": True},
            {"fieldLabel": "Company"},
            {"fieldLabel": "Website"},
            {"fieldLabel": "Budget", "fieldType": "dropdown", "fieldOptions": {"values": [
                {"option": "Not sure yet"}, {"option": "Under $3k"}, {"option": "$3k-$10k"},
                {"option": "$10k-$25k"}, {"option": "$25k+"}]}},
            {"fieldLabel": "Message", "fieldType": "textarea", "requiredField": True},
            {"fieldType": "hiddenField", "fieldName": "hp"}]},          # honeypot: humans never fill it
        "options": {"respondWithOptions": {"values": {
            "formSubmittedText": "Thanks! We got your message and will be in touch shortly."}}}},
        [0, 0], webhookId=env("FORM_PATH", "lia-contact"))
    nodes = [
        form,
        code("form", "To intake payload", [240, 0]),
        node("Hand off to Intake", "n8n-nodes-base.executeWorkflow", 1.2, {
            "source": "database", "workflowId": {"__rl": True, "mode": "id", "value": intake_id},
            "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                               "attemptToConvertTypes": False, "convertFieldsToString": True},
            "mode": "each", "options": {"waitForSubWorkflow": False}}, [480, 0]),
        sticky("## LIA · Form\nHosted n8n form for demos and landing pages. Hands the submission to **LIA · Intake** "
               "(same pipeline as the webhook: honeypot, rate limit per email, spam rules) and shows the thank-you "
               "page immediately.", [-40, -220], 600, 150, 4),
    ]
    con = wire(("Lead form", "To intake payload"), ("To intake payload", "Hand off to Intake"))
    return {"name": WF["form"], "nodes": nodes, "connections": con, "settings": settings(err_id)}


# ================================================================ 4. ERRORS + REPLAY
def build_errors(c, cfg):
    _WF_SCOPE[0] = "errors"
    E = "errors"
    nodes = [
        node("Error trigger", "n8n-nodes-base.errorTrigger", 1, {}, [0, 0]),
        config_node(cfg, [240, 0]),
        code(E, "Describe failure", [480, 0]),
        http_node("n8n · fetch failed run", "GET",
                  "={{ $('Config').first().json.n8n_api_url }}/executions/{{ $json.execution_id }}?includeData=true",
                  [720, 0], ("n8nApi", c["n8n"]), retry={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 2000},
                  onError="continueRegularOutput"),
        code(E, "Extract payload", [960, 0]),
        dt_insert("Save dead letter", "lia_dead_letter", {
            **{k: "={{ $json.%s }}" % k for k in ("workflow", "workflow_id", "node", "error", "execution_id",
                                                  "execution_url", "lead_id", "payload")},
            "status": "={{ $json.replayable ? 'new' : 'not_replayable' }}", "attempts": 0}, [1200, 0]),
        dt_update("Mark lead failed", "lia_leads", [("lead_id", "eq", "={{ $('Extract payload').first().json.lead_id || '-' }}")],
                  {"status": "failed", "note": "={{ 'failed at ' + $('Extract payload').first().json.node + ': ' + "
                                                "$('Extract payload').first().json.error.slice(0, 200) }}"},
                  [1440, 0], alwaysOutputData=True),
        code(E, "Compose alert", [1680, 0]),
        unseal_node([1920, 0], c["private"]),
        telegram("Telegram · alert owner", [2160, 0], c["telegram"]),
        sticky("## LIA · Errors\nError workflow of every LIA workflow. Failed run → original payload pulled from the "
               "execution → `lia_dead_letter` → lead marked failed → Telegram alert. Replay with **LIA · Replay dead letters**.",
               [-40, -260], 700, 170, 3),
    ]
    con = wire(("Error trigger", "Config"), ("Config", "Describe failure"), ("Describe failure", "n8n · fetch failed run"),
               ("n8n · fetch failed run", "Extract payload"), ("Extract payload", "Save dead letter"),
               ("Save dead letter", "Mark lead failed"), ("Mark lead failed", "Compose alert"),
               ("Compose alert", "Unseal owner chat"), ("Unseal owner chat", "Telegram · alert owner"))
    return {"name": WF["errors"], "nodes": nodes, "connections": con, "settings": settings()}


def build_replay(c, cfg, intake_id, err_id):
    _WF_SCOPE[0] = "replay-dead-letters"
    nodes = [
        node("Replay now", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
        config_node(cfg, [240, 0]),
        dt_get("Get open dead letters", "lia_dead_letter",
               [("status", "eq", "new"), ("workflow", "eq", WF["intake"])], [480, 0]),
        code("replay-dead-letters", "Prepare replay", [720, 0]),
        node("Replay through Intake", "n8n-nodes-base.executeWorkflow", 1.2, {
            "source": "database", "workflowId": {"__rl": True, "mode": "id", "value": intake_id},
            "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                               "attemptToConvertTypes": False, "convertFieldsToString": True},
            "mode": "each", "options": {"waitForSubWorkflow": True}}, [960, 0], onError="continueErrorOutput"),
        dt_update("Mark replayed", "lia_dead_letter", [("id", "eq", "={{ $('Prepare replay').item.json.replay_of }}")],
                  {"status": "replayed", "replayed_at": "={{ $now.toISO() }}",
                   "attempts": "={{ $('Prepare replay').item.json.attempts + 1 }}"}, [1200, -100]),
        dt_update("Mark replay failed", "lia_dead_letter", [("id", "eq", "={{ $('Prepare replay').item.json.replay_of }}")],
                  {"status": "={{ $('Prepare replay').item.json.attempts + 1 >= 3 ? 'replay_failed' : 'new' }}",
                   "attempts": "={{ $('Prepare replay').item.json.attempts + 1 }}"}, [1200, 100]),
        sticky("## LIA · Replay dead letters\nRe-runs failed Intake payloads through the same workflow "
               "(duplicate check and rate limit skipped for replays). Gives up after 3 attempts.", [-40, -240], 620, 150, 3),
    ]
    con = wire(("Replay now", "Config"), ("Config", "Get open dead letters"), ("Get open dead letters", "Prepare replay"),
               ("Prepare replay", "Replay through Intake"),
               ("Replay through Intake", "Mark replayed", 0), ("Replay through Intake", "Mark replay failed", 1))
    return {"name": WF["replay"], "nodes": nodes, "connections": con, "settings": settings(err_id)}


def build_all(c, cfg, ids=None):
    """All five workflow definitions, offline. ids: workflow ids to wire in (placeholders when None)."""
    ids = ids or {}
    err_id = ids.get("errors", "REPLACE_WITH_LIA_ERRORS_WORKFLOW_ID")
    intake_id = ids.get("intake", "REPLACE_WITH_LIA_INTAKE_WORKFLOW_ID")
    return {"errors": build_errors(c, cfg), "intake": build_intake(c, cfg, err_id),
            "followups": build_followups(c, cfg, err_id), "replay": build_replay(c, cfg, intake_id, err_id),
            "form": build_form(intake_id, err_id)}


# ================================================================ export
def export_filename(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("lia · ", "lia-")).strip("-") + ".json"


def sanitize(full, pub=None):
    """A live workflow -> a JSON safe to publish: credential ids, workflow ids, private Config values and the
    instance's static data are replaced or dropped. Pure function, covered by tests."""
    pub = pub or public_config()
    wf = {"name": full["name"], "nodes": copy.deepcopy(full["nodes"]),
          "connections": copy.deepcopy(full["connections"]),
          "settings": {k: v for k, v in (full.get("settings") or {}).items()
                       if k in ("executionOrder", "timezone", "saveDataErrorExecution", "saveDataSuccessExecution",
                                "saveManualExecutions", "saveExecutionProgress", "callerPolicy")}}
    if (full.get("settings") or {}).get("errorWorkflow"):
        wf["settings"]["errorWorkflow"] = "REPLACE_WITH_LIA_ERRORS_WORKFLOW_ID"
    for nd in wf["nodes"]:
        for ctype, ref in (nd.get("credentials") or {}).items():
            name = (ref or {}).get("name", "")
            nd["credentials"][ctype] = {"id": "REPLACE_ME", "name": name if name.startswith(PREFIX) else PREFIX + ctype}
        if nd["name"] == "Config":
            for a in nd["parameters"]["assignments"]["assignments"]:
                if a["name"] in pub:
                    a["value"] = pub[a["name"]]
                else:                                         # unknown key: never publish its value
                    a["value"] = ""
        if nd["type"] == "n8n-nodes-base.executeWorkflow":
            nd["parameters"]["workflowId"] = {"__rl": True, "mode": "id", "value": "REPLACE_WITH_LIA_INTAKE_WORKFLOW_ID"}
            nd["parameters"].pop("cachedResultUrl", None)
            nd["parameters"].pop("cachedResultName", None)
        if nd["type"] == "n8n-nodes-base.scheduleTrigger":
            nd["name"] = "Every 15 minutes"
            nd["parameters"]["rule"] = {"interval": [{"field": "minutes", "minutesInterval": 15}]}
    for k in list(wf["connections"]):                         # the schedule node may have been renamed
        if k.startswith("Every minute"):
            wf["connections"]["Every 15 minutes"] = wf["connections"].pop(k)
    return wf


def write_export(wf):
    os.makedirs(os.path.join(ROOT, "workflows"), exist_ok=True)
    fn = export_filename(wf["name"])
    with open(os.path.join(ROOT, "workflows", fn), "w", encoding="utf8", newline="\n") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("exported       ", fn)


def export():
    """Write sanitized copies of the live workflows to workflows/ (the Code node files are already the source)."""
    names = set(WF.values())
    for w in n8n.all("/workflows"):
        if w["name"] in names:
            write_export(sanitize(n8n("GET", "/workflows/%s" % w["id"])))


def main():
    global n8n
    load_env()
    if env("LIA_TARGET") == "docker" or "--bootstrap" in sys.argv:
        import bootstrap_n8n                       # fresh n8n: create the owner and an API key once
        bootstrap_n8n.bootstrap()
    n8n = N8n()
    ensure_tables()
    creds = ensure_credentials(update="--update-credentials" in sys.argv)
    cfg = config_values()
    ids = {"errors": upsert_workflow(build_errors(creds, cfg))}
    ids["intake"] = upsert_workflow(build_intake(creds, cfg, ids["errors"]))
    ids["followups"] = upsert_workflow(build_followups(creds, cfg, ids["errors"]))
    ids["replay"] = upsert_workflow(build_replay(creds, cfg, ids["intake"], ids["errors"]))
    ids["form"] = upsert_workflow(build_form(ids["intake"], ids["errors"]))
    for k in ("errors", "intake", "followups", "form"):
        n8n("POST", "/workflows/%s/activate" % ids[k])
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(os.path.join(ROOT, "logs", "workflow-ids-%s.json" % env("LIA_TARGET", "live")), "w") as f:
        json.dump(ids, f, indent=1)
    print("active: Errors, Intake, Follow-ups, Form   |  manual: Replay")
    print("webhook:", n8n.base + "/webhook/" + env("WEBHOOK_PATH", "leads"), "(auth: %s)" % cfg["webhook_auth"])
    print("form:   ", n8n.base + "/form/" + env("FORM_PATH", "lia-contact"))
    if "--export" in sys.argv:
        export()


if __name__ == "__main__":
    main()
