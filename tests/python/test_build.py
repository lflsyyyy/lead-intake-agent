"""tools/build.py offline: workflow generation, Code node injection, export sanitizing."""
import copy
import json
import os
import unittest
from unittest import mock

from helpers import FAKE_CREDS, ROOT, code_nodes, private_words

import build


def offline(mode="hmac"):
    cfg = dict(build.public_config(), webhook_auth=mode)
    return build.build_all(FAKE_CREDS, cfg, {"errors": "ERR1", "intake": "INT1"})


def by_name(wf):
    return {n["name"]: n for n in wf["nodes"]}


class TestSlugAndCodeFiles(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(build.slug("Normalize & validate"), "normalize-validate")
        self.assertEqual(build.slug("Still waiting for a reply?"), "still-waiting-for-a-reply")
        self.assertEqual(build.slug("Sign request (HMAC)"), "sign-request-hmac")

    def test_code_path_layout(self):
        p = build.code_path("intake", "Route intake")
        self.assertEqual(os.path.relpath(p, ROOT).replace("\\", "/"), "workflows/code/intake/route-intake.js")

    def test_missing_code_file_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as e:
            build.read_code("intake", "No such node")
        self.assertIn("missing Code node source", str(e.exception))

    def test_code_node_carries_the_file_verbatim(self):
        n = build.code("intake", "Route intake", [0, 0])
        with open(build.code_path("intake", "Route intake"), encoding="utf8") as f:
            self.assertEqual(n["parameters"]["jsCode"], f.read().strip())
        self.assertEqual(n["type"], "n8n-nodes-base.code")


class TestGeneratedWorkflows(unittest.TestCase):
    def setUp(self):
        self.wfs = offline()

    def test_five_workflows_with_lia_names(self):
        self.assertEqual(sorted(w["name"] for w in self.wfs.values()),
                         ["LIA · Errors", "LIA · Follow-ups", "LIA · Form", "LIA · Intake", "LIA · Replay dead letters"])

    def test_every_connection_points_at_an_existing_node(self):
        for wf in self.wfs.values():
            names = {n["name"] for n in wf["nodes"]}
            for src, outs in wf["connections"].items():
                self.assertIn(src, names, wf["name"])
                for branch in outs["main"]:
                    for c in branch:
                        self.assertIn(c["node"], names, "%s: %s -> %s" % (wf["name"], src, c["node"]))

    def test_node_names_are_unique(self):
        for wf in self.wfs.values():
            names = [n["name"] for n in wf["nodes"]]
            self.assertEqual(len(names), len(set(names)), wf["name"])

    def test_node_ids_are_deterministic(self):
        again = offline()
        for k, wf in self.wfs.items():
            self.assertEqual([n["id"] for n in wf["nodes"]], [n["id"] for n in again[k]["nodes"]])

    def test_every_code_node_comes_from_a_file(self):
        for key, wf in self.wfs.items():
            folder = {"intake": "intake", "followups": "follow-ups", "errors": "errors", "replay": "replay-dead-letters",
                      "form": "form"}[key]
            for n in code_nodes(wf):
                with open(build.code_path(folder, n["name"]), encoding="utf8") as f:
                    self.assertEqual(n["parameters"]["jsCode"], f.read().strip(), n["name"])

    def test_webhook_keeps_the_raw_body_for_hmac(self):
        hook = by_name(self.wfs["intake"])["Lead webhook"]
        self.assertTrue(hook["parameters"]["options"]["rawBody"])
        self.assertNotIn("authentication", hook["parameters"])

    def test_token_mode_uses_header_auth_on_the_webhook(self):
        hook = by_name(offline("token")["intake"])["Lead webhook"]
        self.assertEqual(hook["parameters"]["authentication"], "headerAuth")
        self.assertEqual(hook["credentials"]["httpHeaderAuth"]["id"], "cred-webhook_token")

    def test_hmac_node_uses_the_webhook_secret_credential(self):
        n = by_name(self.wfs["intake"])["Sign request (HMAC)"]
        self.assertEqual(n["parameters"]["action"], "hmac")
        self.assertEqual(n["credentials"]["crypto"]["id"], "cred-webhook_hmac")

    def test_security_nodes_run_before_normalize(self):
        con = self.wfs["intake"]["connections"]
        nxt = lambda s: [c["node"] for b in con[s]["main"] for c in b]
        self.assertEqual(nxt("Lead webhook"), ["Read raw body"])
        self.assertEqual(nxt("Read raw body"), ["Sign request (HMAC)"])
        self.assertEqual(nxt("Config"), ["Verify signature"])
        self.assertEqual(nxt("Verify signature"), ["Normalize & validate"])

    def test_telegram_always_after_unseal(self):
        for wf in self.wfs.values():
            names = by_name(wf)
            for n in wf["nodes"]:
                if n["type"] == "n8n-nodes-base.telegram":
                    self.assertEqual(n["parameters"]["chatId"], "={{ $json.owner_chat_id }}")
                    srcs = [s for s, o in wf["connections"].items() for b in o["main"] for c in b if c["node"] == n["name"]]
                    self.assertEqual(srcs, ["Unseal owner chat"])
                    self.assertEqual(names["Unseal owner chat"]["credentials"]["crypto"]["id"], "cred-private")

    def test_resend_uses_custom_auth_and_configurable_base(self):
        for key in ("intake", "followups"):
            n = by_name(self.wfs[key])["Resend · send email"]
            self.assertEqual(n["parameters"]["genericAuthType"], "httpCustomAuth")
            self.assertIn("resend_base_url", n["parameters"]["url"])

    def test_hubspot_calls_use_the_configured_base_url(self):
        for wf in self.wfs.values():
            for n in wf["nodes"]:
                url = n["parameters"].get("url", "")
                if n["type"] == "n8n-nodes-base.httpRequest" and "HubSpot" in n["name"]:
                    self.assertTrue(url.startswith("={{ $('Config').first().json.hubspot_base_url }}"), n["name"])
                self.assertNotIn("api.hubapi.com", url)

    def test_every_external_call_retries(self):
        for wf in self.wfs.values():
            for n in wf["nodes"]:
                if n["type"] in ("n8n-nodes-base.httpRequest", "n8n-nodes-base.telegram"):
                    self.assertTrue(n.get("retryOnFail"), n["name"])

    def test_error_workflow_is_wired(self):
        for key in ("intake", "followups", "replay", "form"):
            self.assertEqual(self.wfs[key]["settings"]["errorWorkflow"], "ERR1")
        self.assertNotIn("errorWorkflow", self.wfs["errors"]["settings"])

    def test_form_has_a_honeypot(self):
        fields = by_name(self.wfs["form"])["Lead form"]["parameters"]["formFields"]["values"]
        self.assertIn({"fieldType": "hiddenField", "fieldName": "hp"}, fields)

    def test_unauthorized_and_rate_limited_are_not_logged(self):
        n = by_name(self.wfs["intake"])
        self.assertIn("Log it?", n)
        con = self.wfs["intake"]["connections"]["Log it?"]["main"]
        self.assertEqual([c["node"] for c in con[0]], ["Log lead"])
        self.assertEqual([c["node"] for c in con[1]], ["Came from webhook?"])


class TestConfig(unittest.TestCase):
    def test_public_config_has_no_private_values(self):
        pub = build.public_config()
        for word in private_words():
            self.assertNotIn(word, json.dumps(pub).lower())
        for key in ("telegram_chat_id", "hubspot_portal_id", "hubspot_ui_domain"):
            self.assertNotIn(key, pub)
        self.assertEqual(pub["mail_from"], "Brightline Studio <noreply@example.com>")
        self.assertEqual(pub["business_name"], "Brightline Studio")
        self.assertEqual(pub["owner_chat_sealed"], build.SEALED_PLACEHOLDER)

    def test_config_values_seals_the_chat_id(self):
        env = {"TELEGRAM_CHAT_ID": "424242", "MAIL_FROM": "Real <x@real.example>", "LLM_BASE_URL": "https://x/v1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(build, "seal", return_value="SEALED") as s:
            cfg = build.config_values()
        s.assert_called_once_with("424242")
        self.assertEqual(cfg["owner_chat_sealed"], "SEALED")
        self.assertNotIn("424242", json.dumps(cfg))
        self.assertNotIn("real.example", json.dumps(cfg))
        self.assertEqual(cfg["mail_from"], "Brightline Studio <noreply@example.com>")

    def test_webhook_auth_mode_is_validated(self):
        with mock.patch.dict(os.environ, {"WEBHOOK_AUTH": "basic"}):
            with self.assertRaises(SystemExit):
                build.webhook_auth_mode()
        with mock.patch.dict(os.environ, {"WEBHOOK_AUTH": "TOKEN"}):
            self.assertEqual(build.webhook_auth_mode(), "token")

    def test_resend_credential_carries_the_real_sender_only_when_set(self):
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "MAIL_FROM": "Live <hi@live.example>"}):
            data = json.loads(build.resend_auth_json())
        self.assertEqual(data["headers"]["Authorization"], "Bearer re_test")
        self.assertEqual(data["body"], {"from": "Live <hi@live.example>"})
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "MAIL_FROM": ""}):
            self.assertNotIn("body", json.loads(build.resend_auth_json()))

    def test_token_credential_only_in_token_mode(self):
        with mock.patch.dict(os.environ, {"WEBHOOK_AUTH": "hmac"}):
            self.assertNotIn("webhook_token", build.credential_specs())
        with mock.patch.dict(os.environ, {"WEBHOOK_AUTH": "token"}):
            self.assertEqual(build.credential_specs()["webhook_token"][1], "httpHeaderAuth")

    def test_local_secret_is_generated_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.key")
            with mock.patch.dict(os.environ, {"X_SECRET": "", "X_SECRET_FILE": p}):
                a = build.local_secret("X_SECRET", p)
                b = build.local_secret("X_SECRET", p)
            self.assertEqual(a, b)
            self.assertGreaterEqual(len(a), 40)


class TestSanitize(unittest.TestCase):
    def live(self):
        wf = copy.deepcopy(offline()["intake"])
        wf["id"] = "WfIdLive00000001"
        wf["staticData"] = {"private": "x"}
        wf["settings"]["errorWorkflow"] = "WfIdErrors000001"
        wf["settings"]["timeSavedPerExecution"] = 5
        for n in wf["nodes"]:
            if n["name"] == "Config":
                for a in n["parameters"]["assignments"]["assignments"]:
                    if a["name"] == "owner_chat_sealed":
                        a["value"] = "AQ-real-ciphertext"
                    if a["name"] == "mail_from":
                        a["value"] = "Someone <me@private.example>"
                n["parameters"]["assignments"]["assignments"].append({"id": "z", "name": "legacy_secret", "value": "abc", "type": "string"})
            if n.get("credentials"):
                for ref in n["credentials"].values():
                    ref["id"] = "RealCredId123456"
        return wf

    def test_credential_ids_are_replaced(self):
        out = build.sanitize(self.live())
        for n in out["nodes"]:
            for ref in (n.get("credentials") or {}).values():
                self.assertEqual(ref["id"], "REPLACE_ME")
                self.assertTrue(ref["name"].startswith("LIA · "))

    def test_ids_static_data_and_unknown_settings_are_dropped(self):
        out = build.sanitize(self.live())
        self.assertNotIn("id", out)
        self.assertNotIn("staticData", out)
        self.assertEqual(out["settings"]["errorWorkflow"], "REPLACE_WITH_LIA_ERRORS_WORKFLOW_ID")
        self.assertNotIn("timeSavedPerExecution", out["settings"])
        self.assertNotIn("WfIdErrors000001", json.dumps(out))

    def test_config_values_become_public_placeholders(self):
        out = build.sanitize(self.live())
        cfg = {a["name"]: a["value"] for n in out["nodes"] if n["name"] == "Config"
               for a in n["parameters"]["assignments"]["assignments"]}
        self.assertEqual(cfg["owner_chat_sealed"], build.SEALED_PLACEHOLDER)
        self.assertEqual(cfg["mail_from"], "Brightline Studio <noreply@example.com>")
        self.assertEqual(cfg["legacy_secret"], "")

    def test_sub_workflow_ids_are_placeholders(self):
        out = build.sanitize(dict(offline()["form"], id="x"))
        hand = [n for n in out["nodes"] if n["name"] == "Hand off to Intake"][0]
        self.assertEqual(hand["parameters"]["workflowId"]["value"], "REPLACE_WITH_LIA_INTAKE_WORKFLOW_ID")

    def test_demo_fast_schedule_is_reset(self):
        cfg = dict(build.public_config(), followup_unit_ms=60000)
        wf = build.build_followups(FAKE_CREDS, cfg, "E")
        out = build.sanitize(wf)
        names = [n["name"] for n in out["nodes"]]
        self.assertIn("Every 15 minutes", names)
        self.assertNotIn("Every minute (DEMO_FAST)", names)
        self.assertIn("Every 15 minutes", out["connections"])

    def test_sanitize_does_not_mutate_its_input(self):
        live = self.live()
        before = json.dumps(live, sort_keys=True)
        build.sanitize(live)
        self.assertEqual(before, json.dumps(live, sort_keys=True))

    def test_export_filename(self):
        self.assertEqual(build.export_filename("LIA · Replay dead letters"), "lia-replay-dead-letters.json")
        self.assertEqual(build.export_filename("LIA · Follow-ups"), "lia-follow-ups.json")


if __name__ == "__main__":
    unittest.main()
