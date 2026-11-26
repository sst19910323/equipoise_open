"""scripts/path_fit.py (PF1): the postcheck's path-fit reference block."""
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import path_fit as pf  # noqa: E402


def tree(P0=100.0, T=10):
    return {"schema": "3.4", "T_days": T, "P0": P0, "nodes": [
        {"id": "D", "p": 0.3, "P_T": 92.0, "events": [{"t": 2, "kind": "close", "price": 97.0}, {"t": 3, "kind": "touch", "price": 95.0}]},
        {"id": "F", "p": 0.4, "P_T": 100.0, "events": []},
        {"id": "U", "p": 0.3, "P_T": 108.0, "events": [{"t": 1, "kind": "gap_open", "price": 102.0}, {"t": 5, "kind": "close", "price": 104.0}]}]}


def bars(closes, opens=None, highs=None, lows=None):
    out = []
    for i, c in enumerate(closes):
        out.append({"day": f"2026-11-{10 + i:02d}", "close": c, "open": (opens or closes)[i],
                    "high": (highs or [x + 1 for x in closes])[i], "low": (lows or [x - 1 for x in closes])[i]})
    return out


class CrpsTest(unittest.TestCase):
    def test_mixture_crps_matches_numeric_integration(self):
        comps = [(0.4, 98.0, 2.0), (0.6, 104.0, 3.0)]
        y = 101.0
        closed = pf.crps_gaussian_mixture(comps, y)
        xs = [70 + i * 0.01 for i in range(6001)]
        num = 0.0
        for x in xs:
            F = pf.mixture_cdf(comps, x)
            num += (F - (1.0 if x >= y else 0.0)) ** 2 * 0.01
        self.assertAlmostEqual(closed, num, places=3)

    def test_single_gaussian_baseline_formula(self):
        s = 3.0
        comps = [(1.0, 100.0, s)]
        # CRPS of N(mu, s²) at y = mu is s·(√2 − 1)/√π
        self.assertAlmostEqual(pf.crps_gaussian_mixture(comps, 100.0), s * (math.sqrt(2) - 1) / math.sqrt(math.pi), places=6)

    def test_quantile_inverts_cdf(self):
        comps = [(0.5, 95.0, 2.0), (0.5, 105.0, 2.0)]
        for q in (0.1, 0.5, 0.9):
            self.assertAlmostEqual(pf.mixture_cdf(comps, pf.mixture_quantile(comps, q)), q, places=5)


class PathsTest(unittest.TestCase):
    def test_close_path_uses_close_events_and_terminal_only(self):
        leaves = pf.leaf_paths(tree())
        self.assertEqual([l["id"] for l in leaves], ["D", "F", "U"])
        d = next(l for l in leaves if l["id"] == "D")
        self.assertEqual(d["points"], [(0.0, 100.0), (2.0, 97.0), (10.0, 92.0)])   # the touch event is not a close point
        self.assertAlmostEqual(pf.path_price(d["points"], 1.0), 98.5)
        self.assertAlmostEqual(pf.path_price(d["points"], 6.0), 97.0 + (92.0 - 97.0) * 4 / 8)
        u = next(l for l in leaves if l["id"] == "U")
        self.assertEqual(u["points"][1], (5.0, 104.0))                              # gap_open is not a close point either
        self.assertAlmostEqual(sum(l["p"] for l in leaves), 1.0)


class PathFitTest(unittest.TestCase):
    def test_inside_path_scores_well_and_marks_sources(self):
        out = pf.path_fit(tree(), 2.0, bars([100.2, 99.5, 100.8, 100.1, 101.0]), dt=3)
        self.assertEqual(out["status"], "scored"); self.assertEqual(out["n_valid"], 3)            # d+3 → three days only
        s = out["summary"]
        self.assertEqual((s["tail_days_low"], s["tail_days_high"], s["coverage_90"]), (0, 0, 1.0))
        self.assertIsNone(s["first_breach_day"]); self.assertIsNone(s["recovered"])
        self.assertGreater(s["skill_vs_zero_drift"], -1.0)
        self.assertEqual([d["point_source"] for d in out["days"]], ["interpolated", "authored", "interpolated"])   # day 1 has only a gap_open event: not a close point
        self.assertEqual(out["terminal"]["status"], "not reached in this window")
        self.assertTrue(out["diagnostic_only"]); self.assertEqual(out["method_version"], "PF1")

    def test_breach_persistence_and_recovery(self):
        closes = [100.0, 88.0, 87.0, 86.0, 99.0, 100.0, 100.5, 99.8, 100.2, 100.0, 100.0, 100.0]
        out = pf.path_fit(tree(), 2.0, bars(closes), dt=20)
        self.assertEqual(out["n_valid"], 10)                                                    # truncated at T = 10
        self.assertTrue(out["horizon_truncated"])
        s = out["summary"]
        self.assertEqual(s["first_breach_day"], 2); self.assertEqual(s["longest_breach_run"], 3)
        self.assertEqual(s["recovery_day"], 5); self.assertTrue(s["recovered"]); self.assertEqual(s["bias"], "low")
        self.assertEqual(out["terminal"]["t"], 10); self.assertIn("u", out["terminal"])

    def test_event_checks_and_segments(self):
        b = bars([101.0, 98.0, 96.0, 97.0, 104.5], opens=[102.5, 100.0, 97.0, 96.5, 103.0],
                 highs=[103.0, 100.5, 97.0, 98.0, 105.0], lows=[100.0, 94.9, 95.5, 96.0, 102.0])
        out = pf.path_fit(tree(), 2.0, b, dt=20, event_day=3)
        ec = {(e["node"], e["kind"]): e for e in out["event_checks"]}
        self.assertTrue(ec[("U", "gap_open")]["reached"]); self.assertEqual(ec[("U", "gap_open")]["realized_open"], 102.5)
        self.assertTrue(ec[("D", "touch")]["reached"]); self.assertEqual(ec[("D", "touch")]["realized_extreme"], 94.9)
        self.assertEqual(out["segments"]["event_day"], 3); self.assertEqual(out["segments"]["pre"]["n"], 2); self.assertEqual(out["segments"]["post"]["n"], 3)

    def test_degenerate_inputs(self):
        self.assertEqual(pf.path_fit({}, 2.0, bars([1.0]), 3)["status"], "no_tree")
        self.assertEqual(pf.path_fit(tree(), None, bars([1.0]), 3)["status"], "no_atr")
        self.assertEqual(pf.path_fit(tree(), 2.0, [], 3)["status"], "no_bars")
        self.assertEqual(pf.path_fit(tree(), 2.0, [{"day": "x"}], 3)["status"], "no_close")


if __name__ == "__main__":
    unittest.main()
