#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/divergence.py (the duty loop's routing measure, 2026-11-06). Pure functions; no PG, no IBKR."""
import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dv", ROOT / "scripts" / "divergence.py")
dv = importlib.util.module_from_spec(spec); spec.loader.exec_module(dv)


def tree(P0=100.0):
    """A symmetric three-path tree: down 6%, flat, up 6% at day 15, with day-1 events at ±1%."""
    k = P0 / 100.0
    return {"schema": "3.4", "T_days": 15, "P0": P0, "nodes": [
        {"id": "D", "p": 0.3, "P_T": 94.0 * k, "events": [{"t": 1, "kind": "close", "price": 99.0 * k}, {"t": 15, "kind": "close", "price": 94.0 * k}]},
        {"id": "F", "p": 0.4, "P_T": 100.0 * k, "events": [{"t": 15, "kind": "close", "price": 100.0 * k}]},
        {"id": "U", "p": 0.3, "P_T": 106.0 * k, "events": [{"t": 1, "kind": "close", "price": 101.0 * k}, {"t": 15, "kind": "close", "price": 106.0 * k}]}]}


class PathTest(unittest.TestCase):
    def test_interpolates_and_holds(self):
        n = tree()["nodes"][0]
        self.assertAlmostEqual(dv.path_at(n, 100.0, 0), 100.0)
        self.assertAlmostEqual(dv.path_at(n, 100.0, 0.5), 99.5)
        self.assertAlmostEqual(dv.path_at(n, 100.0, 8), 99.0 + (94.0 - 99.0) * 7 / 14)
        self.assertAlmostEqual(dv.path_at(n, 100.0, 40), 94.0)

    def test_terminal_price_closes_the_path_when_events_stop_short(self):
        n = {"id": "X", "p": 1, "P_T": 90.0, "events": [{"t": 2, "price": 98.0}]}
        self.assertAlmostEqual(dv.path_at(n, 100.0, 2, 15), 98.0)
        self.assertAlmostEqual(dv.path_at(n, 100.0, 15, 15), 90.0)
        self.assertAlmostEqual(dv.path_at(n, 100.0, 15), 98.0)        # without T the path holds its last event


class MeasureTest(unittest.TestCase):
    def test_centre_is_inside_and_learns_nothing(self):
        d = dv.divergence(tree(), 2.0, 1.0, 100.0)
        self.assertEqual(d["level"], "inside")
        self.assertGreater(d["tail"], 0.4)
        self.assertLess(d["kl_bits"], 0.2)
        self.assertEqual(d["dominant"], "F")
        self.assertAlmostEqual(d["move_atr"], 0.0)

    def test_far_outside_is_dead_with_large_kl(self):
        d = dv.divergence(tree(), 2.0, 1.0, 92.0)        # -4 ATR on day 1
        self.assertEqual(d["level"], "dead")
        self.assertLess(d["tail"], 0.01)
        self.assertEqual(d["dominant"], "D")
        self.assertGreater(d["severity"], 0.98)
        self.assertTrue(d["kl_bits"] is None or d["kl_bits"] > 0.5)      # bounded by log2(1/p_D) ≈ 1.74 bits

    def test_drift_between(self):
        d = dv.divergence(tree(), 2.0, 1.0, 97.0)        # -1.5 ATR on day 1
        self.assertEqual(d["level"], "drift")
        self.assertTrue(0.05 <= d["tail"] < 0.25, d)

    def test_sigma_grows_with_time_so_the_same_move_matters_less_later(self):
        early = dv.divergence(tree(), 2.0, 1.0, 97.0)
        late = dv.divergence(tree(), 2.0, 9.0, 97.0)
        self.assertGreater(late["tail"], early["tail"])

    def test_past_the_horizon_the_tree_is_expired(self):
        d = dv.divergence(tree(), 2.0, 15.0, 100.0)
        self.assertEqual((d["level"], d["severity"], d["T_days"]), ("expired", 1.0, 15))
        self.assertTrue(dv.reason_text(d).startswith("tree expired: horizon 15d passed (t=15d)"), dv.reason_text(d))
        self.assertEqual(dv.divergence(tree(), 2.0, 14.5, 100.0)["level"], "inside")

    def test_expires_on_counts_weekdays(self):
        self.assertEqual(dv.expires_on("2026-11-04", 15), "2026-11-25")
        self.assertEqual(dv.expires_on("2026-11-06T18:00:00Z", 1), "2026-11-09")
        self.assertIsNone(dv.expires_on("garbage", 15))

    def test_thresholds_are_parameters(self):
        d = dv.divergence(tree(), 2.0, 1.0, 97.0, dead_tail=0.2, drift_tail=0.3)
        self.assertEqual(d["level"], "dead")

    def test_unusable_inputs_give_none(self):
        self.assertIsNone(dv.divergence({}, 2.0, 1.0, 100.0))
        self.assertIsNone(dv.divergence(tree(), None, 1.0, 100.0))
        self.assertIsNone(dv.divergence(tree(), 2.0, None, 100.0))
        self.assertIsNone(dv.divergence({"P0": 100, "nodes": [{"id": "a", "p": 0}]}, 2.0, 1.0, 100.0))

    def test_reason_text(self):
        self.assertIsNone(dv.reason_text(dv.divergence(tree(), 2.0, 1.0, 100.0)))
        s = dv.reason_text(dv.divergence(tree(), 2.0, 1.0, 92.0))
        self.assertTrue(s.startswith("tree dead: -4.0 ATR in 1d, tail 0.00"), s)
        s = dv.reason_text(dv.divergence(tree(), 2.0, 1.0, 97.0), lens="SPY")
        self.assertTrue(s.startswith("drift: SPY -1.5 ATR in 1d"), s)


class BarsTest(unittest.TestCase):
    def bars(self, days, start=100.0):
        out, px = [], start
        for i, d in enumerate(days):
            out.append({"date": d, "open": px, "high": px + 2, "low": px - 2, "close": px + (1 if i % 2 else -1)})
            px = out[-1]["close"]
        return out

    def test_days_since_counts_partial_last_day_as_half(self):
        self.assertIsNone(dv.days_since([], "2026-11-06"))
        b = self.bars(["2026-11-04", "2026-11-05", "2026-11-06"])
        self.assertEqual(dv.days_since(b, "2026-11-06"), 2.5)
        self.assertEqual(dv.days_since(b, "2026-11-07"), 3.0)
        self.assertEqual(dv.days_since(b[:1], "2026-11-04"), 0.5)

    def test_atr_from_bars_uses_only_bars_up_to_base_day(self):
        b = self.bars([f"2026-10-{d:02d}" for d in range(1, 29)])
        a = dv.atr_pct_from_bars(b, "2026-10-20")
        self.assertIsNotNone(a)
        self.assertTrue(3.0 < a < 5.0, a)                 # true range 4 on ~100 → ~4%
        self.assertIsNone(dv.atr_pct_from_bars(b[:1], "2026-10-01"))


class ReportTest(unittest.TestCase):
    def test_single_tree_report(self):
        rep = {"symbol": "AAA", "scenario": tree(), "atr_pct_14": 2.0}
        after = [{"date": "2026-11-05", "open": 99, "high": 99.5, "low": 91, "close": 92.0}]
        d = dv.report_divergence(rep, lambda s: (after, after) if s == "AAA" else None, "2026-11-04", "2026-11-06")
        self.assertEqual(d["level"], "dead"); self.assertIsNone(d["lens"]); self.assertNotIn("by_lens", d)

    def test_lens_wise_report_returns_the_worst_lens_and_atr_from_bars(self):
        rep = {"symbol": "GENERAL", "lenses": ["SPY", "QQQ"], "atr_pct_14": None,
               "by_lens": {"SPY": {"scenario": tree(700.0)}, "QQQ": {"scenario": tree(600.0)}}}
        hist = {"SPY": [{"date": f"2026-10-{d:02d}", "open": 700, "high": 707, "low": 693, "close": 700} for d in range(10, 29)],
                "QQQ": [{"date": f"2026-10-{d:02d}", "open": 600, "high": 606, "low": 594, "close": 600} for d in range(10, 29)]}
        after = {"SPY": [{"date": "2026-11-05", "open": 700, "high": 701, "low": 699, "close": 700.0}],       # centre
                 "QQQ": [{"date": "2026-11-05", "open": 600, "high": 601, "low": 560, "close": 562.0}]}       # far below
        d = dv.report_divergence(rep, lambda s: (after[s], hist[s] + after[s]), "2026-11-04", "2026-11-06")
        self.assertEqual(d["lens"], "QQQ"); self.assertEqual(d["level"], "dead")
        self.assertEqual(set(d["by_lens"]), {"SPY", "QQQ"}); self.assertEqual(d["by_lens"]["SPY"]["level"], "inside")

    def test_no_tree_is_none(self):
        self.assertIsNone(dv.report_divergence({"symbol": "AAA", "next_review": {}}, lambda s: ([], []), None, None))


class NoBarsTest(unittest.TestCase):
    """The rebalance brief has live prices but no bars: t from weekdays, ATR from the ruler's unit_basis."""

    def test_atr_of_prefers_own_then_unit_basis(self):
        self.assertEqual(dv.atr_of({"atr_pct_14": 2.5}), 2.5)
        self.assertEqual(dv.atr_of({"atr_pct_14": None, "scenario": {"derived": {"unit_basis": {"atr_pct_14": 1.146}}}}), 1.146)
        self.assertIsNone(dv.atr_of({"scenario": {}}))

    def test_trading_days_between(self):
        from datetime import datetime, timezone
        wed = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(dv.trading_days_between(wed, datetime(2026, 11, 4, 20, tzinfo=timezone.utc)), 0.5)   # same day
        self.assertEqual(dv.trading_days_between(wed, datetime(2026, 11, 5, 20, tzinfo=timezone.utc)), 0.5)   # Thu: today half, nothing full
        self.assertEqual(dv.trading_days_between(wed, datetime(2026, 11, 6, 20, tzinfo=timezone.utc)), 1.5)   # Fri: Thu full + half
        self.assertEqual(dv.trading_days_between(wed, datetime(2026, 11, 8, 20, tzinfo=timezone.utc)), 2.0)   # Sun: Thu, Fri
        self.assertEqual(dv.trading_days_between(wed, datetime(2026, 11, 9, 20, tzinfo=timezone.utc)), 2.5)   # Mon

    def test_report_divergence_at_single_and_lens_wise(self):
        from datetime import datetime, timezone
        as_of = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc); now = datetime(2026, 11, 5, 20, tzinfo=timezone.utc)
        rep = {"symbol": "AAA", "scenario": tree(), "atr_pct_14": None}
        rep["scenario"]["derived"] = {"unit_basis": {"atr_pct_14": 2.0}}
        d = dv.report_divergence_at(rep, as_of, now, lambda s: 92.0)
        self.assertEqual((d["level"], d["t"], d["lens"]), ("dead", 0.5, None))
        g = {"symbol": "GENERAL", "lenses": ["SPY", "QQQ"],
             "by_lens": {"SPY": {"scenario": {**tree(700.0), "derived": {"unit_basis": {"atr_pct_14": 2.0}}}},
                         "QQQ": {"scenario": {**tree(600.0), "derived": {"unit_basis": {"atr_pct_14": 2.0}}}}}}
        d = dv.report_divergence_at(g, as_of, now, lambda s: {"SPY": 700.0, "QQQ": 562.0}[s])
        self.assertEqual(d["lens"], "QQQ"); self.assertEqual(d["by_lens"]["SPY"]["level"], "inside")
        self.assertIsNone(dv.report_divergence_at(rep, as_of, now, lambda s: None))


if __name__ == "__main__":
    unittest.main()
