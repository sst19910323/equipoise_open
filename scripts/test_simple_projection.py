"""workflows/simple_projection: read-time simple card for reports archived before v3.5 (2026-11-09)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflows.simple_projection import now_simple, now_simple_for_row, project_simple  # noqa: E402


class NowSimpleTest(unittest.TestCase):
    """/api/reports `now_simple` (2026-11-10): the simple card's action word in the execution card's vocabulary."""

    def test_mapping(self):
        self.assertEqual(now_simple("HOLD_NEUTRAL", "none"), "hold")
        self.assertEqual(now_simple("MARKET_ENTRY", "market"), "buy_now")
        self.assertEqual(now_simple("LIMIT_ENTRY", "limit"), "place_limit_buy")
        self.assertEqual(now_simple("TRIM_POSITION", "market"), "sell_now")
        self.assertEqual(now_simple("TRIM_POSITION", "limit"), "place_limit_sell")
        self.assertEqual(now_simple("LIQUIDATE_NOW", "market"), "sell_all_now")
        self.assertIsNone(now_simple(None, None))
        self.assertIsNone(now_simple("SOMETHING_ELSE", "market"))

    def test_row_uses_the_stored_card_then_projects_once_then_nothing(self):
        calls = []

        def load(sym, as_of):
            calls.append((sym, as_of))
            return {"operation_simple": {"operation_type": "TRIM_POSITION", "order": "limit"}}
        self.assertEqual(now_simple_for_row({"simple_op": "MARKET_ENTRY", "simple_order": "market", "has_tree": True}, "T1", load), "buy_now")
        self.assertIsNone(now_simple_for_row({"has_tree": False, "as_of": "2026-10-20"}, "T1", load))   # before v3.4: no field
        self.assertEqual(calls, [])
        row = {"has_tree": True, "as_of": "2026-11-03T14:00"}
        self.assertEqual(now_simple_for_row(row, "T1", load), "place_limit_sell")
        self.assertEqual(now_simple_for_row(row, "T1", load), "place_limit_sell")
        self.assertEqual(len(calls), 1)                                                              # remembered

    def test_row_never_raises(self):
        def boom(sym, as_of):
            raise RuntimeError("pg down")
        self.assertIsNone(now_simple_for_row({"has_tree": True, "as_of": "x"}, "T2", boom))


class SimpleProjectionTest(unittest.TestCase):
    def test_existing_card_is_left_untouched(self):
        r = {"operation_simple": {"operation_type": "HOLD_NEUTRAL"}}
        self.assertEqual(project_simple(r)["operation_simple"], {"operation_type": "HOLD_NEUTRAL"})   # no `source` key added

    def test_pre_v34_report_gets_no_card(self):
        r = {"operation_type": "HOLD", "composite_score": 50}
        self.assertNotIn("operation_simple", project_simple(r))

    def test_failure_is_a_note_not_an_exception(self):
        r = {"odds": {"gamma": 6.25}, "scenario": {"nodes": [{"id": "A", "p": 1.0, "P_T": "x"}]}}
        out = project_simple(r)
        self.assertNotIn("operation_simple", out)
        self.assertIn("not projected", out.get("operation_simple_note", ""))


if __name__ == "__main__":
    unittest.main()
