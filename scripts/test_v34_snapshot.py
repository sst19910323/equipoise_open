"""v3.4 Task 5: the `_session.json.odds` snapshot (C4) written at open, a session without one refused (fixture m), the cost /
unit inputs (ATR%14, ADV from the daily CSV) with their fallback, the postcheck `delta_crps` and endorsed per-side scenarios
(C8). Mock roots only; no PG, no IBKR.

Module-level helpers (importable by later tasks): `ruler_mock_root(tmp, atr, csv_rows, contract)`, `daily_rows(...)`,
`ruler_report(...)`."""
import copy
import datetime as _dt
import importlib.util
import json
import math
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("debate_driver", ROOT / "scripts" / "debate_driver.py")
dd = importlib.util.module_from_spec(spec); spec.loader.exec_module(dd)
import odds_model as om                                  # noqa: E402
import odds_snapshot as osn                              # noqa: E402
import run_step as rs                                    # noqa: E402
import scenario_replay as sr                             # noqa: E402
from run_step import Session, MockTools, StepError, session_cfg   # noqa: E402

TS = "20260930T000000Z"


def daily_rows(n=30, close=100.0, volume=500_000, early_volume=9_000_000, with_volume=True):
    """n business-day bars; the last 20 carry `volume` (median close×volume = close·volume), earlier ones `early_volume`
    (outside the 20-day window, so they must not move the median)."""
    import pandas as pd
    dates = pd.bdate_range(start="2026-08-20", periods=n)
    vols = [early_volume] * (n - 20) + [volume * f for f in (0.5, 1.5) * 10]   # median of the window = volume
    df = pd.DataFrame({"date": [str(d.date()) + " 00:00:00+00:00" for d in dates], "open": close, "high": close * 1.01,
                       "low": close * 0.99, "close": close})
    if with_volume:
        df["volume"] = vols
    return df


def ruler_mock_root(tmp: Path, atr=3.0, csv_rows=None, contract=None) -> str:
    """dd._mock_root + config/odds.yaml + the TEST technical snapshot (ATR%14 = atr) + an optional daily CSV."""
    ts = dd._mock_root(tmp)
    (tmp / "config" / "odds.yaml").write_text((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    if contract is not None:
        sy = json.loads((tmp / "config" / "symbols.yaml").read_text(encoding="utf-8"))
        sy["symbol_contracts"]["TEST"].update(contract)
        (tmp / "config" / "symbols.yaml").write_text(json.dumps(sy), encoding="utf-8")
    tech = {"TEST_current_snapshot_last_price": 100.0, "TEST_latest_data_timestamp": "2026-09-30T00:00:00+00:00",
            "TEST_session_context_current_session": "closed"}
    if atr is not None:
        tech["TEST_daily_atr_pct_14"] = atr
    (tmp / "data" / "technical" / "TEST" / f"TEST_technical_{ts}.json").write_text(json.dumps(tech), encoding="utf-8")
    if csv_rows is not None:
        d = tmp / "data" / "market_data" / "TEST"
        d.mkdir(parents=True, exist_ok=True)
        csv_rows.to_csv(d / f"TEST_5y_1d_{ts}.csv", index=False)
    return ts


def _open(tmp):
    return Session.open("TEST", tmp, engine="mock", model="m", tools=MockTools(tmp))


def _saved(tmp, ts):
    return json.loads((tmp / "data" / "store" / "TEST" / ts / "_session.json").read_text(encoding="utf-8"))


class AdvTest(unittest.TestCase):
    def test_median_of_the_last_20_days_times_fx(self):
        adv, why = osn.adv_usd_m(daily_rows(), 1.0)
        self.assertIsNone(why)
        self.assertAlmostEqual(adv, 50.0, 6)            # median 100 × 500k = 50 M (earlier 9M-volume bars ignored)
        adv, _ = osn.adv_usd_m(daily_rows(close=1000.0, volume=1_000_000), 0.0066)
        self.assertAlmostEqual(adv, 6.6, 6)            # JPY 1e9 → 6.6 M USD

    def test_missing_or_zero_volume(self):
        self.assertEqual(osn.adv_usd_m(daily_rows(with_volume=False))[0], None)
        self.assertIn("volume", osn.adv_usd_m(daily_rows(with_volume=False))[1])
        adv, why = osn.adv_usd_m(daily_rows(volume=0, early_volume=0))
        self.assertIsNone(adv); self.assertIn("zero", why)
        self.assertEqual(osn.adv_usd_m(None), (None, "no daily bars"))

    def test_adr_rule(self):
        self.assertTrue(osn.is_adr("US", {"adr": True, "currency": "USD"}))
        self.assertTrue(osn.is_adr("US", {"currency": "JPY"}))
        self.assertFalse(osn.is_adr("US", {"currency": "USD"}))
        self.assertFalse(osn.is_adr("JP", {"currency": "JPY"}))

    def test_git_commit_never_raises(self):
        self.assertIsNone(osn.git_commit(Path(tempfile.mkdtemp())))
        c = osn.git_commit(ROOT)
        self.assertTrue(c is None or len(c) == 40)


class SnapshotAtOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v34_snap_"))

    def test_open_writes_cost_and_unit_basis_from_the_daily_csv(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows())
        _open(self.tmp)
        odds = _saved(self.tmp, ts)["odds"]
        self.assertEqual(odds["schema"], "3.4"); self.assertEqual(odds["gamma"], 6.25); self.assertEqual(odds["sigma_ref"], 0.08)
        # US volume is in IBKR lots of 100 → ADV 100 × 500k lots × 100 = 5000 M USD
        self.assertAlmostEqual(odds["cost_bps"], 2 + 10 * 3.0 / math.sqrt(5000.0), 3)      # 2.4243
        ub = odds["unit_basis"]
        self.assertEqual(ub["atr_pct_14"], 3.0); self.assertAlmostEqual(ub["adv_usd_m"], 5000.0, 6)
        self.assertEqual(ub["volume_lot"], 100.0)
        self.assertAlmostEqual(ub["s_s"], 0.03 * math.sqrt(15), 6)
        self.assertEqual((ub["venue"], ub["adr"]), ("US", False))
        self.assertNotIn("fallback", ub)
        self.assertIn("reference order size", ub["reference_size"])
        self.assertEqual(ub["adv_source"], f"data/market_data/TEST/TEST_5y_1d_{ts}.csv")
        self.assertNotIn("reconstructed", odds["provenance"])
        self.assertIn("commit", odds["provenance"])
        self.assertNotIn("baseline", odds.get("unit_basis"))
        # every later stage reads the snapshot
        s = Session("TEST", ts, self.tmp)
        cfg = session_cfg(s)
        self.assertEqual(cfg["cost_bps"], odds["cost_bps"]); self.assertEqual(cfg["unit_basis"]["s_s"], ub["s_s"])

    def test_snapshot_is_not_re_read_from_config_after_open(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows())
        _open(self.tmp)
        (self.tmp / "config" / "odds.yaml").write_text("schema: '3.4'\ngamma: 99\n", encoding="utf-8")
        self.assertEqual(session_cfg(Session("TEST", ts, self.tmp))["gamma"], 6.25)

    def test_adr_and_venue(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows(), contract={"adr": True})
        _open(self.tmp)
        odds = _saved(self.tmp, ts)["odds"]
        self.assertTrue(odds["unit_basis"]["adr"])
        self.assertAlmostEqual(odds["cost_bps"], 2 + 3 + 10 * 3.0 / math.sqrt(5000.0), 3)

    def test_us_volume_lots_aapl_like(self):
        """IBKR reports US daily volume in lots of 100: 304,982 lots = 30,498,200 shares."""
        ts = ruler_mock_root(self.tmp, atr=1.9, csv_rows=daily_rows(close=271.49, volume=304_982))
        _open(self.tmp)
        ub = _saved(self.tmp, ts)["odds"]["unit_basis"]
        self.assertAlmostEqual(ub["adv_usd_m"], round(271.49 * 30_498_200 / 1e6, 4), 4)       # ≈ 8280 M USD
        self.assertEqual(ub["volume_lot"], 100.0)

    def test_non_us_venue_lot_is_one(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows(close=1000.0, volume=1_000_000),
                          contract={"region": "JP", "currency": "JPY"})
        s = _open(self.tmp)
        ub = _saved(self.tmp, s.ts)["odds"]["unit_basis"]
        self.assertEqual((ub["venue"], ub["volume_lot"]), ("JP", 1.0))
        self.assertAlmostEqual(ub["adv_usd_m"], 6.6, 6)                  # 1e9 JPY × 0.0066

    def test_schema_default_follows_the_yaml(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows())
        s = Session.open("TEST", self.tmp, engine="mock", model="m", tools=MockTools(self.tmp))   # no flag
        self.assertEqual(s.schema(), "3.4"); self.assertEqual(_saved(self.tmp, ts)["odds"]["schema"], "3.4")
        self.assertNotIn("schema", s.state)                              # the schema lives in the snapshot only

    def test_provenance_source(self):
        commit, src = osn.commit_of(ROOT)
        self.assertIn(src, ("git", "deploy_file") if commit else (None,))
        t = Path(tempfile.mkdtemp())
        self.assertEqual(osn.commit_of(t), (None, None))
        (t / "DEPLOY_COMMIT").write_text("abc1234def" + chr(10), encoding="utf-8")
        self.assertEqual(osn.commit_of(t), ("abc1234def", "deploy_file"))
        self.assertEqual(osn.git_commit(t), "abc1234def")
        (t / "DEPLOY_COMMIT").write_text("  " + chr(10), encoding="utf-8")        # whitespace-only → ignored
        self.assertEqual(osn.commit_of(t), (None, None))
        subprocess.run(["git", "init", "-q"], cwd=str(t), check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                       cwd=str(t), check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(t), capture_output=True, text=True).stdout.strip()
        self.assertEqual(osn.commit_of(t), (head, "git"))                # blank file falls back to git
        (t / "DEPLOY_COMMIT").write_text("abc1234def" + chr(10), encoding="utf-8")
        self.assertEqual(osn.commit_of(t), ("abc1234def", "deploy_file"))  # file beats a git HEAD
        ts = ruler_mock_root(self.tmp, atr=3.0)
        (self.tmp / "DEPLOY_COMMIT").write_text("feedbeef01" + chr(10), encoding="utf-8")
        _open(self.tmp)
        self.assertEqual(_saved(self.tmp, ts)["odds"]["provenance"],
                         {"commit": "feedbeef01", "source": "deploy_file"})

    def test_missing_volume_falls_back(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows(with_volume=False))
        _open(self.tmp)
        odds = _saved(self.tmp, ts)["odds"]
        self.assertEqual(odds["cost_bps"], 7.0)                          # base_bps[US] 2 + 5
        self.assertIn("volume", odds["unit_basis"]["fallback"])
        self.assertAlmostEqual(odds["unit_basis"]["s_s"], 0.03 * math.sqrt(15), 6)   # ATR still gives the unit

    def test_missing_csv_and_missing_atr_fall_back(self):
        ts = ruler_mock_root(self.tmp, atr=None, csv_rows=None)
        _open(self.tmp)
        ub = _saved(self.tmp, ts)["odds"]["unit_basis"]
        self.assertEqual(_saved(self.tmp, ts)["odds"]["cost_bps"], 7.0)
        self.assertIn("no daily CSV", ub["fallback"]); self.assertIn("ATR", ub["fallback"])
        self.assertIsNone(ub["s_s"])

    def test_general_without_lens_files_fails_open(self):
        st = {"lenses": ["SPY", "QQQ", "DIA"], "region": "US",
              "data_files": {"lenses": {"SPY": "data/technical/SPY/SPY_technical_x.json"}}}
        with self.assertRaises(osn.OddsSnapshotError) as cm:               # no lens files at all: no reference price
            osn.build_odds_snapshot(st, "GENERAL", self.tmp)
        self.assertIn("lens", str(cm.exception))


GENERAL_ATR = {"SPY": 1.0, "QQQ": 3.0, "DIA": 1.5}
GENERAL_PX = {"SPY": 100.0, "QQQ": 400.0, "DIA": 350.0}


def general_root(tmp: Path, lenses=("SPY", "QQQ", "DIA")) -> str:
    """A GENERAL fake root: dd._mock_root + odds.yaml + symbols.yaml with the GENERAL lens weights + one technical snapshot
    (own ATR%14 / price) and one daily CSV per lens."""
    ts = dd._mock_root(tmp)
    (tmp / "config" / "odds.yaml").write_text((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    contracts = {s: {"symbol": s, "region": "US", "currency": "USD"} for s in lenses}
    contracts["GENERAL"] = {"symbol": "GENERAL", "region": "US", "currency": "USD",
                            "lenses": [{"symbol": "SPY", "weight": 0.5}, {"symbol": "QQQ", "weight": 0.3},
                                       {"symbol": "DIA", "weight": 0.2}]}
    (tmp / "config" / "symbols.yaml").write_text(json.dumps({
        "analysis_targets": {"stocks": [], "etfs": [], "generals": ["GENERAL"], "benchmarks": []},
        "symbol_contracts": contracts}), encoding="utf-8")
    for sym in lenses:
        d = tmp / "data" / "technical" / sym
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sym}_technical_{ts}.json").write_text(json.dumps({
            f"{sym}_current_snapshot_last_price": GENERAL_PX[sym], f"{sym}_daily_atr_pct_14": GENERAL_ATR[sym],
            f"{sym}_latest_data_timestamp": "2026-09-30T00:00:00+00:00",
            f"{sym}_session_context_current_session": "closed"}), encoding="utf-8")
        m = tmp / "data" / "market_data" / sym
        m.mkdir(parents=True, exist_ok=True)
        daily_rows(close=GENERAL_PX[sym]).to_csv(m / f"{sym}_5y_1d_{ts}.csv", index=False)
    return ts


def general_state(ts: str, lenses=("SPY", "QQQ", "DIA")) -> dict:
    return {"lenses": list(lenses), "region": "US", "currency": "USD",
            "data_files": {"lenses": {l: f"data/technical/{l}/{l}_technical_{ts}.json" for l in lenses}}}


class PerLensSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v34_lens_"))

    def test_by_lens_snapshot_for_general(self):
        ts = general_root(self.tmp)
        odds = osn.build_odds_snapshot(general_state(ts), "GENERAL", self.tmp)
        bl = odds["by_lens"]
        self.assertEqual(list(bl), ["SPY", "QQQ", "DIA"])
        self.assertEqual(bl["SPY"]["reference_price"], odds["reference_price"])
        self.assertEqual(bl["SPY"]["cost_bps"], odds["cost_bps"])
        self.assertEqual(bl["QQQ"]["reference_price"], 400.0)
        self.assertNotEqual(bl["QQQ"]["cost_bps"], bl["SPY"]["cost_bps"])
        self.assertEqual(bl["DIA"]["s_s"], round(1.5 / 100 * math.sqrt(15), 6))
        for k in ("currency", "data_symbol", "atr_pct_14", "adv_usd_m", "adv_missing", "volume_lot", "volume_lot_basis",
                  "venue", "adr", "adv_source", "technical_source"):
            self.assertIn(k, bl["DIA"])
        self.assertEqual(bl["DIA"]["data_symbol"], "DIA")
        self.assertEqual(odds["lens_weights_default"], {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2})
        ub = odds["unit_basis"]
        self.assertEqual(ub["s_s"], bl["SPY"]["s_s"]); self.assertEqual(ub["atr_pct_14"], bl["SPY"]["atr_pct_14"])

    def test_single_lens_has_no_by_lens(self):
        ts = ruler_mock_root(self.tmp, atr=3.0, csv_rows=daily_rows())
        st = {"region": "US", "currency": "USD",
              "data_files": {"symbol": {"technical": f"data/technical/TEST/TEST_technical_{ts}.json"}}}
        odds = osn.build_odds_snapshot(st, "TEST", self.tmp)
        self.assertNotIn("by_lens", odds); self.assertNotIn("lens_weights_default", odds)

    def test_lens_without_reference_price_fails_open(self):
        ts = general_root(self.tmp)
        (self.tmp / "data" / "technical" / "QQQ" / f"QQQ_technical_{ts}.json").unlink()
        with self.assertRaises(osn.OddsSnapshotError) as cm:
            osn.build_odds_snapshot(general_state(ts), "GENERAL", self.tmp)
        self.assertIn("lens QQQ", str(cm.exception))

    def test_lens_without_atr_fails_open(self):
        ts = general_root(self.tmp)
        p = self.tmp / "data" / "technical" / "DIA" / f"DIA_technical_{ts}.json"
        d = json.loads(p.read_text(encoding="utf-8")); d.pop("DIA_daily_atr_pct_14")
        p.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(osn.OddsSnapshotError) as cm:
            osn.build_odds_snapshot(general_state(ts), "GENERAL", self.tmp)
        self.assertIn("lens DIA", str(cm.exception))

    def test_open_records_default_lens_weights(self):
        ts = general_root(self.tmp)
        s = Session.open("GENERAL", self.tmp, engine="mock", model="m", tools=MockTools(self.tmp))
        want = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
        self.assertEqual(s.state["odds"]["lens_weights_default"], want)
        self.assertEqual(s.state["lens_weights_default"], want)
        self.assertEqual(set(s.state["odds"]["by_lens"]), {"SPY", "QQQ", "DIA"})

    def test_open_converts_snapshot_error_to_step_error(self):
        ts = general_root(self.tmp)
        (self.tmp / "data" / "technical" / "QQQ" / f"QQQ_technical_{ts}.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(StepError) as cm:
            Session.open("GENERAL", self.tmp, engine="mock", model="m", tools=MockTools(self.tmp))
        self.assertEqual(cm.exception.code, 20); self.assertIn("lens QQQ", str(cm.exception))


class SessionBeforeRulerTest(unittest.TestCase):
    """Fixture (m): a session without `odds` was opened before v3.4 and cannot be resumed — every stage refuses it."""

    def test_session_without_odds_is_refused(self):
        from ruling import RulingError, session_cfg as ruling_cfg
        tmp = Path(tempfile.mkdtemp(prefix="v34_legacy_"))
        ts = ruler_mock_root(tmp)
        s = _open(tmp)
        s.state.pop("odds")
        s.save()
        s = Session("TEST", ts, tmp, MockTools(tmp))
        before = (tmp / "data" / "store" / "TEST" / ts / "_session.json").read_bytes()
        with self.assertRaises(StepError) as cm:
            session_cfg(s)
        self.assertIn("session opened before v3.4", str(cm.exception))
        self.assertIsNone(s.schema())
        with self.assertRaises(RulingError) as cm:
            ruling_cfg(s.state)
        self.assertIn("session opened before v3.4", str(cm.exception))
        with self.assertRaises(StepError):
            s._meta()
        self.assertEqual((tmp / "data" / "store" / "TEST" / ts / "_session.json").read_bytes(), before)   # nothing written


# ---------------- postcheck: delta_crps and endorsed per-side scenarios (C8) ----------------

def _sc(atoms, schema="3.4"):
    return {"schema": schema, "T_days": 15, "P0": 100.0, "execution": {},
            "nodes": [{"id": f"N{i}", "label": f"n{i}", "p": p, "P_T": pt, "refs": ["Z1"], "events": []}
                      for i, (p, pt) in enumerate(atoms)]}


Z_ = _sc([(0.55, 110.0), (0.30, 100.0), (0.15, 90.0)])
R_ = _sc([(0.35, 108.0), (0.35, 98.0), (0.30, 88.0)])


def ruler_report(w=0.8, endorsed=True):
    A = sr.mixture(Z_, R_, w, reason="test")
    rep = {"schema_version": "3.4", "scenario": A, "atr_pct_14": 1.5, "odds": om.snapshot_from_config(om.DEFAULT_CONFIG),
           "debate_history": [{"role": "Zealot", "round": "Init", "content": {"scenario": Z_}},
                              {"role": "Reaper", "round": "Init", "content": {"scenario": R_}}]}
    if endorsed:
        rep["dissent"] = {"zealot_endorsed_scenario": _sc([(0.9, 115.0), (0.1, 90.0)]),
                          "reaper_endorsed_scenario": _sc([(0.2, 115.0), (0.8, 90.0)])}
    return rep


class DeltaCrpsTest(unittest.TestCase):
    def setUp(self):
        import pandas as pd
        spec_ = importlib.util.spec_from_file_location("postcheck_quant", ROOT / "workflows" / "postcheck_quant.py")
        self.pq = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(self.pq)
        closes = [100.0] * 3 + [100 + i for i in range(1, 26)]
        idx = pd.date_range("2026-10-01", periods=len(closes), freq="B", tz="UTC")
        df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes}, index=idx)
        self.pq._load_daily_df = lambda *a, **k: df
        self.T = _dt.datetime(2026, 10, 5, 15, 0, tzinfo=_dt.timezone.utc)

    @staticmethod
    def _crps(sc, y):
        return om.crps_discrete([(n["p"], n["P_T"] / 100.0 - 1.0) for n in sc["nodes"]], y)

    def test_delta_crps_for_a_3_5_report(self):
        rep = ruler_report(w=0.8)
        out = self.pq.forecast_scores("TEST", self.T, rep)
        self.assertEqual(out["status"], "scored")
        y = out["realized_return"]                                        # +15%
        half = sr.rebuild_at_half(rep["scenario"])
        exp = self._crps(rep["scenario"], y) - self._crps(half, y)
        self.assertAlmostEqual(out["delta_crps"], round(exp, 6), 6)
        self.assertLess(out["delta_crps"], 0)                           # leaning to the bull side helped on a +15% close
        # endorsed scenarios score the sides (C8), not the last history scenario
        self.assertEqual(out["zealot_last"]["label"], "zealot_endorsed")
        self.assertAlmostEqual(out["zealot_last"]["crps"], round(self._crps(rep["dissent"]["zealot_endorsed_scenario"], y), 6), 6)
        self.assertEqual(out["reaper_last"]["label"], "reaper_endorsed")

    def test_w_half_gives_zero_and_fallback_to_history(self):
        out = self.pq.forecast_scores("TEST", self.T, ruler_report(w=0.5, endorsed=False))
        self.assertAlmostEqual(out["delta_crps"], 0.0, 9)
        self.assertEqual(out["zealot_last"]["label"], "zealot_last")
        self.assertAlmostEqual(out["zealot_last"]["crps"], round(self._crps(Z_, out["realized_return"]), 6), 6)

    def test_3_5_report_without_odds_is_not_scored(self):
        rep = ruler_report()
        rep.pop("odds")
        self.assertEqual(self.pq.forecast_scores("TEST", self.T, rep), {"error": "no odds snapshot"})

    def test_pre_ruler_report_is_not_scored(self):
        rep = ruler_report()
        for k in ("scenario", "schema_version", "odds", "dissent"):
            rep.pop(k, None)
        self.assertEqual(self.pq.forecast_scores("TEST", self.T, rep), {})


if __name__ == "__main__":
    unittest.main()
