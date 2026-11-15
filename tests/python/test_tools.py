"""send_samples verification logic, cleanup filters, common helpers."""
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (sets sys.path)

import cleanup_hubspot as ch
import common
import send_samples as ss


def sent(n, expect, code=202, lead_id="lia_x", status="accepted"):
    return {"n": n, "expect": expect, "email": "a@b.io", "code": code, "res": {"status": status, "lead_id": lead_id}, "ms": 200}


def yes(kind, oid):
    return bool(oid)


class TestCheckLead(unittest.TestCase):
    def row(self, **o):
        r = {"lead_id": "lia_x", "fit": "hot", "status": "processed", "contact_id": "1", "deal_id": "2",
             "telegram_message_id": "7", "review_reason": ""}
        r.update(o)
        return r

    def test_hot_with_reply_is_ok(self):
        self.assertEqual(ss.check_lead(sent(1, "hot"), self.row(), [{"lead_id": "lia_x"}], [], yes), [])

    def test_hot_without_reply(self):
        self.assertIn("no reply", ss.check_lead(sent(1, "hot"), self.row(), [], [], yes))

    def test_warm_needs_follow_ups(self):
        p = ss.check_lead(sent(1, "warm"), self.row(fit="warm"), [], [], yes)
        self.assertIn("no follow-ups", p)
        self.assertEqual(ss.check_lead(sent(1, "warm"), self.row(fit="warm"), [], [{"lead_id": "lia_x"}], yes), [])

    def test_missing_crm_records(self):
        p = ss.check_lead(sent(1, "hot"), self.row(), [{"lead_id": "lia_x"}], [], lambda k, i: False)
        self.assertIn("no contact", p)
        self.assertIn("no deal", p)

    def test_cold_needs_no_deal(self):
        self.assertEqual(ss.check_lead(sent(1, "cold"), self.row(fit="cold", deal_id=""), [], [], lambda k, i: k == "contacts"), [])

    def test_missing_telegram(self):
        self.assertIn("no telegram", ss.check_lead(sent(1, "cold"), self.row(fit="cold", telegram_message_id=""), [], [], yes))

    def test_stuck_status(self):
        self.assertIn("status=accepted", ss.check_lead(sent(1, "warm"), self.row(status="accepted", fit=""), [], [], yes))

    def test_fit_difference_is_soft(self):
        p = ss.check_lead(sent(1, "hot"), self.row(fit="warm"), [], [{"lead_id": "lia_x"}], yes)
        self.assertEqual(p, ["fit differs (llm)"])

    def test_wrong_http_code(self):
        self.assertIn("http", ss.check_lead(sent(1, "spam", code=500), self.row(fit="spam", status="spam"), [], [], yes))

    def test_duplicate(self):
        self.assertEqual(ss.check_lead(sent(10, "duplicate", code=200, status="duplicate"), {}, [], [], yes), [])
        self.assertEqual(ss.check_lead(sent(10, "duplicate", code=202), {}, [], [], yes), ["expected 200 duplicate"])

    def test_injection_must_be_held_and_not_emailed(self):
        ok = self.row(fit="cold", deal_id="", review_reason="prompt injection suspected")
        self.assertEqual(ss.check_lead(sent(11, "review"), ok, [], [], yes), [])
        bad = ss.check_lead(sent(11, "review"), self.row(fit="hot"), [{"lead_id": "lia_x"}], [], yes)
        self.assertIn("injection became hot", bad)
        self.assertIn("injection produced an email", bad)
        self.assertIn("not held for review", bad)


class TestSummarize(unittest.TestCase):
    def test_counts_only_hard_failures(self):
        s = [sent(1, "hot", lead_id="a"), sent(2, "warm", lead_id="b")]
        rows = {"a": {"fit": "warm", "status": "processed", "contact_id": "1", "deal_id": "2", "telegram_message_id": "3"},
                "b": {"fit": "warm", "status": "accepted"}}
        lines, failures = ss.summarize(s, rows, [], [{"lead_id": "a"}], yes)
        self.assertEqual(failures, 1)
        self.assertEqual(len(lines), 4)
        self.assertIn("fit differs (llm)", lines[2])
        self.assertIn("status=accepted", lines[3])

    def test_mail_column(self):
        r = {"fit": "hot"}
        self.assertEqual(ss.mail_column(sent(1, "hot"), r, [{"lead_id": "lia_x", "delivered": True}], []), "sent")
        self.assertEqual(ss.mail_column(sent(1, "hot"), r, [{"lead_id": "lia_x", "delivered": False}], []), "outbox")
        self.assertEqual(ss.mail_column(sent(1, "hot"), {"fit": "warm"}, [], [{"lead_id": "lia_x"}] * 2), "2 follow-ups queued")
        self.assertTrue(ss.mail_column(sent(1, "review"), {"review_reason": "x"}, [], []).startswith("held: x"))


class TestSamples(unittest.TestCase):
    def test_samples_are_fictional(self):
        for expect, body in ss.SAMPLES:
            if body:
                self.assertRegex(body["email"], r"(@example\.com|\.example\.com|@resend\.dev)$")

    def test_duplicate_repeats_the_first(self):
        i = [e for e, _ in ss.SAMPLES].index("duplicate") + 1
        self.assertIs(ss.body_of(i), ss.SAMPLES[0][1])

    def test_mix_of_expectations(self):
        kinds = [e for e, _ in ss.SAMPLES]
        for k, n in {"hot": 2, "warm": 3, "cold": 2, "spam": 2, "duplicate": 1, "review": 2}.items():
            self.assertEqual(kinds.count(k), n, k)


class TestCleanupFilters(unittest.TestCase):
    def test_test_domains_only(self):
        for e in ("a@example.com", "delivered+x@resend.dev", "  A@EXAMPLE.COM "):
            self.assertTrue(ch.is_test_email(e), e)
        for e in ("a@gmail.com", "a@example.com.evil.io", "a@notexample.com", "a@sub.example.com", "", None,
                  "@example.com", "a@b@example.com"):
            self.assertFalse(ch.is_test_email(e), e)

    def test_only_test_contacts(self):
        cs = [{"properties": {"email": "a@example.com"}}, {"properties": {"email": "boss@company.io"}}, {"properties": {}}, {}]
        self.assertEqual(ch.only_test_contacts(cs), [cs[0]])

    def test_search_body(self):
        b = ch.search_body()
        self.assertEqual([g["filters"][0]["value"] for g in b["filterGroups"]], list(ch.TEST_DOMAINS))
        self.assertNotIn("after", b)
        self.assertEqual(ch.search_body("123")["after"], "123")

    def test_tables_are_lia_only(self):
        self.assertTrue(all(t.startswith("lia_") for t in ch.TABLES))


class TestCommon(unittest.TestCase):
    def test_sign_matches_hmac_over_timestamp_and_body(self):
        h = common.sign("k", b'{"a":1}', 1700000000)
        mac = hmac.new(b"k", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()
        self.assertEqual(h, {"X-Lead-Timestamp": "1700000000", "X-Signature": "sha256=" + mac})

    def test_load_env_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.env")
            with open(p, "w", encoding="utf8") as f:
                f.write("# comment\nA_T=1\nB_T = two words # note\nC_T=\"quoted # kept\"\nbad line\nA_T=override\n"
                        "D_T=        # only a comment\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("A_T", "B_T", "C_T", "D_T"):
                    os.environ.pop(k, None)
                common.load_env(p)
                self.assertEqual(os.environ["A_T"], "1")
                self.assertEqual(os.environ["B_T"], "two words")
                self.assertEqual(os.environ["C_T"], "quoted # kept")
                self.assertEqual(os.environ["D_T"], "")

    def test_existing_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.env")
            open(p, "w").write("KEEP_T=file\n")
            with mock.patch.dict(os.environ, {"KEEP_T": "env"}):
                common.load_env(p)
                self.assertEqual(os.environ["KEEP_T"], "env")

    def test_secret_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.key")
            open(p, "w").write("  s3cret \n")
            with mock.patch.dict(os.environ, {"S_T": "", "S_T_FILE": p}):
                self.assertEqual(common.secret("S_T"), "s3cret")
            with mock.patch.dict(os.environ, {"S_T": "", "S_T_FILE": ""}):
                self.assertEqual(common.secret("S_T", required=False), "")
                with self.assertRaises(SystemExit):
                    common.secret("S_T")

    def test_target_selection(self):
        with mock.patch.object(common.sys, "argv", ["x", "--target", "docker"]):
            self.assertEqual(common.target(), "docker")
        with mock.patch.object(common.sys, "argv", ["x", "--target", "prod"]):
            with self.assertRaises(SystemExit):
                common.target()
        with mock.patch.object(common.sys, "argv", ["x", "--env-file", "y.env"]):
            self.assertEqual(common.target(), "y.env")


if __name__ == "__main__":
    unittest.main()
