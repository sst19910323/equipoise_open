#!/usr/bin/env python3
"""取数后的时段过滤: 东证个股去掉场外时段的盘中 bar 与未开盘交易日的假日线; 别的市场原样通过。"""
from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datalayer.fetchers.session_filter import drop_off_exchange_bars, _bar_minutes  # noqa: E402

warnings.filterwarnings("ignore")


def _cfg(tmp: Path) -> Path:
    p = tmp / "symbols.yaml"
    p.write_text("symbol_contracts:\n"
                 "  JPT: {primary_exchange: TSEJ, region: JP, currency: JPY}\n"
                 "  KRT: {primary_exchange: KSE, region: KR, currency: KRW}\n"
                 "  JPI: {primary_exchange: OSE.JPN, region: JP, currency: JPY}\n", encoding="utf-8")
    return p


def _frame(stamps):
    idx = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True), name="date")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}, index=idx)


class SessionFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bar_minutes(self):
        self.assertEqual((_bar_minutes("5 mins"), _bar_minutes("1 min"), _bar_minutes("1 hour"), _bar_minutes("1 day")),
                         (5, 1, 60, None))

    def test_tokyo_five_minute_bars_keep_only_the_regular_session(self):
        # 2026-10-01: 东证 00:00-02:30、03:30-06:30 UTC
        df = _frame(pd.date_range("2026-10-01 00:00", "2026-10-01 20:55", freq="5min", tz="UTC"))
        out, info = drop_off_exchange_bars(df, "JPT", "5 mins", symbols_config_path=self.cfg)
        self.assertEqual(len(out), 66)                       # 上午 30 根 + 下午 36 根
        self.assertEqual(info["dropped"], len(df) - 66)
        self.assertEqual(str(out.index[0]), "2026-10-01 00:00:00+00:00")
        self.assertEqual(str(out.index[-1]), "2026-10-01 06:25:00+00:00")
        self.assertNotIn(pd.Timestamp("2026-10-01 03:00", tz="UTC"), out.index)   # 午休

    def test_tokyo_hourly_bar_that_spans_the_lunch_break_is_kept(self):
        # 东京 08 点到 16 点各一根(UTC 前一日 23:00 起): 08 点那根全在开盘前, 16 点那根全在收盘后
        df = _frame(pd.date_range("2026-09-30 23:00", "2026-10-01 07:00", freq="60min", tz="UTC"))
        out, _ = drop_off_exchange_bars(df, "JPT", "1 hour", symbols_config_path=self.cfg)
        self.assertEqual([t.hour for t in out.index], [0, 1, 2, 3, 4, 5, 6])      # 03:00 UTC 那根含下午开盘后的成交
        # 场外系统 08:20 开始的那根只到 09:00, 不满一小时, 与正股时段不重叠
        df = _frame(["2026-09-30 23:20", "2026-10-01 00:00", "2026-10-01 01:00"])
        out, _ = drop_off_exchange_bars(df, "JPT", "1 hour", symbols_config_path=self.cfg)
        self.assertEqual([str(t)[11:16] for t in out.index], ["00:00", "01:00"])

    def test_daily_bar_of_a_session_that_has_not_opened_is_dropped(self):
        df = _frame(["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"])
        friday_night = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)          # 东京周五 21 点
        out, info = drop_off_exchange_bars(df, "JPT", "1 day", now=friday_night, symbols_config_path=self.cfg)
        self.assertEqual([str(t.date()) for t in out.index], ["2026-09-30", "2026-10-01", "2026-10-02"])
        self.assertEqual(info, {"exchange": "TSEJ", "kind": "daily", "dropped": 1})
        monday_open = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)           # 周一开盘后, 这一根是真的
        out, _ = drop_off_exchange_bars(df, "JPT", "1 day", now=monday_open, symbols_config_path=self.cfg)
        self.assertEqual(len(out), 4)
        monday_preopen = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc)       # 周一 08:30, 还没开盘
        out, _ = drop_off_exchange_bars(df, "JPT", "1 day", now=monday_preopen, symbols_config_path=self.cfg)
        self.assertEqual(len(out), 3)

    def test_other_markets_pass_through_untouched(self):
        df = _frame(pd.date_range("2026-10-01 00:00", "2026-10-01 20:55", freq="5min", tz="UTC"))
        for sym in ("KRT", "JPI", "NVDA"):                   # 韩国、日经指数、美股(配置里没有 → 按美股)
            out, info = drop_off_exchange_bars(df, sym, "5 mins", symbols_config_path=self.cfg)
            self.assertEqual(len(out), len(df), sym)
            self.assertEqual(info, {}, sym)
        out, info = drop_off_exchange_bars(pd.DataFrame(), "JPT", "5 mins", symbols_config_path=self.cfg)
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
