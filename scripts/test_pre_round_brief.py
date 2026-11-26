"""跑前简报工具的纯函数部分(不连 PG): _parse_ts 的几种时刻写法; 三种到期 due_reasons(2026-10-24 council 定), 合成日线, 不连盈透。"""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prb", ROOT / "skills" / "equipoise-dispatch" / "scripts" / "pre_round_brief.py")
prb = importlib.util.module_from_spec(spec); spec.loader.exec_module(prb)

AS_OF = datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)


def df(rows):
    """rows: (日, open, high, low, close); 10-19 当天那根也给, 应被当基准日丢掉。"""
    idx = pd.to_datetime([f"2026-10-{d:02d}" for d, *_ in rows], utc=True)
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c} for _, o, h, l, c in rows], index=idx)


def report(**over):
    r = {"symbol": "AAA", "operation_type": "ENTRY", "operation_execution": {"derived": "LIMIT_ENTRY"},
         "operation_target": "100", "reference_prices": [{"symbol": "AAA", "price": 105.0}],
         "next_review": {"recommend_after": "2026-10-28",
                         "structured_triggers": [{"type": "price_below", "symbol": "AAA", "value": 95},
                                                 {"type": "sector_drop", "sector": "SMH", "pct": 3}]},
         "statement": "x"}
    r.update(over)
    return r


class ParseTsTest(unittest.TestCase):
    def test_every_accepted_shape_is_timezone_aware(self):
        """报告里的建议复查时刻有纯日期、带 Z、紧凑三种写法; 都要能与当前 UTC 时刻相减。"""
        now = datetime.now(timezone.utc)
        for v in ("2026-10-05", "2026-10-05T13:30:00", "2026-10-05T13:30:00Z", "20261005T133000Z"):
            ts = prb._parse_ts(v)
            self.assertIsNotNone(ts, v)
            self.assertIsNotNone(ts.tzinfo, v)
            (now - ts).total_seconds()

    def test_garbage_is_none(self):
        self.assertIsNone(prb._parse_ts("下周再看"))
        self.assertIsNone(prb._parse_ts(None))


class DueTest(unittest.TestCase):
    def test_order_reached_is_status_not_due(self):
        d = df([(19, 105, 106, 104, 105), (20, 104, 105, 99.5, 101), (21, 101, 103, 100.5, 102)])
        out = prb.due_reasons(report(), AS_OF, d, datetime(2026, 10, 22, 6, tzinfo=timezone.utc))
        self.assertIn("simulated fill @10-20", out["order"]); self.assertEqual(out["due"], [], out)
        self.assertEqual(out["bars_through"], "2026-10-21"); self.assertTrue(any("sector line" in m for m in out["manual"]))

    def test_order_expired_after_three_full_days(self):
        d = df([(19, 105, 106, 104, 105), (20, 104, 106, 102, 103), (21, 103, 107, 102.5, 106), (22, 106, 108, 105, 107)])
        out = prb.due_reasons(report(), AS_OF, d, datetime(2026, 10, 23, 6, tzinfo=timezone.utc))
        self.assertIn("expired unfilled", out["order"]); self.assertEqual(out["due"], ["order expired unfilled"])

    def test_order_pending_with_days_left(self):
        d = df([(20, 104, 106, 102, 103), (21, 103, 107, 102.5, 106)])
        out = prb.due_reasons(report(), AS_OF, d, datetime(2026, 10, 22, 6, tzinfo=timezone.utc))
        self.assertIn("open (1 days left)", out["order"]); self.assertEqual(out["due"], [])

    def test_valid_until_in_report_wins(self):
        d = df([(20, 104, 106, 102, 103), (21, 103, 107, 102.5, 106), (22, 106, 108, 105, 107), (23, 106, 108, 105, 107)])
        r = report(statement="限价单有效至 2026-10-30")
        out = prb.due_reasons(r, AS_OF, d, datetime(2026, 10, 24, 6, tzinfo=timezone.utc))
        self.assertIn("until 2026-10-30", out["order"]); self.assertEqual(out["due"], [])

    def test_trigger_touch_and_close_run_and_recommend_after(self):
        d = df([(20, 104, 105, 94.8, 96), (21, 96, 97, 93, 94), (22, 94, 95, 92, 93)])
        r = report(next_review={"recommend_after": "2026-10-21",
                                "structured_triggers": [{"type": "price_below", "value": 95}]},
                   lines=[{"type": "close_below", "value": 95, "n_closes": 2}, {"type": "date", "date": "2026-10-22"}])
        out = prb.due_reasons(r, AS_OF, d, datetime(2026, 10, 23, 6, tzinfo=timezone.utc))
        self.assertTrue(any(x.startswith("review time passed 2d") for x in out["due"]), out)
        self.assertIn("line hit: broke below 95@10-20", out["due"]); self.assertIn("line hit: 2 consecutive closes below 95@10-22", out["due"]); self.assertIn("line hit: date reached 10-22", out["due"])
        self.assertIn("simulated fill @10-20", out["order"]); self.assertNotIn("order expired unfilled", out["due"])   # 10-20 low 94.8 ≤ 100 → 成交, 不算到期

    def test_market_entry_or_hold_has_no_order(self):
        d = df([(20, 104, 106, 102, 103)])
        out = prb.due_reasons(report(operation_type="HOLD_NEUTRAL", operation_execution={}), AS_OF, d, datetime(2026, 10, 21, 6, tzinfo=timezone.utc))
        self.assertIsNone(out["order"])

    def test_no_bars(self):
        out = prb.due_reasons(report(), AS_OF, None, datetime(2026, 10, 21, 6, tzinfo=timezone.utc))
        self.assertIsNone(out["bars_through"]); self.assertEqual(out["n_bars"], 0); self.assertIn("no full-day bars", out["order"]); self.assertEqual(out["due"], [])

    def test_bars_not_past_as_of_day(self):
        d = df([(16, 100, 101, 99, 100), (19, 105, 106, 104, 105)])     # 行情到基准日当天为止
        out = prb.due_reasons(report(), AS_OF, d, datetime(2026, 10, 20, 6, tzinfo=timezone.utc))
        self.assertEqual(out["bars_through"], "2026-10-19"); self.assertEqual(out["n_bars"], 0); self.assertEqual(out["due"], [])

    def test_lens_line_uses_other_symbol_bars(self):
        own = df([(20, 104, 106, 102, 103)])
        spy = {"SPY": df([(20, 700, 705, 690, 695)])}
        r = report(symbol="GENERAL", operation_type="TRIM_POSITION", operation_execution={},
                   next_review={"recommend_after": "2026-10-28", "structured_triggers": [{"type": "price_below", "symbol": "SPY", "value": 692}]})
        out = prb.due_reasons(r, AS_OF, own, datetime(2026, 10, 21, 6, tzinfo=timezone.utc), df_lookup=lambda s: spy.get(s))
        self.assertEqual(out["due"], ["line hit: SPY broke below 692@10-20"])


class LensWiseTest(unittest.TestCase):
    """A4: a lens-wise report gives one line per lens, each lens's order state judged from its own level and bars."""

    def rep(self):
        by = {"SPY": {"operation_type": "HOLD_NEUTRAL", "operation_target": 500.0, "operation_size": "N/A"},
              "QQQ": {"operation_type": "LIMIT_ENTRY", "operation_target": 395.0, "operation_size": "STANDARD_SIZE"},
              "DIA": {"operation_type": "LIMIT_ENTRY", "operation_target": 290.0, "operation_size": "LIGHT_SIZE"}}
        return report(symbol="GENERAL", operation_type="HOLD_NEUTRAL", operation_execution={},
                      operation_target="SPY=500; QQQ=400; DIA=300", lenses=["SPY", "QQQ", "DIA"], by_lens=by,
                      reference_prices=[{"symbol": "SPY", "price": 500.0}, {"symbol": "QQQ", "price": 400.0},
                                        {"symbol": "DIA", "price": 300.0}],
                      next_review={"recommend_after": "2026-10-28"})

    def test_one_row_per_lens_with_its_own_order(self):
        bars = {"SPY": df([(20, 500, 502, 498, 501)]),
                "QQQ": df([(20, 400, 401, 394, 396)]),                       # low 394 ≤ 395 → QQQ fills
                "DIA": df([(20, 300, 301, 296, 297), (21, 297, 299, 295, 296), (22, 296, 298, 294, 295)])}
        out = prb.due_reasons(self.rep(), AS_OF, None, datetime(2026, 10, 23, 6, tzinfo=timezone.utc),
                              df_lookup=lambda s: bars.get(s))
        self.assertEqual(out["order"], "lens-wise, see per-lens lines")
        ln = {x["lens"]: x for x in out["lenses"]}
        self.assertEqual(list(ln), ["SPY", "QQQ", "DIA"])
        self.assertIsNone(ln["SPY"]["order"])
        self.assertEqual((ln["QQQ"]["op_type"], ln["QQQ"]["op_target"]), ("LIMIT_ENTRY", "395"))
        self.assertIn("limit 395 simulated fill @10-20", ln["QQQ"]["order"])
        self.assertIn("limit 290 expired unfilled", ln["DIA"]["order"])          # DIA never reached 290 in 3 sessions
        self.assertEqual(out["due"], ["order expired unfilled (DIA)"])

    def test_single_lens_report_has_no_lens_rows(self):
        out = prb.due_reasons(report(), AS_OF, None, datetime(2026, 10, 21, 6, tzinfo=timezone.utc))
        self.assertNotIn("lenses", out)


class ProjectionTest(unittest.TestCase):
    """2026-11-06 (owner): a tree report whose next_review predates the events format is read as if written in it —
    expires_at from the horizon, events[] projected from the known calendar and marked projected; reports that already
    carry events[] or have no tree are left alone."""

    def tree(self):
        return {"schema": "3.4", "T_days": 10, "P0": 100.0, "nodes": [
            {"id": "D", "p": 0.5, "P_T": 95.0, "events": [{"t": 10, "kind": "close", "price": 95.0}]},
            {"id": "U", "p": 0.5, "P_T": 105.0, "events": [{"t": 10, "kind": "close", "price": 105.0}]}]}

    def test_old_format_tree_report_is_projected(self):
        from datetime import date
        r = report(scenario=self.tree())
        macro = [{"region": "US", "indicator": "cpi_yoy", "release_time": date(2026, 10, 28)},
                 {"region": "US", "indicator": "nfp", "release_time": date(2026, 12, 4)}]          # the second is past the horizon
        proj = prb.project_next_review(r, AS_OF, last_release=date(2026, 7, 30), macro_rows=macro)
        self.assertEqual(proj["expires_at"], "2026-11-02")                                        # 10-19 + 10 weekdays
        names = [e["name"] for e in proj["events"]]
        self.assertEqual(names, ["earnings (expected by quarterly cadence)", "US cpi_yoy"])
        e = proj["events"][0]
        self.assertEqual((e["who"], e["date_precision"], e["coverage"], e["tests"], e["projected"]), ("issuer", "window", "none", [], True))
        self.assertEqual((e["date"], e["window_end"], e["expected_date"]), ("2026-10-22", "2026-11-05", "2026-10-29"))
        self.assertTrue(proj["events"][1]["source"].startswith("projected"))

    def test_events_format_and_treeless_reports_are_left_alone(self):
        r = report(scenario=self.tree(), next_review={"recommend_after": "2026-10-28", "events": []})
        self.assertIsNone(prb.project_next_review(r, AS_OF))
        self.assertIsNone(prb.project_next_review(report(), AS_OF))                                # no tree: legacy lines

    def test_lens_wise_report_projects_from_the_first_lens_tree(self):
        r = report(symbol="GENERAL", lenses=["SPY"], by_lens={"SPY": {"scenario": self.tree()}})
        proj = prb.project_next_review(r, AS_OF)
        self.assertEqual(proj["expires_at"], "2026-11-02"); self.assertEqual(proj["events"], [])


if __name__ == "__main__":
    unittest.main()


class DateTriggerOnReportDayTest(unittest.TestCase):
    """A date trigger dated on or before the report's own day was already known when the report was written: not a hit
    (11-04: a report written on the 4th carried date_reached 2026-11-04 and woke the duty Codex every ten minutes)."""

    def test_date_on_report_day_is_not_a_hit_but_a_later_date_is(self):
        d = df([(19, 100, 101, 99, 100), (20, 100, 102, 99, 101)])
        r = report(next_review={"recommend_after": "2026-10-28",
                                "structured_triggers": [{"type": "date_reached", "date": "2026-10-19"},
                                                        {"type": "date_reached", "date": "2026-10-20"}]})
        out = prb.due_reasons(r, AS_OF, d, datetime(2026, 10, 20, 6, tzinfo=timezone.utc))
        self.assertEqual([x for x in out["due"] if x.startswith("line hit: date")], ["line hit: date reached 10-20"])


class BaseDayBarTest(unittest.TestCase):
    """The duty loop's alerting counts the report's own day (the latest price lives in that partial bar); the brief and
    the postcheck keep excluding it."""

    def test_base_day_hit_only_when_included(self):
        d = df([(19, 100, 101, 94, 100)])      # the report day itself touches 94 < 95 after a 14:30Z report
        r = report()
        now = datetime(2026, 10, 19, 20, tzinfo=timezone.utc)
        self.assertEqual([x for x in prb.due_reasons(r, AS_OF, d, now)["due"] if "broke" in x], [])
        hits = [x for x in prb.due_reasons(r, AS_OF, d, now, include_base_day=True)["due"] if "broke" in x]
        self.assertEqual(hits, ["line hit: AAA broke below 95@10-19"])


class DivergenceTest(unittest.TestCase):
    """2026-11-06: a report with a v3.4 scenario tree is judged by divergence (scripts/divergence.py), not by its trigger
    lines; reports without a tree keep the legacy line-hit path."""

    def tree(self, P0=100.0):
        k = P0 / 100.0
        return {"schema": "3.4", "T_days": 15, "P0": P0, "nodes": [
            {"id": "D", "p": 0.3, "P_T": 94 * k, "events": [{"t": 1, "kind": "close", "price": 99 * k}, {"t": 15, "kind": "close", "price": 94 * k}]},
            {"id": "F", "p": 0.4, "P_T": 100 * k, "events": [{"t": 15, "kind": "close", "price": 100 * k}]},
            {"id": "U", "p": 0.3, "P_T": 106 * k, "events": [{"t": 1, "kind": "close", "price": 101 * k}, {"t": 15, "kind": "close", "price": 106 * k}]}]}

    def test_tree_report_ignores_trigger_lines_and_reports_divergence(self):
        r = report(operation_type="HOLD_NEUTRAL", operation_execution={}, scenario=self.tree(), atr_pct_14=2.0)
        now = datetime(2026, 10, 21, 6, tzinfo=timezone.utc)
        inside = prb.due_reasons(r, AS_OF, df([(20, 100, 101, 99, 100.2)]), now)          # the trigger line 95 is untouched, price at centre
        self.assertEqual(inside["due"], []); self.assertEqual(inside["divergence"]["level"], "inside")
        dead = prb.due_reasons(r, AS_OF, df([(20, 100, 101, 91, 92.0)]), now)             # -4 ATR on day 1
        self.assertEqual(len(dead["due"]), 1); self.assertTrue(dead["due"][0].startswith("tree dead: -4.0 ATR in 1d"), dead["due"])
        self.assertEqual(dead["divergence"]["dominant"], "D"); self.assertNotIn("line hit", " ".join(dead["due"]))
        self.assertTrue(any("sector line" in m for m in dead["manual"]))                   # sector lines stay informational
        drift = prb.due_reasons(r, AS_OF, df([(20, 100, 101, 96, 97.0)]), now, div_cfg={"dead_tail": 0.2, "drift_tail": 0.3})
        self.assertTrue(drift["due"][0].startswith("tree dead"), drift["due"])            # thresholds come from the caller

    def test_legacy_report_keeps_line_hits(self):
        out = prb.due_reasons(report(), AS_OF, df([(20, 104, 105, 94.8, 96)]), datetime(2026, 10, 21, 6, tzinfo=timezone.utc))
        self.assertIn("line hit: AAA broke below 95@10-20", out["due"]); self.assertIsNone(out["divergence"])

    def test_lens_wise_tree_uses_each_lens_bars_and_atr(self):
        by = {"SPY": {"operation_type": "HOLD_NEUTRAL", "scenario": self.tree(700.0)},
              "QQQ": {"operation_type": "HOLD_NEUTRAL", "scenario": self.tree(600.0)}}
        r = report(symbol="GENERAL", operation_type="HOLD_NEUTRAL", operation_execution={}, lenses=["SPY", "QQQ"], by_lens=by,
                   reference_prices=[{"symbol": "SPY", "price": 700.0}, {"symbol": "QQQ", "price": 600.0}],
                   next_review={"recommend_after": "2026-10-28", "structured_triggers": [{"type": "price_above", "symbol": "SPY", "value": 701}]})
        bars = {"SPY": df([(d, 700, 707, 693, 700) for d in range(1, 19)] + [(20, 700, 702, 699, 700.5)]),
                "QQQ": df([(d, 600, 606, 594, 600) for d in range(1, 19)] + [(20, 600, 601, 560, 562.0)])}
        out = prb.due_reasons(r, AS_OF, None, datetime(2026, 10, 21, 6, tzinfo=timezone.utc), df_lookup=lambda s: bars.get(s))
        self.assertEqual(out["divergence"]["lens"], "QQQ"); self.assertEqual(out["divergence"]["by_lens"]["SPY"]["level"], "inside")
        self.assertTrue(out["due"][0].startswith("tree dead: QQQ "), out["due"])          # SPY crossed its old trigger line: not a reason any more


class TreeExpiredByDateTest(unittest.TestCase):
    """GPT review 11-06: the horizon end is a date — a report past expires_at is due even when no bars came this breath."""

    def test_expired_without_bars_and_with_bars(self):
        sc = {"schema": "3.4", "T_days": 15, "P0": 100.0, "nodes": [{"id": "M1", "p": 1.0, "P_T": 105.0, "events": [{"t": 15, "kind": "close", "price": 105.0}]}]}
        r = report(operation_type="HOLD_NEUTRAL", operation_execution={}, scenario=sc, atr_pct_14=2.0,
                   next_review={"recommend_after": "2026-11-09", "expires_at": "2026-11-09", "events": []})
        late = datetime(2026, 11, 10, 6, tzinfo=timezone.utc)
        out = prb.due_reasons(r, AS_OF, None, late)
        self.assertTrue(any(x.startswith("tree expired: horizon 15d passed (expires_at 2026-11-09); divergence unavailable") for x in out["due"]), out["due"])
        self.assertEqual(out["divergence"]["level"], "expired")
        out = prb.due_reasons(r, AS_OF, df([(20, 100, 101, 99, 100.2)]), late)          # bars exist but stop early: still expired by date
        self.assertTrue(any(x.startswith("tree expired") for x in out["due"]), out["due"])
        early = datetime(2026, 11, 8, 6, tzinfo=timezone.utc)
        self.assertEqual([x for x in prb.due_reasons(r, AS_OF, None, early)["due"] if "expired" in x], [])
