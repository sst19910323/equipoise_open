#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/next_review_enrich.py (pure parts; no PG)."""
import importlib.util
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nre", ROOT / "scripts" / "next_review_enrich.py")
nre = importlib.util.module_from_spec(spec); spec.loader.exec_module(nre)


class ExpiresTest(unittest.TestCase):
    def test_weekdays_only(self):
        self.assertEqual(nre.add_weekdays(date(2026, 11, 4), 15), date(2026, 11, 25))     # Wed + 15 weekdays
        self.assertEqual(nre.add_weekdays(date(2026, 11, 6), 1), date(2026, 11, 9))       # Fri + 1 → Mon
        self.assertEqual(nre.expires_at("20261104T182700Z", 15), "2026-11-25")
        self.assertEqual(nre.expires_at("2026-11-04T18:27:00Z", None), "2026-11-25")      # default 15
        self.assertIsNone(nre.expires_at("garbage", 15))


class KnownEventsTest(unittest.TestCase):
    def test_earnings_expected_from_cadence(self):
        e = nre.earnings_expected(date(2026, 8, 19), date(2026, 11, 4), date(2026, 11, 25))
        self.assertEqual(e["date"], "2026-11-18"); self.assertEqual(e["window"], ["2026-11-11", "2026-11-25"])
        self.assertIsNone(nre.earnings_expected(date(2026, 6, 1), date(2026, 11, 4), date(2026, 11, 25)))   # expected 08-31, long past
        self.assertIsNone(nre.earnings_expected(None, date(2026, 11, 4), date(2026, 11, 25)))

    def test_macro_rows_inside_window_dedup_sorted(self):
        rows = [{"region": "US", "indicator": "cpi_yoy", "release_time": date(2026, 11, 13)},
                {"region": "US", "indicator": "core_cpi_yoy", "release_time": "2026-11-13"},
                {"region": "US", "indicator": "cpi_yoy", "release_time": date(2026, 11, 13)},
                {"region": "US", "indicator": "payrolls", "release_time": date(2026, 12, 4)},     # after the window
                {"region": "US", "indicator": "ism", "release_time": date(2026, 11, 4)}]          # on as_of: not future
        out = nre.macro_events(rows, date(2026, 11, 4), date(2026, 11, 25))
        self.assertEqual([(e["name"], e["date"]) for e in out], [("core_cpi_yoy", "2026-11-13"), ("cpi_yoy", "2026-11-13")])

    def test_macro_rows_from_the_refresh_schedule(self):
        sched = {"economic": {"US": {"cpi_yoy": {"last": "2026-10-14", "next": {"time": {"at": "2026-11-13T13:30:00Z", "reason": "US Oct CPI"}}},
                                     "ppi_yoy": {"last": "2026-10-15", "next": {"time": {"at": "2026-12-11T13:30:00Z"}}},
                                     "broken": {"next": {}}},
                              "EZ": {"hicp": {"next": {"time": {"at": "2026-11-18T10:00:00Z"}}}}}}
        rows = nre.macro_rows_from_schedule(sched, "US")
        self.assertEqual([(r["indicator"], r["release_time"].isoformat()) for r in rows], [("cpi_yoy", "2026-11-13"), ("ppi_yoy", "2026-12-11")])
        out = nre.macro_events(rows, date(2026, 11, 4), date(2026, 11, 25))
        self.assertEqual([(e["name"], e["date"], e.get("basis")) for e in out], [("cpi_yoy", "2026-11-13", "US Oct CPI")])
        self.assertEqual([r["indicator"] for r in nre.macro_rows_from_schedule(sched, "EU")], ["hicp"])     # EU → EZ

    def test_build_keeps_arbiter_fields_and_adds_machine_ones(self):
        nr = {"recommend_after": "2026-11-17T12:00:00Z", "events": [{"id": "E1", "name": "Q3 earnings", "date": "2026-11-17"}], "notes": "n"}
        out = nre.build(nr, "20261104T182700Z", 15, date(2026, 8, 19), [], note=None)
        self.assertEqual(out["expires_at"], "2026-11-25")
        self.assertEqual(out["events"], nr["events"]); self.assertEqual(out["recommend_after"], nr["recommend_after"])
        self.assertEqual([e["kind"] for e in out["known_events"]], ["earnings_expected"])
        self.assertNotIn("known_events_note", out)
        out2 = nre.build(None, "garbage", 15, None, [], note="x")
        self.assertIsNone(out2["expires_at"]); self.assertEqual(out2["known_events"], []); self.assertEqual(out2["known_events_note"], "x")


if __name__ == "__main__":
    unittest.main()
