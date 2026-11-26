#!/usr/bin/env python3
"""Technical snapshot V16 regression tests (exchange clock kept from the 2026-09-30 rebuild batch; the V16 feature layer
analysis/snapshot_v16.py added 2026-10-25). Pure functions on synthetic bars; no IBKR, no PG.

Covers: exchange clocks and session classification (unchanged), last completed daily bar (unchanged), benchmark
resolution (unchanged), and V16: RTH filtering / bar completion on the exchange calendar, attribution day and prior RTH
close, the payload's prefixed keys and schema strings, no judgment labels, debater_view truncation, has_volume=false
nulls, unfilled gaps and the loader keeping volume-less rows."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis import technical_snapshot_builder as tsb  # noqa: E402
from analysis import snapshot_v16 as sv  # noqa: E402
from analysis import exchange_clock as ec  # noqa: E402


# ───────────────────────────── kept from V15.1 ───────────────────────────── #

class ExchangeClockTest(unittest.TestCase):
    def test_resolve_clock_from_symbols_yaml(self):
        rhm = tsb.resolve_exchange_clock("RHM")
        self.assertEqual(rhm["calendar"], "XETR")
        self.assertEqual(rhm["tz"], "Europe/Berlin")
        self.assertFalse(rhm["has_extended_hours"])
        aapl = tsb.resolve_exchange_clock("AAPL")
        self.assertEqual(aapl["calendar"], "NYSE")
        self.assertTrue(aapl["has_extended_hours"])
        self.assertEqual(tsb.resolve_exchange_clock("NTDOY")["calendar"], "JPX")
        self.assertEqual(tsb.resolve_exchange_clock("SSNLF")["calendar"], "XKRX")
        self.assertEqual(tsb.resolve_exchange_clock("ZZZZ_NOT_A_SYMBOL")["calendar"], "NYSE")

    def test_xetra_morning_is_regular_on_xetra_clock_but_closed_on_us_clock(self):
        ts = pd.Timestamp("2026-09-29 08:30:00", tz="UTC")
        eu = tsb.classify_session(ts, clock=tsb.resolve_exchange_clock("RHM"))
        us = tsb.classify_session(ts, clock=tsb.resolve_exchange_clock("AAPL"))
        self.assertEqual(eu["current_session"], "regular")
        self.assertEqual(us["current_session"], "pre-market")
        self.assertIn("+02:00", eu["latest_bar_local"])

    def test_tokyo_afternoon_is_regular_and_evening_is_closed(self):
        clock = tsb.resolve_exchange_clock("NTDOY")
        self.assertEqual(tsb.classify_session(pd.Timestamp("2026-09-29 05:00:00", tz="UTC"), clock=clock)["current_session"], "regular")
        self.assertEqual(tsb.classify_session(pd.Timestamp("2026-09-29 08:00:00", tz="UTC"), clock=clock)["current_session"], "closed")

    def test_prior_regular_close_uses_clock(self):
        ts = pd.Timestamp("2026-09-29 08:30:00", tz="UTC")
        eu = tsb.get_prior_regular_close(ts, clock=tsb.resolve_exchange_clock("RHM"))
        self.assertTrue(eu.startswith("2026-09-28T17:30"), eu)


class LastCompletedDailyBarTest(unittest.TestCase):
    def setUp(self):
        self.idx = pd.DatetimeIndex(pd.to_datetime(["2026-09-25", "2026-09-28", "2026-09-29"]))
        self.us = tsb.resolve_exchange_clock("AAPL")

    def test_during_regular_session_last_bar_is_partial(self):
        self.assertEqual(tsb.last_completed_daily_idx(self.idx, pd.Timestamp("2026-09-29 15:00:00", tz="UTC"), clock=self.us), -2)

    def test_after_close_last_bar_is_complete(self):
        self.assertEqual(tsb.last_completed_daily_idx(self.idx, pd.Timestamp("2026-09-29 20:30:00", tz="UTC"), clock=self.us), -1)

    def test_all_bars_partial_returns_none(self):
        idx = pd.DatetimeIndex(pd.to_datetime(["2026-09-29"]))
        self.assertIsNone(tsb.last_completed_daily_idx(idx, pd.Timestamp("2026-09-29 15:00:00", tz="UTC"), clock=self.us))


class BenchmarkTest(unittest.TestCase):
    def test_benchmark_from_config(self):
        self.assertEqual(tsb.resolve_benchmark_symbol("AAPL"), "QQQ")
        self.assertEqual(tsb.resolve_benchmark_symbol("RHM"), "DAX")
        self.assertIsNone(tsb.resolve_benchmark_symbol("SPY"))

    def test_stale_benchmark_is_rejected(self):
        sym = pd.DataFrame({"close": [1.0, 1.1]}, index=pd.to_datetime(["2026-09-28", "2026-09-29"]))
        fresh = pd.DataFrame({"close": [1.0, 1.1]}, index=pd.to_datetime(["2026-09-26", "2026-09-29"]))
        stale = pd.DataFrame({"close": [1.0, 1.1]}, index=pd.to_datetime(["2026-09-01", "2026-09-10"]))
        self.assertTrue(tsb.benchmark_is_fresh(fresh, sym))
        self.assertFalse(tsb.benchmark_is_fresh(stale, sym))


# ───────────────────────────── V16 synthetic market ───────────────────────────── #

def _synthetic_us(as_of_utc: str, days: int = 320, seed: int = 3):
    """Build the four frames for a fake US name ending at as_of (UTC). 1m/5m cover extended hours 04:00-20:00 ET;
    1h is RTH-only (fetched with useRTH=1); daily = regular sessions."""
    clock = ec.resolve_exchange_clock("AAPL")
    cal = ec.get_calendar("NYSE")
    end = pd.Timestamp(as_of_utc, tz="UTC")
    sched = cal.schedule(start_date=(end - pd.Timedelta(days=int(days * 1.6))).date(), end_date=end.date())
    rng = np.random.default_rng(seed)
    price = 100.0
    rows_d, rows_h, rows_5, rows_1 = [], [], [], []
    for i in range(len(sched)):
        o = ec._as_utc(sched.iloc[i]["market_open"]); c = ec._as_utc(sched.iloc[i]["market_close"])
        day_ret = rng.normal(0.0005, 0.012)
        op = price
        cl = price * (1 + day_ret)
        hi, lo = max(op, cl) * 1.004, min(op, cl) * 0.996
        rows_d.append((o.normalize().tz_localize(None).tz_localize("UTC"), op, hi, lo, cl, 1_000_000.0 * rng.uniform(0.6, 1.6)))
        # hourly RTH bars
        t = o
        while t < c:
            rows_h.append((t, op, hi, lo, cl, 150_000.0))
            t += pd.Timedelta(hours=1)
        price = cl
    df_1d = pd.DataFrame(rows_d, columns=["t", "open", "high", "low", "close", "volume"]).set_index("t")
    df_1h = pd.DataFrame(rows_h, columns=["t", "open", "high", "low", "close", "volume"]).set_index("t")
    df_1h = df_1h[df_1h.index >= end - pd.Timedelta(days=62)]
    # 5m and 1m: last 7 / 2 trading days, 04:00-20:00 ET, flat-ish around the close, thin volume outside RTH
    last_days = sched.iloc[-7:]
    for i in range(len(last_days)):
        o = ec._as_utc(last_days.iloc[i]["market_open"]); c = ec._as_utc(last_days.iloc[i]["market_close"])
        start = o - pd.Timedelta(hours=5, minutes=30); stop = c + pd.Timedelta(hours=4)
        t = start
        base = float(df_1d["close"].iloc[-(len(last_days) - i)])
        while t < stop and t < end:
            in_rth = o <= t < c
            rows_5.append((t, base, base * 1.001, base * 0.999, base * (1 + rng.normal(0, 0.0005)), 20_000.0 if in_rth else 500.0, base))
            t += pd.Timedelta(minutes=5)
        if i >= len(last_days) - 2:
            t = start
            while t < stop and t < end:
                in_rth = o <= t < c
                rows_1.append((t, base, base * 1.0005, base * 0.9995, base * (1 + rng.normal(0, 0.0003)), 4_000.0 if in_rth else 100.0, base))
                t += pd.Timedelta(minutes=1)
    df_5m = pd.DataFrame(rows_5, columns=["t", "open", "high", "low", "close", "volume", "wap"]).set_index("t")
    df_1m = pd.DataFrame(rows_1, columns=["t", "open", "high", "low", "close", "volume", "wap"]).set_index("t")
    return clock, {"1m": df_1m, "5m": df_5m, "1h": df_1h, "1d": df_1d}


class SessionsAndCompletionTest(unittest.TestCase):
    def setUp(self):
        self.clock, self.frames = _synthetic_us("2026-10-23 15:31:00")      # 11:31 ET, regular session
        self.s = sv.Sessions(self.clock, pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-10-24").date())

    def test_rth_filter_drops_extended_hours(self):
        m5 = sv.rth_only(self.frames["5m"], self.s)
        self.assertTrue(len(m5) < len(self.frames["5m"]))
        self.assertTrue(all(self.s.in_rth(t) for t in m5.index))

    def test_completion_uses_bar_end_and_first_hour_is_short(self):
        # RTH-fetched hourly bars are aligned to the open (09:30-10:30 … 15:30-16:00): the LAST bucket is the short one
        first = pd.Timestamp("2026-10-23 13:30:00", tz="UTC")                   # 09:30 ET bar
        last = pd.Timestamp("2026-10-23 19:30:00", tz="UTC")                    # 15:30 ET bar
        self.assertEqual(self.s.bar_end(first, 60), first + pd.Timedelta(hours=1))
        self.assertEqual(self.s.bar_end(last, 60), pd.Timestamp("2026-10-23 20:00:00", tz="UTC"))   # cut at the close
        as_of = pd.Timestamp("2026-10-23 14:30:00", tz="UTC")                  # 10:30 ET: first bar just completed
        hc = sv.completed(self.frames["1h"], self.s, 60, as_of)
        self.assertEqual(hc.index[-1], first)
        hc2 = sv.completed(self.frames["1h"], self.s, 60, as_of - pd.Timedelta(minutes=1))
        self.assertLess(hc2.index[-1], first)

    def test_daily_completion_and_attribution_day(self):
        as_of = pd.Timestamp("2026-10-23 15:31:00", tz="UTC")
        dc = sv.completed(self.frames["1d"], self.s, None, as_of)
        self.assertEqual(pd.Timestamp(dc.index[-1]).date(), pd.Timestamp("2026-10-22").date())
        self.assertEqual(str(sv.attribution_day(as_of, self.s)), "2026-10-23")
        early = pd.Timestamp("2026-10-23 07:00:00", tz="UTC")                   # 03:00 ET → belongs to 10-22
        self.assertEqual(str(sv.attribution_day(early, self.s)), "2026-10-22")
        self.assertEqual(str(sv.attribution_day(pd.Timestamp("2026-10-23 09:00:00", tz="UTC"), self.s)), "2026-10-23")


class PayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock, cls.frames = _synthetic_us("2026-10-23 15:31:00")
        cls.p = sv.build_payload("TEST", cls.frames, cls.clock, "20261023T153100Z",
                                 generated_at=pd.Timestamp("2026-10-23 15:40:00", tz="UTC"))

    def test_prefix_schema_and_reserved_keys(self):
        p = self.p
        self.assertTrue(all(k.startswith("TEST_") for k in p))
        for k in ("TEST_symbol", "TEST_market_data_timestamp_utc", "TEST_latest_data_timestamp",
                  "TEST_current_snapshot_last_price", "TEST_session_context_current_session"):
            self.assertIn(k, p)
        self.assertEqual(p["TEST_feature_version"], "v16")
        for layer in ("strategic_2y", "tactical_2m", "intraday_7d"):
            self.assertIn("[TEST", p[f"TEST_price_structure_{layer}_schema"])
            self.assertIsInstance(p[f"TEST_price_structure_{layer}_confirmed"], list)
        self.assertTrue(p["TEST_price_digest_schema"].startswith("[TEST]"))
        self.assertEqual(p["TEST_session_context_current_session"], "regular")
        self.assertEqual(p["TEST_session_context_attribution_day"], "2026-10-23")
        self.assertEqual(p["TEST_session_context_bar_age_minutes"], 10)

    def test_no_judgment_labels(self):
        banned = ("cross_timeframe", "ma_alignment", "volatility_regime", "is_consolidating", "today_open_gap_direction",
                  "weekly_snapshot", "ma_40_weekly", "macd", "bb_pct_b", "obv", "sharpe", "correlation", "percentile_in_52w",
                  "last_wap", "stop_clusters")
        for k in self.p:
            for b in banned:
                self.assertNotIn(b, k, k)
        for v in self.p.values():
            if isinstance(v, str):
                self.assertNotIn("Bullish", v); self.assertNotIn("Bearish", v)

    def test_current_snapshot_regular_session(self):
        p = self.p
        self.assertIsNotNone(p["TEST_current_snapshot_prior_rth_close"])
        self.assertAlmostEqual(p["TEST_current_snapshot_prior_rth_close"], float(self.frames["1d"]["close"].iloc[-2]), places=3)
        self.assertIsNotNone(p["TEST_current_snapshot_open_gap_pct"])
        self.assertIsNotNone(p["TEST_current_snapshot_rth_vwap"])
        self.assertIsNone(p["TEST_current_snapshot_ext_session_change_pct"])
        self.assertIsNotNone(p["TEST_current_snapshot_rvol_tod"])
        self.assertGreater(p["TEST_current_snapshot_rvol_tod"], 0.5)

    def test_signed_distances_and_returns(self):
        p = self.p
        self.assertLessEqual(p["TEST_positioning_range_anchors_30d_dist_to_high_pct"], 0)
        self.assertGreaterEqual(p["TEST_positioning_range_anchors_30d_dist_to_low_pct"], 0)
        self.assertIsNotNone(p["TEST_daily_returns_pct_20d"])
        self.assertIsNotNone(p["TEST_trend_signal_inputs_ret_20d_close_pct"])
        self.assertIn("TEST_positioning_pullback_ladder_90d_>=2%", p)

    def test_debater_view_strips_chart_and_truncates(self):
        dv = sv.debater_view(self.p)
        self.assertFalse(any("_chart_" in k for k in dv))
        full = self.p["TEST_price_structure_strategic_2y_confirmed"]
        self.assertLessEqual(len(dv["TEST_price_structure_strategic_2y_confirmed"]), 16)
        self.assertEqual(dv["TEST_price_structure_strategic_2y_earlier_count"], max(0, len(full) - 16))

    def test_premarket_and_after_hours_fields(self):
        clock, frames = _synthetic_us("2026-10-23 12:00:00")                     # 08:00 ET pre-market
        p = sv.build_payload("PRE", frames, clock, "20261023T120000Z")
        self.assertEqual(p["PRE_session_context_current_session"], "pre-market")
        self.assertIsNone(p["PRE_current_snapshot_rth_vwap"])
        self.assertIsNone(p["PRE_current_snapshot_rvol_tod"])
        self.assertIsNotNone(p["PRE_current_snapshot_ext_session_change_pct"])
        self.assertIsNone(p["PRE_current_snapshot_open_gap_pct"])
        clock, frames = _synthetic_us("2026-10-23 21:30:00")                     # 17:30 ET after-hours
        p = sv.build_payload("AFT", frames, clock, "20261023T213000Z")
        self.assertEqual(p["AFT_session_context_current_session"], "after-hours")
        self.assertIsNotNone(p["AFT_current_snapshot_ext_session_change_pct"])
        self.assertIsNotNone(p["AFT_current_snapshot_rth_vwap"])
        # after the close, today's bar is complete and the prior close is still the previous day
        self.assertAlmostEqual(p["AFT_current_snapshot_prior_rth_close"], float(frames["1d"]["close"].iloc[-2]), places=3)


class NoVolumeAndGapsTest(unittest.TestCase):
    def test_index_contract_nulls_volume_fields(self):
        clock, frames = _synthetic_us("2026-10-23 15:31:00")
        for k in frames:
            frames[k] = frames[k].assign(volume=0.0)
        p = sv.build_payload("ZEROVOL", frames, clock, "20261023T153100Z")
        self.assertFalse(p["ZEROVOL_session_context_has_volume"])
        self.assertIsNone(p["ZEROVOL_current_snapshot_rth_vwap"])
        self.assertIsNone(p["ZEROVOL_current_snapshot_rvol_tod"])
        self.assertIsNone(p["ZEROVOL_daily_up_down_vol_ratio_20d"])
        self.assertTrue(all(r[5] is None for r in p["ZEROVOL_price_digest_daily"]))

    def test_unfilled_gap_detected_and_filled_gap_dropped(self):
        clock, frames = _synthetic_us("2026-10-23 15:31:00")
        d = frames["1d"].copy()
        # make an up gap 5 days ago: low above prior high by 3%; keep later lows above it (unfilled)
        i = len(d) - 6
        ph = float(d["high"].iloc[i - 1])
        for j in range(i, len(d)):
            lvl = ph * 1.03
            d.iloc[j, d.columns.get_loc("low")] = max(float(d["low"].iloc[j]), lvl)
            d.iloc[j, d.columns.get_loc("open")] = max(float(d["open"].iloc[j]), lvl)
            d.iloc[j, d.columns.get_loc("close")] = max(float(d["close"].iloc[j]), lvl * 1.001)
            d.iloc[j, d.columns.get_loc("high")] = max(float(d["high"].iloc[j]), lvl * 1.002)
        frames["1d"] = d
        for k in ("1m", "5m", "1h"):
            frames[k] = frames[k].assign(close=frames[k]["close"] * 0 + float(d["close"].iloc[-1]),
                                         open=float(d["close"].iloc[-1]), high=float(d["close"].iloc[-1]) * 1.001,
                                         low=float(d["close"].iloc[-1]) * 0.999)
        p = sv.build_payload("GAP", frames, clock, "20261023T153100Z")
        gaps = p["GAP_daily_unfilled_gaps_60d"]
        self.assertTrue(any(g[1] == "up" for g in gaps), gaps)


class BadPriceRowsTest(unittest.TestCase):
    def test_zero_price_daily_rows_are_dropped_and_flagged(self):
        clock, frames = _synthetic_us("2026-10-23 15:31:00")
        d = frames["1d"].copy()
        for i in (0, 40, len(d) - 30):
            d.iloc[i, [d.columns.get_loc(c) for c in ("open", "high", "low", "close")]] = 0.0
        frames["1d"] = d
        p = sv.build_payload("KR", frames, clock, "20261023T153100Z")
        self.assertGreater(p["KR_positioning_range_anchors_2y_low"], 0)
        self.assertGreater(p["KR_positioning_range_anchors_52w_low"], 0)
        self.assertTrue(any("non-positive prices" in f and f.startswith("daily") for f in p["KR_session_context_quality_flags"]))
        self.assertTrue(all(r[3] > 0 for r in p["KR_price_digest_quarterly"] + p["KR_price_digest_monthly"]))


class LoaderTest(unittest.TestCase):
    def test_load_ohlcv_keeps_rows_without_volume(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            pth = Path(td) / "X_5y_1d_20261023T000000Z.csv"
            pth.write_text("date,open,high,low,close,volume,barCount,wap\n"
                           "2026-10-21 00:00:00+00:00,1,2,0.5,1.5,,,\n2026-10-22 00:00:00+00:00,1.5,2,1,1.8,,,\n", encoding="utf-8")
            df = tsb.load_ohlcv(pth)
            self.assertEqual(len(df), 2)
            self.assertIn("volume", df.columns)


if __name__ == "__main__":
    unittest.main()
