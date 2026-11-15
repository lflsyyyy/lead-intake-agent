"""eval/: the labelled data set and the metrics code."""
import json
import os
import unittest

from helpers import ROOT

import run_eval as ev


def leads():
    with open(os.path.join(ROOT, "eval", "leads.jsonl"), encoding="utf8") as f:
        return [json.loads(x) for x in f if x.strip()]


class TestDataset(unittest.TestCase):
    def test_size_and_mix(self):
        L = leads()
        self.assertTrue(40 <= len(L) <= 50)
        labels = {d["label"] for d in L}
        self.assertTrue({"hot", "warm", "cold", "spam"} <= labels)

    def test_ids_unique_and_labels_known(self):
        L = leads()
        self.assertEqual(len({d["id"] for d in L}), len(L))
        for d in L:
            self.assertIn(d["label"], ev.CLASSES)
            for a in d.get("accept", []):
                self.assertIn(a, ev.CLASSES)

    def test_all_addresses_are_fictional(self):
        for d in leads():
            email = d["lead"]["email"]
            if "@" in email:
                self.assertTrue(email.endswith("example.com"), email)

    def test_required_edge_cases_are_present(self):
        tags = {t for d in leads() for t in d["tags"]}
        for t in ("injection", "foreign", "long", "vague-budget", "empty-message", "vendor-pitch", "competitor"):
            self.assertIn(t, tags)

    def test_injection_leads_must_not_be_automated(self):
        for d in leads():
            if "injection" in d["tags"]:
                self.assertTrue(d.get("must_not_automate"), d["id"])


class TestMetrics(unittest.TestCase):
    L = [{"id": "h1", "label": "hot"}, {"id": "h2", "label": "hot"}, {"id": "w1", "label": "warm", "accept": ["warm", "cold"]},
         {"id": "s1", "label": "spam"}, {"id": "c1", "label": "cold"},
         {"id": "i1", "label": "cold", "accept": ["cold", "spam"], "must_not_automate": True}]

    def preds(self, **over):
        base = {"h1": "hot", "h2": "warm", "w1": "cold", "s1": "spam", "c1": "spam", "i1": "cold"}
        base.update(over)
        return [{"id": k, "final": v, "status": "accepted", "valid_json": True, "needs_review": k == "i1"} for k, v in base.items()]

    def test_accuracy_with_accepted_alternatives(self):
        m = ev.metrics(self.L, self.preds(), [1.0], [{"prompt_tokens": 1000, "completion_tokens": 200}], "openai/gpt-oss-120b")
        self.assertEqual(m["correct"], 4)     # h1, w1 (accepted alt), s1, i1
        self.assertAlmostEqual(m["accuracy"], 4 / 6 * 100)

    def test_hot_recall_and_not_lost(self):
        m = ev.metrics(self.L, self.preds(), [], [], "x")
        self.assertEqual(m["hot_recall"], 50.0)
        self.assertEqual(m["hot_not_lost"], 100.0)

    def test_spam_precision_recall(self):
        m = ev.metrics(self.L, self.preds(), [], [], "x")
        self.assertEqual(m["spam_recall"], 100.0)
        self.assertEqual(m["spam_precision"], 50.0)    # c1 predicted spam wrongly

    def test_injection_automation_is_counted(self):
        p = self.preds(i1="hot")
        for x in p:
            if x["id"] == "i1":
                x["needs_review"] = False
        self.assertEqual(ev.metrics(self.L, p, [], [], "x")["injection_automated"], 1)
        self.assertEqual(ev.metrics(self.L, self.preds(), [], [], "x")["injection_automated"], 0)

    def test_confusion_matrix(self):
        m = ev.metrics(self.L, self.preds(), [], [], "x")
        self.assertEqual(m["confusion"]["hot"], {"hot": 1, "warm": 1})
        self.assertEqual(m["confusion"]["cold"], {"spam": 1, "cold": 1})

    def test_cost_estimate(self):
        m = ev.metrics(self.L, self.preds(), [0.5, 1.5], [{"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}],
                       "openai/gpt-oss-120b")
        self.assertAlmostEqual(m["cost_per_lead_usd"], 0.75)
        self.assertIsNone(ev.metrics(self.L, self.preds(), [], [], "unknown/model")["cost_per_lead_usd"])

    def test_percentiles(self):
        self.assertEqual(ev.percentile([], 50), 0.0)
        self.assertEqual(ev.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(ev.percentile(list(range(1, 101)), 95), 95.05)

    def test_retry_hint(self):
        self.assertEqual(ev.wait_hint("", {"retry-after": "3"}), 3.5)
        self.assertAlmostEqual(ev.wait_hint("Please try again in 1.2s.", {}), 1.7)
        self.assertAlmostEqual(ev.wait_hint("try again in 640ms", {}), 1.14)
        self.assertEqual(ev.wait_hint("", {}), 10.0)

    def test_final_class(self):
        self.assertEqual(ev.final_class({"status": "invalid"}, {}), "invalid")
        self.assertEqual(ev.final_class({"status": "spam"}, {}), "spam")
        self.assertEqual(ev.final_class({"status": "accepted"}, {"fit": "warm"}), "warm")
        self.assertEqual(ev.final_class({"status": "accepted"}, {}), "error")


if __name__ == "__main__":
    unittest.main()
