"""Shared helpers: .env loading, secrets, n8n / HubSpot REST clients (stdlib only)."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=None):
    """Load KEY=VALUE lines from .env into os.environ (existing variables win)."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:                                  # inline comment
            v = v.split(" #", 1)[0].strip()
        os.environ.setdefault(k, v)


def env(name, default=""):
    return os.environ.get(name, default)


def secret(name, required=True):
    """Value of NAME, or the contents of the file named by NAME_FILE."""
    v = os.environ.get(name, "").strip()
    if not v:
        p = os.environ.get(name + "_FILE", "").strip()
        if p:
            p = p if os.path.isabs(p) else os.path.join(ROOT, p)
            if os.path.exists(p):
                v = open(p, encoding="utf8").read().strip()
    if not v and required:
        raise SystemExit("missing secret: set %s or %s_FILE" % (name, name))
    return v


def http(method, url, body=None, headers=None, timeout=60):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode("utf8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:500].decode("utf8", "replace")}


class N8n:
    def __init__(self):
        self.base = env("N8N_URL", "http://127.0.0.1:5678").rstrip("/")
        self.key = secret("N8N_API_KEY")

    def __call__(self, method, path, body=None, ok=(200, 201, 204)):
        code, res = http(method, self.base + "/api/v1" + path, body, {"X-N8N-API-KEY": self.key})
        if code not in ok:
            raise RuntimeError("n8n %s %s -> %s %s" % (method, path, code, json.dumps(res)[:600]))
        return res

    def all(self, path):
        out, cursor = [], None
        while True:
            sep = "&" if "?" in path else "?"
            res = self("GET", path + sep + "limit=250" + ("&cursor=" + urllib.parse.quote(cursor) if cursor else ""))
            out += res.get("data", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def table_id(self, name):
        for t in self.all("/data-tables"):
            if t["name"] == name:
                return t["id"]
        return None

    def rows(self, table):
        tid = self.table_id(table)
        return self.all("/data-tables/%s/rows" % tid) if tid else []


class HubSpot:
    BASE = "https://api.hubapi.com"

    def __init__(self):
        self.token = secret("HUBSPOT_TOKEN")

    def __call__(self, method, path, body=None, ok=(200, 201, 204, 207)):
        code, res = http(method, self.BASE + path, body, {"Authorization": "Bearer " + self.token})
        if code not in ok:
            raise RuntimeError("HubSpot %s %s -> %s %s" % (method, path, code, json.dumps(res)[:400]))
        return res
