# -*- coding: utf-8 -*-
"""Tests for scripts/lens_views.py (C11 lens projection)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import lens_views as lv  # noqa: E402

LENSES = ["SPY", "QQQ", "DIA"]


def _op(lens, **kw):
    d = {"op": "p_move", "node": "n1", "delta": 0.1}
    d.update(kw)
    if lens is not None:
        d["lens"] = lens
    return d


def _content():
    return {
        "scenario_by_lens": {L: {"nodes": [{"id": "n1", "p": 0.5, "end": 100 + i}]} for i, L in enumerate(LENSES)},
        "claims": [{"id": "Z1", "effect": {"if_true": [_op("SPY"), _op("QQQ")], "if_false": []}}],
        "scenario_updates": [{"claim": "Z1", "ops": [_op("SPY"), _op("DIA")]}],
        "other": "kept",
    }


def _odds():
    by = {L: {"reference_price": 100.0 + i, "currency": "USD", "data_symbol": L, "atr_pct_14": 1.0 + i,
              "s_s": 0.5 + i, "cost_bps": 3.0 + i} for i, L in enumerate(LENSES)}
    return {"reference_price": 100.0, "cost_bps": 3.0, "unit_basis": {"s_s": 0.5}, "q_ref": 0.5,
            "by_lens": by, "lens_weights_default": {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}}


class LensViewsTest(unittest.TestCase):
    def test_project_content_filters_ops_and_strips_lens(self):
        c = _content()
        p = lv.project_content(c, "QQQ", LENSES)
        self.assertEqual(p["scenario"], c["scenario_by_lens"]["QQQ"])
        self.assertEqual(p["claims"][0]["effect"]["if_true"], [_op(None)])
        self.assertEqual(p["scenario_updates"][0]["ops"], [])
        self.assertNotIn("scenario_by_lens", p)
        self.assertEqual(p["other"], "kept")
        self.assertIn("scenario_by_lens", c)  # input untouched

    def test_project_identity_for_single_lens(self):
        c = {"scenario": {"nodes": []}, "claims": []}
        p = lv.project_content(c, "X", [])
        self.assertIsNot(p, c)
        self.assertEqual(p, c)

    def test_missing_lens_raises(self):
        c = _content()
        del c["scenario_by_lens"]["DIA"]
        with self.assertRaises(lv.LensError) as cm:
            lv.project_content(c, "DIA", LENSES)
        self.assertIn("lens DIA", str(cm.exception))

    def test_plain_scenario_in_multi_lens_raises(self):
        with self.assertRaises(lv.LensError) as cm:
            lv.project_content({"scenario": {"nodes": []}}, "SPY", LENSES)
        self.assertIn("scenario_by_lens missing", str(cm.exception))

    def test_lenses_of(self):
        self.assertEqual(lv.lenses_of({"lenses": ["SPY", "QQQ"]}), ["SPY", "QQQ"])
        self.assertEqual(lv.lenses_of({"lenses": ["N225"]}), [])
        self.assertEqual(lv.lenses_of({}), [])
        self.assertTrue(lv.is_multi({"lenses": LENSES}))
        self.assertFalse(lv.is_multi({"lenses": None}))

    def test_project_record_and_report(self):
        rec = {"seq": 1, "id": "r1", "role": "zealot", "round": 1, "content": _content()}
        pr = lv.project_record(rec, "SPY", LENSES)
        self.assertEqual(pr["id"], "r1")
        self.assertEqual(pr["content"]["scenario_updates"][0]["ops"], [_op(None)])
        doc = {
            "operation_type": "x",
            "by_lens": {L: {"scenario": {"s": L}, "operation_type": "BUY", "operation_target": 700 + i,
                            "operation_size": 1, "operation_execution": "m", "composite_score": 50 + i,
                            "s_ref": 1, "s_ref_z": 2, "s_ref_r": 3} for i, L in enumerate(LENSES)},
            "debate_history": [rec],
            "provisional_ruling": _content(),
            "dissent": {"zealot_endorsed_scenario_by_lens": {L: {"e": L} for L in LENSES}},
            "odds": _odds(),
            "objections": [{"scenario_revision": {"ops": [_op("QQQ"), _op("DIA")]}}],
            "unknown": 7,
        }
        out = lv.project_report(doc, "QQQ", LENSES)
        self.assertEqual(out["scenario"], {"s": "QQQ"})
        self.assertEqual(out["operation_target"], 701)
        self.assertEqual(out["composite_score"], 51)
        self.assertEqual(out["debate_history"][0]["content"]["scenario"], _content()["scenario_by_lens"]["QQQ"])
        self.assertEqual(out["provisional_ruling"]["scenario"], _content()["scenario_by_lens"]["QQQ"])
        self.assertEqual(out["dissent"]["zealot_endorsed_scenario"], {"e": "QQQ"})
        self.assertEqual(out["odds"]["reference_price"], 101.0)
        self.assertEqual(out["objections"][0]["scenario_revision"]["ops"], [_op(None)])
        self.assertEqual(out["atr_pct_14"], 2.0)
        self.assertEqual(out["unknown"], 7)
        with self.assertRaises(lv.LensError):
            lv.project_report(doc, "ZZZ", LENSES)

    def test_project_report_does_not_inherit_primary_keys(self):
        """A lens without a per-lens key (a HOLD lens has no operation_execution) never shows the primary lens's value,
        in the report and in a derived provisional ruling."""
        def entry(L, execution):
            e = {"scenario": {"s": L}, "operation_type": "HOLD_NEUTRAL", "operation_target": 400.0,
                 "operation_size": "N/A", "composite_score": 50, "s_ref": 50.0}
            if execution:
                e.update(operation_type="MARKET_ENTRY", operation_execution={"stop": {"level": 380.0, "semantics": "close"}})
            return e
        by = {"SPY": entry("SPY", True), "QQQ": entry("QQQ", False), "DIA": entry("DIA", True)}
        doc = {"by_lens": by, "scenario": by["SPY"]["scenario"], "operation_type": "MARKET_ENTRY",
               "operation_execution": by["SPY"]["operation_execution"], "s_ref_z": 60.0,
               "provisional_ruling": {"by_lens": by, "scenario": by["SPY"]["scenario"],
                                      "operation_execution": by["SPY"]["operation_execution"]}}
        out = lv.project_report(doc, "QQQ", LENSES)
        self.assertNotIn("operation_execution", out)
        self.assertNotIn("s_ref_z", out)
        self.assertEqual(out["operation_type"], "HOLD_NEUTRAL")
        self.assertNotIn("operation_execution", out["provisional_ruling"])
        self.assertEqual(lv.project_report(doc, "DIA", LENSES)["operation_execution"], by["DIA"]["operation_execution"])

    def test_ops_lens_errors(self):
        e = lv.ops_lens_errors("w", [_op(None)], LENSES)
        self.assertEqual(len(e), 1)
        self.assertIn("op[0] has no lens", e[0])
        e = lv.ops_lens_errors("w", [_op("XYZ")], LENSES)
        self.assertIn("is not a session lens", e[0])
        e = lv.ops_lens_errors("w", [_op("SPY")], [])
        self.assertIn("single-lens session", e[0])
        self.assertEqual(lv.ops_lens_errors("w", [_op("SPY")], LENSES), [])
        self.assertEqual(lv.ops_lens_errors("w", [_op(None)], []), [])

    def test_lens_snapshot_substitutes_cost_unit_price(self):
        o = _odds()
        s = lv.lens_snapshot(o, "QQQ")
        self.assertEqual(s["cost_bps"], o["by_lens"]["QQQ"]["cost_bps"])
        self.assertEqual(s["reference_price"], o["by_lens"]["QQQ"]["reference_price"])
        self.assertEqual(s["unit_basis"]["s_s"], o["by_lens"]["QQQ"]["s_s"])
        self.assertNotIn("cost_bps", s["unit_basis"])
        self.assertNotIn("reference_price", s["unit_basis"])
        self.assertIn("by_lens", s)
        self.assertEqual(s["lens_weights_default"], o["lens_weights_default"])
        plain = {"reference_price": 1.0, "cost_bps": 2.0, "unit_basis": {"s_s": 1}}
        self.assertEqual(lv.lens_snapshot(plain, "SPY"), plain)
        with self.assertRaises(lv.LensError):
            lv.lens_snapshot(o, "ZZZ")

    def test_lens_weights_rules(self):
        d = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
        self.assertEqual(lv.lens_weights_errors(None, d, LENSES, None, set()), ([], d))
        errs, lam = lv.lens_weights_errors({"SPY": 0.6, "QQQ": 0.3, "DIA": 0.1}, d, LENSES, None, set())
        self.assertEqual(errs, [])
        self.assertAlmostEqual(sum(lam.values()), 1.0)
        big = {"SPY": 0.9, "QQQ": 0.05, "DIA": 0.05}
        errs, _ = lv.lens_weights_errors(big, d, LENSES, None, {"Z1"})
        self.assertTrue(any("lens_weights_reason" in e for e in errs))
        errs, _ = lv.lens_weights_errors(big, d, LENSES, "Z1 shows tech leads", {"Z1"})
        self.assertEqual(errs, [])
        errs, _ = lv.lens_weights_errors(big, d, LENSES, "no ids here", {"Z1"})
        self.assertTrue(errs)
        for bad in ("Z10", "XZ1", "Z12 only"):
            errs, _ = lv.lens_weights_errors(big, d, LENSES, bad, {"Z1"})
            self.assertTrue(errs, bad)
        errs, _ = lv.lens_weights_errors(big, d, LENSES, "see Z1 and R.EV2", {"Z1"})
        self.assertEqual(errs, [])
        errs, _ = lv.lens_weights_errors({"SPY": 0.5, "QQQ": 0.25, "DIA": 0.2}, d, LENSES, None, set())
        self.assertTrue(any("sum" in e for e in errs))
        errs, _ = lv.lens_weights_errors({"SPY": 0.5, "QQQ": 0.5}, d, LENSES, None, set())
        self.assertTrue(any("DIA" in e for e in errs))
        errs, _ = lv.lens_weights_errors({"SPY": 0.6, "QQQ": 0.5, "DIA": -0.1}, d, LENSES, "Z1", {"Z1"})
        self.assertTrue(errs)

    def test_weighted_score_and_join_targets(self):
        lam = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
        self.assertEqual(lv.weighted_score({"SPY": 60, "QQQ": 70, "DIA": 40}, lam), 59)
        self.assertEqual(lv.weighted_score({"A": 61, "B": 60}, {"A": 0.5, "B": 0.5}), 60)  # exact .5: round() half-even
        by = {"SPY": {"operation_target": 769}, "QQQ": {"operation_target": 703.5},
              "DIA": {"operation_target": 512.0}}
        self.assertEqual(lv.join_targets(by, LENSES), "SPY=769; QQQ=703.5; DIA=512")

    def test_fmt_level_matches_ruling(self):
        import ruling  # lazy: ruling will import lens_views
        for v in (769, 703.5, 512.0, 1234.567):
            self.assertEqual(lv.fmt_level(v), ruling.fmt_level(v))

    def test_default_weights_from_config(self):
        self.assertEqual(lv.default_weights(ROOT, "GENERAL"), {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2})
        self.assertEqual(lv.default_weights(ROOT, "GENERAL_EU"), {"DAX": 0.6, "IAEX": 0.4})

    def test_default_weights_missing_raises(self):
        with self.assertRaises(lv.LensError):
            lv.default_weights(ROOT, "AAPL")


if __name__ == "__main__":
    unittest.main()
