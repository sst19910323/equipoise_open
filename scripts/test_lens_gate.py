# -*- coding: utf-8 -*-
"""Task 5 (C11.6): the release gate and the assembler on a lens-wise report.

The scripted three-lens session of test_lens_fixtures runs through signoff; its analysis JSON (a lens-wise report:
`by_lens`, `lenses`, `lens_weights*`) must pass the gate, and every hand edit of a per-lens derived number, of λ, of the
display target string or of a primary-lens compatibility field must be FATAL naming the lens / field. A single-lens
GENERAL ruler report (no `by_lens`) takes the unchanged single-lens checks."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import test_lens_fixtures as F
import validate_debate as vd   # noqa: E402
from lens_views import weighted_score   # noqa: E402


def _summary(vals: dict, lam: dict) -> float:
    return sum(lam[L] * vals[L] for L in lam)


class LensGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="lens_gate_"))
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.out = F.drive_lens_session(cls.tmp, until="signoff")
        rc = cls.out["results"].get("signoff") or {}
        cls.doc = (json.loads((cls.tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
                   if rc.get("analysis_path") else None)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def d(self):
        self.assertIsNotNone(self.doc, self.out["errors"])
        return copy.deepcopy(self.doc)

    def fatal(self, doc):
        return vd.validate(doc)[0]

    # ---------------------------------------------------------------- the clean report
    def test_lenswise_report_passes_gate(self):
        self.assertEqual(self.out["errors"], {})
        doc = self.d()
        self.assertEqual(doc["lenses"], F.LENSES)
        self.assertEqual(list(doc["by_lens"]), F.LENSES)
        self.assertEqual(self.fatal(doc), [])
        self.assertTrue(self.out["results"]["signoff"]["gate"]["ok"])

    def test_assembler_derives_execution_per_lens(self):
        doc = self.d()
        b = doc["by_lens"]
        self.assertEqual(b["QQQ"]["operation_type"], "MARKET_ENTRY")          # 400 = QQQ's reference price
        self.assertEqual(b["QQQ"]["operation_execution"]["derived"], "MARKET_ENTRY")
        self.assertEqual(b["QQQ"]["operation_execution"]["stop"]["level"], 380.0)   # the derived contract kept
        self.assertEqual(b["SPY"]["operation_type"], "HOLD_NEUTRAL")
        self.assertEqual(b["DIA"]["operation_type"], "TRIM_POSITION")
        self.assertEqual(doc["operation_type"], b["SPY"]["operation_type"])   # the primary lens's family
        self.assertEqual(doc["operation_target"], "SPY=500; QQQ=400; DIA=300")
        self.assertEqual(doc["scenario"], b["SPY"]["scenario"])

    def test_assembler_summaries_and_dissent_per_lens(self):
        doc = self.d()
        b, lam0 = doc["by_lens"], doc["lens_weights_default"]
        self.assertEqual(lam0, F.WEIGHTS)
        self.assertEqual(doc["composite_score"], 52)
        for k in ("s_ref", "s_ref_z", "s_ref_r"):
            for L in F.LENSES:
                self.assertIsInstance(b[L].get(k), float, (k, L))
            self.assertAlmostEqual(doc[k], _summary({L: b[L][k] for L in F.LENSES}, lam0), places=6, msg=k)
        dis = doc["dissent"]
        for side in ("zealot", "reaper"):
            per = dis[f"{side}_composite_by_lens"]
            self.assertEqual(list(per), F.LENSES)
            self.assertEqual(dis[f"{side}_composite_score"], weighted_score(per, lam0))

    # ---------------------------------------------------------------- per-lens re-derivation (Review Focus 3)
    def test_gate_recomputes_every_lens(self):
        doc = self.d()
        doc["by_lens"]["QQQ"]["composite_score"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("lens QQQ" in x and "composite_score" in x for x in f), f)
        self.assertFalse(any("lens SPY" in x or "lens DIA" in x for x in f), f)
        doc = self.d()
        d = doc["by_lens"]["DIA"]["scenario"]["derived"]
        d["operation_type"] = "HOLD_NEUTRAL" if d["operation_type"] != "HOLD_NEUTRAL" else "ENTRY"
        f = self.fatal(doc)
        self.assertTrue(any("lens DIA" in x and "recompute mismatch" in x for x in f), f)

    def test_gate_rebuilds_each_lens_from_history(self):
        doc = self.d()
        doc["by_lens"]["DIA"]["scenario"]["mixture"]["z_prime"]["nodes"][0]["P_T"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("lens DIA" in x and "z_prime" in x for x in f), f)

    def test_lens_set_is_the_frozen_one(self):
        doc = self.d()
        del doc["by_lens"]["DIA"]
        f = self.fatal(doc)
        self.assertTrue(any("DIA" in x and "by_lens" in x for x in f), f)
        doc = self.d()
        doc["lenses"] = ["SPY", "DIA", "QQQ"]
        f = self.fatal(doc)
        self.assertTrue(any("lenses" in x for x in f), f)

    # ---------------------------------------------------------------- display string + compatibility fields
    def test_target_string_order_and_values(self):
        doc = self.d()
        doc["operation_target"] = "QQQ=400; SPY=500; DIA=300"
        f = self.fatal(doc)
        self.assertTrue(any("operation_target" in x for x in f), f)
        doc = self.d()
        doc["operation_target"] = "SPY=500; QQQ=401; DIA=300"
        f = self.fatal(doc)
        self.assertTrue(any("operation_target" in x for x in f), f)
        doc = self.d()
        doc["by_lens"]["DIA"]["operation_target"] = 301.0
        f = self.fatal(doc)
        self.assertTrue(any("lens DIA" in x and "operation_target" in x for x in f), f)

    def test_primary_compat_fields(self):
        doc = self.d()
        doc["operation_type"] = "TRIM_POSITION"
        f = self.fatal(doc)
        self.assertTrue(any("operation_type" in x and "SPY" in x for x in f), f)
        doc = self.d()
        doc["operation_size"] = "STANDARD_SIZE"
        f = self.fatal(doc)
        self.assertTrue(any("operation_size" in x for x in f), f)
        doc = self.d()
        doc["scenario"] = copy.deepcopy(doc["by_lens"]["QQQ"]["scenario"])
        f = self.fatal(doc)
        self.assertTrue(any("scenario" in x and "SPY" in x for x in f), f)

    # ---------------------------------------------------------------- λ
    def test_lambda_rechecked_at_gate(self):
        doc = self.d()
        doc["lens_weights"]["SPY"] = 0.9
        f = self.fatal(doc)
        self.assertTrue(any("lens_weights" in x for x in f), f)
        doc = self.d()
        doc["lens_weights"] = {"SPY": 0.42, "QQQ": 0.38, "DIA": 0.2}   # within 0.1 of the default: no reason needed
        f = self.fatal(doc)
        self.assertTrue(any("composite_score" in x and "lens_weights" in x for x in f), f)
        doc["composite_score"] = weighted_score({L: doc["by_lens"][L]["composite_score"] for L in F.LENSES},
                                                doc["lens_weights"])
        self.assertNotEqual(doc["composite_score"], 52)
        self.assertFalse(any("lens_weights" in x for x in self.fatal(doc)), self.fatal(doc))
        doc = self.d()
        doc["lens_weights"] = {"SPY": 0.2, "QQQ": 0.6, "DIA": 0.2}
        doc["composite_score"] = weighted_score({L: doc["by_lens"][L]["composite_score"] for L in F.LENSES},
                                                doc["lens_weights"])
        f = self.fatal(doc)
        self.assertTrue(any("lens_weights_reason" in x for x in f), f)
        doc = self.d()
        doc["lens_weights_default"] = {"SPY": 0.4, "QQQ": 0.4, "DIA": 0.2}
        f = self.fatal(doc)
        self.assertTrue(any("lens_weights_default" in x for x in f), f)

    def test_provisional_and_dissent_summaries(self):
        doc = self.d()
        doc["provisional_ruling"]["composite_score"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("provisional_ruling.composite_score" in x for x in f), f)
        doc = self.d()
        doc["dissent"]["zealot_composite_score"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("dissent.zealot_composite_score" in x for x in f), f)
        doc = self.d()
        doc["dissent"]["reaper_composite_by_lens"]["QQQ"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("lens QQQ" in x and "dissent.reaper_composite_score" in x for x in f), f)

    def _forge_w(self, doc, L, w):
        """Re-mix lens L with weight w and re-derive everything downstream consistently (a forged but internally
        consistent lens): only the one-mixture rule can catch it."""
        import lens_views as lv
        import odds_model as om
        import scenario_replay as sr
        b = doc["by_lens"][L]
        old = b["scenario"]
        mix = old["mixture"]
        A = sr.mixture(mix["z_prime"], mix["r_prime"], w, mix.get("reason"))
        pdoc = lv.project_report(doc, L, doc["lenses"])
        cands = vd._candidates(pdoc, A)
        cfg = om.config_from_snapshot(lv.lens_snapshot(doc["odds"], L))
        d = om.derive_for_report(A, cands, cfg, atr_pct=doc["odds"]["by_lens"][L]["atr_pct_14"])
        self.assertTrue(d["ok"], d)
        A["derived"] = dict(d, s_ref=d["score"]["S_ref"], unit_basis=cfg.get("unit_basis"), warn=list(d.get("warn") or []),
                            replay=old["derived"]["replay"], stage=old["derived"]["stage"])
        b["scenario"] = A
        b["composite_score"] = int(round(d["score"]["S_ref"]))
        b["s_ref"] = d["score"]["S_ref"]
        import ruling as rl
        b["operation_simple"] = dict(rl.simple_fields(d, None, None), operation_target=float(d["simple"]["operation_target"]))
        doc["operation_simple"] = rl.simple_summary(doc["by_lens"], doc["lenses"])
        self.assertEqual(b["operation_type"].replace("MARKET_", "").replace("LIMIT_", ""), d["operation_type"])
        lam = doc["lens_weights"]
        doc["composite_score"] = weighted_score({x: doc["by_lens"][x]["composite_score"] for x in F.LENSES}, lam)
        doc["s_ref"] = round(_summary({x: doc["by_lens"][x]["s_ref"] for x in F.LENSES}, doc["lens_weights_default"]), 6)
        return doc

    def test_one_mixture_weight_for_every_lens(self):
        doc = self._forge_w(self.d(), "QQQ", 0.55)
        f = self.fatal(doc)
        self.assertTrue(any("lens QQQ" in x and "mixture" in x and "one w" in x for x in f), f)
        self.assertEqual([x for x in f if "one w" not in x], [], f)   # consistent otherwise: only the rule catches it

    def test_simple_card_tampering_is_caught(self):
        doc = self.d()
        doc["by_lens"]["QQQ"]["operation_simple"]["q_star"] = 123
        self.assertTrue(any("operation_simple" in x for x in self.fatal(doc)))
        doc = self.d()
        doc["operation_simple"]["operation_type"] = "LIQUIDATE_NOW"
        self.assertTrue(any("operation_simple" in x for x in self.fatal(doc)))
        doc = self.d()
        doc["by_lens"]["DIA"].pop("operation_simple")
        self.assertTrue(any("operation_simple" in x for x in self.fatal(doc)))

    def test_projection_carries_the_lens_own_simple_card(self):
        import lens_views as lv
        doc = self.d()
        for L in F.LENSES:
            self.assertEqual(lv.project_report(doc, L, doc["lenses"])["operation_simple"], doc["by_lens"][L]["operation_simple"], L)

    def test_history_summary_scores_checked(self):
        doc = self.d()
        doc["debate_history"][1]["content"]["composite_score"] += 1
        f = self.fatal(doc)
        self.assertTrue(any("history[1]" in x and "composite_by_lens" in x for x in f), f)
        doc = self.d()
        del doc["debate_history"][2]["content"]["composite_by_lens"]["DIA"]
        f = self.fatal(doc)
        self.assertTrue(any("history[2]" in x and "DIA" in x for x in f), f)
        doc = self.d()
        del doc["debate_history"][0]["content"]["composite_by_lens"]
        f = self.fatal(doc)
        self.assertTrue(any("history[0]: composite_by_lens missing" in x for x in f), f)

    def test_lens_weights_recorded(self):
        doc = self.d()
        del doc["lens_weights"]
        f = self.fatal(doc)
        self.assertTrue(any("lens_weights missing" in x for x in f), f)

    def test_summary_s_ref_checked(self):
        doc = self.d()
        doc["s_ref"] = doc["by_lens"]["SPY"]["s_ref"]   # the primary's, not the default-weighted summary
        f = self.fatal(doc)
        self.assertTrue(any("s_ref" in x for x in f), f)

    def test_provisional_lens_set_is_the_frozen_one(self):
        """B6: provisional_ruling.by_lens obeys the final's rule: an extra lens, a wrong order or a dropped lens is
        FATAL naming the lens."""
        doc = self.d()
        pb = doc["provisional_ruling"]["by_lens"]
        pb["IWM"] = copy.deepcopy(pb["SPY"])
        f = self.fatal(doc)
        self.assertTrue(any("provisional_ruling.by_lens" in x and "IWM" in x for x in f), f)
        doc = self.d()
        pb = doc["provisional_ruling"]["by_lens"]
        doc["provisional_ruling"]["by_lens"] = {L: pb[L] for L in ("QQQ", "SPY", "DIA")}
        f = self.fatal(doc)
        self.assertTrue(any("provisional_ruling.by_lens order" in x and "QQQ" in x for x in f), f)
        doc = self.d()
        del doc["provisional_ruling"]["by_lens"]["DIA"]
        f = self.fatal(doc)
        self.assertTrue(any("lens DIA: provisional_ruling.by_lens missing" in x for x in f), f)


class SingleLensGeneralTest(unittest.TestCase):
    """C11.6 compatibility: a single-lens GENERAL ruler report (no by_lens) goes through the dispatch unchanged."""

    def test_single_lens_general_report_untouched(self):
        import test_v34_review_fixes as t_rev
        import test_v34_flow as t_flow
        fl = t_rev._run(t_flow.ruler_script(), general=True)
        doc = fl["a"]
        self.assertNotIn("by_lens", doc)
        cwd = os.getcwd()
        os.chdir(fl["tmp"])
        try:
            self.assertEqual(vd.checks(copy.deepcopy(doc)), vd._checks_one(copy.deepcopy(doc)))
            self.assertEqual(vd.checks(copy.deepcopy(doc), True), vd._checks_one(copy.deepcopy(doc), True))
            self.assertEqual(vd.validate(copy.deepcopy(doc))[0], [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(fl["tmp"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
