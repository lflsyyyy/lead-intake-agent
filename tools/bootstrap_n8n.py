"""First-run setup for a fresh n8n (the Docker stack): owner account + public API key.

    python tools/bootstrap_n8n.py --target docker

A brand-new n8n has no users and no API key, and the public API cannot create either. This script uses
n8n's own REST endpoints (the ones the setup screen calls): it creates the owner if the instance has none,
logs in and creates an API key, then saves it to the file named by N8N_API_KEY_FILE.
It does nothing if that file already holds a working key. Only meant for local demo stacks.
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, env, load_env  # noqa: E402


class Session:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "Accept": "application/json", "browser-id": "lia-bootstrap"}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=30) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        try:                                    # n8n answers 200 with an HTML stub while it is still starting
            return code, (json.loads(raw) if raw else {})
        except ValueError:
            return code, {"raw": raw[:300].decode("utf8", "replace")}


def key_works(base, key):
    req = urllib.request.Request(base + "/api/v1/workflows?limit=1", headers={"X-N8N-API-KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def key_path():
    p = env("N8N_API_KEY_FILE") or ".docker/n8n-api.key"
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def bootstrap():
    base = env("N8N_URL", "http://127.0.0.1:5678").rstrip("/")
    path = key_path()
    if os.path.exists(path) and key_works(base, open(path, encoding="utf8").read().strip()):
        print("api key ok      ", os.path.relpath(path, ROOT))
        return
    email = env("N8N_OWNER_EMAIL", "owner@example.com")
    password = env("N8N_OWNER_PASSWORD")
    if not password:
        raise SystemExit("set N8N_OWNER_PASSWORD (demo stack only)")
    s = Session(base)
    code, res = 0, {}
    for _ in range(90):                         # a fresh container needs a few seconds before it answers
        try:
            code, res = s("GET", "/rest/settings")
        except (urllib.error.URLError, OSError):  # refused, or reset by Docker's port proxy while n8n starts
            code, res = 0, {}
        if code == 200 and isinstance(res.get("data"), dict):
            break
        time.sleep(2)
    if code != 200:
        raise SystemExit("n8n is not reachable at %s (HTTP %s)" % (base, code))
    if (res.get("data") or {}).get("userManagement", {}).get("showSetupOnFirstLoad"):
        code, res = s("POST", "/rest/owner/setup", {"email": email, "firstName": "Demo", "lastName": "Owner",
                                                    "password": password})
        if code != 200:
            raise SystemExit("owner setup failed: HTTP %s %s" % (code, json.dumps(res)[:300]))
        print("owner created   ", email)
    else:
        code, res = s("POST", "/rest/login", {"emailOrLdapLoginId": email, "password": password})
        if code != 200:
            raise SystemExit("login failed: HTTP %s (does N8N_OWNER_EMAIL/PASSWORD match the instance?)" % code)
    code, res = s("GET", "/rest/api-keys/scopes")
    scopes = res.get("data") if code == 200 and isinstance(res.get("data"), list) else []
    code, res = s("POST", "/rest/api-keys", {"label": "lia-build", "expiresAt": None, "scopes": scopes})
    raw = (res.get("data") or {}).get("rawApiKey") or (res.get("data") or {}).get("apiKey")
    if code not in (200, 201) or not raw:
        raise SystemExit("api key creation failed: HTTP %s %s" % (code, json.dumps(res)[:300]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as f:
        f.write(raw)
    print("api key saved   ", os.path.relpath(path, ROOT))


def main():
    load_env()
    bootstrap()


if __name__ == "__main__":
    main()
