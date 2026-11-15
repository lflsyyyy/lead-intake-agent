"""Delete the demo records so the sample run can be repeated on a clean CRM.

    python tools/cleanup_hubspot.py --dry-run   # show what would be deleted
    python tools/cleanup_hubspot.py             # delete contacts @example.com / @resend.dev + their deals and notes
    python tools/cleanup_hubspot.py --tables    # ...and empty the lia_* data tables in n8n

Only contacts whose email domain is a reserved test domain are touched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HubSpot, N8n, load_env  # noqa: E402

TEST_DOMAINS = ("example.com", "resend.dev")
TABLES = ("lia_leads", "lia_followups", "lia_outbox", "lia_dead_letter")


def test_contacts(hs):
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": [{"propertyName": "hs_email_domain", "operator": "EQ", "value": d}]}
                                 for d in TEST_DOMAINS],
                "properties": ["email"], "limit": 100}
        if after:
            body["after"] = after
        res = hs("POST", "/crm/v3/objects/contacts/search", body)
        out += res.get("results", [])
        after = (res.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    # belt and braces: never touch anything that is not on a test domain
    return [c for c in out if (c["properties"].get("email") or "").lower().split("@")[-1] in TEST_DOMAINS]


def associated(hs, cid, kind):
    res = hs("GET", "/crm/v4/objects/contacts/%s/associations/%s?limit=500" % (cid, kind))
    return [str(r["toObjectId"]) for r in res.get("results", [])]


def archive(hs, kind, ids):
    for i in range(0, len(ids), 100):
        hs("POST", "/crm/v3/objects/%s/batch/archive" % kind, {"inputs": [{"id": x} for x in ids[i:i + 100]]})


def main():
    load_env()
    dry = "--dry-run" in sys.argv
    hs = HubSpot()
    contacts = test_contacts(hs)
    deals, notes = set(), set()
    for c in contacts:
        deals.update(associated(hs, c["id"], "deals"))
        notes.update(associated(hs, c["id"], "notes"))
    print("contacts: %d  deals: %d  notes: %d" % (len(contacts), len(deals), len(notes)))
    for c in contacts:
        print("  ", c["id"], c["properties"]["email"])
    if dry:
        print("dry run, nothing deleted")
    else:
        archive(hs, "deals", sorted(deals))
        archive(hs, "notes", sorted(notes))
        archive(hs, "contacts", [c["id"] for c in contacts])
        print("deleted")

    if "--tables" in sys.argv:
        n8n = N8n()
        for t in TABLES:
            tid = n8n.table_id(t)
            if not tid:
                continue
            count = len(n8n.rows(t))
            if not dry:
                n8n("DELETE", "/data-tables/%s/rows/clear" % tid)
            print("table %-16s %d rows %s" % (t, count, "would be cleared" if dry else "cleared"))


if __name__ == "__main__":
    main()
