"""V16 price structure (analysis/trend_analyzer.py): causal ATR zigzag + telescope digest. Pure functions, synthetic bars.

Contracts under test (design §2): strict H/L alternation; causality (appending future bars never rewrites a confirmed
pivot); threshold recorded at candidate time; stage switch keeps one alternating chain; provisional right end always
present once a direction exists; digest segments are exclusive and contiguous; compatibility wrapper shape."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis import trend_analyzer as ta  # noqa: E402


def _bars(closes, spread_pct=0.6, start="2024-01-02", freq="B"):
    """OHLC bars around a close path: high/low = close ± spread%, open = previous close."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + spread_pct / 100)
    lo = np.minimum(o, c) * (1 - spread_pct / 100)
    idx = pd.bdate_range(start, periods=len(c), freq=freq)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1_000_000.0}, index=idx)


def _zigzag_path(n_legs=8, leg_len=30, amp=12.0, base=100.0):
    """Alternating legs of ±amp% built from straight lines, with small deterministic wiggle."""
    out = []
    price = base
    rng = np.random.default_rng(7)
    for leg in range(n_legs):
        target = price * (1 + amp / 100) if leg % 2 == 0 else price * (1 - amp / 100)
        seg = np.linspace(price, target, leg_len, endpoint=False) * (1 + rng.normal(0, 0.002, leg_len))
        out.extend(seg.tolist())
        price = target
    return out


class AlternationAndCausalityTest(unittest.TestCase):
    def setUp(self):
        self.df = _bars(_zigzag_path())

    def test_strict_alternation_and_provisional(self):
        z = ta.atr_zigzag(self.df, [ta.Stage(0, *ta.L1_NEAR)])
        kinds = [c[2] for c in z["confirmed"]]
        self.assertGreaterEqual(len(kinds), 5, z)
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b, kinds)
        self.assertIsNotNone(z["provisional"])
        self.assertNotEqual(z["provisional"][2], kinds[-1])           # provisional continues the alternation
        self.assertEqual(z["anchor_last"], round(float(self.df["close"].iloc[-1]), 4))
        for c in z["confirmed"]:
            self.assertLessEqual(c[0], c[3])                          # pivot time ≤ confirmed_at
            self.assertIsNotNone(c[5])                                # threshold recorded

    def test_appending_future_bars_never_rewrites_confirmed(self):
        full = ta.atr_zigzag(self.df, [ta.Stage(0, *ta.L1_NEAR)])
        for cut in (90, 120, 160, 200):
            part = ta.atr_zigzag(self.df.iloc[:cut], [ta.Stage(0, *ta.L1_NEAR)])
            self.assertEqual(part["confirmed"], full["confirmed"][: len(part["confirmed"])], cut)

    def test_threshold_is_causal_median_not_full_window(self):
        # a late volatility explosion must not change early thresholds
        quiet = _bars(_zigzag_path(n_legs=4, leg_len=40, amp=10.0))
        loud_tail = _bars(_zigzag_path(n_legs=4, leg_len=40, amp=40.0, base=float(quiet["close"].iloc[-1])),
                          start=str((quiet.index[-1] + pd.offsets.BDay(1)).date()))
        both = pd.concat([quiet, loud_tail])
        z_quiet = ta.atr_zigzag(quiet, [ta.Stage(0, *ta.L1_NEAR)])
        z_both = ta.atr_zigzag(both, [ta.Stage(0, *ta.L1_NEAR)])
        self.assertEqual(z_quiet["confirmed"], z_both["confirmed"][: len(z_quiet["confirmed"])])

    def test_stage_switch_keeps_one_chain(self):
        stages = ta.l1_stages(len(self.df), near_bars=100)
        self.assertEqual(len(stages), 2)
        z = ta.atr_zigzag(self.df, stages)
        kinds = [c[2] for c in z["confirmed"]]
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b)
        self.assertIn("from bar", z["stages"])

    def test_ties_keep_earlier_time(self):
        c = [100, 110, 110, 100, 90, 100]
        df = _bars(c * 8, spread_pct=0.0)
        z = ta.atr_zigzag(df, [ta.Stage(0, 1.0, 3.0, 50.0)])
        highs = [x for x in z["confirmed"] if x[2] == "H"]
        self.assertGreaterEqual(len(highs), 3)
        # every plateau high is dated at the FIRST 110 of its cycle (bar %6 == 1), never at the tying 110s after it
        # (the very first confirmed H may be seeded by the confirming bar itself, so start from the second)
        for h in highs[1:]:
            self.assertEqual(df.index.get_loc(pd.Timestamp(h[0])) % 6, 1, h)

    def test_too_short_or_empty(self):
        self.assertIsNone(ta.atr_zigzag(self.df.iloc[0:0], [ta.Stage(0, *ta.L1_NEAR)]))
        z = ta.atr_zigzag(self.df.iloc[:5], [ta.Stage(0, *ta.L1_NEAR)])
        self.assertEqual(z["confirmed"], [])                          # no ATR warm-up → nothing confirmed
        self.assertIsNone(z["provisional"])


class DigestTest(unittest.TestCase):
    def test_segments_exclusive_contiguous_and_cover_everything(self):
        df = _bars(_zigzag_path(n_legs=20, leg_len=30))              # 600 bars → tail 504
        dg = ta.price_digest(df)
        rows = dg["quarterly"] + dg["monthly"] + dg["weekly"] + dg["daily"]
        self.assertEqual(sum(r[6] for r in rows), 504)
        firsts = [r[0].split("..")[0] for r in rows]
        self.assertEqual(firsts, sorted(firsts))
        self.assertEqual(len(dg["daily"]), 10)
        self.assertTrue(all(r[6] == 1 for r in dg["daily"]))
        self.assertEqual(sum(r[6] for r in dg["weekly"]), 63)
        self.assertEqual(sum(r[6] for r in dg["monthly"]), 179)
        self.assertEqual(sum(r[6] for r in dg["quarterly"]), 252)
        self.assertTrue(all(r[5] is not None for r in rows))
        self.assertAlmostEqual(dg["daily"][0][5], 1.0, places=2)      # flat volume → vol_rel 1

    def test_short_history_and_no_volume(self):
        df = _bars(_zigzag_path(n_legs=2, leg_len=20))               # 40 bars
        dg = ta.price_digest(df, has_volume=False)
        self.assertEqual(dg["quarterly"], []); self.assertEqual(dg["monthly"], [])
        self.assertEqual(sum(r[6] for r in dg["weekly"]) + 10, 40)
        self.assertTrue(all(r[5] is None for r in dg["weekly"] + dg["daily"]))


class CompatWrapperTest(unittest.TestCase):
    def test_calculate_swing_points_shape(self):
        df = _bars(_zigzag_path())
        out = ta.calculate_swing_points(df, lookback_window=20, min_swing_pct=0.08, max_points=25)
        self.assertIn("pivots", out)
        self.assertTrue(all(p["type"] in ("high", "low") for p in out["pivots"]))
        self.assertTrue(all("confirmed_at" in p for p in out["pivots"]))
        self.assertIsNone(ta.calculate_simplified_trend(df))


if __name__ == "__main__":
    unittest.main()
