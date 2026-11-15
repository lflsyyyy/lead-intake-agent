"""Accuracy evaluation of the qualifier on hand-labelled leads.

    python eval/run_eval.py                         # primary model + comparison model, writes eval/results.md
    python eval/run_eval.py --models openai/gpt-oss-120b
    python eval/run_eval.py --limit 5 --tag before  # quick run; the tag names the results file
    python eval/run_eval.py --combine after,before  # eval/results.md + the table in README.md from saved runs

Each lead in eval/leads.jsonl goes through the same Code nodes as the live workflow (normalize, spam rules,
request builder, schema check, output gate; see eval/pipeline.mjs). Only the model call is done here, straight
to the OpenAI-compatible endpoint: no HubSpot, no Telegram, no email.
The API key comes from EVAL_API_KEY / EVAL_API_KEY_FILE, else LLM_API_KEY / LLM_API_KEY_FILE from .env. It is
never printed. 429 answers are retried after the time the API asks for (free-tier friendly).
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from common import env, load_env, secret  # noqa: E402

CLASSES = ["hot", "warm", "cold", "spam", "invalid"]
# USD per 1M tokens (input, output). Groq list prices, checked by hand; treat cost figures as estimates.
PRICES = {"openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30), "qwen/qwen3.8-27b": (0.29, 0.59)}
MODEL_EXTRA = {"openai/gpt-oss-120b": {"reasoning_effort": "low"}, "openai/gpt-oss-20b": {"reasoning_effort": "low"},
               "qwen/qwen3.8-27b": {"reasoning_effort": "none"}}


def node(mode, records, *args):
    p = subprocess.run(["node", os.path.join(HERE, "pipeline.mjs"), mode, *args],
                       input="\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                       capture_output=True, text=True, encoding="utf8", cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit("pipeline.mjs %s failed: %s" % (mode, p.stderr[-800:]))
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


def wait_hint(err_body, headers):
    ra = headers.get("retry-after")
    if ra:
        try:
            return float(ra) + 0.5
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)(ms|s)", err_body or "")
    if m:
        return float(m.group(1)) / (1000 if m.group(2) == "ms" else 1) + 0.5
    return 10.0


def call(base, key, body, tries=8):
    """POST /chat/completions with 429/5xx retries. Returns (response json or None, latency s, attempts)."""
    data = json.dumps(body).encode()
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=data, method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                              "User-Agent": "lia-eval/1"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r), time.time() - t0, attempt
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf8", "replace")
            if e.code == 429 or e.code >= 500:
                pause = wait_hint(raw, {k.lower(): v for k, v in e.headers.items()})
                print("    %s, waiting %.1fs" % (e.code, pause), flush=True)
                time.sleep(min(pause, 90))
                continue
            print("    HTTP %s: %s" % (e.code, raw[:200]), flush=True)
            return {"error": raw[:300]}, time.time() - t0, attempt
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print("    network error %s, retrying" % e, flush=True)
            time.sleep(5)
    return None, 0.0, tries


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def percentile(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def metrics(leads, preds, latencies, usage, model):
    """Pure: labelled leads + predictions -> metrics dict."""
    by_id = {p["id"]: p for p in preds}
    conf = defaultdict(Counter)
    correct = 0
    per_class = defaultdict(lambda: [0, 0])
    rows = []
    for ld in leads:
        p = by_id.get(ld["id"], {})
        pred = p.get("final", "error")
        ok = pred in (ld.get("accept") or [ld["label"]])
        correct += ok
        per_class[ld["label"]][0] += ok
        per_class[ld["label"]][1] += 1
        conf[ld["label"]][pred] += 1
        rows.append((ld, p, ok))
    spam_tp = sum(1 for ld, p, _ in rows if ld["label"] == "spam" and p.get("final") == "spam")
    spam_fp = sum(1 for ld, p, _ in rows if ld["label"] != "spam" and p.get("final") == "spam"
                  and "spam" not in (ld.get("accept") or []))
    spam_fn = sum(1 for ld, p, _ in rows if ld["label"] == "spam" and p.get("final") != "spam")
    hot = [(ld, p) for ld, p, _ in rows if ld["label"] == "hot"]
    inj = [(ld, p) for ld, p, _ in rows if ld.get("must_not_automate")]
    automated = lambda p: p.get("final") in ("hot", "warm") and not p.get("needs_review")
    clean = [(ld, p) for ld, p, _ in rows if not ld.get("must_not_automate") and p.get("status") == "accepted"]
    tin = sum(u.get("prompt_tokens", 0) for u in usage)
    tout = sum(u.get("completion_tokens", 0) for u in usage)
    price = PRICES.get(model)
    calls = len(usage)
    return {
        "model": model, "n": len(leads), "accuracy": pct(correct, len(leads)), "correct": correct,
        "per_class": {c: {"correct": v[0], "total": v[1], "acc": pct(*v)} for c, v in per_class.items()},
        "confusion": {k: dict(v) for k, v in conf.items()},
        "spam_precision": pct(spam_tp, spam_tp + spam_fp), "spam_recall": pct(spam_tp, spam_tp + spam_fn),
        "hot_recall": pct(sum(1 for _, p in hot if p.get("final") == "hot"), len(hot)),
        "hot_not_lost": pct(sum(1 for _, p in hot if p.get("final") in ("hot", "warm")), len(hot)),
        "injection_automated": sum(1 for _, p in inj if automated(p)), "injection_total": len(inj),
        "strict_accuracy": pct(sum(1 for ld, p, _ in rows if p.get("final") == ld["label"]), len(leads)),
        "false_review": sum(1 for _, p in clean if p.get("needs_review")), "clean_total": len(clean),
        "wrongly_held": sum(1 for ld, p in clean if p.get("needs_review") and ld["label"] in ("hot", "warm")),
        "invalid_json": sum(1 for p in preds if p.get("status") == "accepted" and not p.get("valid_json")),
        "p50_s": percentile(latencies, 50), "p95_s": percentile(latencies, 95), "calls": calls,
        "tokens_in": tin, "tokens_out": tout,
        "cost_per_lead_usd": ((tin * price[0] + tout * price[1]) / 1e6 / calls) if price and calls else None,
        "holds": [{"id": ld["id"], "label": ld["label"], "review": p.get("review_reasons")} for ld, p in clean
                  if p.get("needs_review")],
        "mistakes": [{"id": ld["id"], "label": ld["label"], "pred": p.get("final"), "model_fit": p.get("model_fit"),
                      "review": p.get("review_reasons")} for ld, p, ok in rows if not ok],
    }


def final_class(prep, parsed):
    if prep["status"] == "invalid":
        return "invalid"
    if prep["status"] == "spam":
        return "spam"
    return parsed.get("fit") or "error"


def run_model(model, leads, base, key, profile):
    print("== %s" % model, flush=True)
    prepared = node("prepare", leads, "--model", model, "--profile", profile,
                    "--effort", MODEL_EXTRA.get(model, {}).get("reasoning_effort", ""))
    answered, latencies, usage = [], [], []
    for pr in prepared:
        if pr["status"] != "accepted":
            continue
        body = dict(pr["request"])
        body.update(MODEL_EXTRA.get(model, {}))
        res, dt, attempts = call(base, key, body)
        if res and "choices" in res:
            latencies.append(dt)
            usage.append(res.get("usage") or {})
        answered.append({"id": pr["id"], "routed": pr["routed"], "response": res or {}})
        print("  %-8s %5.2fs %s" % (pr["id"], dt, "" if res and "choices" in res else "(no answer)"), flush=True)
    parsed = {p["id"]: p for p in node("parse", answered, "--model", model)}
    preds = []
    for pr in prepared:
        p = parsed.get(pr["id"], {})
        preds.append({"id": pr["id"], "status": pr["status"], "final": final_class(pr, p), **p})
    return metrics(leads, preds, latencies, usage, model), preds


def fmt_report(results, tag):
    out = ["# Qualifier evaluation: run `%s`" % tag, "",
           "%d hand-labelled leads in `eval/leads.jsonl` (fictional, @example.com). Each goes through the workflow's own "
           "Code nodes (`eval/pipeline.mjs`): rules, request builder, schema check and output gate. A prediction counts as "
           "correct when it is the label or one of the accepted alternatives for genuinely ambiguous leads. Injection leads "
           "are expected to end up *held for review* (cold or spam), never automated." % results[0]["n"], "",
           "| Model | Accuracy | Exact label | Hot recall | Hot not lost | Spam P / R | Injections automated | "
           "Gate holds (non-injection) | Good leads held | Invalid JSON | p50 / p95 | Cost per lead (est.) |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        cost = "$%.5f" % r["cost_per_lead_usd"] if r["cost_per_lead_usd"] is not None else "n/a"
        out.append("| `%s` | **%.0f%%** (%d/%d) | %.0f%% | %.0f%% | %.0f%% | %.0f%% / %.0f%% | %d of %d | %d of %d | %d | %d | %.2f s / %.2f s | %s |" % (
            r["model"], r["accuracy"], r["correct"], r["n"], r["strict_accuracy"], r["hot_recall"], r["hot_not_lost"],
            r["spam_precision"], r["spam_recall"], r["injection_automated"], r["injection_total"], r["false_review"],
            r["clean_total"], r["wrongly_held"], r["invalid_json"], r["p50_s"], r["p95_s"], cost))
    for r in results:
        out += ["", "## `%s`" % r["model"], "", "Per class: " + ", ".join(
            "%s %d/%d" % (c, r["per_class"][c]["correct"], r["per_class"][c]["total"]) for c in CLASSES if c in r["per_class"]),
            "", "Confusion matrix (rows: label, columns: prediction):", "",
            "| label \\ pred | " + " | ".join(CLASSES + ["error"]) + " |", "|---" * (len(CLASSES) + 2) + "|"]
        for c in CLASSES:
            if c in r["confusion"]:
                out.append("| **%s** | " % c + " | ".join(str(r["confusion"][c].get(k, 0) or "·") for k in CLASSES + ["error"]) + " |")
        out += ["", "Tokens: %d in, %d out over %d calls." % (r["tokens_in"], r["tokens_out"], r["calls"])]
        if r.get("holds"):
            out += ["", "Held by the output gate (not injection leads):", ""]
            out += ["- `%s` (%s): %s" % (h["id"], h["label"], "; ".join(h["review"])) for h in r["holds"]]
        if r["mistakes"]:
            out += ["", "Misses:", ""]
            for m in r["mistakes"]:
                out.append("- `%s` labelled %s, got %s%s%s" % (
                    m["id"], m["label"], m["pred"],
                    " (model said %s)" % m["model_fit"] if m["model_fit"] and m["model_fit"] != m["pred"] else "",
                    "; held: " + "; ".join(m["review"]) if m["review"] else ""))
    out += ["", "Cost uses Groq list prices per 1M tokens (%s); it is an estimate, not a bill." % ", ".join(
        "%s $%.3f in / $%.2f out" % (k, v[0], v[1]) for k, v in PRICES.items() if any(r["model"] == k for r in results)),
        "Latency is wall time per call from this machine, including queueing on the free tier."]
    return "\n".join(out) + "\n"


def reload(tag, leads):
    """Metrics for a finished run, recomputed from its saved predictions (latency and tokens as recorded)."""
    out = []
    for path in sorted(glob.glob(os.path.join(HERE, "results-%s-*.json" % tag))):
        with open(path, encoding="utf8") as f:
            d = json.load(f)
        old = d["metrics"]
        m = metrics(leads, d["predictions"], [], [], old["model"])
        for k in ("p50_s", "p95_s", "calls", "tokens_in", "tokens_out", "cost_per_lead_usd"):
            m[k] = old[k]
        out.append(m)
    order = {"openai/gpt-oss-120b": 0}
    return sorted(out, key=lambda m: (order.get(m["model"], 1), m["model"]))


README_START, README_END = "<!-- eval:start -->", "<!-- eval:end -->"


def combine(tags, leads):
    """eval/results.md with every run (newest first) and the summary table in README.md."""
    sections = []
    for tag in tags:
        res = reload(tag, leads)
        if res:
            sections.append((tag, res, fmt_report(res, tag)))
    if not sections:
        raise SystemExit("no saved results for %s" % ",".join(tags))
    body = ["# Qualifier evaluation results", "",
            "Produced by `python eval/run_eval.py`. Runs: %s." % ", ".join("`%s`" % t for t, _, _ in sections), ""]
    nl = "\n"
    for _, _, rep in sections:
        body.append(rep.replace("# Qualifier evaluation: run", "## Run", 1).replace(nl + "## `", nl + "### `"))
    with open(os.path.join(HERE, "results.md"), "w", encoding="utf8", newline=nl) as f:
        f.write(nl.join(body))
    table = [l for l in sections[0][2].splitlines() if l.startswith("|")][:2 + len(sections[0][1])]
    readme = os.path.join(ROOT, "README.md")
    text = open(readme, encoding="utf8").read()
    if README_START in text and README_END in text:
        a, rest = text.split(README_START, 1)
        _, b = rest.split(README_END, 1)
        text = a + README_START + nl + nl.join(table) + nl + README_END + b
        with open(readme, "w", encoding="utf8", newline=nl) as f:
            f.write(text)
    print("wrote eval/results.md%s" % (" and the README table" if README_START in text else ""))


def main():
    load_env()
    a = sys.argv
    if "--combine" in a:
        leads = [json.loads(x) for x in open(os.path.join(HERE, "leads.jsonl"), encoding="utf8") if x.strip()]
        return combine(a[a.index("--combine") + 1].split(","), leads)
    models = a[a.index("--models") + 1].split(",") if "--models" in a else ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    tag = a[a.index("--tag") + 1] if "--tag" in a else "latest"
    leads = [json.loads(x) for x in open(os.path.join(HERE, "leads.jsonl"), encoding="utf8") if x.strip()]
    if "--limit" in a:
        leads = leads[: int(a[a.index("--limit") + 1])]
    base = env("EVAL_BASE_URL") or "https://api.groq.com/openai/v1"
    key = secret("EVAL_API_KEY", required=False) or secret("LLM_API_KEY")
    from build import DEFAULT_PROFILE  # noqa: E402  (same business profile as the workflow)
    profile = env("BUSINESS_PROFILE") or DEFAULT_PROFILE
    results = []
    for m in models:
        r, preds = run_model(m, leads, base, key, profile)
        results.append(r)
        with open(os.path.join(HERE, "results-%s-%s.json" % (tag, re.sub(r"[^a-z0-9]+", "-", m.lower()))), "w",
                  encoding="utf8", newline="\n") as f:
            json.dump({"metrics": r, "predictions": preds}, f, ensure_ascii=False, indent=1)
        print("   accuracy %.0f%%  hot recall %.0f%%  injections automated %d" % (
            r["accuracy"], r["hot_recall"], r["injection_automated"]), flush=True)
    report = fmt_report(results, tag)
    with open(os.path.join(HERE, "results-%s.md" % tag), "w", encoding="utf8", newline="\n") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
