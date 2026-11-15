"""Post 10 realistic sample leads to the Intake webhook and verify what happened.

    python tools/send_samples.py            # all 10
    python tools/send_samples.py --only 1,9 # a subset (1-based)

Every person and company is fictional. Emails use example.com (never delivered: the safety rule writes
them to lia_outbox) or Resend's test inbox delivered+<label>@resend.dev (accepted by Resend, goes nowhere).
Checks, per lead: webhook response, final status in lia_leads, HubSpot contact/deal exist (via API),
Telegram message id recorded, email sent or kept in the outbox, follow-ups queued.
"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HubSpot, N8n, env, http, load_env  # noqa: E402

SAMPLES = [
    ("hot", {"name": "Priya Natarajan", "email": "delivered+priya@resend.dev", "company": "Fernhill Dental Group",
             "website": "https://www.fernhilldental.example/", "budget": "$15k-$25k",
             "message": "We're opening our fourth clinic in March and need a new website with online booking that syncs "
                        "with our practice software, plus automated appointment reminders. Budget is approved and I'd "
                        "like to start in December. Are you free for a call on Thursday?",
             "utm": {"source": "google", "medium": "cpc", "campaign": "dental-websites"}}),
    ("hot", {"name": "Hannah Lee", "email": "delivered+hannah@resend.dev", "company": "Lumen Yoga Studio",
             "website": "lumenyoga.example", "budget": "$3k-$10k",
             "message": "Looking at options to sell class packs and memberships online instead of at the front desk. "
                        "Not in a rush, maybe in Q1."}),
    ("warm", {"name": "Sofia Alvarez", "email": "delivered+sofia@resend.dev", "company": "Casa Verde Interiors",
              "website": "casaverde.example",
              "message": "Our website is about eight years old and looks broken on phones. We're thinking about a "
                         "redesign sometime next year. What would that involve?",
              "utm_source": "instagram", "utm_medium": "social"}),
    ("warm", {"name": "Tom Becker", "email": "tom.becker@example.com", "company": "Becker & Sons Plumbing",
              "budget": "Not sure yet",
              "message": "We get leads from our website and from directories, but they sit in an inbox for hours. Is "
                         "there a way to get them into a CRM and text us right away?"}),
    ("warm", {"name": "Marcus Webb", "email": "marcus.webb@example.com", "company": "Ridgeline Outdoor Co.",
              "website": "ridgelineoutdoor.example", "source": "referral",
              "message": "We sell camping gear on Shopify. Orders are copied by hand into ShipStation and a Google "
                         "Sheet, which gets messy every peak season. Curious whether this could be automated at some "
                         "point. What does a project like that usually look like?"}),
    ("cold", {"name": "Jake Morrison", "email": "jake.morrison@example.com", "budget": "Under $3k",
              "message": "hi, i'm a student and need a website for a school project, can you make it for $50? due friday"}),
    ("cold", {"name": "Olivia Grant", "email": "olivia@example.com", "company": "Grant Tutoring",
              "message": "Do you build native iPhone apps? We need an iOS app with in-app purchases published on the App "
                         "Store."}),
    ("spam", {"name": "Best Deals", "email": "promo@example.com", "hp": "http://cheap-offers.example",
              "message": "Great site!!! Check our offer today."}),
    ("spam", {"name": "Ravi from RankBoost", "email": "ravi@rankboost.example.com", "company": "RankBoost Digital",
              "message": "Hello, I checked your website and it is not ranking on the first page of Google. We offer "
                         "affordable SEO services and backlink packages from $99/month. Reply for a free audit."}),
    ("duplicate", None),    # same as #1, sent again a few seconds later
]


def main():
    load_env()
    n8n, hs = N8n(), HubSpot()
    url = env("N8N_URL", "http://127.0.0.1:5678").rstrip("/") + "/webhook/" + env("WEBHOOK_PATH", "leads")
    only = None
    if "--only" in sys.argv:
        only = {int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")}

    sent = []
    for i, (expect, body) in enumerate(SAMPLES, 1):
        if only and i not in only:
            continue
        if body is None:
            body = SAMPLES[0][1]
            time.sleep(2)
        t0 = time.time()
        code, res = http("POST", url, body, timeout=30)
        ms = int((time.time() - t0) * 1000)
        print("#%-2d %-9s -> HTTP %s in %4d ms  %s" % (i, expect, code, ms, res))
        sent.append({"n": i, "expect": expect, "email": body["email"], "code": code, "res": res, "ms": ms})
        time.sleep(1.5)

    # wait for background processing (the webhook already answered)
    want = {s["res"].get("lead_id") for s in sent if s["res"].get("status") == "accepted"}
    deadline = time.time() + 240
    rows = {}
    while time.time() < deadline:
        rows = {r["lead_id"]: r for r in n8n.rows("lia_leads") if r.get("lead_id") in want}
        if len(rows) == len(want) and all(r["status"] in ("processed", "spam", "failed") for r in rows.values()):
            break
        time.sleep(3)
    outbox = n8n.rows("lia_outbox")
    followups = n8n.rows("lia_followups")

    print()
    hdr = "%-3s %-9s %-26s %-5s %-6s %-9s %-14s %-13s %-8s %-18s %s" % (
        "#", "expected", "email", "http", "fit", "status", "hs contact", "hs deal", "tg msg", "email", "check")
    print(hdr)
    print("-" * len(hdr))
    failures = 0
    for s in sent:
        lid = s["res"].get("lead_id")
        problems = []
        if s["expect"] == "duplicate":
            ok = s["code"] == 200 and s["res"].get("status") == "duplicate"
            failures += not ok
            print("%-3s %-9s %-26s %-5s %-6s %-9s %-14s %-13s %-8s %-18s %s" % (
                s["n"], s["expect"], s["email"][:26], s["code"], "-", "duplicate", "-", "-", "-", "-",
                "ok" if ok else "FAIL"))
            continue
        r = rows.get(lid, {})
        fit, status = r.get("fit", ""), r.get("status", "?")
        if s["code"] != 202:
            problems.append("http")
        if status not in ("processed", "spam"):
            problems.append("status=" + status)
        cid, did = r.get("contact_id") or "", r.get("deal_id") or ""
        if status == "processed":
            try:
                hs("GET", "/crm/v3/objects/contacts/%s" % cid)
            except Exception:
                problems.append("no contact")
            if fit in ("hot", "warm"):
                try:
                    hs("GET", "/crm/v3/objects/deals/%s" % did)
                except Exception:
                    problems.append("no deal")
            if not r.get("telegram_message_id"):
                problems.append("no telegram")
        mail = "-"
        ob = [o for o in outbox if o.get("lead_id") == lid]
        if fit == "hot":
            mail = ("sent" if ob and ob[0].get("delivered") else "outbox") if ob else "MISSING"
            if not ob:
                problems.append("no reply")
        elif fit == "warm":
            fu = [f for f in followups if f.get("lead_id") == lid]
            mail = "%d follow-ups queued" % len(fu)
            if len(fu) < 1:
                problems.append("no follow-ups")
        if fit != s["expect"]:
            problems.append("fit differs (llm)")
        hard = [p for p in problems if not p.startswith("fit differs")]
        failures += bool(hard)
        print("%-3s %-9s %-26s %-5s %-6s %-9s %-14s %-13s %-8s %-18s %s" % (
            s["n"], s["expect"], s["email"][:26], s["code"], fit or "-", status, cid or "-", did or "-",
            r.get("telegram_message_id") or "-", mail, "ok" if not problems else ", ".join(problems)))
    print()
    print("webhook answered in %d-%d ms; pipeline checks failed: %d" % (
        min(s["ms"] for s in sent), max(s["ms"] for s in sent), failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
