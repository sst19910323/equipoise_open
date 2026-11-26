#!/usr/bin/env python3
"""evidence_quant 的纯计算部分(不连盈透, 不读本地行情)。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import evidence_quant as Q  # noqa: E402


class CorrDriftTest(unittest.TestCase):
    def test_negative_correlation_deepening_is_tightening(self):
        """从 -0.3 走到 -0.7 是反向联动变强, 不是脱钩(2026-10-02 修: 原先按带符号的差值判)。"""
        self.assertEqual(Q._corr_drift(-0.3, -0.7), "tightening")

    def test_negative_correlation_fading_is_decoupling(self):
        self.assertEqual(Q._corr_drift(-0.7, -0.3), "decoupling")

    def test_positive_side_unchanged(self):
        self.assertEqual(Q._corr_drift(0.3, 0.7), "tightening")
        self.assertEqual(Q._corr_drift(0.7, 0.3), "decoupling")
        self.assertEqual(Q._corr_drift(0.5, 0.55), "stable")

    def test_sign_flip_is_reported_as_such(self):
        self.assertEqual(Q._corr_drift(0.4, -0.4), "sign flip")
        self.assertEqual(Q._corr_drift(0.05, -0.3), "tightening")   # 一头贴零: 不算换向, 按强度变化读

    def test_direction_label(self):
        self.assertEqual(Q._corr_direction(0.6), "same direction")
        self.assertEqual(Q._corr_direction(-0.6), "opposite direction")
        self.assertEqual(Q._corr_direction(0.03), "no clear direction")



class IntradayBarsTest(unittest.TestCase):
    """bars 的盘中档: 只读备餐落下的文件, 时段按交易所日历, 一次一个交易日(5 分钟线), 不读本场时刻之后的数据。"""

    def setUp(self):
        import tempfile
        self.old_root, self.tmp = Q.ROOT, tempfile.mkdtemp()
        Q.ROOT = self.tmp

    def tearDown(self):
        import shutil
        Q.ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, sym, tag, file_ts, stamps):
        import csv
        import os
        d = os.path.join(self.tmp, "data", "market_data", sym)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sym}_7d_{tag}_{file_ts}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume", "barCount", "wap"])
            for i, t in enumerate(stamps):
                w.writerow([t.strftime("%Y-%m-%d %H:%M:%S+00:00"), 100 + i, 101 + i, 99 + i, 100.5 + i, 10, 1, 100])

    @staticmethod
    def _stamps(day, h0, m0, h1, m1, step):
        from datetime import datetime, timedelta, timezone
        t = datetime(*day, h0, m0, tzinfo=timezone.utc)
        end = datetime(*day, h1, m1, tzinfo=timezone.utc)
        out = []
        while t <= end:
            out.append(t)
            t += timedelta(minutes=step)
        return out

    def _jp_config(self):
        import os
        os.makedirs(os.path.join(self.tmp, "config"), exist_ok=True)
        with open(os.path.join(self.tmp, "config", "symbols.yaml"), "w", encoding="utf-8") as f:
            f.write("symbol_contracts:" + chr(10) + "  JPT:" + chr(10) + "    primary_exchange: TSEJ" + chr(10)
                    + "    region: JP" + chr(10) + "    currency: JPY" + chr(10))
        from analysis import technical_snapshot_builder as tb
        tb._SYMBOLS_CFG_CACHE.clear()

    def test_us_one_trading_day_with_three_sessions(self):
        # 美东夏令时: 盘前 08:00 UTC 起, 正股 13:30-20:00 UTC, 盘后到 24:00 UTC
        stamps = self._stamps((2026, 9, 30), 8, 0, 23, 55, 5) + self._stamps((2026, 10, 1), 8, 0, 11, 50, 5)
        self._write("USX", "5m", "20261001T115000Z", stamps)
        r = Q.handle({"mode": "bars", "symbol": "usx", "interval": "5m", "as_of": "20261001T115100Z"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["trading_days"], ["2026-10-01"])             # 默认最近一个交易日
        self.assertEqual({b["session"] for b in r["data"]}, {"pre-market"})
        self.assertTrue(r["data"][-1].get("partial"))                    # 11:50 这根到 11:51 还没走完
        self.assertNotIn("partial", r["data"][-2])
        self.assertIn("pre-market", r["coverage"])
        self.assertTrue(set(r["data"][0]) >= {"t", "local", "session", "open", "high", "low", "close", "volume"})
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "5m", "date": "2026-09-30",
                      "as_of": "20261001T115100Z"})
        n = {}
        for b in r["data"]:
            n[b["session"]] = n.get(b["session"], 0) + 1
        self.assertEqual(n, {"pre-market": 66, "regular": 78, "after-hours": 48})
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "5m", "date": "2026-09-29",
                      "as_of": "20261001T115100Z"})
        self.assertFalse(r["ok"])                                        # 指定的那天没有 → 报缺, 不换成别的日子
        self.assertIn("2026-09-30", r["error"])

    def test_nothing_after_the_session_time_is_read(self):
        self._write("USX", "5m", "20261001T115000Z", self._stamps((2026, 10, 1), 8, 0, 11, 50, 5))
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "5m", "as_of": "20261001T090200Z"})
        self.assertFalse(r["ok"])                                        # 这份文件是本场时刻之后才备的
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "5m", "as_of": "20261001T160000Z"})
        self.assertFalse(r["ok"])                                        # 文件离本场时刻太远, 不算本场备的
        r = Q.handle({"mode": "bars", "symbol": "NOFILE", "interval": "1h"})
        self.assertFalse(r["ok"])
        self.assertIn("daily bars", r["error"])                                # 别的标的只有日线, 不现拉盘中数据
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "2h"})
        self.assertFalse(r["ok"])

    def test_hourly_gives_recent_trading_days_with_a_cap(self):
        stamps = []
        for d in (14, 15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30):   # 十三个交易日
            stamps += self._stamps((2026, 9, d), 13, 30, 19, 30, 60)
        self._write("USX", "1h", "20260930T200000Z", stamps)
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "1h"})
        self.assertEqual(len(r["trading_days"]), 5)                      # 默认五天
        r = Q.handle({"mode": "bars", "symbol": "USX", "interval": "1h", "lookback_days": 99})
        self.assertEqual(len(r["trading_days"]), 10)                     # 上限十天

    def test_non_us_regular_session_only_and_lunch_break(self):
        self._jp_config()
        # 东京 2026-10-01: 开盘 00:00 UTC, 午休 02:30-03:30 UTC, 收盘 06:30 UTC; 指数类在午休里也有平走的报价
        self._write("JPT", "5m", "20261001T063000Z", self._stamps((2026, 10, 1), 0, 0, 6, 25, 5))
        r = Q.handle({"mode": "bars", "symbol": "JPT", "interval": "5m"})
        self.assertTrue(r["ok"], r)
        self.assertEqual({b["session"] for b in r["data"]}, {"regular"})
        self.assertEqual(r["dropped_lunch_break"], 12)
        self.assertEqual(len(r["data"]), 66)
        self.assertIn("regular session only", r["coverage"])
        self.assertEqual(r["exchange_tz"], "Asia/Tokyo")

    def test_korea_has_no_lunch_break_in_the_bars(self):
        import os
        os.makedirs(os.path.join(self.tmp, "config"), exist_ok=True)
        with open(os.path.join(self.tmp, "config", "symbols.yaml"), "w", encoding="utf-8") as f:
            f.write("symbol_contracts:" + chr(10) + "  KRT:" + chr(10) + "    primary_exchange: KSE" + chr(10)
                    + "    region: KR" + chr(10) + "    currency: KRW" + chr(10))
        from analysis import technical_snapshot_builder as tb
        tb._SYMBOLS_CFG_CACHE.clear()
        # 首尔 2026-09-29: 09:00-15:30 当地 = 00:00-06:30 UTC, 中午不停
        self._write("KRT", "5m", "20260929T063000Z", self._stamps((2026, 9, 29), 0, 0, 6, 25, 5))
        r = Q.handle({"mode": "bars", "symbol": "KRT", "interval": "5m"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["data"]), 78)
        self.assertNotIn("dropped_lunch_break", r)

    def _kr_config(self):
        import os
        os.makedirs(os.path.join(self.tmp, "config"), exist_ok=True)
        with open(os.path.join(self.tmp, "config", "symbols.yaml"), "w", encoding="utf-8") as f:
            f.write("symbol_contracts:" + chr(10) + "  KRT:" + chr(10) + "    primary_exchange: KSE" + chr(10)
                    + "    region: KR" + chr(10) + "    currency: KRW" + chr(10))
        from analysis import technical_snapshot_builder as tb
        tb._SYMBOLS_CFG_CACHE.clear()

    def test_non_us_file_full_of_off_session_bars_is_withheld(self):
        self._kr_config()
        self._write("KRT", "5m", "20260929T200000Z", self._stamps((2026, 9, 29), 0, 0, 19, 55, 5))
        r = Q.handle({"mode": "bars", "symbol": "KRT", "interval": "5m"})
        self.assertFalse(r["ok"])                                        # 盘外数据太多, 来源没核实, 不靠裁剪冒充正常
        self.assertIn("unverified", r["error"])

    def test_tokyo_stock_off_session_bars_are_dropped_with_the_known_source(self):
        """东证个股: 盘外 bar 的来源已向盈透核实(场外交易系统), 剔除并写明, 不整份拒绝。"""
        self._jp_config()
        self._write("JPT", "5m", "20261001T200000Z", self._stamps((2026, 10, 1), 0, 0, 19, 55, 5))
        r = Q.handle({"mode": "bars", "symbol": "JPT", "interval": "5m"})
        self.assertTrue(r["ok"], r)
        self.assertEqual({b["session"] for b in r["data"]}, {"regular"})
        self.assertEqual(len(r["data"]), 66)                             # 六个半小时减一小时午休
        self.assertEqual(r["dropped_off_session"], 162)
        self.assertIn("Japannext", r["off_session_source"])

    def test_daily_default_is_unchanged_and_carries_open_when_present(self):
        import csv
        import os
        from datetime import date, timedelta
        d = os.path.join(self.tmp, "data", "market_data", "USX")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "USX_5y_1d_1.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for i in range(5):
                w.writerow([str(date.today() - timedelta(days=4 - i)), 9 + i, 11 + i, 8 + i, 10 + i, 100])
        os.environ["EQUIPOISE_EVIDENCE_LIVE_FETCH"] = "0"
        r = Q.handle({"mode": "bars", "symbol": "USX", "lookback_days": 3})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["data"]), 3)
        self.assertEqual(r["data"][-1]["open"], 13.0)
        self.assertNotIn("interval", r)


class LunchBreakSessionTest(unittest.TestCase):
    def test_snapshot_session_is_not_regular_during_the_lunch_break(self):
        import pandas as pd
        from analysis import technical_snapshot_builder as tb
        clock = dict(tb.EXCHANGE_CLOCKS["TSEJ"], exchange="TSEJ")
        got = {h: tb.classify_session(pd.Timestamp(f"2026-10-02 {h}", tz="UTC"), clock)["current_session"]
               for h in ("01:00", "02:25", "02:30", "03:00", "03:25", "03:30", "06:00", "07:00")}
        self.assertEqual(got, {"01:00": "regular", "02:25": "regular", "02:30": "closed", "03:00": "closed",
                               "03:25": "closed", "03:30": "regular", "06:00": "regular", "07:00": "closed"})
        us = tb.classify_session(pd.Timestamp("2026-10-02 15:00", tz="UTC"))["current_session"]
        self.assertEqual(us, "regular")                                  # 没有午休的市场不受影响

    def test_a_lunch_break_abolished_long_ago_is_not_applied(self):
        """日历库对韩国仍给出 2000 年就取消的午休时间: 首尔中午照常交易, 不能判成休市。"""
        import pandas as pd
        from analysis import technical_snapshot_builder as tb
        clock = dict(tb.EXCHANGE_CLOCKS["KSE"], exchange="KSE")
        for h in ("02:55", "03:00", "03:30", "03:55", "04:00"):          # 首尔 11:55 到 13:00
            got = tb.classify_session(pd.Timestamp(f"2026-09-29 {h}", tz="UTC"), clock)["current_session"]
            self.assertEqual(got, "regular", h)


class AsOfClockTest(unittest.TestCase):
    """演习(PTR)时钟: 设了 EQUIPOISE_AS_OF, 数据腿只给那一刻已知的数据; 不设时行为不变。"""

    AS_OF = "20261009T133700Z"

    def setUp(self):
        import os
        import tempfile
        self.old_root, self.tmp = Q.ROOT, tempfile.mkdtemp()
        Q.ROOT = self.tmp
        self.old_env = {k: os.environ.get(k) for k in ("EQUIPOISE_AS_OF", "EQUIPOISE_IBKR_PORT",
                                                        "EQUIPOISE_EVIDENCE_LIVE_FETCH")}
        for k in self.old_env:
            os.environ.pop(k, None)
        self._daily("A", ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"])

    def tearDown(self):
        import os
        import shutil
        Q.ROOT = self.old_root
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _daily(self, sym, dates):
        import csv
        import os
        d = os.path.join(self.tmp, "data", "market_data", sym)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sym}_5y_1d_20261106T000000Z.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for i, day in enumerate(dates):
                w.writerow([day, 10 + i, 11 + i, 9 + i, 10.5 + i, 1000])

    def _set(self):
        import os
        os.environ["EQUIPOISE_AS_OF"] = self.AS_OF

    def test_as_of_drops_same_day_bar(self):
        self._set()
        r = Q.handle({"mode": "bars", "symbol": "A", "lookback_days": 10})
        self.assertTrue(r["ok"], r)
        self.assertEqual([b["date"] for b in r["data"]], ["2026-10-07", "2026-10-08"])
        self._daily("B", ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"])
        r = Q.handle({"mode": "relative_strength", "symbol": "A", "benchmark": "B", "window_days": 10})
        self.assertEqual([p["date"] for p in r["data"]["series"]], ["2026-10-07", "2026-10-08"])

    def test_as_of_lookback_counts_from_the_cutoff(self):
        self._set()
        r = Q.handle({"mode": "bars", "symbol": "A", "lookback_days": 1})
        self.assertEqual([b["date"] for b in r["data"]], ["2026-10-08"])   # 截断在取尾部之前

    def test_as_of_disables_live_fetch(self):
        import os
        self._set()
        os.environ["EQUIPOISE_IBKR_PORT"] = "4001"
        import builtins
        real, attempted = builtins.__import__, []

        def guard(name, *a, **kw):
            if name.startswith("ib_insync"):
                attempted.append(name)          # _live_fetch 吞异常, 所以记下尝试而不是靠抛错
                raise ImportError("blocked in test")
            return real(name, *a, **kw)

        builtins.__import__ = guard
        try:
            self.assertIsNone(Q._live_fetch("A", 10))
        finally:
            builtins.__import__ = real
        self.assertEqual(attempted, [])

    def test_as_of_not_stale(self):
        self._set()
        r = Q.handle({"mode": "bars", "symbol": "A", "lookback_days": 10})
        self.assertNotIn("stale_warning", r)                                # "今天" = as_of 那天, 不是墙上时钟

    def test_as_of_calendar_window(self):
        import os
        self._set()
        os.makedirs(os.path.join(self.tmp, "config"), exist_ok=True)
        with open(os.path.join(self.tmp, "config", "event_calendar.yaml"), "w", encoding="utf-8") as f:
            f.write("- {date: 2026-10-08, type: cpi}\n- {date: 2026-10-15, type: cpi}\n- {date: 2026-11-05, type: fomc}\n")
        r = Q.handle({"mode": "event_calendar", "horizon_days": 14})
        self.assertEqual([e["date"] for e in r["data"]["events"]], ["2026-10-15"])

    def test_as_of_econ_picks_file_not_after(self):
        import os
        self._set()
        d = os.path.join(self.tmp, "data", "economic")
        os.makedirs(d, exist_ok=True)
        for ts, v in (("20261008T120000Z", "old"), ("20261009T134050Z", "prep"), ("20261020T090000Z", "later")):
            with open(os.path.join(d, f"economic_US_{ts}.txt"), "w", encoding="utf-8") as f:
                f.write(f"cpi_yoy {v}\n")
        r = Q.handle({"mode": "econ_series", "region": "US", "indicator": "cpi_yoy"})
        self.assertEqual(r["data"]["source_file"], "economic_US_20261009T134050Z.txt")  # 备餐余量 15 分钟内
        self.assertEqual(r["data"]["lines"], ["cpi_yoy prep"])

    def test_as_of_intraday_defaults_and_clamps(self):
        import csv
        import os
        from datetime import datetime, timedelta, timezone
        self._set()
        d = os.path.join(self.tmp, "data", "market_data", "A")
        t, stamps = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc), []
        while t <= datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc):
            stamps.append(t)
            t += timedelta(minutes=5)
        with open(os.path.join(d, "A_7d_5m_20261009T133500Z.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for s in stamps:
                w.writerow([s.strftime("%Y-%m-%d %H:%M:%S+00:00"), 1, 1, 1, 1, 1])
        r = Q.handle({"mode": "bars", "symbol": "A", "interval": "5m"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data_through_utc"], "2026-10-09T13:37:00Z")
        r = Q.handle({"mode": "bars", "symbol": "A", "interval": "5m", "as_of": "20261009T150000Z"})
        self.assertEqual(r["data_through_utc"], "2026-10-09T13:37:00Z")    # 请求里写得更晚也不行

    def test_bad_as_of_fails_loudly(self):
        import os
        os.environ["EQUIPOISE_AS_OF"] = "yesterday"
        with self.assertRaises(ValueError):
            Q._drill_as_of()

    def test_no_env_unchanged(self):
        r = Q.handle({"mode": "bars", "symbol": "A", "lookback_days": 10})
        self.assertEqual([b["date"] for b in r["data"]][-1], "2026-10-12")
        self.assertIsNone(Q._drill_as_of())


if __name__ == "__main__":
    unittest.main()
