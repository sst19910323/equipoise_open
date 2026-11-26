#!/usr/bin/env python3
"""mean_shrink (v3.7.1): when sizing, each strategy's mean payoff is multiplied by k and its dispersion kept. A session
whose snapshot lacks the key sizes exactly as before."""
import statistics as st
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds_model as om  # noqa: E402
from test_odds_model import CFG, P0, ruler_scenario  # noqa: E402


def tree(p_up=0.6):
    return ruler_scenario([("U", p_up, 104.0, None), ("M", 0.1, 100.5, None), ("D", 0.9 - p_up, 96.5, None)])


def derive(sc, **over):
    return om.derive_for_report(sc, om.build_candidates({}, {}, sc, []), dict(CFG, **over), s_s=0.04)


class MeanShrinkTest(unittest.TestCase):
    def test_absent_or_one_changes_nothing(self):
        sc = tree()
        a, b = derive(sc), derive(sc, mean_shrink=1.0)
        self.assertEqual((a["chosen"], a["x_star"], a["operation_size"]), (b["chosen"], b["x_star"], b["operation_size"]))
        self.assertEqual(om.mean_shrink({}), 1.0)
        self.assertEqual(om.mean_shrink({"mean_shrink": 0}), 1.0)      # out of range → no shrinkage, never a silent zero
        self.assertEqual(om.mean_shrink({"mean_shrink": 1.7}), 1.0)

    def test_rows_keep_dispersion_and_scale_the_mean(self):
        rows = [(0.5, {"filled": True, "r_path": 0.04}), (0.3, {"filled": True, "r_path": -0.02}), (0.2, {"filled": False, "r_path": 0.0})]
        out = om._shrink_rows(rows, 0.25)
        mean = lambda rs: sum(p * s["r_path"] for p, s in rs if s["filled"]) / 0.8  # noqa: E731
        self.assertAlmostEqual(mean(out), 0.25 * mean(rows), places=12)
        self.assertAlmostEqual(out[0][1]["r_path"] - out[1][1]["r_path"], 0.06, places=12)   # spread unchanged
        self.assertEqual(out[2][1]["r_path"], 0.0)                                           # unfilled stays at zero
        self.assertEqual(rows[0][1]["r_path"], 0.04)                                         # input untouched
        self.assertIs(om._shrink_rows(rows, 1.0), rows)

    def test_smaller_size_same_direction(self):
        sc = tree()
        full, shrunk = derive(sc), derive(sc, mean_shrink=0.33)
        self.assertEqual(full["chosen"], "overweight_now")
        self.assertEqual(shrunk["chosen"], "overweight_now")
        self.assertLess(shrunk["x_star"], full["x_star"])
        self.assertEqual(shrunk["score"], full["score"])                 # the score is the tree's, sizing does not touch it
        self.assertEqual(shrunk["comparison"]["mean_shrink"], 0.33)

    def test_mirror_holds_under_shrinkage(self):
        up = ruler_scenario([("U", 0.6, 104.0, None), ("D", 0.4, 97.0, None)])
        dn = ruler_scenario([("U", 0.4, 103.0, None), ("D", 0.6, 96.0, None)])
        a, b = derive(up, mean_shrink=0.33, cost_bps=0.0), derive(dn, mean_shrink=0.33, cost_bps=0.0)
        self.assertEqual((a["chosen"], b["chosen"]), ("overweight_now", "underweight_now"))
        self.assertAlmostEqual(a["x_star"], b["x_star"], places=2)

    def test_snapshot_carries_the_key(self):
        snap = om.snapshot_from_config(dict(om.DEFAULT_CONFIG, mean_shrink=0.33), cost_bps=3.0)
        self.assertEqual(snap["mean_shrink"], 0.33)
        self.assertEqual(om.mean_shrink(om.config_from_snapshot(snap)), 0.33)
        self.assertNotIn("mean_shrink", om.snapshot_from_config(dict(om.DEFAULT_CONFIG), cost_bps=3.0))
        self.assertEqual(om.mean_shrink(om.load_config()), 0.33)         # config/odds.yaml declares the provisional value
        self.assertTrue(st.mean([1, 1]) == 1)


if __name__ == "__main__":
    unittest.main()
