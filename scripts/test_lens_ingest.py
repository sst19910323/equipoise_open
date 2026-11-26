# -*- coding: utf-8 -*-
"""Task 3 (C11.2-C11.4): per-lens ingestion of a multi-lens debater final (scenario_by_lens, lens-tagged ops)."""
import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("debate_driver", ROOT / "scripts" / "debate_driver.py")
dd = importlib.util.module_from_spec(spec); spec.loader.exec_module(dd)
import scenario_replay as sr  # noqa: E402
from lens_views import weighted_score  # noqa: E402
from validate_role_record import ingest  # noqa: E402

LENSES = ["SPY", "QQQ", "DIA"]
REF = {"SPY": 500.0, "QQQ": 400.0, "DIA": 300.0}
LAM = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
MOVE = {"op": "p_move", "from": "C", "to": "A", "amount": 0.10}


def scaled(sc, L):
    """The single-lens fixture scenario on lens L's price scale (P0 = REF[L])."""
    k = REF[L] / 100.0
    s = copy.deepcopy(sc)
    s["P0"] = REF[L]
    for n in s["nodes"]:
        n["P_T"] *= k
        for e in n["events"]:
            e["price"] *= k
    return s


def tag(ops, *lenses):
    return [dict(o, lens=L) for L in lenses for o in ops]


def multi_final(score, claims, base, updates=(), p0=None):
    """A debater final on three lenses; `base` = the single-lens scenario template. `p0` overrides a lens' P0."""
    r = dd.ruler_final(score, claims, base, updates)
    del r["scenario"]
    r["scenario_by_lens"] = {L: scaled(base, L) for L in LENSES}
    for L, v in (p0 or {}).items():
        r["scenario_by_lens"][L]["P0"] = v
    return r


def ing(rec, role, rnd, history, **kw):
    kw.setdefault("lenses", LENSES)
    kw.setdefault("ref_prices", REF)
    kw.setdefault("lens_weights", LAM)
    return ingest(rec, role, rnd, history, **kw)


def store_rec(seq, role, rnd, content):
    t = "Z" if role == "Zealot" else "R"
    return {"seq": seq, "id": f"R{rnd}.{t}", "role": t, "round": rnd, "content": content}


R_CH = dd.ruler_claim("R1", "guidance raise is already priced", kind="challenge", target="Z1")


def r_challenge(*lenses):
    return dict(R_CH, effect={"if_true": tag([MOVE], *lenses), "if_false": []})


class LensIngestTest(unittest.TestCase):
    def opening_history(self, *r_lenses):
        errs, z = ing(multi_final(99, [dd.Z_C1], dd.Z14), "Zealot", 1, [])
        self.assertEqual(errs, [])
        errs, r = ing(multi_final(1, [r_challenge(*r_lenses), dd.R_C2], dd.R14), "Reaper", 1, [store_rec(1, "Zealot", 1, z)])
        self.assertEqual(errs, [], errs)
        return [store_rec(1, "Zealot", 1, z), store_rec(2, "Reaper", 1, r)]

    def round2(self, history, ops_by_lens, upd_ops, applies="Z1#true"):
        """R's round 2: each lens' scenario = its round-1 scenario after `ops_by_lens[L]`; update carries `upd_ops`."""
        base = {L: sr.apply_ops(history[1]["content"]["scenario_by_lens"][L], ops_by_lens.get(L, [])) for L in LENSES}
        rec = multi_final(2, [dd.R_C1_DONE if False else dict(r_challenge("SPY", "QQQ"), status="withdrawn"), dd.R_C2_DEAD],
                          dd.R14)
        rec["scenario_by_lens"] = base
        rec["scenario_updates"] = [{"claim": "Z1", "applies": applies, "ops": upd_ops, "why": "Z1 verified"}]
        return rec

    def test_multi_lens_opening_ingests_and_pins_each_p0(self):
        rec = multi_final(99, [dd.Z_C1], dd.Z14, p0={"QQQ": 400.1})   # within display rounding
        errs, out = ing(rec, "Zealot", 1, [])
        self.assertEqual(errs, [])
        for L in LENSES:
            self.assertEqual(out["scenario_by_lens"][L]["P0"], REF[L])
        self.assertEqual(rec["scenario_by_lens"]["QQQ"]["P0"], 400.1)   # input untouched
        cbl = out["composite_by_lens"]
        self.assertEqual(set(cbl), set(LENSES))
        self.assertTrue(all(isinstance(v, int) for v in cbl.values()))
        self.assertEqual(out["composite_score"], weighted_score(cbl, LAM))
        self.assertEqual(out["authored_score"], 99)

    def test_p0_off_the_lens_reference_is_refused_per_lens(self):
        errs, _ = ing(multi_final(99, [dd.Z_C1], dd.Z14, p0={"DIA": 310.0}), "Zealot", 1, [])
        self.assertTrue(any(e.startswith("lens DIA: scenario.P0") for e in errs), errs)

    def test_lens_set_must_match_session(self):
        rec = multi_final(99, [dd.Z_C1], dd.Z14)
        del rec["scenario_by_lens"]["DIA"]
        errs, _ = ing(rec, "Zealot", 1, [])
        self.assertTrue(any("missing lens DIA" in e for e in errs), errs)
        rec = multi_final(99, [dd.Z_C1], dd.Z14)
        rec["scenario_by_lens"]["QQQQ"] = rec["scenario_by_lens"]["QQQ"]
        errs, _ = ing(rec, "Zealot", 1, [])
        self.assertTrue(any("unknown lens QQQQ" in e for e in errs), errs)

    def test_scenario_key_refused_in_multi_lens(self):
        rec = multi_final(99, [dd.Z_C1], dd.Z14)
        rec["scenario"] = scaled(dd.Z14, "SPY")
        errs, _ = ing(rec, "Zealot", 1, [])
        self.assertIn("scenario: a multi-lens session writes scenario_by_lens", errs)

    def test_untagged_op_refused_in_multi_lens(self):
        z = dd.ruler_claim("Z1", "g", if_true=[MOVE])
        errs, _ = ing(multi_final(99, [z], dd.Z14), "Zealot", 1, [])
        self.assertTrue(any("has no lens" in e for e in errs), errs)

    def test_lens_key_refused_in_single_lens(self):
        z = dd.ruler_claim("Z1", "g", if_true=tag([MOVE], "SPY"))
        rec = dd.ruler_final(99, [z], dd.Z14)
        errs, _ = ingest(rec, "Zealot", 1, [], ref_price=100.0)
        self.assertTrue(any("carries lens in a single-lens session" in e for e in errs), errs)

    def test_scenario_by_lens_refused_in_single_lens(self):
        rec = dd.ruler_final(99, [dd.Z_C1], dd.Z14)
        with_both = dict(copy.deepcopy(rec), scenario_by_lens={"SPY": copy.deepcopy(dd.Z14)})
        only = copy.deepcopy(with_both)
        del only["scenario"]
        for r in (with_both, only):
            errs, _ = ingest(r, "Zealot", 1, [], ref_price=100.0)
            self.assertTrue(any("scenario_by_lens" in e and "single-lens session" in e for e in errs), errs)

    def test_applies_must_carry_every_lens(self):
        h = self.opening_history("SPY", "QQQ")
        one = tag([MOVE], "SPY")
        errs, _ = ing(self.round2(h, {"SPY": [MOVE]}, one), "Reaper", 2, h)
        self.assertTrue(any("every lens (missing ['QQQ'])" in e for e in errs), errs)
        both = tag([MOVE], "SPY", "QQQ")
        errs, out = ing(self.round2(h, {"SPY": [MOVE], "QQQ": [MOVE]}, both), "Reaper", 2, h)
        self.assertEqual(errs, [], errs)
        self.assertEqual(len(out["composite_by_lens"]), 3)

    def test_update_verified_per_lens(self):
        h = self.opening_history("SPY", "QQQ")
        # the QQQ scenario does not move, yet the update carries a QQQ op
        errs, _ = ing(self.round2(h, {}, tag([MOVE], "QQQ"), applies=None), "Reaper", 2, h)
        self.assertTrue(any(e.startswith("lens QQQ: scenario_updates") for e in errs), errs)
        self.assertFalse(any(e.startswith("lens SPY:") for e in errs), errs)

    def test_single_lens_unchanged(self):
        rec = dd.ruler_final(99, [dd.Z_C1], dd.Z14)
        a = ingest(copy.deepcopy(rec), "Zealot", 1, [], ref_price=100.0)
        b = ingest(copy.deepcopy(rec), "Zealot", 1, [], ref_price=100.0, lenses=None, ref_prices=None, lens_weights=None)
        c = ingest(copy.deepcopy(rec), "Zealot", 1, [], ref_price=100.0, lenses=[])
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a[0], [])
        self.assertNotIn("composite_by_lens", a[1])


if __name__ == "__main__":
    unittest.main()
