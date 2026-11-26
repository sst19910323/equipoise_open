"""Fix wave 2 (GPT external review, council round 2): G1-G8 regression tests."""
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


om = _load("odds_model", "scripts/odds_model.py")
tom = _load("test_odds_model", "scripts/test_odds_model.py")


# ---------------------------------------------------------------------------------------------------------------- G1

class CrossDirectionTieTest(unittest.TestCase):
    """G1: a CE tie across directions is neutral; who filed which contract never decides the result."""
    def _sc(self):
        return tom.two_node(0.5, 110, 90, up_events=tom.leaf_events((1, "touch", 105), (15, "close", 110)),
                            down_events=tom.leaf_events((1, "touch", 95), (15, "close", 90)))

    BUY = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "touch > 104", "fill": "at_level"}]}}
    SELL = {"underweight": {"stop": None, "conditional": [{"name": "brk", "observe": "touch < 96", "fill": "at_level"}]}}

    def _derive(self, z, r, cost):
        sc = self._sc()
        return om.derive_for_report(sc, om.build_candidates(z, r, sc, []), tom._cfg(cost_bps=cost), s_s=0.08)

    def test_swapping_filers_gives_identical_result_and_neutral(self):
        for cost in (0.0, 5.0):
            a = self._derive(self.BUY, self.SELL, cost)
            b = self._derive(self.SELL, self.BUY, cost)
            self.assertEqual(a["operation_type"], b["operation_type"], cost)
            self.assertEqual(a["chosen"], "neutral", cost)
            self.assertEqual(a["operation_type"], "HOLD_NEUTRAL")
            self.assertTrue(a["comparison"]["tie_selected"])
            self.assertIn("cross_direction_tie", a["comparison"].get("tie_rule", ""))
            self.assertEqual(sorted(a["comparison"]["tie_set"]), sorted(b["comparison"]["tie_set"]))

    def test_candidates_order_independent(self):
        sc = self._sc()
        a = om.build_candidates(self.BUY, self.SELL, sc, [])
        b = om.build_candidates(self.SELL, self.BUY, sc, [])
        strip = lambda cs: [{k: v for k, v in c.items() if k != "provenance"} for c in cs]
        self.assertEqual(strip(a), strip(b))

    def test_same_direction_tie_keeps_policy(self):
        sc = tom.two_node(0.6, 110, 95)
        d = om.derive_for_report(sc, om.build_candidates({}, {}, sc, []), tom._cfg(cost_bps=0.0), s_s=0.08)
        self.assertEqual(d["chosen"], "overweight_now")

# ---------------------------------------------------------------------------------------------------------------- G2

tli = _load("test_lens_ingest", "scripts/test_lens_ingest.py")
dd = tli.dd


class NullAppliesCarryTest(unittest.TestCase):
    """G2: an update with applies: null whose ops equal its claim's registered effect is refused (no double carry)."""
    opening_history = tli.LensIngestTest.opening_history
    round2 = tli.LensIngestTest.round2

    def test_null_applies_with_the_registered_ops_is_refused(self):
        h = self.opening_history("SPY", "QQQ")
        rec = self.round2(h, {"SPY": [tli.MOVE], "QQQ": [tli.MOVE]}, tli.tag([tli.MOVE], "SPY", "QQQ"), applies=None)
        errs, _ = tli.ing(rec, "Reaper", 2, h)
        self.assertTrue(any("lens QQQ: scenario_updates[0]: ops equal the registered effect of Z1#true" in e
                            and "applies: 'Z1#true'" in e for e in errs), errs)

    def test_null_applies_with_other_ops_is_accepted(self):
        h = self.opening_history("SPY", "QQQ")
        other = {"op": "p_move", "from": "C", "to": "A", "amount": 0.05}
        rec = self.round2(h, {"SPY": [other], "QQQ": [other]}, tli.tag([other], "SPY", "QQQ"), applies=None)
        errs, _ = tli.ing(rec, "Reaper", 2, h)
        self.assertEqual(errs, [])

    def test_null_applies_under_another_claim_id_is_refused(self):
        h = self.opening_history("SPY", "QQQ")
        for cid in ("R1", "E1"):   # the challenge's own id, an id outside the registry
            rec = self.round2(h, {"SPY": [tli.MOVE], "QQQ": [tli.MOVE]}, tli.tag([tli.MOVE], "SPY", "QQQ"), applies=None)
            rec["scenario_updates"][0]["claim"] = cid
            errs, _ = tli.ing(rec, "Reaper", 2, h)
            self.assertTrue(any("ops equal the registered effect of Z1#true" in e for e in errs), (cid, errs))

    def test_single_lens_null_applies_refused(self):
        from validate_role_record import ingest
        st = tli.store_rec
        e, z = ingest(dd.ruler_final(99, [dd.Z_C1], dd.Z14), "Zealot", 1, [], ref_price=100.0)
        self.assertEqual(e, [])
        e, r = ingest(dd.ruler_final(1, [dd.R_C1, dd.R_C2], dd.R14), "Reaper", 1, [st(1, "Zealot", 1, z)], ref_price=100.0)
        self.assertEqual(e, [])
        h = [st(1, "Zealot", 1, z), st(2, "Reaper", 1, r)]
        upd = dict(dd.UPD_Z1, applies=None)
        e, _ = ingest(dd.ruler_final(2, [dd.R_C1_DONE, dd.R_C2_DEAD], dd.R14_AFTER_Z1, [upd, dd.UPD_R2]), "Reaper", 2, h,
                      ref_price=100.0)
        self.assertTrue(any(x.startswith("scenario_updates[0]: ops equal the registered effect of Z1#true") for x in e), e)
        e, _ = ingest(dd.ruler_final(2, [dd.R_C1_DONE, dd.R_C2_DEAD], dd.R14_AFTER_Z1, [dd.UPD_Z1, dd.UPD_R2]), "Reaper", 2,
                      h, ref_price=100.0)
        self.assertEqual(e, [])

# ---------------------------------------------------------------------------------------------------------------- G3

import copy  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402


class _GateBase(unittest.TestCase):
    def d(self):
        self.assertIsNotNone(self.doc)
        return copy.deepcopy(self.doc)

    def fatal(self, doc):
        import validate_debate as vd
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            return vd.validate(doc)[0]
        finally:
            os.chdir(cwd)

    def assertFatal(self, doc, *needles):
        f = self.fatal(doc)
        self.assertTrue(any(all(n in x for n in needles) for x in f), (needles, f))


class LensGateRederiveTest(_GateBase):
    """G3 on the three-lens report: each edit that passed validate() before is FATAL naming lens and field."""
    @classmethod
    def setUpClass(cls):
        import test_lens_fixtures as F
        cls.F = F
        cls.tmp = Path(tempfile.mkdtemp(prefix="wave2_gate_"))
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        try:
            cls.out = F.drive_lens_session(cls.tmp, until="signoff")
            rc = cls.out["results"].get("signoff") or {}
            cls.doc = (json.loads((cls.tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
                       if rc.get("analysis_path") else None)
        finally:
            os.chdir(cls.cwd)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clean_report_passes(self):
        self.assertEqual(self.fatal(self.d()), [])

    def test_entry_family_rederived(self):
        doc = self.d()
        self.assertEqual(doc["by_lens"]["QQQ"]["operation_type"], "MARKET_ENTRY")
        doc["by_lens"]["QQQ"]["operation_type"] = "LIMIT_ENTRY"
        self.assertFatal(doc, "lens QQQ", "operation_type")
        doc = self.d()
        doc["by_lens"]["QQQ"]["operation_execution"]["derived"] = "LIMIT_ENTRY"
        self.assertFatal(doc, "lens QQQ", "operation_execution")

    def test_candidate_contract_compared_in_full(self):
        doc = self.d()
        cands = doc["by_lens"]["QQQ"]["scenario"]["derived"]["candidates"]
        c = next(c for c in cands if c.get("stop"))
        c["stop"] = dict(c["stop"], level=1)
        self.assertFatal(doc, "lens QQQ", "candidates")

    def test_display_rederived(self):
        doc = self.d()
        doc["by_lens"]["QQQ"]["scenario"]["derived"]["display"] = "100% exposure; buy 999 shares"
        self.assertFatal(doc, "lens QQQ", "display")

    def test_comparison_table_rederived(self):
        doc = self.d()
        st = doc["by_lens"]["QQQ"]["scenario"]["derived"]["comparison"]["strategies"]
        name = next(n for n in st if n != "neutral")
        st[name]["q"] = 0.99
        self.assertFatal(doc, "lens QQQ", "comparison")
        doc = self.d()
        st = doc["by_lens"]["QQQ"]["scenario"]["derived"]["comparison"]["strategies"]
        st[name]["x"] = 0.01
        self.assertFatal(doc, "lens QQQ", "comparison")

    def test_per_lens_s_ref_z_rebuilt(self):
        doc = self.d()
        doc["by_lens"]["QQQ"]["s_ref_z"] = 99.0
        doc["s_ref_z"] = sum(doc["lens_weights_default"][L] * doc["by_lens"][L]["s_ref_z"] for L in self.F.LENSES)
        self.assertFatal(doc, "lens QQQ", "s_ref_z")

    def test_unknown_lens_tag_in_history_is_fatal(self):
        doc = self.d()
        for e in doc["debate_history"]:
            for c in (e.get("content") or {}).get("claims") or []:
                if c.get("id") == "R1":
                    c["effect"]["if_true"].append({"op": "p_move", "from": "C", "to": "A", "amount": 0.1, "lens": "IWM"})
        self.assertFatal(doc, "IWM")

    def test_applies_must_carry_every_lens_rechecked(self):
        doc = self.d()
        for e in doc["debate_history"]:
            for c in (e.get("content") or {}).get("claims") or []:
                if c.get("id") == "R1":   # the registered effect gains a DIA op the carrying update never applied
                    c["effect"]["if_true"].append({"op": "p_move", "from": "C", "to": "A", "amount": 0.1, "lens": "DIA"})
        self.assertFatal(doc, "applies", "every lens")


class EntryToleranceTest(unittest.TestCase):
    def test_gate_and_assembler_share_the_tolerance(self):
        import ruling
        import assemble_debate_artifacts as asm
        self.assertEqual(ruling.ENTRY_AT_MARKET_TOL_PCT, asm._ENTRY_AT_MARKET_TOL_PCT)
        self.assertEqual(ruling.entry_family(400.0, 400.0), "MARKET_ENTRY")
        self.assertEqual(ruling.entry_family(410.0, 400.0), "LIMIT_ENTRY")
        self.assertEqual(ruling.entry_family(410.0, None), "ENTRY")


class SingleLensGateRederiveTest(_GateBase):
    """G3 on a single-lens ruler report (symbol TEST): candidate contract, entry family, display, s_ref_z / s_ref_r."""
    @classmethod
    def setUpClass(cls):
        t_rev = _load("test_v34_review_fixes", "scripts/test_v34_review_fixes.py")
        t_flow = _load("test_v34_flow", "scripts/test_v34_flow.py")
        fl = t_rev._run(t_flow.ruler_script())
        cls.tmp, cls.doc = fl["tmp"], fl["a"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clean_and_reference_price_is_the_snapshot(self):
        doc = self.d()
        self.assertEqual(self.fatal(doc), [])
        refs = [r for r in doc.get("reference_prices") or [] if r.get("symbol") == doc["symbol"]]
        self.assertEqual(float(refs[0]["price"]), float(doc["odds"]["reference_price"]))

    def test_entry_family(self):
        doc = self.d()
        op = doc["operation_type"]
        self.assertIn(op, ("MARKET_ENTRY", "LIMIT_ENTRY"))
        doc["operation_type"] = "LIMIT_ENTRY" if op == "MARKET_ENTRY" else "MARKET_ENTRY"
        self.assertFatal(doc, "operation_type")

    def test_candidate_display_s_ref_z(self):
        doc = self.d()
        c = next(c for c in doc["scenario"]["derived"]["candidates"] if c.get("stop"))
        c["stop"] = dict(c["stop"], level=1)
        self.assertFatal(doc, "candidates")
        doc = self.d()
        doc["scenario"]["derived"]["display"] = "buy 999 shares"
        self.assertFatal(doc, "display")
        for k in ("s_ref_z", "s_ref_r"):
            doc = self.d()
            doc[k] = 99.0
            self.assertFatal(doc, k)


# ---------------------------------------------------------------------------------------------------------------- G4

sys.path.insert(0, str(ROOT))
COND = {"conditional": {"observe": "close > 410", "fill": "next_open"}, "stop": None}
NOT_MET = [{"date": "2026-09-16", "open": 400.0, "high": 415.0, "low": 398.0, "close": 400.0},
           {"date": "2026-09-17", "open": 401.0, "high": 405.0, "low": 399.0, "close": 402.0}]
MET = [{"date": "2026-09-16", "open": 405.0, "high": 415.0, "low": 400.0, "close": 412.0},
       {"date": "2026-09-17", "open": 413.0, "high": 414.0, "low": 411.0, "close": 413.5}]


class ConditionalFillTest(unittest.TestCase):
    """G4: a conditional order is settled by its observe rule on daily bars, never as a resting limit at its level."""
    def test_settlement_function(self):
        from workflows import postcheck_quant as pq
        no = pq.conditional_fill_stats(NOT_MET, 400.0, "close > 410", "next_open", prev_close=400.0)
        self.assertFalse(no["entry_price_reached"]); self.assertFalse(no["condition_met"])
        self.assertIsNone(no.get("fill_price"))
        yes = pq.conditional_fill_stats(MET, 400.0, "close > 410", "next_open", prev_close=400.0)
        self.assertTrue(yes["condition_met"]); self.assertEqual(yes["condition_met_date"], "2026-09-16")
        self.assertEqual(yes["fill_date"], "2026-09-17"); self.assertEqual(yes["fill_price"], 413.0)
        self.assertTrue(yes["entry_price_reached"])
        lvl = pq.conditional_fill_stats(MET, 400.0, "touch > 410", "at_level", prev_close=400.0)
        self.assertEqual((lvl["fill_date"], lvl["fill_price"]), ("2026-09-16", 410.0))
        gap = pq.conditional_fill_stats(MET, 400.0, "gap_open > 404", "next_open", prev_close=400.0)
        self.assertEqual(gap["condition_met_date"], "2026-09-16")   # open 405 above the previous close 400 and 404
        gap = pq.conditional_fill_stats(MET, 400.0, "gap_open > 412.5", "next_open", prev_close=400.0)
        self.assertEqual(gap["condition_met_date"], "2026-09-17")   # open 413 above the previous close 412 and the level
        part = [{"date": "2026-09-15", "open": 420.0, "high": 421.0, "low": 399.0, "close": 400.0, "partial_day": True}] + NOT_MET
        for rule in ("open > 410", "gap_open > 410"):   # the partial first bar's open is not a session open
            self.assertFalse(pq.conditional_fill_stats(part, 400.0, rule, "next_open", prev_close=400.0)["condition_met"])
        self.assertTrue(pq.conditional_fill_stats(part, 400.0, "touch > 420", "at_level", prev_close=400.0)["condition_met"])
        sell = pq.conditional_fill_stats(MET, 400.0, "close < 405", "next_open", prev_close=400.0)
        self.assertFalse(sell["condition_met"])

    def _brief(self, bars, op="LIMIT_ENTRY"):
        brief = _load("pre_round_brief", "skills/equipoise-dispatch/scripts/pre_round_brief.py")
        rep = {"symbol": "QQQ", "operation_type": op, "operation_target": "410",
               "operation_execution": dict(COND, derived="LIMIT_ENTRY"),
               "reference_prices": [{"symbol": "QQQ", "price": 400.0}]}
        return brief._order_state(rep, bars, 400.0, "2026-09-18")

    def test_brief_not_met_is_never_a_fill(self):
        s, expired = self._brief(NOT_MET)
        self.assertNotIn("simulated fill", s)
        self.assertIn("conditional: close > 410, fill next_open", s)
        self.assertIn("not met", s)

    def test_brief_met(self):
        s, _ = self._brief(MET)
        self.assertIn("met on 09-16", s)
        self.assertNotIn("simulated fill", s)
        s, _ = self._brief(MET, op="TRIM_POSITION")
        self.assertIn("conditional: close > 410", s)


def _csv(root, sym, after, base):
    import pandas as pd
    d = root / sym
    d.mkdir(parents=True, exist_ok=True)
    pre = pd.bdate_range(start="2026-09-08", end="2026-09-15")
    rows = [{"date": t.strftime("%Y-%m-%d"), "open": base, "high": base, "low": base, "close": base, "volume": 1000}
            for t in pre] + [dict(b, volume=1000) for b in after]
    pd.DataFrame(rows).to_csv(d / f"{sym}_5y_1d_20260930T000000Z.csv", index=False)


def _flat(base):
    return [{"date": d, "open": base, "high": base, "low": base, "close": base} for d in ("2026-09-16", "2026-09-17")]


class ConditionalPostcheckTest(unittest.TestCase):
    def _run(self, sym, rep, bars_by):
        import tempfile as _tf
        from workflows import postcheck_quant as pq
        tmp = Path(_tf.mkdtemp(prefix="wave2_pq_"))
        old = pq.MARKET_DATA_ROOT
        pq.MARKET_DATA_ROOT = tmp
        try:
            for s, (after, base) in bars_by.items():
                _csv(tmp, s, after, base)
            return pq.compute_window_digest(sym, "2026-09-15T21:00:00Z", 3, rep)
        finally:
            pq.MARKET_DATA_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def _lens_report(self):
        tlp = _load("test_lens_postcheck", "scripts/test_lens_postcheck.py")
        rep = tlp._report()
        rep["by_lens"]["QQQ"].update(operation_target=410.0, operation_execution=dict(COND))
        rep["reference_prices"] = [{"symbol": "SPY", "price": 100.0}, {"symbol": "QQQ", "price": 400.0},
                                   {"symbol": "DIA", "price": 350.0}]
        return rep

    def test_lens_member_not_met(self):
        dg = self._run("GENERAL", self._lens_report(), {"SPY": (_flat(100.0), 100.0), "QQQ": (NOT_MET, 400.0),
                                                        "DIA": (_flat(350.0), 350.0)})
        q = next(m for m in dg["members"] if m["symbol"] == "QQQ")
        self.assertFalse(q["entry"]["entry_price_reached"], q["entry"])
        self.assertFalse(q["entry"]["condition_met"])
        self.assertIn("not met", q["alternatives"]["actual_conditional"])

    def test_lens_member_met(self):
        dg = self._run("GENERAL", self._lens_report(), {"SPY": (_flat(100.0), 100.0), "QQQ": (MET, 400.0),
                                                        "DIA": (_flat(350.0), 350.0)})
        q = next(m for m in dg["members"] if m["symbol"] == "QQQ")
        e = q["entry"]
        self.assertTrue(e["entry_price_reached"])
        self.assertEqual((e["condition_met_date"], e["fill_date"], e["fill_price"]), ("2026-09-16", "2026-09-17", 413.0))

    def test_single_lens_ruler_report_not_met(self):
        rep = {"schema_version": "3.4", "symbol": "TEST", "operation_type": "LIMIT_ENTRY", "operation_target": 410.0,
               "operation_execution": dict(COND, derived="LIMIT_ENTRY"),
               "reference_prices": [{"symbol": "TEST", "price": 400.0}],
               "short_term": {"direction": "上行", "target": "430", "invalid": "390", "horizon_days": 10}}
        dg = self._run("TEST", rep, {"TEST": (NOT_MET, 400.0)})
        self.assertFalse(dg["entry"]["entry_price_reached"], dg["entry"])
        self.assertFalse(dg["entry"]["condition_met"])

    def test_pre_ruler_untouched(self):
        rep = {"symbol": "TEST", "operation_type": "LIMIT_ENTRY", "operation_target": 410.0,
               "operation_execution": dict(COND, derived="LIMIT_ENTRY"),
               "reference_prices": [{"symbol": "TEST", "price": 400.0}],
               "short_term": {"direction": "上行", "target": "430", "invalid": "390", "horizon_days": 10}}
        dg = self._run("TEST", rep, {"TEST": (NOT_MET, 400.0)})
        self.assertTrue(dg["entry"]["entry_price_reached"])   # pre-ruler: the touch reading, unchanged
        self.assertNotIn("condition_met", dg["entry"])


# ---------------------------------------------------------------------------------------------------------------- G5

class OperationLensErrorTest(unittest.TestCase):
    """G5: any error on an operation lens makes the period immature; a HOLD lens's error stays a member error."""
    def _run(self, rep, boom):
        import tempfile as _tf
        from unittest import mock
        from workflows import postcheck_quant as pq
        tlp = _load("test_lens_postcheck", "scripts/test_lens_postcheck.py")
        tmp = Path(_tf.mkdtemp(prefix="wave2_g5_"))
        real = pq._one_symbol_digest

        def fake(sym, *a, **k):
            if sym == boom:
                raise ZeroDivisionError("synthetic")
            return real(sym, *a, **k)
        old = pq.MARKET_DATA_ROOT
        pq.MARKET_DATA_ROOT = tmp
        try:
            tlp._write_bars(tmp, {"SPY", "QQQ", "DIA"})
            with mock.patch.object(pq, "_one_symbol_digest", fake), self.assertLogs(level="WARNING") as logs:
                dg = pq.compute_window_digest("GENERAL", "2026-09-15T20:00:00Z", 20, rep)
                import logging
                logging.warning("sentinel")
            return dg, logs.output
        finally:
            pq.MARKET_DATA_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_operation_lens_error_is_immature(self):
        tlp = _load("test_lens_postcheck", "scripts/test_lens_postcheck.py")
        dg, logs = self._run(tlp._report(), "DIA")
        self.assertEqual(dg, {})
        self.assertTrue(any("lens DIA" in x and "error" in x and "ZeroDivisionError" in x for x in logs), logs)

    def test_hold_lens_error_is_a_member_error(self):
        tlp = _load("test_lens_postcheck", "scripts/test_lens_postcheck.py")
        rep = tlp._report()
        rep["by_lens"]["DIA"].update(operation_type="HOLD_NEUTRAL", operation_target=350.0)
        dg, _ = self._run(rep, "DIA")
        dia = next(m for m in dg["members"] if m["symbol"] == "DIA")
        self.assertIn("error", dia)
        self.assertNotIn("_error", dia)


# ---------------------------------------------------------------------------------------------------------------- G6

class ObserveOnlyIndexLensTest(_GateBase):
    """G6: an index lens (sec_type IND) is frozen observe-only at open, carried into by_lens, and never becomes shares."""
    @classmethod
    def setUpClass(cls):
        from unittest import mock
        import test_lens_fixtures as F
        cls.F = F
        real = F.lens_root

        def ind_root(tmp, *a, **k):
            ts = real(tmp, *a, **k)
            p = tmp / "config" / "symbols.yaml"
            cfg = json.loads(p.read_text(encoding="utf-8"))
            cfg["symbol_contracts"]["DIA"]["sec_type"] = "IND"   # DIA stands in for an index lens such as DAX
            p.write_text(json.dumps(cfg), encoding="utf-8")
            return ts
        cls.tmp = Path(tempfile.mkdtemp(prefix="wave2_g6_"))
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        try:
            with mock.patch.object(F, "lens_root", ind_root):
                cls.out = F.drive_lens_session(cls.tmp, until="signoff")
            rc = cls.out["results"].get("signoff") or {}
            cls.doc = (json.loads((cls.tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
                       if rc.get("analysis_path") else None)
        finally:
            os.chdir(cls.cwd)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_frozen_and_carried(self):
        doc = self.d()
        self.assertEqual(self.out["errors"], {})
        self.assertTrue(doc["odds"]["by_lens"]["DIA"].get("observe_only"))
        self.assertNotIn("observe_only", doc["odds"]["by_lens"]["SPY"])
        self.assertTrue(doc["by_lens"]["DIA"].get("observe_only"))
        self.assertNotIn("observe_only", doc["by_lens"]["SPY"])
        self.assertEqual(self.fatal(doc), [])
        doc["by_lens"]["DIA"].pop("observe_only")
        self.assertFatal(doc, "lens DIA", "observe_only")



# ---------------------------------------------------------------------------------------------------------------- G7

class LambdaEdgesTest(unittest.TestCase):
    D = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
    LS = ["SPY", "QQQ", "DIA"]

    def test_non_finite_refused(self):
        from lens_views import lens_weights_errors
        for bad in (float("nan"), float("inf"), float("-inf")):
            errs, _ = lens_weights_errors({"SPY": bad, "QQQ": 0.3, "DIA": 0.2}, self.D, self.LS, None, set())
            self.assertTrue(any("lens SPY" in e and "finite" in e for e in errs), (bad, errs))

    def test_exactly_one_tenth_needs_no_reason(self):
        from lens_views import lens_weights_errors
        errs, lam = lens_weights_errors({"SPY": 0.4, "QQQ": 0.4, "DIA": 0.2}, self.D, self.LS, None, set())
        self.assertEqual(errs, [])
        errs, _ = lens_weights_errors({"SPY": 0.39, "QQQ": 0.41, "DIA": 0.2}, self.D, self.LS, None, set())
        self.assertTrue(any("lens_weights_reason" in e for e in errs), errs)


# ---------------------------------------------------------------------------------------------------------------- G8

class CorpusAndMaterialsTest(unittest.TestCase):
    def test_debate_format_selects_the_mode_before_the_example(self):
        # wave 3: debate.md keeps one signpost before its example; the mode selection lives in formats/lenses.md
        t = (ROOT / "prompts" / "agent" / "formats" / "debate.md").read_text(encoding="utf-8")
        self.assertLess(t.index("When a LENSES block is present, first read the rules file it names"), t.index("```json"))
        self.assertIn("`applies: null` is only for an independent increment", t)
        L = (ROOT / "prompts" / "agent" / "formats" / "lenses.md").read_text(encoding="utf-8")
        sel = L.index("Replace the entire `scenario` field with `scenario_by_lens`")
        self.assertLess(sel, L.index("```json"))
        self.assertIn("The opening round still writes `scenario_updates: []`", L[sel:L.index("```json")])

    def test_operators_deploy_lists_name_the_entry_point(self):
        t = (ROOT / "docs" / "handoff" / "V34-OPERATORS-2026-10-31.md").read_text(encoding="utf-8")
        archive = next(l for l in t.splitlines() if "archive $C --" in l)
        self.assertIn("equipoise_entry.py", archive)
        files = next(l for l in t.splitlines() if "git ls-tree -r --name-only $C" in l)
        self.assertIn("equipoise_entry.py", files)
        self.assertIn("main example", t)


tlfw = _load("test_lens_final_wave", "scripts/test_lens_final_wave.py")


class StageLensesBlockTest(tlfw._InTmp):
    def test_stage_specific_wording(self):
        s = self.open()
        # wave 3: the block carries one stage reminder; the rules (opening updates, shared predecessor) are in lenses.md
        deb = s._lenses_block("debate")
        self.assertIn("OUTPUT REMINDER: scenario_by_lens; every effect/update op carries lens", deb)
        for stage in ("objection", "signoff"):
            b = s._lenses_block(stage)
            self.assertIn("OUTPUT REMINDER: use this task's base format; every revision op carries lens", b, stage)
            self.assertNotIn("scenario_by_lens", b)
        L = (ROOT / "prompts" / "agent" / "formats" / "lenses.md").read_text(encoding="utf-8")
        self.assertIn("`predecessor` is shared across lenses", L)
        self.assertIn("even when that revision changed a different lens", L)
        self.assertIn("no full scenario of any kind", L)

    def test_objection_and_signoff_material_use_their_block(self):
        tlf = tlfw.F
        out = tlf.drive_lens_session(self.tmp, until="ruling")
        s = out["session"]
        for text in ((self.tmp / s.objection_material("Zealot", 1)).read_text(encoding="utf-8"),
                     (self.tmp / s.signoff_material("Reaper", 1)).read_text(encoding="utf-8")):
            self.assertIn("--- LENSES", text)
            self.assertIn("every revision op carries lens", text)
            self.assertNotIn("scenario_by_lens", text)


if __name__ == "__main__":
    unittest.main()
