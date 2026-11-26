# -*- coding: utf-8 -*-
"""Task 4 (C11.5): ruling, endorsement and tally per lens on the scripted three-lens session of test_lens_fixtures.

The fixture's per-lens prices are tuned so the derived operations diverge (see test_lens_fixtures' docstring): SPY's
mixture is symmetric → HOLD_NEUTRAL (S_ref 50.0); QQQ is the single-lens §1.4 fixture ×4 → ENTRY (S_ref 58.9); DIA leans
−0.1 % on a small s_s → TRIM_POSITION (S_ref 46.4); default λ 0.5 / 0.3 / 0.2 → composite 52."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import test_lens_fixtures as F
import scenario_replay as sr   # noqa: E402
from lens_views import weighted_score   # noqa: E402
from ruling import RulingError, derive_ruling   # noqa: E402


class LensRulingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="lens_ruling_"))
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.out = F.drive_lens_session(cls.tmp, until="ruling")
        cls.s = cls.out["session"]

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def obj(self, oid):
        self.assertIn(oid, self.s.store.ids(), self.out["errors"])
        return self.s.store.obj(oid)["content"]

    def arbiter_input(self, **kw):
        return dict(copy.deepcopy(F.LENS_ARBITER), **kw)

    # ---------------------------------------------------------------- derivation per lens
    def test_ruling_derives_every_lens(self):
        rec = self.obj("RULING.v1")
        self.assertEqual(list(rec["by_lens"]), F.LENSES)
        for L in F.LENSES:
            b = rec["by_lens"][L]
            self.assertEqual(b["scenario"]["mixture"]["w"], 0.5, L)
            self.assertTrue(b["scenario"]["derived"]["ok"], L)
            self.assertEqual(b["scenario"]["P0"], F.REF[L], L)
            self.assertEqual(b["scenario"]["derived"]["unit_basis"]["s_s"], F.s_s_of(L), L)
            self.assertEqual(b["scenario"]["derived"]["unit_basis"]["data_symbol"], L, L)
            self.assertAlmostEqual(b["s_ref"], b["scenario"]["derived"]["s_ref"])
            self.assertEqual(round(b["s_ref"], 1), F.EXPECTED_S_REF[L], L)
            self.assertIsInstance(b["operation_target"], float, L)
            simple = b["operation_simple"]   # the simple card: same distribution, no stop, never replacing the full card
            self.assertEqual(simple["x_star"], b["scenario"]["derived"]["simple"]["x_star"], L)
            self.assertIsInstance(simple["operation_target"], float, L)
            self.assertNotEqual(simple["operation_type"], "ENTRY", L)   # mapped to MARKET_ENTRY / LIMIT_ENTRY
        self.assertEqual(rec["operation_simple"]["operation_type"], rec["by_lens"]["SPY"]["operation_simple"]["operation_type"])
        self.assertEqual(rec["operation_simple"]["operation_target"],
                         "; ".join(f"{L}={rec['by_lens'][L]['operation_simple']['operation_target']:g}" for L in F.LENSES))
        want = "; ".join(f"{L}={F.REF[L]:g}" for L in F.LENSES)
        self.assertEqual(rec["operation_target"], want)
        self.assertEqual(rec["operation_type"], rec["by_lens"]["SPY"]["operation_type"])
        self.assertEqual(rec["operation_size"], rec["by_lens"]["SPY"]["operation_size"])
        self.assertEqual(rec["scenario"], rec["by_lens"]["SPY"]["scenario"])
        self.assertEqual(rec["lens_weights_default"], F.WEIGHTS)
        self.assertEqual(rec["lens_weights"], F.WEIGHTS)
        self.assertEqual(rec["authored_score"], 95)

    def test_divergent_operations(self):
        rec = self.obj("RULING.v1")
        for L, op in F.EXPECTED_OPS.items():
            self.assertEqual(rec["by_lens"][L]["operation_type"], op, L)
        self.assertEqual(rec["by_lens"]["QQQ"]["operation_type"], "ENTRY")
        self.assertEqual(rec["by_lens"]["DIA"]["operation_type"], "TRIM_POSITION")
        self.assertEqual(rec["by_lens"]["DIA"]["operation_execution"]["stop"]["level"], 318.0)

    def test_composite_is_lambda_weighted(self):
        rec = self.obj("RULING.v1")
        per = {L: rec["by_lens"][L]["composite_score"] for L in F.LENSES}
        self.assertEqual(per, {"SPY": 50, "QQQ": 59, "DIA": 46})
        self.assertEqual(rec["composite_score"], weighted_score(per, rec["lens_weights_default"]))
        self.assertEqual(rec["composite_score"], 52)

    def test_provisional_and_proposed_are_lens_wise(self):
        for oid in ("PROV", "PROPOSED"):
            rec = self.obj(oid)
            self.assertEqual(list(rec["by_lens"]), F.LENSES, oid)
            self.assertEqual(rec["composite_score"], 52, oid)
            self.assertEqual(rec["by_lens"]["QQQ"]["operation_type"], "ENTRY", oid)

    def test_meta_freezes_lenses(self):
        meta = json.loads((self.s.store.dir / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["lenses"], F.LENSES)
        self.assertEqual(meta["lens_weights_default"], F.WEIGHTS)

    # ---------------------------------------------------------------- λ rules
    def test_lens_weights_rules(self):
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(lens_weights={"SPY": 0.48, "QQQ": 0.3, "DIA": 0.2}),
                          "proposed")
        self.assertIn("sum", str(cm.exception))
        tilt = {"SPY": 0.2, "QQQ": 0.6, "DIA": 0.2}
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(lens_weights=tilt), "proposed")
        self.assertIn("lens_weights_reason", str(cm.exception))
        rec = derive_ruling(self.s.store, self.s, self.arbiter_input(
            lens_weights={"SPY": 0.2, "QQQ": 0.605, "DIA": 0.2}, lens_weights_reason="Z1 is a QQQ driver"), "proposed")
        lam = rec["lens_weights"]
        self.assertAlmostEqual(sum(lam.values()), 1.0)
        self.assertAlmostEqual(lam["QQQ"], 0.605 / 1.005)
        self.assertEqual(rec["lens_weights_reason"], "Z1 is a QQQ driver")
        self.assertEqual(rec["lens_weights_default"], F.WEIGHTS)
        per = {L: rec["by_lens"][L]["composite_score"] for L in F.LENSES}
        self.assertEqual(rec["composite_score"], weighted_score(per, lam))
        # λ touches only the summary score: every lens's operation is the default-λ one
        base = self.obj("RULING.v1")
        for L in F.LENSES:
            self.assertEqual(rec["by_lens"][L]["operation_type"], base["by_lens"][L]["operation_type"], L)
            self.assertEqual(rec["by_lens"][L]["composite_score"], base["by_lens"][L]["composite_score"], L)

    def test_mixture_reason_rule_applies_lens_wise(self):
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(mixture={"w": 0.9, "reason": "trust me"}), "proposed")
        self.assertIn("mixture.reason", str(cm.exception))

    def test_void_on_unknown_lens_refused(self):
        v = {"effects": [{"claim": "Z1", "side": "R", "outcome": "true", "reason": "r", "lens": "IWM"}]}
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(voided=v), "proposed")
        self.assertIn("lens 'IWM'", str(cm.exception))

    def test_void_scoped_to_one_lens(self):
        # R carried Z1#true by its update, so a void of it fails on every lens it reaches: with `lens` only on that lens
        v = {"effects": [{"claim": "Z1", "side": "R", "outcome": "true", "reason": "op misfiled", "lens": "QQQ"}]}
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(voided=v), "proposed")
        self.assertIn("lens QQQ:", str(cm.exception))
        self.assertNotIn("lens SPY:", str(cm.exception))
        # unscoped (A3, final wave): tried on every lens the effect touches — R1's if_true has ops on QQQ only — and
        # refused there; SPY and DIA, where the effect has no ops, are not tried
        v["effects"][0].pop("lens")
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, self.arbiter_input(voided=v), "proposed")
        self.assertIn("lens QQQ:", str(cm.exception))
        for L in ("SPY", "DIA"):
            self.assertNotIn(f"lens {L}:", str(cm.exception))

    def test_authored_by_lens_refused(self):
        for key in ("by_lens", "lens_weights_default"):
            with self.assertRaises(RulingError) as cm:
                derive_ruling(self.s.store, self.s, self.arbiter_input(**{key: {}}), "proposed")
            self.assertIn(key, str(cm.exception))

    # ---------------------------------------------------------------- endorsement per lens
    def test_objection_endorsement_per_lens(self):
        obj = self.obj("OBJ1.R")
        ebl = obj["endorsed_scenario_by_lens"]
        self.assertEqual(list(ebl), F.LENSES)
        last = self.s.store.obj("R2.R")["content"]["scenario_by_lens"]
        self.assertEqual(ebl["QQQ"], sr.apply_ops(last["QQQ"], [{"op": "p_move", "from": "C", "to": "B", "amount": 0.05}]))
        for L in ("SPY", "DIA"):
            self.assertEqual(ebl[L], last[L], L)
        self.assertEqual(obj["endorsed_scenario"], ebl["SPY"])

    def test_endorsement_per_lens(self):
        before = self.s._with_endorsement("Zealot", {"reservation": "z"}, signoff=True)
        after = self.s._with_endorsement("Zealot", {"reservation": "z", "endorsed_composite_score": 11,
                                                    "scenario_revision": copy.deepcopy(F.Z_SIGN_REV)}, signoff=True)
        last = self.s.store.obj("R2.Z")["content"]["scenario_by_lens"]
        for L in ("SPY", "DIA"):
            self.assertEqual(after["endorsed_scenario_by_lens"][L], last[L], L)
            self.assertEqual(after["endorsed_composite_by_lens"][L], before["endorsed_composite_by_lens"][L], L)
        self.assertNotEqual(after["endorsed_scenario_by_lens"]["QQQ"], last["QQQ"])
        self.assertLess(after["endorsed_composite_by_lens"]["QQQ"], before["endorsed_composite_by_lens"]["QQQ"])
        self.assertEqual(after["endorsed_composite_score"],
                         weighted_score(after["endorsed_composite_by_lens"], self.s.state["lens_weights_default"]))
        self.assertEqual(after["authored_score"], 11)
        self.assertEqual(after["endorsed_scenario"], after["endorsed_scenario_by_lens"]["SPY"])

    def test_signoff_dissent_per_lens(self):
        sides = {r: self.s._with_endorsement(r, {"reservation": r[0].lower()}, signoff=True) for r in ("Zealot", "Reaper")}
        dis = self.s._signoff_dissent(sides, 1)
        for k in ("zealot", "reaper"):
            self.assertEqual(list(dis[f"{k}_endorsed_scenario_by_lens"]), F.LENSES)
            self.assertEqual(dis[f"{k}_endorsed_scenario"], dis[f"{k}_endorsed_scenario_by_lens"]["SPY"])
            self.assertEqual(dis[f"{k}_composite_score"], sides[k.capitalize()]["endorsed_composite_score"])

    def test_unknown_lens_in_revision_refused(self):
        bad = dict(copy.deepcopy(F.Z_SIGN_REV), ops=[{"op": "p_move", "from": "A", "to": "B", "amount": 0.05}])
        from run_step import StepError
        with self.assertRaises(StepError) as cm:
            self.s._with_endorsement("Zealot", {"scenario_revision": bad}, signoff=True)
        self.assertIn("lens", str(cm.exception))

    # ---------------------------------------------------------------- tally per lens
    def test_tally_lines_per_lens(self):
        res = self.s._scenario_lines(2)
        text = "\n".join(res["lines"])
        self.assertIn("Z QQQ scenario", text)
        self.assertIn("R DIA scenario", text)
        self.assertTrue(res["lines"][0].startswith("[Scenario ruler 3.4"))
        self.assertEqual(set(res["by_side"]["Zealot"]), set(F.LENSES))
        self.assertTrue(res["by_side"]["Reaper"]["QQQ"]["ok"])
        tally = (self.s.store.dir / "_tally_R2.txt").read_text(encoding="utf-8")
        self.assertIn("Z SPY scenario", tally)


if __name__ == "__main__":
    unittest.main()
