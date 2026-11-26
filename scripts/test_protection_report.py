import unittest
import json
import tempfile
from contextlib import chdir
from pathlib import Path

from datalayer.pg import i18n
from ruling import PROTECTION_SUMMARIES, RulingError, project_protection


def assessment(phase):
    return {"phase": phase, "basis": "Three-day stress scenario at the opening price.",
            "review": "Reassess at the next close or a reversal of the catalyst."}


class ProtectionReportTest(unittest.TestCase):
    def test_signed_general_and_localized_dashboard(self):
        import test_lens_fixtures as fixtures
        from equipoise_entry import _signal_from_debate
        from translate_reports import extract

        script = fixtures.lens_script()
        filings = {s: assessment(p) for s, p in zip(fixtures.LENSES, ("quiet", "emerging", "easing"))}
        for stage in ("proposed", "ruling"):
            script[stage][0]["protection_by_symbol"] = filings
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            result = fixtures.drive_lens_session(Path(temp), script=script, until="signoff")
            self.assertEqual(result["errors"], {})
            report = json.loads(Path(result["results"]["signoff"]["analysis_path"]).read_text("utf-8"))
        self.assertNotIn("protection_by_symbol", report)
        self.assertEqual(report["hedge_summary"], PROTECTION_SUMMARIES["quiet"])
        _, texts = extract(report, "en")
        self.assertIn((("by_lens", "QQQ", "hedge_summary"), PROTECTION_SUMMARIES["emerging"]), texts)
        overlay = {"zh": {"_leaves": {"by_lens.QQQ.hedge_summary": "测试摘要"}}}
        signal = _signal_from_debate({"report": report, "lang": "en", "report_i18n": overlay}, "zh")
        self.assertEqual(signal["by_lens"]["QQQ"]["hedge_summary"], "测试摘要")
        self.assertEqual(signal["by_lens"]["DIA"]["hedge_summary"], PROTECTION_SUMMARIES["easing"])
        self.assertEqual(signal["by_lens"]["SPY"]["operation_simple"], report["by_lens"]["SPY"]["operation_simple"])

    def test_positions_are_unnecessary(self):
        for phase, summary in PROTECTION_SUMMARIES.items():
            report = {"protection_by_symbol": {"NVDA": assessment(phase)}}
            project_protection(report, {"NVDA": report})
            self.assertEqual(report["hedge_summary"], summary)
            self.assertEqual(report["downside_protection"], assessment(phase))
            self.assertNotIn("protection_by_symbol", report)

    def test_localized_scopes_and_index_primary(self):
        scopes = {"INDEX": {"observe_only": True}, "ETF": {}}
        report = {"by_lens": scopes, "protection_by_symbol": {"ETF": assessment("persistent")}}
        project_protection(report, scopes)
        self.assertNotIn("hedge_summary", report)
        self.assertNotIn("hedge_summary", scopes["INDEX"])
        paths = {tuple(path) for path, _ in i18n.collect_leaves(report)}
        self.assertIn(("by_lens", "ETF", "hedge_summary"), paths)
        self.assertNotIn(("by_lens", "ETF", "downside_protection", "phase"), paths)

    def test_invalid_inputs_and_old_reports(self):
        with self.assertRaises(RulingError):
            project_protection({}, {"SPY": {}}, required=True)
        project_protection({}, {"INDEX": {"observe_only": True}}, required=True)
        for value in ({}, {"SPY": assessment("bull")}, {"DIA": assessment("quiet")},
                      {"SPY": dict(assessment("emerging"), quantity=3)}):
            with self.assertRaises(RulingError):
                project_protection({"protection_by_symbol": value}, {"SPY": {}})
        old = {"symbol": "SPY"}
        project_protection(old, {"SPY": old})
        self.assertEqual(old, {"symbol": "SPY"})

    def test_dashboard_preserves_phases_without_using_translated_copy(self):
        from equipoise_entry import _signal_from_debate, get_dashboard_summary
        from unittest.mock import patch

        for phase, summary in PROTECTION_SUMMARIES.items():
            report = {"symbol": "NVDA", "hedge_summary": summary,
                      "downside_protection": assessment(phase)}
            overlay = {"zh": {"_leaves": {"hedge_summary": "Localized copy unrelated to enum"}}}
            row = {"report": report, "lang": "en", "report_i18n": overlay,
                   "as_of": "20261125T025851Z"}
            signal = _signal_from_debate(row, "zh")
            self.assertEqual(signal["hedge_summary"], "Localized copy unrelated to enum")
            self.assertEqual(signal["downside_protection"], {"phase": phase})
            with patch("equipoise_entry.get_symbols", return_value=[{"symbol": "NVDA", "type": "stock"}]), \
                 patch("datalayer.pg.lowfreq_reader.get_debate_latest_batch", return_value={"NVDA": row}):
                dashboard = get_dashboard_summary("zh")
            self.assertEqual(dashboard[0]["downside_protection"], {"phase": phase})

    def test_dashboard_lens_phase_does_not_leak_to_other_scopes(self):
        from equipoise_entry import _lens_summary, _protection_summary

        report = {"hedge_summary": PROTECTION_SUMMARIES["quiet"],
                  "downside_protection": assessment("quiet"),
                  "lenses": ["SPY", "QQQ", "DIA", "INDEX"], "by_lens": {
                      "SPY": {"hedge_summary": PROTECTION_SUMMARIES["emerging"],
                              "downside_protection": assessment("emerging")},
                      "QQQ": {"hedge_summary": PROTECTION_SUMMARIES["persistent"],
                              "downside_protection": assessment("persistent")},
                      "DIA": {}, "INDEX": {"observe_only": True}}}
        result = _lens_summary(report)
        self.assertEqual(list(result), report["lenses"])
        self.assertEqual(result["SPY"]["downside_protection"], {"phase": "emerging"})
        self.assertEqual(result["QQQ"]["downside_protection"], {"phase": "persistent"})
        for lens in ["DIA", "INDEX"]:
            self.assertNotIn("downside_protection", result[lens])
        self.assertEqual(_protection_summary({}), {})
        self.assertEqual(_protection_summary({"hedge_summary": "Legacy"}), {"hedge_summary": "Legacy"})
