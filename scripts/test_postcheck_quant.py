#!/usr/bin/env python3
"""后验尺子回归(重建批 1, 2026-09-30; 决策契约 §4.6–4.7, FINAL §6, 11.2)。

覆盖: 方向以操作为主 / GENERAL 震荡两沿解析 / short_term 按 horizon 柔性清算(两侧同一宽限,
到期未触按进展部分分, 过期不再评) / 触价≠成交 / 尺子版本化 / 档位 3d·20d。
纯函数为主; compute_window_digest 用临时目录里的合成 CSV, 不碰盈透与 PG。
"""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflows import postcheck_quant as pq  # noqa: E402


def _bars(closes, spread=2.0, start="2026-09-16"):
    """合成日线: close 序列 → [{date, high, low, close}], high/low = close ± spread。"""
    import pandas as pd
    dates = pd.bdate_range(start=start, periods=len(closes))
    return [{"date": d, "high": c + spread, "low": c - spread, "close": c}
            for d, c in zip(dates, closes)]


class DirectionTest(unittest.TestCase):
    """后验主要由操作决定; short_term 次之; action 只是综合基调, 不按 55/45 判方向。"""

    def test_operation_family_first(self):
        self.assertEqual(pq._direction({"action": 40, "operation_type": "LIMIT_ENTRY"}), 1)
        self.assertEqual(pq._direction({"action": 60, "operation_type": "TRIM_POSITION"}), -1)

    def test_hold_falls_back_to_short_term_then_action(self):
        self.assertEqual(pq._direction({"action": 40, "operation_type": "HOLD_NEUTRAL",
                                        "short_term": {"direction": "上行"}}), 1)
        self.assertEqual(pq._direction({"action": 62, "operation_type": "HOLD_NEUTRAL",
                                        "short_term": {"direction": "震荡"}}), 1)
        self.assertEqual(pq._direction({"action": 50, "operation_type": "HOLD_NEUTRAL"}), 0)


class TargetMapTest(unittest.TestCase):
    """GENERAL 的 'SPY=95-105' 是区间, 不能被读成 95。"""

    def test_band_and_point_entries(self):
        m = pq.parse_target_map("SPY=95-105; QQQ=500; DIA=44000")
        self.assertEqual(m["SPY"], (95.0, 105.0))
        self.assertEqual(m["QQQ"], 500.0)
        self.assertEqual(pq._parse_price_map("SPY=95-105; QQQ=500"), {"QQQ": 500.0})
        self.assertEqual(pq._parse_band_map("SPY=95-105; QQQ=500"), {"SPY": (95.0, 105.0)})


class ShortTermSettlementTest(unittest.TestCase):
    """short_term 柔性清算: 以 horizon_days 为主窗口, 宽限两侧一致, 到期未触给进展。"""

    def test_grace_is_predefined_and_bounded(self):
        self.assertEqual(pq.grace_days(1), 1)
        self.assertEqual(pq.grace_days(3), 2)
        self.assertEqual(pq.grace_days(7), 3)
        self.assertEqual(pq.grace_days(30), 3)

    def test_incomplete_window_keeps_observed_touches_without_expiry_values(self):
        for levels, expected in (({"st_target": 110.0}, "pending"),
                                 ({"st_target": 103.0}, "target_first"),
                                 ({"st_band": (95.0, 103.0)}, "band_broken")):
            with self.subTest(expected=expected):
                result = pq.short_term_settlement(_bars([101, 103], spread=1), 100.0, 5, **levels)
                self.assertFalse(result["horizon_covered"])
                self.assertEqual(result["outcome_at_expiry"], expected)
                self.assertIsNone(result["expiry_close"])
                self.assertIsNone(result["expiry_date"])
                self.assertIsNone(result["progress_at_expiry_pct"])
                if "st_band" in levels:
                    self.assertIsNone(result["expiry_inside_band"])

    def test_target_first_within_horizon(self):
        bars = _bars([101, 103, 109, 100, 95])
        s = pq.short_term_settlement(bars, price_T=100.0, horizon_days=3, st_target=110.0, invalid=95.0)
        self.assertEqual(s["outcome_at_expiry"], "target_first")
        self.assertEqual(s["outcome_with_grace"], "target_first")
        self.assertEqual(s["first_touch"]["line"], "st_target")

    def test_untouched_at_expiry_gives_progress(self):
        bars = _bars([101, 102, 104, 120, 130], spread=1.0)
        s = pq.short_term_settlement(bars, price_T=100.0, horizon_days=3, st_target=110.0, invalid=95.0)
        self.assertEqual(s["outcome_at_expiry"], "untouched")
        self.assertEqual(s["expiry_close"], 104.0)
        self.assertEqual(s["progress_at_expiry_pct"], 40.0)   # (104-100)/(110-100)
        # 宽限期(第 4、5 天)里触到目标 → 宽限口径算兑现; 到期口径保留原始结果
        self.assertEqual(s["outcome_with_grace"], "target_first")

    def test_grace_applies_to_invalid_side_equally(self):
        bars = _bars([101, 102, 103, 90, 89], spread=1.0)
        s = pq.short_term_settlement(bars, price_T=100.0, horizon_days=3, st_target=110.0, invalid=95.0)
        self.assertEqual(s["outcome_at_expiry"], "untouched")
        self.assertEqual(s["outcome_with_grace"], "invalid_first")

    def test_touch_after_grace_is_not_evaluated(self):
        bars = _bars([101, 102, 103, 104, 105, 106, 107, 112], spread=1.0)
        s = pq.short_term_settlement(bars, price_T=100.0, horizon_days=3, st_target=110.0, invalid=95.0)
        self.assertEqual(s["outcome_with_grace"], "untouched")
        self.assertTrue(s["expired_before_window_end"])

    def test_band_held_and_broken(self):
        held = pq.short_term_settlement(_bars([100, 101, 99], spread=1.0), price_T=100.0,
                                        horizon_days=3, st_band=(95.0, 105.0))
        self.assertEqual(held["outcome_at_expiry"], "band_held")
        broken = pq.short_term_settlement(_bars([100, 107, 99], spread=1.0), price_T=100.0,
                                          horizon_days=3, st_band=(95.0, 105.0))
        self.assertEqual(broken["outcome_at_expiry"], "band_broken")
        self.assertEqual(broken["first_touch"]["line"], "st_high")

    def test_pending_when_window_shorter_than_horizon(self):
        s = pq.short_term_settlement(_bars([101, 102], spread=1.0), price_T=100.0,
                                     horizon_days=5, st_target=110.0, invalid=95.0)
        self.assertEqual(s["outcome_at_expiry"], "pending")
        self.assertFalse(s["horizon_covered"])

    def test_no_horizon_settles_over_whole_window(self):
        s = pq.short_term_settlement(_bars([101, 102, 111], spread=1.0), price_T=100.0,
                                     horizon_days=None, st_target=110.0, invalid=95.0)
        self.assertIsNone(s["horizon_days"])
        self.assertEqual(s["outcome_at_expiry"], "target_first")


class EntryReachTest(unittest.TestCase):
    """触价 ≠ 满足全部条件: 机器只知道价格到没到, 字段名照实说。"""

    def test_price_reached_naming(self):
        bars = _bars([101, 96, 100], spread=1.0)
        e = pq.entry_fill_stats(bars, price_T=100.0, entry=97.0)
        self.assertTrue(e["entry_price_reached"])
        self.assertIn("reached_date", e)
        self.assertIn("post_reach_ret_pct", e)
        self.assertNotIn("entry_filled", e)


class DigestTest(unittest.TestCase):
    """compute_window_digest 端到端(合成 CSV): 尺子版本、GENERAL 两沿、short_term 清算进 digest。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_root = pq.MARKET_DATA_ROOT
        pq.MARKET_DATA_ROOT = Path(self.tmp.name)
        import pandas as pd
        for sym, base in (("SPY", 100.0), ("QQQ", 500.0), ("DIA", 400.0), ("MSFT", 500.0)):
            d = Path(self.tmp.name) / sym
            d.mkdir()
            dates = pd.bdate_range(start="2026-09-10", periods=30)
            closes = [base * (1 + 0.004 * i) for i in range(30)]
            pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                          "low": [c * 0.99 for c in closes], "close": closes,
                          "volume": [1000] * 30}).to_csv(d / f"{sym}_5y_1d_20260930T000000Z.csv", index=False)

    def tearDown(self):
        pq.MARKET_DATA_ROOT = self._old_root
        self.tmp.cleanup()

    def test_general_band_reaches_member_and_ruler_recorded(self):
        report = {"action": 55, "operation_type": "HOLD_NEUTRAL",
                  "operation_target": "SPY=101; QQQ=505; DIA=404",
                  "short_term": {"direction": "震荡", "target": "SPY=95-105; QQQ=480-520; DIA=380-420",
                                 "invalid": "SPY=95-105; QQQ=480-520; DIA=380-420", "horizon_days": 5}}
        dg = pq.compute_window_digest("GENERAL", "2026-09-15T20:00:00Z", 20, report)
        self.assertEqual(dg["ruler"], pq.RULER_VERSION)
        spy = next(m for m in dg["members"] if m["symbol"] == "SPY")
        self.assertEqual(spy["st_band"], {"low": 95.0, "high": 105.0})
        self.assertIn("short_term", spy)
        self.assertEqual(spy["short_term"]["horizon_days"], 5)

    def test_stock_short_term_settlement_in_digest(self):
        report = {"action": 58, "operation_type": "LIMIT_ENTRY", "operation_target": "498",
                  "short_term": {"direction": "上行", "target": "520", "invalid": "490", "horizon_days": 5}}
        dg = pq.compute_window_digest("MSFT", "2026-09-15T20:00:00Z", 20, report)
        self.assertEqual(dg["direction_call"], "看多")
        self.assertEqual(dg["short_term"]["horizon_days"], 5)
        self.assertIn(dg["short_term"]["outcome_at_expiry"], {"target_first", "invalid_first", "untouched"})
        self.assertIn("entry_price_reached", dg["entry"])

    def test_delta_crps_in_the_digest_for_3_5_only(self):
        """v3.4 (C8): forecast.delta_crps = CRPS(A) − CRPS(A at w = ½) on the day-15 close; a scenario without the
        session's ruler is not scored; a pre-ruler report (no scenario) has no forecast."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import odds_model as om
        import scenario_replay as sr

        def sc(atoms, schema="3.4"):
            return {"schema": schema, "T_days": 15, "P0": 500.0, "execution": {},
                    "nodes": [{"id": f"N{i}", "p": p, "P_T": pt, "refs": ["Z1"], "events": []} for i, (p, pt) in enumerate(atoms)]}
        A = sr.mixture(sc([(0.7, 560.0), (0.3, 480.0)]), sc([(0.3, 530.0), (0.7, 470.0)]), 0.75, reason="t")
        rep = {"schema_version": "3.4", "operation_type": "HOLD_NEUTRAL", "operation_target": "500", "scenario": A,
               "atr_pct_14": 1.5, "odds": om.snapshot_from_config(om.DEFAULT_CONFIG)}
        dg = pq.compute_window_digest("MSFT", "2026-09-15T20:00:00Z", 20, rep)
        fc = dg["forecast"]
        self.assertEqual(fc["status"], "scored")
        y = fc["realized_return"]
        crps = lambda s_: om.crps_discrete([(n["p"], n["P_T"] / 500.0 - 1.0) for n in s_["nodes"]], y)  # noqa: E731
        self.assertAlmostEqual(fc["delta_crps"], round(crps(A) - crps(sr.rebuild_at_half(A)), 6), 6)
        no_odds = {"operation_type": "HOLD_NEUTRAL", "operation_target": "500", "atr_pct_14": 1.5,
                   "scenario": sc([(0.6, 560.0), (0.4, 480.0)])}
        fc_no = pq.compute_window_digest("MSFT", "2026-09-15T20:00:00Z", 20, no_odds)["forecast"]
        self.assertEqual(fc_no, {"error": "no odds snapshot"})
        pre = {"operation_type": "HOLD_NEUTRAL", "operation_target": "500", "atr_pct_14": 1.5}
        self.assertNotIn("forecast", pq.compute_window_digest("MSFT", "2026-09-15T20:00:00Z", 20, pre))


class R23AnchorAndOrderTest(unittest.TestCase):
    """R23(2026-10-10, Codex 复现的三处缺陷): 盘中报告的起点不是当天收盘、当天余下路径进窗口;
    短期裁定用 5 分钟撞线时序(同日先失效后到目标 = invalid_first); 日韩镜头代号可解析。"""

    def setUp(self):
        import pandas as pd
        self.tmp = tempfile.TemporaryDirectory()
        self._old_root = pq.MARKET_DATA_ROOT
        pq.MARKET_DATA_ROOT = Path(self.tmp.name)
        self.days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
        closes = {"2026-10-01": 100.0, "2026-10-02": 101.0, "2026-10-05": 103.0, "2026-10-06": 104.0, "2026-10-07": 105.0}

        def write(sym, day_hi_lo, intraday):
            d = Path(self.tmp.name) / sym
            d.mkdir()
            rows = []
            for day in self.days:
                hi, lo = day_hi_lo.get(day, (closes[day] + 1, closes[day] - 1))
                rows.append({"date": f"{day} 00:00:00+00:00", "open": closes[day], "high": hi, "low": lo,
                             "close": closes[day], "volume": 1000})
            pd.DataFrame(rows).to_csv(d / f"{sym}_5y_1d_20261008T000000Z.csv", index=False)
            irows = []
            for day in self.days:
                for h in range(13, 20):
                    for m in (0, 30):
                        if h == 13 and m == 0:
                            continue
                        ts = f"{day} {h:02d}:{m:02d}:00+00:00"
                        hi, lo, c = intraday.get((day, h, m), (closes[day] + 0.3, closes[day] - 0.3, closes[day] - 0.5))
                        irows.append({"date": ts, "open": c, "high": hi, "low": lo, "close": c, "volume": 10})
            pd.DataFrame(irows).to_csv(d / f"{sym}_7d_5m_20261008T000000Z.csv", index=False)

        # TGT: 10-02 14:30 发布, 15:00 冲到 110.5(目标 110), 当天收 101
        write("TGT", {"2026-10-02": (111.0, 99.0)}, {("2026-10-02", 15, 0): (110.5, 101.0, 102.0)})
        # ORD: 同日 14:00 先打到 89(失效 90), 15:00 再冲 110.5(目标 110)
        write("ORD", {"2026-10-02": (111.0, 89.0)}, {("2026-10-02", 14, 0): (100.0, 89.0, 95.0),
                                                     ("2026-10-02", 15, 0): (110.5, 101.0, 102.0)})
        # NOI: 只有日线, 没有 5 分钟文件
        d = Path(self.tmp.name) / "NOI"
        d.mkdir()
        pd.DataFrame([{"date": f"{day} 00:00:00+00:00", "open": closes[day], "high": closes[day] + 1,
                       "low": closes[day] - 1, "close": closes[day], "volume": 1000} for day in self.days]
                     ).to_csv(d / "NOI_5y_1d_20261008T000000Z.csv", index=False)

    def tearDown(self):
        pq.MARKET_DATA_ROOT = self._old_root
        self.tmp.cleanup()

    def test_intraday_report_starts_at_its_own_time_and_keeps_the_rest_of_the_day(self):
        report = {"action": 58, "operation_type": "HOLD_NEUTRAL",
                  "short_term": {"direction": "上行", "target": "110", "invalid": "90", "horizon_days": 3}}
        dg = pq.compute_window_digest("TGT", "2026-10-02T14:30:00Z", 3, report)
        self.assertEqual(dg["ruler"], pq.RULER_VERSION)
        self.assertEqual(dg["anchor_source"], "5m_close_at_T")
        self.assertAlmostEqual(dg["price_T"], 100.5)                      # 不是当天收盘 101
        self.assertTrue(dg.get("day_T_partial"))
        self.assertEqual(dg["win_start"], "2026-10-02")                    # 当天余下路径在窗口里
        hits = {e["line"]: e["at"][:10] for e in dg["touch_timeline"]["events"]}
        self.assertEqual(hits.get("st_target") or hits.get("target"), "2026-10-02")   # 15:00 那一笔被看见了(操作锚与短期目标同价时排班只留一个名字)
        self.assertEqual(dg["short_term"]["outcome_at_expiry"], "target_first")
        # 报告带了参考价就用参考价
        report["reference_prices"] = [{"symbol": "TGT", "price": 100.2}]
        dg2 = pq.compute_window_digest("TGT", "2026-10-02T14:30:00Z", 3, report)
        self.assertEqual(dg2["anchor_source"], "report_reference_price")
        self.assertAlmostEqual(dg2["price_T"], 100.2)

    def test_same_day_invalid_then_target_is_invalid_first(self):
        report = {"action": 58, "operation_type": "HOLD_NEUTRAL",
                  "short_term": {"direction": "上行", "target": "110", "invalid": "90", "horizon_days": 3}}
        dg = pq.compute_window_digest("ORD", "2026-10-02T13:45:00Z", 3, report)
        self.assertEqual(dg["touch_timeline"]["resolution"], "5m")
        self.assertEqual(dg["short_term"]["settled_by"], "5m")
        self.assertEqual(dg["short_term"]["outcome_at_expiry"], "invalid_first")   # 旧版按插入顺序给 target_first
        self.assertEqual(dg["short_term"]["first_touch"]["line"], "invalid")
        # 只用日线时同一根 bar 两线都碰到 → 不硬猜
        bars = [{"date": "2026-10-02", "high": 111.0, "low": 89.0, "close": 101.0}]
        st = pq.short_term_settlement(bars, 100.0, 3, st_target=110.0, invalid=90.0,
                                      timeline={"resolution": "daily", "events": [
                                          {"line": "st_target", "price": 110.0, "at": "2026-10-02", "elapsed_td": 1.0, "same_bar_ambiguous": True},
                                          {"line": "invalid", "price": 90.0, "at": "2026-10-02", "elapsed_td": 1.0, "same_bar_ambiguous": True}]})
        self.assertEqual(st["outcome_at_expiry"], "order_unknown")

    def test_without_intraday_data_the_anchor_is_prior_close_and_it_says_so(self):
        report = {"action": 58, "operation_type": "HOLD_NEUTRAL",
                  "short_term": {"direction": "上行", "target": "110", "invalid": "90", "horizon_days": 3}}
        dg = pq.compute_window_digest("NOI", "2026-10-02T14:30:00Z", 3, report)
        self.assertEqual(dg["anchor_source"], "prior_close")
        self.assertAlmostEqual(dg["price_T"], 100.0)                        # 10-01 收盘, 不是 10-02 的 101
        self.assertIn("anchor_note", dg)
        self.assertEqual(dg["win_start"], "2026-10-05")                     # 当天路径没数据, 不计也不冒充

    def test_partial_day_is_not_a_full_trading_day(self):
        """Codex 10-11 复现: 10-02 盘中出报告, 只有当天余下 + 10-05/06 两天; 要 3 天时不能算成熟, 到期也不能提前到 10-06。"""
        import pandas as pd
        d = Path(self.tmp.name) / "PDY"
        d.mkdir()
        days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
        pd.DataFrame([{"date": f"{day} 00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                      for day in days]).to_csv(d / "PDY_5y_1d_20261007T000000Z.csv", index=False)
        irows = [{"date": f"{day} {h:02d}:{m:02d}:00+00:00", "open": 100, "high": 100.3, "low": 99.7, "close": 100, "volume": 1}
                 for day in days for h in range(13, 20) for m in (0, 30) if not (h == 13 and m == 0)]
        pd.DataFrame(irows).to_csv(d / "PDY_7d_5m_20261007T000000Z.csv", index=False)
        report = {"action": 58, "operation_type": "HOLD_NEUTRAL",
                  "short_term": {"direction": "上行", "target": "110", "invalid": "90", "horizon_days": 3}}
        dg = pq.compute_window_digest("PDY", "2026-10-02T14:30:00Z", 3, report)
        self.assertTrue(dg["day_T_partial"])
        self.assertEqual(dg["bars_in_window"], 2)                           # 旧版 3: 部分日冒充了一个交易日
        self.assertAlmostEqual(dg["day_T_fraction"], round(10 / 13, 2), places=2)   # 14:30 之后余下 10 根 ÷ 整日 13 根
        self.assertFalse(dg["short_term"]["horizon_covered"])
        self.assertEqual(dg["short_term"]["outcome_at_expiry"], "pending")
        self.assertEqual(dg["win_start"], "2026-10-02")                     # 当天路径仍在窗口里
        # 三个完整交易日到齐(setUp 的 TGT 有 10-05/06/07): 到期日是第三个完整交易日 10-07, 不是 10-06
        dg2 = pq.compute_window_digest("TGT", "2026-10-02T14:30:00Z", 3, report)
        self.assertEqual(dg2["bars_in_window"], 3)
        self.assertTrue(dg2["short_term"]["horizon_covered"])
        self.assertEqual(dg2["short_term"]["expiry_date"], "2026-10-07")
        # 坐标: 当天 15:00 那笔到目标 elapsed 不到 1(第 0 日), 之后完整交易日从 1 起
        ev = {float(e["price"]): e for e in dg2["touch_timeline"]["events"]}   # 操作锚与短期目标同价, 排班只留一个名字
        self.assertLess(ev[110.0]["elapsed_td"], 1.0)
        self.assertGreater(ev[110.0]["elapsed_td"], 0.0)
        self.assertAlmostEqual(dg2["touch_timeline"]["day_T_fraction"], round(10 / 13, 2), places=2)

    def test_20d_inherits_fine_order_before_settling(self):
        """Codex 10-11 复现: 3d 排班已明确当天先失效后到目标; 20d 合并后 short_term 也要跟着判 invalid_first。"""
        import pandas as pd
        d = Path(self.tmp.name) / "INH"
        d.mkdir()
        dates = [str(x)[:10] for x in pd.bdate_range("2026-09-30", periods=22)]
        rows = [{"date": f"{day} 00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1} for day in dates]
        rows[2]["high"], rows[2]["low"] = 111, 89                           # 10-02 日线两线都碰到
        pd.DataFrame(rows).to_csv(d / "INH_5y_1d_20261107T000000Z.csv", index=False)   # 20d 时 5 分钟文件早已过期: 不给
        report = {"action": 58, "operation_type": "HOLD_NEUTRAL",
                  "short_term": {"direction": "上行", "target": "110", "invalid": "90", "horizon_days": 3}}
        prior = {"resolution": "5m", "untouched": [], "day_T_fraction": 0.85, "events": [
            {"line": "invalid", "price": 90.0, "at": "2026-10-02 14:00:00+00:00", "elapsed_td": 0.4},
            {"line": "st_target", "price": 110.0, "at": "2026-10-02 15:00:00+00:00", "elapsed_td": 0.5}]}
        # 甲(Codex 的复现): 10-01 收盘后发布, 10-02 是第一个完整交易日
        dg = pq.compute_window_digest("INH", "2026-10-01T23:00:00Z", 20, report, prior_timeline=prior)
        self.assertEqual(dg["touch_timeline"]["events"][0]["line"], "invalid")
        self.assertEqual(dg["short_term"]["outcome_at_expiry"], "invalid_first")   # 旧版: order_unknown / settled_by=daily
        self.assertEqual(dg["short_term"]["settled_by"], "5m+daily")
        self.assertTrue(dg["short_term"]["first_touch"].get("inherited_3d"))
        # 没有继承时日线同根 bar 两线都碰 → 仍是 order_unknown(不硬猜)
        dg0 = pq.compute_window_digest("INH", "2026-10-01T23:00:00Z", 20, report)
        self.assertEqual(dg0["short_term"]["outcome_at_expiry"], "order_unknown")
        # 乙: 10-02 盘中发布, 20d 窗口里没有当天那根部分 bar(5m 文件早已过期) → 继承的当天事件记在第一根之前, 照样裁定
        dgb = pq.compute_window_digest("INH", "2026-10-02T13:45:00Z", 20, report, prior_timeline=prior)
        self.assertEqual(dgb["win_start"], "2026-10-05")
        self.assertEqual(dgb["short_term"]["outcome_at_expiry"], "invalid_first")
        self.assertEqual(dgb["short_term"]["first_touch"]["line"], "invalid")
        dgb0 = pq.compute_window_digest("INH", "2026-10-02T13:45:00Z", 20, report)
        self.assertEqual(dgb0["short_term"]["outcome_at_expiry"], "untouched")     # 当天路径没数据就不计, 也不冒充

    def test_lens_symbols_with_digits_parse(self):
        self.assertEqual(pq.parse_target_map("SPY=95-105; N225=39000-41000; KODEX200=100000-110000"),
                         {"SPY": (95.0, 105.0), "N225": (39000.0, 41000.0), "KODEX200": (100000.0, 110000.0)})
        self.assertEqual(pq.parse_target_map("N225=39500.00"), {"N225": 39500.0})


class R24Test(unittest.TestCase):
    """R24(2026-10-17, D+20 口径三件定案): 千分位与止损解析 / 限价单有效期与模拟退出 / 事件分段 / 判断线接受状态 / 方向无关路径字段 /
    触线方向按角色 / 老报告起点价退到技术快照。"""

    def test_first_float_strips_thousands_and_stop_skips_entry_price(self):
        self.assertEqual(pq._first_float("¥2,785"), 2785.0)                       # 旧版 2.0
        self.assertEqual(pq._first_float("SPY=745.0; QQQ=660"), 745.0)
        rep = {"risk_management": {"stop_loss": "若44.50守稳低吸成交, 常规时段收盘低于39.25即停止加仓"}}
        self.assertEqual(pq._stop_price(rep, entry=44.5), 39.25)                   # 旧版把 44.50 当止损
        self.assertEqual(pq._stop_price(rep, entry=None), 44.5)

    def test_order_valid_days_from_report_else_default(self):
        self.assertEqual(pq._order_valid_days({"mentality_basis": "回落至2945后收盘收回2998才执行; 最多等5个交易日, 等不到即撤单"}), (5, "report"))
        self.assertEqual(pq._order_valid_days({"short_term_path": {"horizon_days": 7}}), (pq.ORDER_VALID_DAYS_DEFAULT, "default"))  # 不看 horizon

    def test_order_valid_days_english_phrases(self):
        """English reports (from 2026-10-27): the same four shapes as the Chinese branches; Chinese alternatives stay."""
        self.assertEqual(pq._order_valid_days({"mentality_basis": "pull back to 2945 first; wait at most 5 trading days, then cancel"}), (5, "report"))
        self.assertEqual(pq._order_valid_days({"next_review": {"notes": "limit order valid for 3 trading days"}}), (3, "report"))
        self.assertEqual(pq._order_valid_days({"statement": "the order stands for the next 5 sessions"}), (5, "report"))
        self.assertEqual(pq._order_valid_days({"mentality_basis": "valid for 4 trading days, cancel if unfilled"}), (4, "report"))
        self.assertEqual(pq._order_valid_days({"mentality_basis": "5 trading days without a fill -> cancel"}), (5, "report"))
        self.assertEqual(pq._order_valid_days({"mentality_basis": "if not filled within 4 trading days, pull the order"}), (4, "report"))
        self.assertEqual(pq._order_valid_days({"risk_management": {"review_triggers": ["cancel if unfilled within 5 days"]}}), (5, "report"))

    def test_entry_expiry_and_simulated_exit(self):
        bars = [{"date": f"2026-10-0{i}", "high": 101, "low": 100 - (3 if i == 4 else 0), "close": 100} for i in range(1, 6)]
        # 挂 97: 第 4 根才触到; 有效期 3 → 过期触价不算成交
        fs = pq.entry_fill_stats(bars, 100.0, 97.0, valid_days=3)
        self.assertFalse(fs["entry_price_reached"]); self.assertEqual(fs["expired_touch_date"], "2026-10-04")
        fs = pq.entry_fill_stats(bars, 100.0, 97.0, valid_days=5, stop=90.0)
        self.assertTrue(fs["entry_price_reached"]); self.assertEqual(fs["exit_reason"], "window_end")
        bars2 = bars + [{"date": "2026-10-06", "high": 95, "low": 88, "close": 89}]
        fs = pq.entry_fill_stats(bars2, 100.0, 97.0, valid_days=5, stop=90.0)
        self.assertEqual(fs["exit_reason"], "stop"); self.assertAlmostEqual(fs["simulated_exit_ret_pct"], (90 / 97 - 1) * 100, places=2)

    def test_line_state_accepted_pending_touched(self):
        mk = lambda closes, highs=None: [{"date": f"2026-10-{i+1:02d}", "high": (highs or closes)[i], "low": closes[i] - 1, "close": closes[i]} for i in range(len(closes))]
        # 上方失效线 110: 连续两收盘越线且越幅 >= 0.5*ATR(ATR=2 → 1)
        self.assertEqual(pq.line_state(mk([100, 112, 113, 105]), 110.0, "above", atr=2.0)["state"], "accepted_then_reclaimed")  # 期末已收回
        self.assertEqual(pq.line_state(mk([100, 112, 113, 114]), 110.0, "above", atr=2.0)["state"], "accepted")
        self.assertEqual(pq.line_state(mk([100, 110.2, 110.3, 110.4]), 110.0, "above", atr=2.0)["state"], "pending")  # 越幅不够: 窗末在线外记 pending
        self.assertEqual(pq.line_state(mk([100, 100, 100, 112]), 110.0, "above", atr=2.0)["state"], "pending")     # 窗末刚越线
        self.assertEqual(pq.line_state(mk([100, 100, 100, 100], highs=[100, 111, 100, 100]), 110.0, "above", atr=2.0)["state"], "touched_only")
        self.assertEqual(pq.line_state(mk([100, 100, 100, 100]), 110.0, "above", atr=2.0)["state"], "untouched")
        self.assertEqual(pq.line_state(mk([100, 110.2, 110.3, 100]), 110.0, "above", atr=2.0)["state"], "touched_only")  # 越幅不够且已回来
        st = pq.line_state(mk([100, 110.2, 110.3, 125]), 110.0, "above", atr=2.0)
        self.assertEqual((st["state"], st["accepted_at"]), ("accepted", "2026-10-04"))                   # 确认日 = 幅度也够的那根, 不倒填到 10-03

    def test_segments_single_out_event_bar(self):
        bars = [{"date": f"2026-10-{d:02d}", "high": c + 1, "low": c - 1, "close": c} for d, c in ((1, 101), (2, 102), (5, 98), (6, 97), (7, 96))]
        seg = pq.segments(bars, 100.0, "2026-10-04", sigma=0.01, lines={"invalid": (99.0, "below")})   # 10-04 非交易日 → 取 10-05
        self.assertEqual(seg["event_bar"]["date"], "2026-10-05"); self.assertTrue(seg["event_bar"]["is_next_session"])
        self.assertEqual(seg["event_bar"]["close_beyond"], ["invalid"])
        self.assertEqual(seg["pre"]["n"], 2); self.assertAlmostEqual(seg["pre"]["ret_pct"], 2.0)
        self.assertEqual(seg["post"]["n"], 2); self.assertAlmostEqual(seg["post"]["anchor"], 98.0)
        self.assertIsNone(pq.segments(bars, 100.0, "2026-11-01", 0.01))                              # 事件不在窗口内

    def test_eps_and_direction_free_path_fields(self):
        self.assertEqual(pq.eps_pct(None, 20), pq.EPS_MIN_PCT)
        self.assertAlmostEqual(pq.eps_pct(0.02, 16), max(pq.EPS_MIN_PCT, 0.5 * 0.02 * 4 * 100), places=2)
        bars = [{"date": "2026-10-01", "high": 105, "low": 99, "close": 104}, {"date": "2026-10-02", "high": 110, "low": 103, "close": 108},
                {"date": "2026-10-03", "high": 109, "low": 96, "close": 98}]
        st = pq.window_stats(bars, 100.0, -1, None, None)
        self.assertEqual(st["up_exc_pct"], 10.0); self.assertEqual(st["down_exc_pct"], -4.0)    # 不随方向翻转(mfe 这里是 4.0)
        self.assertAlmostEqual(st["time_above_T_pct"], 66.7, places=1)
        self.assertAlmostEqual(st["efficiency_ratio"], 2 / (4 + 4 + 10), places=3)

    def test_touch_direction_follows_line_role(self):
        # HYMTF 型: 起点价 382500 回退到了失效线 381500 之上; 失效线是上方线, 窗口最高 374500 从未触及
        bars = [{"date": f"2026-10-0{i}", "high": 374500, "low": 360000, "close": 365000} for i in range(1, 4)]
        tl = pq.touch_timeline(bars, 382500.0, {"invalid": (381500.0, "above")})
        self.assertEqual(tl["untouched"], ["invalid"])
        tl_old = pq.touch_timeline(bars, 382500.0, {"invalid": 381500.0})                       # 没带角色时旧规则会误判
        self.assertEqual([e["line"] for e in tl_old["events"]], ["invalid"])

    def test_stop_price_prefers_price_like_numbers(self):
        rep = {"risk_management": {"stop_loss": "2 日未收回 95 则减仓"}}
        self.assertEqual(pq._stop_price(rep, entry=None, ref=100.0), 95.0)                 # 不读成 2
        rep = {"risk_management": {"stop_loss": "成交价 97 即为止损位(等于挂单价)"}}
        self.assertEqual(pq._stop_price(rep, entry=97.0, ref=100.0), 97.0)                 # 没有别的候选时接受等于挂单价
        self.assertIsNone(pq._stop_price({"risk_management": {"stop_loss": "3 根 K 线内"}}, entry=None, ref=100.0))

    def test_order_valid_days_ignores_non_order_phrases(self):
        self.assertEqual(pq._order_valid_days({"statement": "未来 5 个交易日内观察方向"})[1], "default")
        self.assertEqual(pq._order_valid_days({"risk_management": {"review_triggers": ["两日未收回 95 则减仓"]}})[1], "default")
        self.assertEqual(pq._order_valid_days({"next_review": {"notes": "挂单有效期为 4 个交易日"}}), (4, "report"))
        self.assertEqual(pq._order_valid_days({"mentality_basis": "5 个交易日内未触价即撤单"}), (5, "report"))
        # English: a path validity window or an observation span is not an order validity
        self.assertEqual(pq._order_valid_days({"statement": "the view is valid for 5 trading days"})[1], "default")
        self.assertEqual(pq._order_valid_days({"statement": "watch direction over the next 5 trading days"})[1], "default")
        self.assertEqual(pq._order_valid_days({"risk_management": {"review_triggers": ["trim if 95 is not reclaimed within 2 days"]}})[1], "default")

    def test_segments_before_window_and_candidates_after_acceptance(self):
        bars = [{"date": f"2026-10-{d:02d}", "high": c + 1, "low": c - 1, "close": c} for d, c in ((1, 101), (2, 96), (5, 94), (6, 92), (7, 90))]
        self.assertIsNone(pq.segments(bars, 100.0, "2026-09-28", 0.01))                              # 事件早于窗口: 不造假事件日
        states = {"invalid": pq.line_state(bars, 97.0, "below", atr=2.0)}
        self.assertEqual(states["invalid"]["state"], "accepted"); self.assertEqual(states["invalid"]["accepted_at"], "2026-10-05")
        ref = pq.reference_candidates(bars, 100.0, {"ret_pct": -10.0}, {}, states, 3, None)
        ops = [c["op"] for c in ref["candidates"]]
        self.assertNotIn("MARKET_ENTRY@T", ops); self.assertNotIn("TRIM@T", ops)                        # 不用 T 时事后视角
        trim = next(c for c in ref["candidates"] if c["op"].startswith("TRIM@accepted"))
        self.assertAlmostEqual(trim["realized_ret_pct"], -(90 / 94 - 1) * 100, places=2)             # 从确认那根收盘起算
        self.assertTrue(ref["single_side_available"])

    def test_daily_settlement_respects_line_roles_and_same_price_lines(self):
        import pandas as pd
        tmp = tempfile.TemporaryDirectory(); old_root = pq.MARKET_DATA_ROOT; pq.MARKET_DATA_ROOT = Path(tmp.name)
        try:
            d = Path(tmp.name) / "ROL"; d.mkdir()
            days = ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
            rows = [{"date": f"{day} 00:00:00+00:00", "open": 100, "high": 104, "low": 99, "close": 100, "volume": 10} for day in days]
            rows.append({"date": "2026-10-05 00:00:00+00:00", "open": 100, "high": 104, "low": 99, "close": 100, "volume": 10})  # 重复日期
            pd.DataFrame(rows).to_csv(d / "ROL_5y_1d_20261010T000000Z.csv", index=False)
            # 下行路径: 失效线 103 在上方(窗口最高 104 碰到了), 目标 95 在下方(没碰); 操作锚 target 也是 95(同价)
            report = {"action": 45, "operation_type": "HOLD_NEUTRAL", "operation_target": "95",
                      "short_term_path": {"direction": "下行", "target": "95", "invalid": "103", "horizon_days": 3}}
            dg = pq.compute_window_digest("ROL", "2026-10-01T23:00:00Z", 20, report)
            self.assertEqual(dg["short_term"]["outcome_at_expiry"], "invalid_first")                   # 日线清算按角色: 上方失效线被触及
            self.assertIn("st_target", dg["line_state"]); self.assertEqual(dg["line_state"]["st_target"]["state"], "untouched")  # 同价去重不丢状态
            self.assertEqual(dg["line_state"]["st_target"]["eval_bars"], 3 + pq.grace_days(3))         # 只在有效期含宽限内评
            self.assertEqual(len(dg["daily_bars"]), 5)                                                 # 10-02..10-08 五根, 重复日期去重
        finally:
            pq.MARKET_DATA_ROOT = old_root; tmp.cleanup()

    def test_stop_rule_and_dated_order_expiry(self):
        self.assertEqual(pq._stop_price({"risk_management": {"stop_loss": "2 日未收回 2.8 则减仓"}}, entry=None, ref=3.0), 2.8)   # 带单位的 2 不是价
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "收盘跌破 95 且次日未收回"}}), ("close", 2))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "95; 2 日未收回即退出"}}), ("close", 2))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "触及 95 即退出"}}), ("touch", 1))
        from datetime import datetime, timezone
        self.assertEqual(pq._order_valid_until({"mentality_basis": "挂单有效至 2026-09-21, 未成交撤单"}, datetime(2026, 9, 14, tzinfo=timezone.utc)), "2026-09-21")
        self.assertEqual(pq._order_valid_until({"next_review": {"notes": "最晚 9月21日 撤单"}}, datetime(2026, 9, 14, tzinfo=timezone.utc)), "2026-09-21")
        self.assertIsNone(pq._order_valid_until({"statement": "9月21日 五个交易日窗口到期, 短线判断作废"}, None))

    def test_stop_rule_and_dated_order_expiry_english(self):
        """English reports (from 2026-10-27): close confirmation / N days without reclaim / touch; ISO order expiry dates."""
        from datetime import datetime, timezone
        T = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "a close below 95 confirms the stop"}}), ("close", 1))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "close confirmation at 95"}}), ("close", 1))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "2 consecutive closes below 95"}}), ("close", 2))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "95; 2 days without reclaim -> exit"}}), ("close", 2))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "exit if 95 is not reclaimed within 3 trading days"}}), ("close", 3))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "touch of 95 exits"}}), ("touch", 1))
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "stop close to 95"}}), ("touch", 1))              # "close to" = near, not a close rule
        self.assertEqual(pq._stop_rule({"risk_management": {"stop_loss": "time stop: exit after 5 days"}}), ("touch", 1))  # time stop is not modelled (same as 时间止损)
        self.assertEqual(pq._stop_price({"risk_management": {"stop_loss": "2 days without reclaim of 95 -> exit"}}, entry=None, ref=100.0), 95.0)   # the 2 carries a unit
        self.assertEqual(pq._stop_price({"risk_management": {"stop_loss": "stop 2.8 if 2 days without reclaim"}}, entry=None, ref=3.0), 2.8)
        self.assertEqual(pq._order_valid_until({"mentality_basis": "order valid through 2026-10-30, cancel if unfilled"}, T), "2026-10-30")
        self.assertEqual(pq._order_valid_until({"next_review": {"notes": "limit order expires 2026-10-30 if unfilled"}}, T), "2026-10-30")
        self.assertEqual(pq._order_valid_until({"statement": "valid until 2026-10-30"}, T), "2026-10-30")
        self.assertEqual(pq._order_valid_until({"statement": "good till 2026-10-30"}, T), "2026-10-30")
        self.assertEqual(pq._order_valid_until({"statement": "cancel no later than 2026-10-30"}, T), "2026-10-30")
        self.assertIsNone(pq._order_valid_until({"statement": "the 5-day window ends 2026-10-30, the short-term call is void"}, None))
        bars = [{"date": f"2026-10-0{i}", "high": 101, "low": 100 - (3 if i >= 2 else 0), "close": 100 - (3 if i >= 3 else 0)} for i in range(1, 6)]
        fs = pq.entry_fill_stats(bars, 100.0, 97.0, valid_until="2026-10-01", stop=98.0, long_side=True)
        self.assertFalse(fs["entry_price_reached"]); self.assertEqual(fs["expired_touch_date"], "2026-10-02")                 # 日期到期
        fs = pq.entry_fill_stats(bars, 100.0, 97.0, valid_days=5, stop=98.0, long_side=True, stop_rule=("close", 2))
        self.assertEqual((fs["exit_reason"], fs["exit_date"], fs["exit_rule"]), ("stop_close", "2026-10-04", "close x2"))    # 连续两收盘低于 98

    def test_old_report_anchor_falls_back_to_technical_snapshot(self):
        import pandas as pd
        tmp = tempfile.TemporaryDirectory()
        old_root, old_proj = pq.MARKET_DATA_ROOT, pq.PROJECT_ROOT
        pq.MARKET_DATA_ROOT, pq.PROJECT_ROOT = Path(tmp.name), Path(tmp.name)
        try:
            d = Path(tmp.name) / "SNP"; d.mkdir()
            days = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
            pd.DataFrame([{"date": f"{day} 00:00:00+00:00", "open": 6500, "high": 6600, "low": 6400, "close": 6540, "volume": 10}
                          for day in days]).to_csv(d / "SNP_5y_1d_20261010T000000Z.csv", index=False)     # 没有 5m 文件(已过期)
            snapdir = Path(tmp.name) / "data" / "technical" / "SNP"; snapdir.mkdir(parents=True)
            (snapdir / "SNP_technical_20260914T050000Z.json").write_text(json.dumps({"SNP_current_snapshot_last_price": 5823.0}), encoding="utf-8")
            report = {"action": 48, "operation_type": "HOLD_NEUTRAL", "operation_target": "5600",
                      "data_files": {"symbol": {"technical": "data/technical/SNP/SNP_technical_20260914T050000Z.json"}},
                      "short_term_path": {"direction": "下行", "target": "5530", "invalid": "6230", "horizon_days": 5},
                      "next_review": {"recommend_after": "2026-09-16", "notes": "BOJ 后重评"}}
            dg = pq.compute_window_digest("SNP", "2026-09-14T05:00:00Z", 3, report)
            self.assertEqual(dg["anchor_source"], "technical_snapshot"); self.assertAlmostEqual(dg["price_T"], 5823.0)
            self.assertEqual(dg["order_valid_source"], "default"); self.assertEqual(dg["order_valid_days"], pq.ORDER_VALID_DAYS_DEFAULT)
            self.assertEqual(dg["segments"]["event_bar"]["date"], "2026-09-16")
            self.assertEqual(dg["line_state"]["invalid"]["side"], "above")                           # 下行路径的失效线在上方
            self.assertEqual(dg["line_state"]["invalid"]["state"], "accepted")                       # 收盘 6540 连续在 6230 之上
            self.assertIn("daily_bars", dg); self.assertIn("reference", dg)
        finally:
            pq.MARKET_DATA_ROOT, pq.PROJECT_ROOT = old_root, old_proj
            tmp.cleanup()


class TiersTest(unittest.TestCase):
    def test_50d_retired(self):
        from datalayer.pg import postcheck_store as ps
        self.assertEqual(ps.POSTCHECK_TIERS, {"3d": 3, "20d": 20})
        self.assertIn("ruler", ps._UPSERT_SQL)


if __name__ == "__main__":
    unittest.main()


class EventDatesTest(unittest.TestCase):
    """2026-11-06: segmentation reads the report's events (new format), a projection of known release dates (old
    reports), and only failing both the legacy recommend_after."""

    def test_sources(self):
        new = {"next_review": {"recommend_after": "2026-11-10", "expires_at": "2026-11-25",
                               "events": [{"id": "E1", "date": "2026-11-17"}, {"id": "E2", "date": "2026-11-13"}, {"id": "E3", "date_precision": "tbd"}]}}
        self.assertEqual(pq._event_dates(new), (["2026-11-13", "2026-11-17"], "events"))
        self.assertEqual(pq._event_dates(new, ["2026-11-19"]), (["2026-11-13", "2026-11-17"], "events"))     # new format never projected
        old = {"next_review": {"recommend_after": "2026-09-16", "notes": "BOJ"}}
        self.assertEqual(pq._event_dates(old, ["2026-09-18"]), (["2026-09-16", "2026-09-18"], "projected+legacy"))
        self.assertEqual(pq._event_dates(old), (["2026-09-16"], "recommend_after_legacy"))
        self.assertEqual(pq._event_dates({"next_review": {"events": []}}), ([], "events"))

    def test_segments_take_the_first_event_inside_the_bars_window(self):
        bars = [{"date": f"2026-11-{d:02d}", "open": 100, "high": 101, "low": 99, "close": 100} for d in (5, 6, 9)]
        self.assertEqual(pq.segments(bars, 100.0, ["2026-11-04", "2026-11-06"], 0.01)["event_date"], "2026-11-06")   # 11-04 is before the window
        self.assertIsNone(pq.segments(bars, 100.0, ["2026-11-04", "2026-11-11"], 0.01))
        self.assertEqual(pq.segments(bars, 100.0, ["2026-11-09", "2026-11-06"], 0.01)["event_date"], "2026-11-06")
