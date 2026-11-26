"""v3.4 data-layer and postcheck-ruler pieces: snapshot leg_z (extension in own-range units) and R25 forecast scores on the
day-15 close (CRPS / Brier / interval / skill vs the versioned ATR baseline). No files, no PG: the daily loader is monkeypatched."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class LegZTest(unittest.TestCase):
    def setUp(self):
        try:
            import pandas  # noqa: F401
        except Exception as e:
            self.skipTest(f"pandas missing: {e}")
        self.sv = _load("snapshot_v16", "analysis/snapshot_v16.py")

    def test_leg_z_from_last_confirmed_pivot(self):
        import pandas as pd
        idx = pd.date_range("2026-09-01", periods=30, freq="B")          # 30 completed daily bars
        layers = {"tactical_2m": {"confirmed": [["2026-09-10T15:00Z", 100.0, "H"], ["2026-10-02T10:00Z", 90.0, "L", "2026-10-06", -10.0, 1.5]]}}
        out = self.sv.leg_z(layers, idx, 99.0, 2.0)
        n = int((idx > pd.Timestamp("2026-10-02")).sum())
        self.assertEqual(out["leg_bars"], n); self.assertEqual(out["leg_pivot_type"], "L")
        self.assertAlmostEqual(out["leg_z"], round((99.0 / 90.0 - 1) / (0.02 * n ** 0.5), 2), 2)
        # nulls carry a reason, never an exception
        self.assertIsNone(self.sv.leg_z({}, idx, 99.0, 2.0)["leg_z"])
        self.assertIn("ATR", self.sv.leg_z(layers, idx, 99.0, 0)["leg_z_null_reason"])
        last = {"tactical_2m": {"confirmed": [[str(idx[-1].date()) + "T15:00Z", 95.0, "L"]]}}
        self.assertIn("n = 0", self.sv.leg_z(last, idx, 99.0, 2.0)["leg_z_null_reason"])


class ForecastScoresTest(unittest.TestCase):
    def setUp(self):
        try:
            import pandas  # noqa: F401
        except Exception as e:
            self.skipTest(f"pandas missing: {e}")
        self.pq = _load("postcheck_quant", "workflows/postcheck_quant.py")

    def _df(self, closes, start="2026-10-01"):
        import pandas as pd
        idx = pd.date_range(start, periods=len(closes), freq="B", tz="UTC")
        return pd.DataFrame({"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes], "close": closes}, index=idx)

    def test_scores_on_day_15_close(self):
        pq = self.pq
        closes = [100.0] * 3 + [100 + i for i in range(1, 26)]      # T = day 3; day-15 after T = +15
        pq._load_daily_df = lambda *a, **k: self._df(closes)
        T = __import__("datetime").datetime(2026, 10, 5, 15, 0, tzinfo=__import__("datetime").timezone.utc)
        sc = {"schema": "3.4", "T_days": 15, "P0": 100.0,
              "nodes": [{"id": "A", "p": 0.6, "P_T": 112.0, "refs": ["Z1"]}, {"id": "B", "p": 0.4, "P_T": 94.0, "refs": ["R1"]}]}
        om = _load("odds_model", "scripts/odds_model.py")
        report = {"scenario": sc, "atr_pct_14": 1.5, "odds": om.snapshot_from_config(om.DEFAULT_CONFIG), "debate_history": [
            {"role": "Zealot", "round": "Init", "content": json.dumps({"scenario": dict(sc, nodes=[{"id": "A", "p": 0.8, "P_T": 112.0, "refs": ["Z1"]}, {"id": "B", "p": 0.2, "P_T": 94.0, "refs": ["R1"]}])})},
            {"role": "Reaper", "round": "Init", "content": {"scenario": dict(sc, nodes=[{"id": "A", "p": 0.3, "P_T": 112.0, "refs": ["Z1"]}, {"id": "B", "p": 0.7, "P_T": 94.0, "refs": ["R1"]}])}}]}
        out = pq.forecast_scores("TEST", T, report)
        self.assertEqual(out["status"], "scored"); self.assertEqual(out["T_days"], 15)
        self.assertAlmostEqual(out["realized_return"], 0.15, 5)          # close on the 15th day after T = 115
        self.assertEqual(out["arbiter"]["status"], "scored"); self.assertTrue(out["arbiter"]["upside_weight_realized"])
        self.assertLess(out["zealot_last"]["crps"], out["reaper_last"]["crps"])   # the bull was closer to +15%
        self.assertIn("skill", out["arbiter"]); self.assertGreater(out["zealot_last"]["skill"], out["reaper_last"]["skill"])
        self.assertAlmostEqual(out["baseline"]["sigma"], 0.015 * 15 ** 0.5, 4)
        # immature window
        pq._load_daily_df = lambda *a, **k: self._df([100.0] * 10)
        self.assertEqual(pq.forecast_scores("TEST", T, report)["status"], "immature")
        # a scenario without the session's ruler is not scored; no scenario (pre-ruler) → {}
        self.assertEqual(pq.forecast_scores("TEST", T, {k: v for k, v in report.items() if k != "odds"}),
                         {"error": "no odds snapshot"})
        self.assertEqual(pq.forecast_scores("TEST", T, {"composite_score": 50}), {})

    def test_ruler_version_bumped(self):
        self.assertEqual(self.pq.RULER_VERSION, "R25")


if __name__ == "__main__":
    unittest.main()
