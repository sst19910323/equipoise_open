#!/usr/bin/env python3
"""The drift switch on the ruler (EXPERIMENTAL, off in production; docs/design/drill/DRIFT-ANCHOR-2026-11-10.md): with
the snapshot key `drift_mu`, score and payoffs are read against the benchmark B_t = (1+mu)^(t/T) while fills and stops
stay on real prices. Without the key nothing changes."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds_model as om  # noqa: E402
from test_odds_model import CFG, P0, leaf_events, ruler_scenario  # noqa: E402

MU = 0.004
ZERO_COST = dict(CFG, cost_bps=0.0)


def anchor_tree(mu=MU, a=0.03):
    """No evidence: symmetric around the drift anchor P0·(1+mu)."""
    c = P0 * (1 + mu)
    return ruler_scenario([("U", 0.25, c * (1 + a), None), ("M", 0.5, c, None), ("D", 0.25, c * (1 - a), None)])


class DriftRulerTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(om.drift_mu({}), 0.0)
        self.assertEqual(om.drift_mu(None), 0.0)
        sc = anchor_tree()
        self.assertEqual(om.score(sc), om.score(sc, 0.0))
        self.assertGreater(om.score(sc)["S_ref"], 50.0)          # read against P0, the anchor tree leans up
        d = om.derive_for_report(sc, om.build_candidates({}, {}, sc, []), ZERO_COST, s_s=0.04)
        self.assertEqual(d["chosen"], "overweight_now")           # "no view" read as a buy: the problem the switch addresses

    def test_anchor_tree_is_neutral_at_zero_cost(self):
        for mu in (-0.003, 0.0, 0.002, MU, 0.01):
            for a in (0.01, 0.03, 0.08):
                sc = anchor_tree(mu, a)
                cfg = dict(ZERO_COST, drift_mu=mu)
                self.assertAlmostEqual(om.score(sc, mu)["S_ref"], 50.0, places=1)
                d = om.derive_for_report(sc, om.build_candidates({}, {}, sc, []), cfg, s_s=0.04)
                self.assertTrue(d["ok"], d)
                self.assertEqual(d["chosen"], "neutral", (mu, a, d["comparison"]["strategies"]))
                for name, e in d["comparison"]["strategies"].items():
                    self.assertLessEqual(e["CE"], 1e-12, (mu, a, name))

    def test_mirror_about_the_anchor(self):
        c = P0 * (1 + MU)
        up = ruler_scenario([("U", 0.6, c * 1.04, None), ("D", 0.4, c * 0.97, None)])
        dn = ruler_scenario([("U", 0.4, c * 1.03, None), ("D", 0.6, c * 0.96, None)])
        self.assertAlmostEqual(om.score(up, MU)["S_ref"] + om.score(dn, MU)["S_ref"], 100.0, places=1)
        cfg = dict(ZERO_COST, drift_mu=MU)
        a = om.derive_for_report(up, om.build_candidates({}, {}, up, []), cfg, s_s=0.04)
        b = om.derive_for_report(dn, om.build_candidates({}, {}, dn, []), cfg, s_s=0.04)
        self.assertEqual((a["chosen"], b["chosen"]), ("overweight_now", "underweight_now"))
        self.assertAlmostEqual(a["x_star"], b["x_star"], places=2)

    def test_payoff_is_read_against_the_benchmark_only_while_held(self):
        cfg = dict(CFG, drift_mu=MU)
        leaf = {"id": "L", "p": 1.0, "P_T": 104.0, "events": leaf_events((15, "close", 104))}
        sc = ruler_scenario([("L", 1.0, 104.0, None)])
        held = om.simulate_leaf(leaf, "overweight_now", None, sc, cfg)
        self.assertAlmostEqual(held["r_path"], (104.0 / (1 + MU) - 100.0) / P0, places=12)
        # a conditional that never triggers holds nothing and pays no drift
        never = {"id": "c", "strategy": "overweight_on_condition", "kind": "close", "op": ">", "level": 110.0, "fill": "at_level", "stop": None}
        self.assertEqual(om.simulate_leaf(leaf, "overweight_on_condition", never, sc, cfg)["r_path"], 0.0)
        # a late fill is charged from its own day; the trigger itself is decided on the real price
        late = {"id": "L", "p": 1.0, "P_T": 104.0, "events": leaf_events((10, "close", 102), (15, "close", 104))}
        cond = {"id": "c", "strategy": "overweight_on_condition", "kind": "close", "op": ">=", "level": 102.0, "fill": "at_level", "stop": None}
        got = om.simulate_leaf(late, "overweight_on_condition", cond, sc, cfg)
        self.assertTrue(got["filled"])
        self.assertAlmostEqual(got["r_path"], (104.0 / (1 + MU) - 102.0 / (1 + MU) ** (10 / 15)) / P0, places=12)
        # a touch stop on day 5 ends the charge on day 5
        stopped = {"id": "L", "p": 1.0, "P_T": 104.0, "events": leaf_events((5, "touch", 96), (15, "close", 104))}
        now = {"id": "overweight_now", "strategy": "overweight_now", "stop": {"level": 97.0, "semantics": "touch"}}
        got = om.simulate_leaf(stopped, "overweight_now", now, sc, cfg)
        self.assertTrue(got["stopped"])
        self.assertAlmostEqual(got["r_path"], (96.0 / (1 + MU) ** (5 / 15) - 100.0) / P0, places=12)

    def test_real_and_deflated_prices_agree(self):
        """The same path computed two ways: real prices with the benchmark, or every price deflated first (barriers too)."""
        cfg = dict(CFG, drift_mu=MU)
        B = lambda t: (1 + MU) ** (t / 15)  # noqa: E731
        ev = [(4, "close", 98.0), (9, "close", 103.0), (15, "close", 105.0)]
        sc = ruler_scenario([("L", 1.0, 105.0, leaf_events(*ev))])
        leaf = {"id": "L", "p": 1.0, "P_T": 105.0, "events": leaf_events(*ev)}
        cond = {"id": "c", "strategy": "overweight_on_condition", "kind": "close", "op": ">=", "level": 103.0, "fill": "at_level", "stop": None}
        real = om.simulate_leaf(leaf, "overweight_on_condition", cond, sc, cfg)
        dev = [(t, k, p / B(t)) for t, k, p in ev]
        dsc = ruler_scenario([("L", 1.0, 105.0 / B(15), leaf_events(*dev))])
        dleaf = {"id": "L", "p": 1.0, "P_T": 105.0 / B(15), "events": leaf_events(*dev)}
        dcond = dict(cond, level=103.0 / B(9))
        defl = om.simulate_leaf(dleaf, "overweight_on_condition", dcond, dsc, CFG)
        self.assertEqual(real["filled"], defl["filled"])
        self.assertAlmostEqual(real["r_path"], defl["r_path"], places=12)
        self.assertTrue(math.isfinite(real["r_path"]))


if __name__ == "__main__":
    unittest.main()
