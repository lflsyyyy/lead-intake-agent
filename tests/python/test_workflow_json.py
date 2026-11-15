"""The committed workflow exports: valid n8n JSON, in sync with workflows/code (drift check), nothing private."""
import glob
import json
import os
import re
import unittest

from helpers import FAKE_CREDS, ROOT, code_nodes, exported, private_words

import build

FOLDER = {"lia-intake.json": "intake", "lia-follow-ups.json": "follow-ups", "lia-errors.json": "errors",
          "lia-replay-dead-letters.json": "replay-dead-letters", "lia-form.json": "form"}
KEY = {"lia-intake.json": "intake", "lia-follow-ups.json": "followups", "lia-errors.json": "errors",
       "lia-replay-dead-letters.json": "replay", "lia-form.json": "form"}


class TestExportsAreValid(unittest.TestCase):
    def setUp(self):
        self.wfs = exported()

    def test_all_five_exports_exist(self):
        self.assertEqual(sorted(self.wfs), sorted(FOLDER))

    def test_top_level_shape(self):
        for fn, wf in self.wfs.items():
            self.assertEqual(set(wf), {"name", "nodes", "connections", "settings"}, fn)
            self.assertTrue(wf["name"].startswith("LIA · "), fn)
            self.assertEqual(wf["settings"].get("executionOrder"), "v1", fn)

    def test_nodes_have_the_required_fields(self):
        for fn, wf in self.wfs.items():
            for n in wf["nodes"]:
                for k in ("id", "name", "type", "typeVersion", "position", "parameters"):
                    self.assertIn(k, n, "%s: %s" % (fn, n.get("name")))
                self.assertRegex(n["type"], r"^n8n-nodes-base\.[A-Za-z]+$")
                self.assertEqual(len(n["position"]), 2)

    def test_node_names_and_ids_are_unique(self):
        for fn, wf in self.wfs.items():
            for key in ("name", "id"):
                vals = [n[key] for n in wf["nodes"]]
                self.assertEqual(len(vals), len(set(vals)), "%s: duplicate %s" % (fn, key))

    def test_connections_point_at_existing_nodes(self):
        for fn, wf in self.wfs.items():
            names = {n["name"] for n in wf["nodes"]}
            for src, outs in wf["connections"].items():
                self.assertIn(src, names, fn)
                for branch in outs["main"]:
                    for c in branch:
                        self.assertIn(c["node"], names, "%s: %s -> %s" % (fn, src, c["node"]))

    def test_every_non_trigger_node_is_reachable(self):
        triggers = ("webhook", "executeWorkflowTrigger", "scheduleTrigger", "manualTrigger", "formTrigger", "errorTrigger")
        for fn, wf in self.wfs.items():
            targets = {c["node"] for o in wf["connections"].values() for b in o["main"] for c in b}
            for n in wf["nodes"]:
                if n["type"].split(".")[-1] in triggers or n["type"].endswith("stickyNote"):
                    continue
                self.assertIn(n["name"], targets, "%s: %s has no incoming connection" % (fn, n["name"]))

    def test_expressions_reference_existing_nodes(self):
        for fn, wf in self.wfs.items():
            names = {n["name"] for n in wf["nodes"]}
            text = json.dumps(wf, ensure_ascii=False)
            for ref in set(re.findall(r"\$\('([^']+)'\)", text)):
                self.assertIn(ref, names, "%s references missing node %s" % (fn, ref))

    def test_code_node_references_exist(self):
        for fn, wf in self.wfs.items():
            names = {n["name"] for n in wf["nodes"]}
            for n in code_nodes(wf):
                for ref in set(re.findall(r"\$\('([^']+)'\)", n["parameters"]["jsCode"])):
                    self.assertIn(ref, names, "%s / %s references missing node %s" % (fn, n["name"], ref))


class TestDrift(unittest.TestCase):
    """workflows/code/** is the single source of truth; the exported JSON must carry exactly that code."""

    def test_exported_code_nodes_match_the_files(self):
        for fn, wf in exported().items():
            for n in code_nodes(wf):
                path = build.code_path(FOLDER[fn], n["name"])
                self.assertTrue(os.path.exists(path), "no file for %s / %s" % (fn, n["name"]))
                with open(path, encoding="utf8") as f:
                    self.assertEqual(n["parameters"]["jsCode"].strip(), f.read().strip(),
                                     "%s / %s differs from %s: run tools/build.py --export" % (
                                         fn, n["name"], os.path.relpath(path, ROOT)))

    def test_every_code_file_is_used(self):
        used = {build.code_path(FOLDER[fn], n["name"]) for fn, wf in exported().items() for n in code_nodes(wf)}
        for p in glob.glob(os.path.join(ROOT, "workflows", "code", "*", "*.js")):
            self.assertIn(os.path.normpath(p), {os.path.normpath(u) for u in used}, "orphan file %s" % p)

    def test_exports_match_what_build_generates(self):
        """Same nodes, types and wiring as build.py would create (catches an export that was not refreshed)."""
        gen = build.build_all(FAKE_CREDS, build.public_config())
        for fn, wf in exported().items():
            g = build.sanitize(gen[KEY[fn]])
            self.assertEqual(sorted((n["name"], n["type"]) for n in wf["nodes"]),
                             sorted((n["name"], n["type"]) for n in g["nodes"]), fn)
            self.assertEqual(wf["connections"], g["connections"], fn)


class TestNothingPrivate(unittest.TestCase):
    PATTERNS = {
        "telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
        "groq key": r"\bgsk_[A-Za-z0-9]{20,}",
        "openai key": r"\bsk-[A-Za-z0-9_-]{20,}",
        "resend key": r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{8,}",
        "hubspot token": r"\bpat-[a-z]{2,3}\d?-[0-9a-f-]{20,}",
        "n8n api key (jwt)": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
    }

    def files(self):
        out = []
        for pat in ("workflows/*.json", "workflows/code/*/*.js", "docker.env", ".env.example", "README.md", "docs/*.md",
                    "eval/*.jsonl", "eval/*.md", "mock/*.py", "tools/*.py", "tests/**/*.*", "docker-compose.yml"):
            out += glob.glob(os.path.join(ROOT, pat), recursive=True)
        return out

    def test_no_secrets_in_tracked_text(self):
        for p in self.files():
            text = open(p, encoding="utf8", errors="replace").read()
            for name, rx in self.PATTERNS.items():
                self.assertIsNone(re.search(rx, text), "%s looks like a %s" % (p, name))

    def test_no_personal_names_or_domains_in_exports(self):
        for fn, wf in exported().items():
            text = json.dumps(wf, ensure_ascii=False).lower()
            self.assertNotIn("@gmail.com", text, fn)
            for word in private_words():
                self.assertNotIn(word, text, fn)

    def test_no_private_words_in_any_published_file(self):
        words = private_words()
        for p in self.files():
            text = open(p, encoding="utf8", errors="replace").read().lower()
            for word in words:
                self.assertNotIn(word, text, p)

    def test_credentials_are_placeholders(self):
        for fn, wf in exported().items():
            for n in wf["nodes"]:
                for ref in (n.get("credentials") or {}).values():
                    self.assertEqual(ref["id"], "REPLACE_ME", fn)

    def test_config_is_the_public_set(self):
        pub = build.public_config()
        for fn, wf in exported().items():
            for n in wf["nodes"]:
                if n["name"] == "Config":
                    cfg = {a["name"]: a["value"] for a in n["parameters"]["assignments"]["assignments"]}
                    self.assertEqual(cfg, {k: pub[k] for k in cfg}, fn)
                    self.assertEqual(cfg["mail_from"], "Brightline Studio <noreply@example.com>")
                    self.assertEqual(cfg["owner_chat_sealed"], build.SEALED_PLACEHOLDER)

    def test_no_instance_ids(self):
        for fn, wf in exported().items():
            self.assertNotIn("id", wf)
            text = json.dumps(wf)
            self.assertNotIn("staticData", text)
            self.assertNotIn("cachedResultUrl", text)
            if "errorWorkflow" in wf["settings"]:
                self.assertEqual(wf["settings"]["errorWorkflow"], "REPLACE_WITH_LIA_ERRORS_WORKFLOW_ID")


if __name__ == "__main__":
    unittest.main()
