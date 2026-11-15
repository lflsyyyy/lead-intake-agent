"""Tiny OpenAI-compatible bridge to a local Claude Code CLI.

Lets any tool that speaks the OpenAI Chat Completions API (n8n HTTP Request,
LangChain, curl...) use a locally logged-in `claude` CLI as the model backend.
Useful for demos and development when no paid API key is available; in
production point the workflow at OpenAI / Groq / OpenRouter instead.

    python -I -X utf8 bridge/claude_bridge.py            # 127.0.0.1:41220

Endpoints
    GET  /health
    GET  /v1/models
    POST /v1/chat/completions   (non-streaming; supports response_format=json_object)

Environment
    BRIDGE_HOST / BRIDGE_PORT   bind address (default 127.0.0.1:41220)
    BRIDGE_API_KEY              if set, requests must send "Authorization: Bearer <key>"
    BRIDGE_KEY_FILE             alternative: read the key from this file
    CLAUDE_BIN                  path to the claude executable (default: found on PATH)
    BRIDGE_DEFAULT_MODEL        haiku | sonnet (default haiku)
    BRIDGE_MAX_CONCURRENCY      parallel CLI processes (default 3)
    BRIDGE_TIMEOUT              seconds per completion (default 120)
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "logs")
WORK_DIR = os.path.join(HERE, ".work")          # empty cwd: no project context leaks into the model

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "41220"))
DEFAULT_MODEL = os.environ.get("BRIDGE_DEFAULT_MODEL", "haiku")
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "120"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("BRIDGE_MAX_CONCURRENCY", "3")))
ALLOWED_MODELS = ("haiku", "sonnet")
MAX_BODY = 512 * 1024

JSON_RULE = ("Respond with exactly one valid JSON object and nothing else: "
             "no prose, no markdown, no code fences.")

log = logging.getLogger("bridge")


def load_key():
    key = os.environ.get("BRIDGE_API_KEY", "").strip()
    path = os.environ.get("BRIDGE_KEY_FILE", "").strip()
    if not key and path and os.path.exists(path):
        key = open(path, encoding="utf8").read().strip()
    return key


API_KEY = load_key()


def claude_bin():
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found (set CLAUDE_BIN)")
    return exe


def pick_model(requested):
    r = (requested or "").lower()
    for m in ALLOWED_MODELS:
        if m in r:
            return m
    return DEFAULT_MODEL


def text_of(content):
    """OpenAI content can be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def render(messages):
    """Split OpenAI messages into (system prompt, prompt for the CLI)."""
    system = [text_of(m.get("content")) for m in messages if m.get("role") in ("system", "developer")]
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not turns:
        raise ValueError("messages must contain at least one user message")
    if len(turns) == 1:
        prompt = text_of(turns[0].get("content"))
    else:
        prompt = "\n\n".join("%s: %s" % ("User" if m["role"] == "user" else "Assistant", text_of(m.get("content")))
                             for m in turns) + "\n\nAssistant:"
    return "\n\n".join(s for s in system if s).strip(), prompt


def extract_json(text):
    """Return the first JSON object found in text, or None."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    return None


def run_claude(system, prompt, model):
    os.makedirs(WORK_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf8", dir=WORK_DIR) as f:
        f.write(system or "You are a helpful assistant.")
        sys_file = f.name
    cmd = [claude_bin(), "-p", "--model", model, "--system-prompt-file", sys_file,
           "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--no-session-persistence", "--output-format", "json"]
    try:
        with SLOTS:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf8",
                               timeout=TIMEOUT, cwd=WORK_DIR,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        try:
            os.unlink(sys_file)
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError("claude exited %s: %s" % (r.returncode, (r.stderr or r.stdout or "")[:300]))
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError("claude error: %s" % str(out.get("result"))[:300])
    usage = out.get("usage") or {}
    return out.get("result") or "", {
        "prompt_tokens": int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0)),
        "completion_tokens": int(usage.get("output_tokens", 0)),
    }


def complete(body):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array")
    model = pick_model(body.get("model"))
    system, prompt = render(messages)
    want_json = (body.get("response_format") or {}).get("type") in ("json_object", "json_schema")
    if want_json:
        system = (system + "\n\n" + JSON_RULE).strip()

    attempts = 2 if want_json else 1
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    content = ""
    for attempt in range(attempts):
        content, usage = run_claude(system, prompt if attempt == 0 else
                                    prompt + "\n\nYour previous answer was not valid JSON. " + JSON_RULE, model)
        for k in usage_total:
            usage_total[k] += usage[k]
        if not want_json:
            break
        obj = extract_json(content)
        if obj is not None:
            content = json.dumps(obj, ensure_ascii=False)
            break
    else:
        raise RuntimeError("model did not return valid JSON")

    usage_total["total_tokens"] = usage_total["prompt_tokens"] + usage_total["completion_tokens"]
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "claude-" + model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": usage_total,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-bridge/1.0"

    def log_message(self, fmt, *args):            # route http.server noise into our log
        log.debug("%s %s", self.address_string(), fmt % args)

    def send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def error(self, code, message, kind="invalid_request_error"):
        self.send_json(code, {"error": {"message": message, "type": kind, "code": code}})

    def authorized(self):
        if not API_KEY:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + API_KEY

    def do_GET(self):
        if self.path == "/health":
            return self.send_json(200, {"status": "ok"})
        if not self.authorized():
            return self.error(401, "invalid API key", "authentication_error")
        if self.path in ("/v1/models", "/models"):
            return self.send_json(200, {"object": "list", "data": [
                {"id": "claude-" + m, "object": "model", "owned_by": "local-bridge"} for m in ALLOWED_MODELS]})
        self.error(404, "not found")

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self.error(404, "not found")
        if not self.authorized():
            return self.error(401, "invalid API key", "authentication_error")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self.error(400, "body missing or too large")
            body = json.loads(self.rfile.read(n))
            if body.get("stream"):
                return self.error(400, "streaming is not supported by this bridge")
        except ValueError:
            return self.error(400, "body is not valid JSON")
        t0 = time.time()
        try:
            res = complete(body)
        except ValueError as e:
            return self.error(400, str(e))
        except subprocess.TimeoutExpired:
            log.warning("timeout after %ss", TIMEOUT)
            return self.error(504, "model timed out", "server_error")
        except Exception as e:                    # noqa: BLE001 - report every backend failure as 502
            log.exception("completion failed")
            return self.error(502, str(e)[:300], "server_error")
        log.info("ok model=%s ms=%d in=%d out=%d json=%s", res["model"], (time.time() - t0) * 1000,
                 res["usage"]["prompt_tokens"], res["usage"]["completion_tokens"],
                 bool(body.get("response_format")))
        self.send_json(200, res)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(LOG_DIR, "bridge.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    with open(os.path.join(LOG_DIR, "bridge.pid"), "w") as f:
        f.write(str(os.getpid()))
    log.info("listening on http://%s:%d (auth %s, default model %s)", HOST, PORT,
             "on" if API_KEY else "off", DEFAULT_MODEL)
    try:
        srv.serve_forever()
    finally:
        try:
            os.unlink(os.path.join(LOG_DIR, "bridge.pid"))
        except OSError:
            pass


if __name__ == "__main__":
    main()
