"""Regression checks for the per-symbol report and shared debate boundary."""
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from workflows.group_report_view import project_member_report


FIXTURE = Path(__file__).resolve().parents[1] / "docs/handoff/fixtures/group-report/group.json"


class GroupReportViewTest(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_two_member_pages_share_only_session_data(self):
        mu = project_member_report(self.report, "MU")
        self.report["members"].reverse()
        amd = project_member_report(self.report, "AMD")
        self.assertEqual(mu["debate_ref"], amd["debate_ref"])
        self.assertEqual(mu["debate_members"], ["MU", "AMD"])
        self.assertEqual(amd["debate_members"], ["AMD", "MU"])
        self.assertEqual(mu["debate_history"], amd["debate_history"])
        self.assertEqual((mu["composite_score"], amd["composite_score"]), (42, 61))
        self.assertNotEqual(mu["execution_card"]["notices"], amd["execution_card"]["notices"])
        self.assertEqual(mu["reference_prices"][0]["symbol"], "MU")
        self.assertEqual(amd["reference_prices"], [])
        self.assertNotIn("members", mu)
        mu["debate_history"].clear()
        self.assertTrue(self.report["debate_history"])

    def test_member_lookup_fails_closed(self):
        with self.assertRaises(ValueError):
            project_member_report(self.report, "NVDA")
        self.report["members"].append(self.report["members"][0])
        with self.assertRaises(ValueError):
            project_member_report(self.report, "AMD")

    def test_member_cannot_override_shared_content(self):
        self.report["members"][0]["debate_summary"] = "wrong scope"
        with self.assertRaises(ValueError):
            project_member_report(self.report, "MU")

    def test_old_shapes(self):
        for symbol in ("MU", "GENERAL"):
            old = {"symbol": symbol, "by_lens": {}} if symbol == "GENERAL" else {"symbol": symbol}
            self.assertEqual(project_member_report(old, symbol), old)

    def test_detail_reader_uses_exact_session_and_member_translation(self):
        from datalayer.pg import lowfreq_reader as reader
        con = MagicMock()
        cur = con.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {
            "report": self.report, "lang": "en",
            "report_i18n": {"zh": {"members": {"MU": {"statement": "MU translation"}}}},
        }
        group, cutoff = self.report["symbol"], self.report["market_data_timestamp_utc"]
        with patch.object(reader, "_connect_ro", return_value=con), patch.object(reader, "_load_pg_cfg"):
            mu = reader.get_debate_report_row(group, cutoff, member_symbol="MU")
            amd = reader.get_debate_report_row(group, cutoff, member_symbol="AMD")
            self.assertEqual(mu["report"]["debate_ref"], amd["report"]["debate_ref"])
            self.assertEqual(mu["report_i18n"], {"zh": {"statement": "MU translation"}})
            self.assertEqual(amd["report_i18n"], {})
            cur.reset_mock()
            cur.fetchone.return_value = None
            self.assertEqual(reader.get_debate_report_row(group, cutoff, member_symbol="MU"), {})
            self.assertEqual(cur.execute.call_count, 1)
