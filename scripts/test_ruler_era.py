"""ruler_era: one "is this a ruler (v3.4) report / buffer" test, shared by validate_debate, closure_check, ledger_tally,
rebalance_brief and postcheck_quant; each former predicate's inputs give the same answer through it."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import closure_check as cc            # noqa: E402
import ledger_tally as lt             # noqa: E402
import validate_debate as vd          # noqa: E402
from ruler_era import is_ruler_entries, is_ruler_report   # noqa: E402

SC = {"schema": "3.4", "T_days": 15, "P0": 100.0, "nodes": []}
PRE_FINAL = {"role": "Zealot", "round": "Init", "content": {"composite_score": 60, "claims": [{"id": "Z1", "stake": 4}]}}
RULER_FINAL = {"role": "Zealot", "round": "Init", "content": {"composite_score": 60, "scenario": SC}}
RULER_FINAL_STR = {"role": "Zealot", "round": "Init", "content": json.dumps({"scenario": SC})}

CASES = [   # (name, report, expected)
    ("pre-ruler v3.3 report", {"symbol": "X", "debate_history": [PRE_FINAL], "ledger_score_z": 62}, False),
    ("empty", {}, False),
    ("report scenario", {"scenario": SC}, True),
    ("PG row form: schema_version only", {"schema_version": "3.4", "q_star": 0.6}, True),
    ("history final with scenario", {"debate_history": [PRE_FINAL, RULER_FINAL]}, True),
    ("history final with scenario as a JSON string", {"debate_history": [RULER_FINAL_STR]}, True),
]


class RulerEraTest(unittest.TestCase):
    def test_report_predicate_and_its_callers_agree(self):
        for name, doc, want in CASES:
            with self.subTest(name):
                self.assertIs(is_ruler_report(doc), want)
                self.assertIs(vd.is_ruler(doc), want)                 # validate_debate (no tally marker)
                self.assertIs(cc._is_ruler_doc(doc), want)            # closure_check

    def test_entries_predicate(self):
        self.assertFalse(is_ruler_entries([PRE_FINAL]))
        self.assertFalse(is_ruler_entries([]))
        self.assertFalse(is_ruler_entries(None))
        self.assertTrue(is_ruler_entries([PRE_FINAL, RULER_FINAL]))
        self.assertTrue(is_ruler_entries([RULER_FINAL_STR]))
        for entries in ([PRE_FINAL], [RULER_FINAL]):
            self.assertIs(lt.is_ruler(entries), is_ruler_entries(entries))   # ledger_tally

    def test_tally_marker_still_counts_in_validate_debate(self):
        self.assertTrue(vd.is_ruler({}, {"zealot": {"deadlock_requested": []}}))

    def test_postcheck_general_uses_it(self):
        src = (ROOT / "workflows" / "postcheck_quant.py").read_text(encoding="utf-8")
        self.assertIn("ruler_era = is_ruler_report(report)", src)


if __name__ == "__main__":
    unittest.main()
