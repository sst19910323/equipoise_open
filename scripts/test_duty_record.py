#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the trusted duty bookkeeping (scripts/duty_record.py verify_decision) and the defer rule in
scripts/duty_status.py (apply_defers). Pure functions; no PG, no IBKR."""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


dr = _load("duty_record")
NOW = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)
STATUS = {"running": [{"unit": "u1", "harness": "codex1", "symbols": ["SPY"]}],
          "markets": {
    "us": {"market": {"state": "open", "in_window": True},
           "due": [{"symbol": "GENERAL", "reasons": ["review time passed 1d ago"]},
                   {"symbol": "NVDA", "reasons": ["line hit: price_close_above 214.5"]},
                   {"symbol": "MU", "reasons": ["review time passed 0d ago"]}]},
    "eu": {"market": {"state": "closed", "in_window": False},
           "due": [{"symbol": "DBK", "reasons": ["order expired unfilled"]}]}}}
UNIVERSE = {"us": {"GENERAL", "NVDA", "MU", "AAPL", "SPY"}, "eu": {"GENERAL_EU", "DBK"}}


class TestVerifyDecision(unittest.TestCase):
    def test_accepts_within_cap_and_updates_skips(self):
        dec = {"harness": "codex1", "launch": [{"market": "us", "symbols": ["GENERAL", "NVDA"]}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 3, NOW, 24, {"MU": 2, "GENERAL": 1}, UNIVERSE)
        self.assertIsNone(r["refused"])
        self.assertEqual(r["launch"], [{"market": "us", "symbols": ["GENERAL", "NVDA"]}])
        self.assertEqual(r["skips"], {"MU": 3, "DBK": 1})        # launched → cleared; due-not-launched → +1

    def test_market_members_allowed_running_and_foreign_dropped(self):
        dec = {"harness": "codex1", "launch": [{"market": "us", "symbols": ["AAPL", "SPY", "XYZ", "GENERAL"]}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 5, NOW, 24, {}, UNIVERSE)
        self.assertEqual(r["launch"], [{"market": "us", "symbols": ["AAPL", "GENERAL"]}])   # AAPL: not due, but in the market
        d = [x for x in r["dropped"] if x["what"] == "launch"][0]
        self.assertEqual(sorted(d["symbols"]), ["SPY", "XYZ"])
        self.assertIn("already running", d["why"])

    def test_market_outside_window_is_dropped_whole(self):
        dec = {"harness": "codex1", "launch": [{"market": "eu", "symbols": ["DBK"]}, {"market": "us", "symbols": ["MU"]}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 5, NOW, 24, {}, UNIVERSE)
        self.assertEqual(r["launch"], [{"market": "us", "symbols": ["MU"]}])
        self.assertTrue(any("outside its data window" in x["why"] for x in r["dropped"]))

    def test_over_cap(self):
        dec = {"harness": "codex1", "launch": [{"market": "us", "symbols": ["GENERAL", "AAPL", "NVDA", "MU"]}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 2, NOW, 24, {}, UNIVERSE)
        self.assertEqual(r["launch"], [{"market": "us", "symbols": ["GENERAL", "AAPL"]}])
        self.assertTrue(any("over launch_cap" in d["why"] for d in r["dropped"]))

    def test_wrong_identity_is_refused_and_skips_untouched(self):
        dec = {"harness": "codex2", "launch": [{"market": "us", "symbols": ["GENERAL"]}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 3, NOW, 24, {"MU": 2}, UNIVERSE)
        self.assertIn("codex2", r["refused"])
        self.assertEqual(r["launch"], [])
        self.assertEqual(r["skips"], {"MU": 2})

    def test_defer_rules(self):
        dec = {"harness": "codex1", "launch": [{"market": "us", "symbols": ["GENERAL"]}],
               "defer": [{"symbol": "MU", "until": "2026-11-04T14:30:00Z", "why": "wait for the US open"},
                         {"symbol": "DBK", "until": "2026-11-08T08:30:00Z", "why": "next week"},
                         {"symbol": "GENERAL", "until": "2026-11-04T15:00:00Z", "why": "x"},
                         {"symbol": "AAPL", "until": "2026-11-04T15:00:00Z", "why": "x"},
                         {"symbol": "NVDA", "until": "2026-11-04T12:00:00Z", "why": "past"},
                         {"symbol": "NVDA", "until": "tomorrow-ish", "why": "bad"}]}
        r = dr.verify_decision(STATUS, dec, "codex1", 3, NOW, 24, {}, UNIVERSE)
        kept = {d["symbol"]: d for d in r["defer"]}
        self.assertEqual(set(kept), {"MU", "DBK"})
        self.assertEqual(kept["MU"]["until"], "2026-11-04T14:30:00+00:00")
        self.assertFalse(kept["MU"]["clamped"])
        self.assertTrue(kept["DBK"]["clamped"])                 # 4 days → clamped to now + 24h
        self.assertEqual(kept["DBK"]["until"], (NOW + timedelta(hours=24)).isoformat())
        whys = [d["why"] for d in r["dropped"] if d["what"] == "defer"]
        self.assertEqual(len(whys), 4)
        self.assertTrue(any("also launched" in w for w in whys))
        self.assertTrue(any("not on the due list" in w for w in whys))
        self.assertTrue(any("already passed" in w for w in whys))
        self.assertTrue(any("unparseable" in w for w in whys))


class TestApplyDefers(unittest.TestCase):
    def setUp(self):
        self.ds = _load("duty_status")

    def test_defer_holds_soft_reasons_but_not_a_line_hit(self):
        items = [{"symbol": "MU", "reasons": ["review time passed 0d ago"], "skips": 1},
                 {"symbol": "NVDA", "reasons": ["tree dead: -3.6 ATR in 1d, tail 0.00, KL 2.1 bits"], "skips": 0},
                 {"symbol": "GENERAL", "reasons": ["review time passed 1d ago"], "skips": 0},
                 {"symbol": "AMD", "reasons": ["earnings landed: release 11-03"], "skips": 0}]
        defers = {"MU": {"until": "2026-11-04T14:30:00+00:00", "why": "wait for the open", "harness": "codex1"},
                  "NVDA": {"until": "2026-11-04T14:30:00+00:00", "why": "wait", "harness": "codex1"},
                  "AMD": {"until": "2026-11-04T14:30:00+00:00", "why": "tree priced the print", "harness": "codex1"}}
        due, deferred = self.ds.apply_defers(items, defers, NOW)
        self.assertEqual([d["symbol"] for d in due], ["NVDA", "GENERAL"])
        self.assertEqual([d["symbol"] for d in deferred], ["MU", "AMD"])     # 11-06: earnings landed no longer overrides a defer
        self.assertEqual(deferred[0]["until"], "2026-11-04T14:30:00+00:00")
        self.assertIn("overrides", due[0].get("notes_duty", ""))      # the dead tree overrode the defer, and says so

    def test_dismiss_rules(self):
        st = {"running": [], "markets": {"us": {"market": {"state": "open", "in_window": True},
              "due": [{"symbol": "MU", "reasons": ["review time passed 0d ago"], "last_run": "2026-11-04T10:00:00+00:00"},
                      {"symbol": "NVDA", "reasons": ["earnings landed: release 11-03"], "last_run": "2026-11-03T10:00:00+00:00"}]}}}
        dec = {"harness": "codex1", "launch": [{"market": "us", "symbols": ["NVDA"]}],
               "dismiss": [{"symbol": "MU", "why": "the call point was the horizon end written a day early; nothing new"},
                           {"symbol": "NVDA", "why": "x"}, {"symbol": "AAPL", "why": "x"}, {"symbol": "MU", "why": ""}]}
        r = dr.verify_decision(st, dec, "codex1", 3, NOW, 24, {"MU": 5}, UNIVERSE)
        self.assertEqual(r["dismiss"], [{"symbol": "MU", "why": dec["dismiss"][0]["why"], "last_run": "2026-11-04T10:00:00+00:00",
                                         "reasons": ["review time passed"]}])
        whys = [d["why"] for d in r["dropped"] if d["what"] == "dismiss"]
        self.assertTrue(any("also launched" in w for w in whys) and any("not on the due list" in w for w in whys) and any("needs a why" in w for w in whys), whys)
        self.assertNotIn("MU", r["skips"])                                                       # a dismissed call is not a skipped wake
        st2 = {"running": [], "markets": {"us": {"market": {"state": "open", "in_window": True},
               "due": [{"symbol": "MU", "reasons": ["tree dead: -3 ATR in 1d, tail 0.00, KL 2 bits"], "last_run": "x"}]}}}
        r2 = dr.verify_decision(st2, {"harness": "codex1", "launch": [], "dismiss": [{"symbol": "MU", "why": "w"}]}, "codex1", 3, NOW, 24, {}, UNIVERSE)
        self.assertEqual(r2["dismiss"], []); self.assertTrue(any("nothing dismissable" in d["why"] for d in r2["dropped"]))

    def test_apply_dismissals(self):
        ds = _load("duty_status")
        items = [{"symbol": "MU", "reasons": ["review time passed 0d ago"], "last_run": "2026-11-04T10:00:00+00:00", "skips": 0},
                 {"symbol": "NVDA", "reasons": ["tree dead: -3.6 ATR in 1d, tail 0.00, KL 2.1 bits", "earnings landed: release 11-03"], "last_run": "2026-11-03T10:00:00+00:00", "skips": 0},
                 {"symbol": "AMD", "reasons": ["review time passed 1d ago"], "last_run": "2026-11-05T10:00:00+00:00", "skips": 0}]
        dis = {"MU": {"last_run": "2026-11-04T10:00:00+00:00", "why": "w", "harness": "codex1", "wake": "x", "reasons": ["review time passed"]},
               "NVDA": {"last_run": "2026-11-03T10:00:00+00:00", "why": "w", "harness": "codex1", "wake": "x", "reasons": ["earnings landed: release 11-03"]},
               "AMD": {"last_run": "2026-11-01T10:00:00+00:00", "why": "old report", "harness": "codex1", "wake": "x", "reasons": ["review time passed"]}}
        due, dismissed = ds.apply_dismissals(items, dis)
        self.assertEqual([d["symbol"] for d in dismissed], ["MU"])
        self.assertEqual([d["symbol"] for d in due], ["NVDA", "AMD"])
        self.assertEqual(due[0]["reasons"], ["tree dead: -3.6 ATR in 1d, tail 0.00, KL 2.1 bits"])      # the hard reason survives
        self.assertEqual(due[1]["reasons"], ["review time passed 1d ago"])                             # a newer report voids the dismissal
        items2 = [{"symbol": "MU", "reasons": ["review time passed 1d ago", "earnings landed: release 11-06"], "last_run": "2026-11-04T10:00:00+00:00", "skips": 0}]
        due2, dis2 = ds.apply_dismissals(items2, {"MU": dis["MU"]})
        self.assertEqual(due2[0]["reasons"], ["earnings landed: release 11-06"]); self.assertEqual(dis2, [])   # a later event is new

    def test_due_rank_divergence_first_then_events(self):
        items = [{"symbol": "A", "reasons": ["review time passed 3d ago"], "skips": 5, "overdue_days": 3, "severity": None},
                 {"symbol": "B", "reasons": ["earnings landed: release 11-03"], "skips": 0, "overdue_days": 0, "severity": 0.1},
                 {"symbol": "C", "reasons": ["drift: -1.5 ATR in 1d, tail 0.12, KL 0.6 bits"], "skips": 0, "overdue_days": 0, "severity": 0.76},
                 {"symbol": "D", "reasons": ["tree dead: -3.6 ATR in 1d, tail 0.00, KL 2.1 bits"], "skips": 0, "overdue_days": 0, "severity": 0.99},
                 {"symbol": "E", "reasons": ["drift: +1.2 ATR in 2d, tail 0.2, KL 0.3 bits", "order expired unfilled"], "skips": 2, "overdue_days": 1, "severity": 0.6},
                 {"symbol": "F", "reasons": ["tree expired: horizon 15d passed (t=16d), +0.3 ATR since the report"], "skips": 0, "overdue_days": 2, "severity": 1.0}]
        self.assertEqual([x["symbol"] for x in sorted(items, key=self.ds.due_rank)], ["F", "D", "C", "E", "B", "A"])

    def test_expired_defer_is_ignored(self):
        items = [{"symbol": "MU", "reasons": ["review time passed 0d ago"], "skips": 0}]
        defers = {"MU": {"until": "2026-11-04T12:00:00+00:00", "why": "old", "harness": "codex1"}}
        due, deferred = self.ds.apply_defers(items, defers, NOW)
        self.assertEqual(len(due), 1)
        self.assertEqual(deferred, [])


if __name__ == "__main__":
    unittest.main()


class TestPickHarness(unittest.TestCase):
    def setUp(self):
        self.ds = _load("duty_status")

    def test_idle_identity_wins_over_the_busy_one(self):
        usage = {"codex1": {"available": True, "debates_left": 60, "days_to_reset": 5.0, "launched_today": 20},
                 "codex2": {"available": True, "debates_left": 48, "days_to_reset": 6.0, "launched_today": 0}}
        self.assertEqual(self.ds.pick_harness(usage, set(), ["codex1", "codex2"]), "codex2")

    def test_priority_breaks_ties_and_running_identity_is_kept(self):
        usage = {"codex1": {"available": True, "debates_left": 60, "days_to_reset": 5.0, "launched_today": 0},
                 "codex2": {"available": True, "debates_left": 48, "days_to_reset": 6.0, "launched_today": 0}}
        self.assertEqual(self.ds.pick_harness(usage, set(), ["codex1", "codex2"]), "codex1")
        self.assertEqual(self.ds.pick_harness(usage, {"codex2"}, ["codex1", "codex2"]), "codex2")

    def test_unavailable_identity_is_never_picked(self):
        usage = {"codex1": {"available": False, "debates_left": 0, "days_to_reset": 5.0, "launched_today": 0},
                 "codex2": {"available": True, "debates_left": 48, "days_to_reset": 6.0, "launched_today": 9}}
        self.assertEqual(self.ds.pick_harness(usage, set(), ["codex1", "codex2"]), "codex2")
        self.assertIsNone(self.ds.pick_harness({"codex1": {"available": False}}, set(), ["codex1", "codex2"]))


class TestPickHarnessThree(unittest.TestCase):
    """codex3 (2026-11-06) joins with codex1's rules: the pace rule rotates between the two mains; codex2 stays last on ties."""

    def test_rotates_to_the_idle_main(self):
        ds = _load("duty_status")
        usage = {"codex1": {"available": True, "debates_left": 60, "days_to_reset": 5.0, "launched_today": 12},
                 "codex3": {"available": True, "debates_left": 70, "days_to_reset": 6.0, "launched_today": 0},
                 "codex2": {"available": True, "debates_left": 48, "days_to_reset": 6.0, "launched_today": 0}}
        self.assertEqual(ds.pick_harness(usage, set(), ["codex1", "codex3", "codex2"]), "codex3")
        usage["codex3"]["launched_today"] = 14
        self.assertEqual(ds.pick_harness(usage, set(), ["codex1", "codex3", "codex2"]), "codex2")   # both mains busy → codex2 (if under its line)
        self.assertEqual(ds.pick_harness(usage, {"codex1"}, ["codex1", "codex3", "codex2"]), "codex1")  # one running → only it
