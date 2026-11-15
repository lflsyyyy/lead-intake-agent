import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
for p in (TOOLS, os.path.join(ROOT, "eval"), os.path.join(ROOT, "mock")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAKE_CREDS = {k: {"id": "cred-" + k, "name": "LIA · " + k} for k in
              ("llm", "llm_fallback", "hubspot", "telegram", "n8n", "resend", "webhook_hmac", "private", "webhook_token")}


def exported():
    """{filename: workflow json} for workflows/*.json"""
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "workflows", "*.json"))):
        with open(p, encoding="utf8") as f:
            out[os.path.basename(p)] = json.load(f)
    return out


def code_nodes(wf):
    return [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code"]


def private_words():
    """Words that must never appear in published files (the owner's real brand, domains, names).
    Kept out of the repository: secrets/private-words.txt (gitignored) or LIA_PRIVATE_WORDS, comma-separated."""
    words = [w for w in os.environ.get("LIA_PRIVATE_WORDS", "").split(",") if w.strip()]
    p = os.path.join(ROOT, "secrets", "private-words.txt")
    if os.path.exists(p):
        with open(p, encoding="utf8") as f:
            words += [w.strip() for w in f if w.strip() and not w.startswith("#")]
    return [w.lower() for w in words]
