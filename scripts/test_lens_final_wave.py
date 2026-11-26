# -*- coding: utf-8 -*-
"""Final review wave (lenses, v3.4.1): the live-debater lens block in spawn material (A1), one lens set at open (A6),
weighted_score / ingestion without a uniform fallback (A6)."""
import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_lens_fixtures as F
import lens_views as lv                       # noqa: E402
from run_step import MockTools, Session, StepError   # noqa: E402
from validate_role_record import ingest       # noqa: E402


class _InTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lens_wave_"))
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open(self, symbol="GENERAL", lenses=None, cfg_lenses=None):
        """Open `symbol` in a lens root whose symbols.yaml lists `cfg_lenses` (default F.LENSES) while the data manifest's
        GENERAL lens list is `lenses` (default F.LENSES)."""
        from workflows import data_manifest
        F.lens_root(self.tmp, cfg_lenses)
        with mock.patch.dict(data_manifest.GENERAL_LENSES, {F.SYMBOL: list(lenses or F.LENSES)}):
            return Session.open(symbol, self.tmp, engine="mock", model="mock-judge", tools=MockTools(self.tmp))


class SpawnMaterialLensesTest(_InTmp):
    """A1: the debater's material names the session lenses, each lens's reference price (the exact P0) and T_days."""

    def test_multi_lens_material_carries_the_lenses_block(self):
        s = self.open()
        for role in ("Zealot", "Reaper"):
            text = (self.tmp / s.spawn_material(role, 1)).read_text(encoding="utf-8")
            self.assertIn("--- LENSES", text)
            for L in F.LENSES:
                self.assertIn(f"{L}: reference price {lv.fmt_level(F.REF[L])} ", text)
            self.assertIn(f"T_days: {s.state['odds']['T_days']}", text)
            self.assertIn("REQUIRED READ: prompts/agent/formats/lenses.md", text)   # wave 3: the rules live there
            self.assertIn("OUTPUT REMINDER: scenario_by_lens; every effect/update op carries lens", text)
            i = [text.index(f"{L}: reference price") for L in F.LENSES]
            self.assertEqual(i, sorted(i))   # the frozen session order
        self.assertIn("--- LENSES", s._lenses_block())

    def test_single_lens_material_has_no_lenses_block(self):
        s = self.open("TEST")
        text = (self.tmp / s.spawn_material("Zealot", 1)).read_text(encoding="utf-8")
        self.assertNotIn("--- LENSES", text)
        self.assertNotIn("scenario_by_lens", text)
        self.assertEqual(s._lenses_block(), "")


class OpenLensSetTest(_InTmp):
    """A6: the session lenses (data manifest) and the config lens weights (symbols.yaml) must be one list."""

    def test_same_lists_open(self):
        s = self.open()
        self.assertEqual(list(s.state["lens_weights_default"]), F.LENSES)

    def assertRefused(self, lenses, cfg_lenses, *names):
        with self.assertRaises(StepError) as cm:
            self.open(lenses=lenses, cfg_lenses=cfg_lenses)
        self.assertEqual(cm.exception.code, 20)
        for n in names:
            self.assertIn(n, str(cm.exception))

    def test_config_lacks_a_session_lens(self):
        self.assertRefused(F.LENSES, ["SPY", "QQQ"], "DIA", "no weight for ['DIA']")

    def test_config_weights_a_non_session_lens(self):
        self.assertRefused(["SPY", "QQQ"], F.LENSES, "DIA", "non-session lens ['DIA']")

    def test_order_differs(self):
        self.assertRefused(["QQQ", "SPY", "DIA"], F.LENSES, "same order")


class WeightsNoFallbackTest(unittest.TestCase):
    def test_weighted_score_names_the_missing_and_extra_lens(self):
        lam = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
        with self.assertRaises(lv.LensError) as cm:
            lv.weighted_score({"SPY": 50, "QQQ": 50}, lam)
        self.assertIn("DIA", str(cm.exception))
        with self.assertRaises(lv.LensError) as cm:
            lv.weighted_score({"SPY": 50, "QQQ": 50, "DIA": 50, "IWM": 50}, lam)
        self.assertIn("IWM", str(cm.exception))

    def test_multi_lens_ingest_without_weights_is_an_error(self):
        rec = F.lens_final(50, [F.Z_C1], F.Z_BY)
        kw = dict(lenses=F.LENSES, ref_prices=F.REF)
        errs, out = ingest(copy.deepcopy(rec), "Zealot", 1, [], **kw, lens_weights=F.WEIGHTS)
        self.assertEqual(errs, [])
        self.assertEqual(out["composite_score"], lv.weighted_score(out["composite_by_lens"], F.WEIGHTS))
        errs, _ = ingest(copy.deepcopy(rec), "Zealot", 1, [], **kw, lens_weights=None)
        self.assertTrue(errs and "lens_weights" in errs[0], errs)
        errs, _ = ingest(copy.deepcopy(rec), "Zealot", 1, [], **kw, lens_weights={"SPY": 0.6, "QQQ": 0.4})
        self.assertTrue(errs and "DIA" in errs[0], errs)


class ApiLensSummaryTest(unittest.TestCase):
    """A4: the report summary keeps op_type = the primary lens's (the PG column) and adds each lens's own operation."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import importlib
        import io
        import sys
        sys.path.insert(0, str(F.ROOT))
        with contextlib.redirect_stdout(io.StringIO()):   # the module prints its results directory on import
            cls.api = importlib.import_module("equipoise_entry")

    def test_by_lens_summary(self):
        by = {"SPY": {"operation_type": "HOLD_NEUTRAL", "operation_target": 500.0, "operation_size": "N/A", "scenario": {}},
              "QQQ": {"operation_type": "MARKET_ENTRY", "operation_target": 400.0, "operation_size": "STANDARD_SIZE"},
              "DIA": {"operation_type": "TRIM_POSITION", "operation_target": 300.0, "operation_size": "STANDARD_SIZE"}}
        rep = {"lenses": ["SPY", "QQQ", "DIA"], "by_lens": by, "operation_type": "HOLD_NEUTRAL",
               "operation_target": "SPY=500; QQQ=400; DIA=300", "composite_score": 52}
        row = {"report": rep, "operation_type": "HOLD_NEUTRAL", "operation_target": "SPY=500; QQQ=400; DIA=300",
               "composite_score": 52, "operation_size": "N/A"}
        out = self.api._signal_from_debate(row)
        self.assertEqual(out["op_type"], "HOLD_NEUTRAL")
        self.assertEqual(list(out["by_lens"]), ["SPY", "QQQ", "DIA"])
        qqq = dict(out["by_lens"]["QQQ"])
        qqq.pop("execution_card", None)   # 2026-11-03: the per-lens summary also carries the execution card
        self.assertIn("operation_simple", qqq)   # 2026-11-10: and the lens's simple card (None here: the fixture has no tree)
        qqq.pop("operation_simple", None)
        self.assertEqual(qqq, {"operation_type": "MARKET_ENTRY", "operation_target": 400.0, "operation_size": "STANDARD_SIZE"})
        single = self.api._signal_from_debate({"report": {"operation_type": "ENTRY"}, "operation_type": "ENTRY"})
        self.assertNotIn("by_lens", single)
        # served on /api/dashboard: a lens-wise entry carries by_lens, a single-lens entry keeps its shape
        import sys
        import types
        single_row = {"report": {"operation_type": "ENTRY", "operation_target": "SPY=500"}, "operation_type": "ENTRY",
                      "operation_target": "SPY=500", "composite_score": 60, "as_of": "2026-10-31T00:00:00Z"}
        latest = {"GENERAL": dict(row, as_of="2026-10-31T00:00:00Z"), "GENERAL_JP": single_row}
        fake = types.ModuleType("datalayer.pg.lowfreq_reader")
        fake.get_debate_latest_batch = lambda: latest
        gens = [{"symbol": "GENERAL", "type": "general"}, {"symbol": "GENERAL_JP", "type": "general"}]
        with mock.patch.dict(sys.modules, {"datalayer.pg.lowfreq_reader": fake}):
            with mock.patch.object(self.api, "list_general_symbols", lambda: gens):
                with mock.patch.object(self.api, "CONFIG_SYMBOLS", Path("nonexistent_symbols.yaml")):
                    board = {e["symbol"]: e for e in self.api.get_dashboard_summary(lang=None)}
        self.assertEqual(list(board["GENERAL"]["by_lens"]), ["SPY", "QQQ", "DIA"])
        self.assertEqual(board["GENERAL"]["by_lens"]["DIA"]["operation_type"], "TRIM_POSITION")
        self.assertEqual(board["GENERAL"]["op_type"], "HOLD_NEUTRAL")   # still the primary lens's (PG column)
        self.assertNotIn("by_lens", board["GENERAL_JP"])
        self.assertEqual(board["GENERAL_JP"]["op_type"], "ENTRY")


if __name__ == "__main__":
    unittest.main()
