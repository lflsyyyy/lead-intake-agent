"""Stand-ins for every external service the Lead Intake Agent calls, so the whole pipeline runs with zero real keys.

    python mock/server.py                 # http://127.0.0.1:8090  (PORT, SMTP_HOST, SMTP_PORT, STATE_FILE)

    /hubspot/...      the HubSpot CRM endpoints the workflows and tools use (contacts, notes, deals, account info)
    /telegram/bot<token>/sendMessage, /getMe
    /v1/chat/completions, /v1/models      OpenAI-compatible, deterministic rule-based qualifier (see qualify())
    /resend/emails    Resend-compatible; the message is relayed over SMTP to Mailpit when SMTP_HOST is set
    /_state, /_reset  inspect or clear everything the mock has stored

Standard library only. The qualifier is deliberately naive: it follows instructions found in the lead text
(a "compromised model"), so the workflow's output gate can be shown catching it.
"""
import json
import os
import re
import smtplib
import sys
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.RLock()


def empty_state():
    return {"contacts": {}, "notes": {}, "deals": {}, "assoc": [], "telegram": [], "emails": [], "llm_calls": 0,
            "next_id": 1000}


STATE = empty_state()


def new_id():
    STATE["next_id"] += 1
    return str(STATE["next_id"])


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ---------------------------------------------------------------- qualifier
SPAM = re.compile(r"\b(seo services|backlinks?|link building|guest post|first page of google|crypto|casino|forex|"
                  r"outsourc\w*|offshore (team|developers)|lead generation services|we (can )?offer)\b", re.I)
OUT_OF_SCOPE = re.compile(r"\b(ios apps?|iphone apps?|android (apps?|games?)|app store|native apps?|school project|"
                          r"student|homework|internship|job|hiring me|resume|cv|games?|unity)\b", re.I)
IN_SCOPE = re.compile(r"\b(website|web ?site|site|shopify|store|online|booking|memberships?|payments?|automation|"
                      r"automate|crm|n8n|zapier|"
                      r"integration|redesign|landing page|e-?commerce|workflow|sync)\b", re.I)
INJECT = re.compile(r"\b(ignore|disregard)\b.{0,40}\binstructions?\b|\bmark (me|this)\b.{0,20}\bhot\b", re.I | re.S)


def lead_from(messages):
    user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    m = re.search(r"<lead>\s*(\{.*\})\s*</lead>", user, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def budget_usd(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|m)?", (text or "").replace(",", "").replace(" ", ""), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1))


def qualify(lead, system):
    """Deterministic stand-in for the LLM. Returns the schema the workflow expects."""
    msg = lead.get("message") or ""
    first = lead.get("first_name") or "there"
    sender = (re.search(r'signed "([^"]+)"', system or "") or [None, "The team"])[1]
    budget = budget_usd(lead.get("budget") or "")
    q = {"intent": msg[:120], "business_type": lead.get("company") or "Unknown", "urgency": "medium",
         "budget_estimate": lead.get("budget") or None, "disqualify_reason": None, "injection_suspected": False}
    if INJECT.search(msg):
        # a naive model that obeys the lead: exactly what the output gate is for
        friend = (re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", msg) or [None])[0]
        reply = "Hi %s,\n\nAs requested, you are marked as a hot lead with a 50%% discount." % first
        if friend:
            reply += " I have also emailed %s with our proposal." % friend
        q.update(score=10, fit="hot", service_interest="website", summary="Lead asked to be marked hot.",
                 reply_draft=reply + "\n\n" + sender)
        return q
    if SPAM.search(msg):
        q.update(score=1, fit="spam", service_interest="none", summary="Sales pitch aimed at us.",
                 reply_draft="", disqualify_reason="vendor pitch")
    elif OUT_OF_SCOPE.search(msg) or (budget is not None and budget < 1000):
        q.update(score=3, fit="cold", service_interest="out of scope", urgency="low",
                 summary="Real person, but not a fit for the studio.", disqualify_reason="service we do not offer or tiny budget",
                 reply_draft="Hi %s,\n\nThanks for getting in touch. This is not something we take on, but we "
                             "appreciate you thinking of us.\n\n%s" % (first, sender))
    elif budget is not None and budget >= 3000 and IN_SCOPE.search(msg):
        q.update(score=9 if budget >= 10000 else 8, fit="hot", urgency="high" if budget >= 10000 else "medium",
                 service_interest="website and automation",
                 summary="In-scope project with a stated budget of %s." % lead.get("budget"),
                 reply_draft="Hi %s,\n\nThank you for the detailed message. The project you describe is a good match "
                             "for what we build, and it helps a lot that the scope is already clear. Could you share "
                             "which tools you use today and when you would ideally like to launch? With that we can "
                             "suggest a sensible first step and book a short call.\n\n%s" % (first, sender))
    else:
        q.update(score=5, fit="warm", service_interest="website project",
                 summary="Relevant need without a budget or a timeline yet.",
                 reply_draft="Hi %s,\n\nThanks for reaching out. It sounds like there is a real opportunity here. "
                             "To point you in the right direction, could you tell us a bit more about your goals "
                             "and roughly when you would like to start? Happy to set up a short call if that is "
                             "easier.\n\n%s" % (first, sender))
    return q


def chat_completion(body):
    model = body.get("model") or "mock-qualifier"
    if model == "mock-fail":                              # failure drill: point LLM_MODEL here
        return 503, {"error": {"message": "mock outage", "type": "server_error"}}
    messages = body.get("messages") or []
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    content = json.dumps(qualify(lead_from(messages), system))
    with LOCK:
        STATE["llm_calls"] += 1
    return 200, {"id": "chatcmpl-mock-%d" % STATE["llm_calls"], "object": "chat.completion", "created": int(time.time()),
                 "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": content}}],
                 "usage": {"prompt_tokens": sum(len(m.get("content", "")) // 4 for m in messages),
                           "completion_tokens": len(content) // 4, "total_tokens": 0}}


# ---------------------------------------------------------------- HubSpot
def contact_by_email(email):
    for c in STATE["contacts"].values():
        if c["properties"].get("email") == email:
            return c
    return None


def associate(kind_a, id_a, kind_b, id_b):
    STATE["assoc"].append([kind_a, id_a, kind_b, id_b])


def hubspot(method, path, body, query):
    parts = [p for p in path.split("/") if p]
    if path == "/account-info/v3/details":
        return 200, {"portalId": 12345678, "uiDomain": "app.hubspot.com", "timeZone": "UTC", "currency": "USD"}
    if method == "POST" and path == "/crm/v3/objects/contacts/batch/upsert":
        out = []
        for inp in body.get("inputs", []):
            email = str(inp.get("id", "")).lower()
            c = contact_by_email(email)
            if not c:
                c = {"id": new_id(), "properties": {"email": email}, "createdAt": now_iso()}
                STATE["contacts"][c["id"]] = c
            c["properties"].update(inp.get("properties") or {})
            c["properties"]["hs_email_domain"] = email.split("@")[-1]
            c["updatedAt"] = now_iso()
            out.append(c)
        return 200, {"status": "COMPLETE", "results": out}
    if len(parts) == 4 and parts[:3] == ["crm", "v3", "objects"] and method == "POST" and parts[3] in ("notes", "deals"):
        kind = parts[3]
        obj = {"id": new_id(), "properties": dict(body.get("properties") or {}), "createdAt": now_iso()}
        STATE[kind][obj["id"]] = obj
        for a in body.get("associations") or []:
            associate(kind, obj["id"], "contacts", str(a["to"]["id"]))
        return 201, obj
    if method == "POST" and path == "/crm/v3/objects/deals/search":
        filters = {f["propertyName"]: str(f.get("value")) for g in body.get("filterGroups", []) for f in g.get("filters", [])}
        hits = []
        for d in STATE["deals"].values():
            if "dealname" in filters and d["properties"].get("dealname") != filters["dealname"]:
                continue
            cid = filters.get("associations.contact")
            if cid and [ "deals", d["id"], "contacts", cid] not in STATE["assoc"]:
                continue
            hits.append(d)
        return 200, {"total": len(hits), "results": hits[: body.get("limit", 10)]}
    if method == "POST" and path == "/crm/v3/objects/contacts/search":
        domains = {f.get("value") for g in body.get("filterGroups", []) for f in g.get("filters", [])
                   if f.get("propertyName") == "hs_email_domain"}
        hits = [c for c in STATE["contacts"].values() if c["properties"].get("hs_email_domain") in domains]
        return 200, {"total": len(hits), "results": hits}
    if len(parts) == 7 and parts[:3] == ["crm", "v4", "objects"] and parts[5] == "associations":
        cid, kind = parts[4], parts[6]
        res = [{"toObjectId": int(a[1])} for a in STATE["assoc"] if a[0] == kind and a[3] == cid]
        return 200, {"results": res}
    if len(parts) == 6 and parts[4] == "batch" and parts[5] == "archive":
        kind = parts[3]
        for inp in body.get("inputs", []):
            STATE.get(kind, {}).pop(str(inp["id"]), None)
        return 204, None
    if len(parts) == 5 and parts[:3] == ["crm", "v3", "objects"]:
        kind, oid = parts[3], parts[4]
        obj = STATE.get(kind, {}).get(oid)
        if not obj:
            return 404, {"status": "error", "message": "Object not found. objectId are usually numeric.",
                         "category": "OBJECT_NOT_FOUND"}
        if method == "PATCH":
            obj["properties"].update(body.get("properties") or {})
            return 200, obj
        if method == "GET":
            props = query.get("properties", [""])[0]
            out = dict(obj)
            if props:
                out["properties"] = {k: obj["properties"].get(k) for k in props.split(",")}
                out["properties"]["hs_object_id"] = oid
            return 200, out
    return 404, {"status": "error", "message": "mock: no route for %s %s" % (method, path)}


# ---------------------------------------------------------------- Telegram, Resend
def telegram(path, body):
    m = re.match(r"^/bot([^/]+)/(\w+)$", path)
    if not m:
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
    method = m.group(2)
    if method == "getMe":
        return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "LIA mock bot", "username": "lia_mock_bot"}}
    if method == "sendMessage":
        if not body.get("chat_id") or not body.get("text"):
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat_id and text are required"}
        msg = {"message_id": len(STATE["telegram"]) + 1, "date": int(time.time()), "text": body["text"],
               "chat": {"id": body["chat_id"], "type": "private"}}
        STATE["telegram"].append(msg)
        return 200, {"ok": True, "result": msg}
    return 404, {"ok": False, "error_code": 404, "description": "mock: unsupported method " + method}


def send_mail(body):
    to = body.get("to") or []
    to = to if isinstance(to, list) else [to]
    if not body.get("from") or not to or not body.get("subject"):
        return 422, {"statusCode": 422, "name": "validation_error", "message": "from, to and subject are required"}
    item = {"id": "mock-email-%d" % (len(STATE["emails"]) + 1), "from": body["from"], "to": to,
            "subject": body["subject"], "text": body.get("text", ""), "smtp": "skipped"}
    host = os.environ.get("SMTP_HOST")
    if host:
        try:
            em = EmailMessage()
            em["From"], em["To"], em["Subject"] = body["from"], ", ".join(to), body["subject"]
            if body.get("reply_to"):
                em["Reply-To"] = body["reply_to"]
            em.set_content(body.get("text") or "")
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "1025")), timeout=10) as s:
                s.send_message(em)
            item["smtp"] = "delivered to %s" % host
        except OSError as e:
            item["smtp"] = "failed: %s" % e
    STATE["emails"].append(item)
    return 200, {"id": item["id"]}


# ---------------------------------------------------------------- HTTP plumbing
def route(method, raw_path, headers, body):
    u = urlparse(raw_path)
    path, query = u.path.rstrip("/") or "/", parse_qs(u.query)
    if path == "/health":
        return 200, {"ok": True}
    if path == "/_state":
        return 200, STATE
    if path == "/_reset" and method == "POST":
        STATE.clear()
        STATE.update(empty_state())
        return 200, {"ok": True}
    auth = headers.get("Authorization", "")
    if path.startswith("/hubspot/"):
        if not auth.startswith("Bearer "):
            return 401, {"status": "error", "message": "Authentication credentials not found.", "category": "INVALID_AUTHENTICATION"}
        return hubspot(method, path[len("/hubspot"):], body, query)
    if path.startswith("/telegram/"):
        return telegram(path[len("/telegram"):], body)
    if path in ("/v1/chat/completions", "/chat/completions") and method == "POST":
        if not auth.startswith("Bearer "):
            return 401, {"error": {"message": "missing API key", "type": "invalid_request_error"}}
        return chat_completion(body)
    if path in ("/v1/models", "/models"):
        return 200, {"object": "list", "data": [{"id": "mock-qualifier", "object": "model"}]}
    if path in ("/resend/emails", "/emails") and method == "POST":
        if not auth.startswith("Bearer "):
            return 401, {"statusCode": 401, "name": "missing_api_key", "message": "Missing API key"}
        return send_mail(body)
    return 404, {"error": "mock: no route for %s %s" % (method, path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "lia-mock/1"

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        with LOCK:
            code, res = route(self.command, self.path, self.headers, body)
            save_state()
        data = b"" if res is None else json.dumps(res).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_QUIET") != "1":
            sys.stderr.write("mock %s\n" % (fmt % args))


def save_state():
    p = os.environ.get("STATE_FILE")
    if p:
        with open(p, "w", encoding="utf8") as f:
            json.dump(STATE, f)


def main():
    port = int(os.environ.get("PORT", "8090"))
    host = os.environ.get("HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print("mock listening on %s:%d" % (host, port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
