# -*- coding: utf-8 -*-
"""Task 9 (C11.10): the divergent three-lens fixture end to end, and the lens invariants on the whole flow.

One session (test_lens_fixtures: SPY HOLD_NEUTRAL / QQQ ENTRY / DIA TRIM_POSITION) runs open -> rounds -> objection ->
ruling -> signoff -> release gate in `setUpClass`; the invariant tests each drive their own session from a transformed
script (or a transformed root) and compare it with that base run:
- reflection (w = 0.5 in place; w = 0.65 with the sides swapped, see static_script): every lens's scenarios mirrored about that lens's P0 on both sides (executions mirrored with them) and
  w -> 1 - w: every lens's operation mirrors (ENTRY <-> TRIM_POSITION, HOLD_NEUTRAL fixed), x* and size kept, composite
  -> 100 - composite within rounding;
- permutation: the session's lens list reordered -> every by_lens value unchanged; only the display string and the
  primary-lens compatibility fields move;
- unit scaling: every DIA price x10 -> DIA's operation, x* and score unchanged, its level x10; SPY / QQQ untouched;
- a lens-scoped void skips a failing op on QQQ only;
- postcheck: compute_window_digest on the fixture's analysis with synthetic bars per lens -> three operation members.
PG is not exercised here (pg_store_debate re-runs this same gate before writing)."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import test_lens_fixtures as F
import validate_debate as vd          # noqa: E402
from test_odds_model import reflect_scenario, reflect_execution   # noqa: E402

MIRROR = {"ENTRY": "TRIM_POSITION", "TRIM_POSITION": "ENTRY", "HOLD_NEUTRAL": "HOLD_NEUTRAL"}


def family(op: str) -> str:
    return str(op).replace("MARKET_", "").replace("LIMIT_", "")


def _walk(obj, fn):
    """Apply fn to every dict of a JSON-like tree (in place, depth first)."""
    if isinstance(obj, dict):
        fn(obj)
        for v in obj.values():
            _walk(v, fn)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, fn)


def _mirror_sc(sc: dict) -> dict:
    out = reflect_scenario(copy.deepcopy(sc))
    ex = reflect_execution(sc.get("execution") or {}, float(sc["P0"]))
    out["execution"] = {k: v for k, v in ex.items() if v}
    return out


def mirrored_script() -> dict:
    """The fixture script with every lens's scenario mirrored about its P0 on both sides and w -> 1 - w. Ops name node
    ids, so they mirror with the prices (moving p towards A is bullish before, bearish after)."""
    script = F.lens_script()

    def fn(d):
        if isinstance(d.get("scenario_by_lens"), dict):
            d["scenario_by_lens"] = {L: _mirror_sc(sc) for L, sc in d["scenario_by_lens"].items()}
        if isinstance(d.get("mixture"), dict) and isinstance(d["mixture"].get("w"), (int, float)):
            d["mixture"]["w"] = round(1 - d["mixture"]["w"], 12)
    _walk(script, fn)
    return script


def static_script(w: float, z_by: dict, r_by: dict) -> dict:
    """The fixture flow with scenarios that do not move during the rounds (R1's challenge registers no effect, R carries
    no update), Z filing `z_by` and R `r_by` on every lens, and the arbiter's mixture weight `w`. With nothing moving,
    the sides can be swapped: the exact mirror of (Z, R, w) is (refl R, refl Z, 1 - w), since
    A = w Z' + (1 - w) R' -> (1 - w) refl R' + w refl Z' = refl A."""
    script = F.lens_script()
    r_c1 = dict(F.R_C1, effect={"if_true": [], "if_false": []})
    script["beat_zealot_r1_b1"][0]["scenario_by_lens"] = copy.deepcopy(z_by)
    for k in ("beat_zealot_r1_b2", "beat_zealot_r2_b1"):
        script[k][0]["scenario_by_lens"] = copy.deepcopy(z_by)
    script["beat_reaper_r1_b1"] = [F.lens_final(1, [r_c1, F.R_C2], r_by)]
    script["beat_reaper_r2_b1"] = [F.lens_final(2, [dict(r_c1, status="withdrawn"), F.R_C2_DEAD], r_by, [F.UPD_R2])]
    for k in ("provisional", "proposed", "ruling"):
        script[k][0]["mixture"] = {"w": w, "reason": None}
    return script


def scaled_script(lens: str, k: float) -> dict:
    """The fixture script with every price of `lens` (P0, P_T, event prices, execution levels) xk."""
    script = F.lens_script()

    def sc_scale(sc):
        sc["P0"] = sc["P0"] * k
        for n in sc["nodes"]:
            n["P_T"] = n["P_T"] * k
            for e in n.get("events") or []:
                e["price"] = e["price"] * k
        for side in (sc.get("execution") or {}).values():
            if isinstance(side, dict) and isinstance(side.get("stop"), dict):
                side["stop"]["level"] = side["stop"]["level"] * k
            assert not (side or {}).get("conditional"), "scale conditional levels too"

    def fn(d):
        if isinstance(d.get("scenario_by_lens"), dict) and lens in d["scenario_by_lens"]:
            sc_scale(d["scenario_by_lens"][lens])
    _walk(script, fn)
    return script


GAP_CLOSE, GAP_OPEN = 319.0, 326.0   # DIA: a close beyond R's underweight stop (318, close semantics), then a gap open


def gap_script() -> dict:
    """The fixture with DIA's node A (both sides, every final) carrying a close at 319 on day 2 — beyond the underweight
    stop at 318 — and a gap open at 326 on day 3: the close-semantics stop exits at the gap price, not at the level."""
    script = F.lens_script()

    def fn(d):
        sbl = d.get("scenario_by_lens")
        if isinstance(sbl, dict) and "DIA" in sbl:
            for n in sbl["DIA"]["nodes"]:
                if n["id"] == "A":
                    n["events"] = [{"t": 2, "kind": "close", "price": GAP_CLOSE},
                                   {"t": 3, "kind": "gap_open", "price": GAP_OPEN},
                                   {"t": 15, "kind": "close", "price": n["P_T"]}]
    _walk(script, fn)
    return script


def void_script(scope, spy_ok: bool = False) -> dict:
    """The fixture with R's challenge R1 registering a failing op on QQQ only (no node D on QQQ) and R no longer carrying
    Z1#true by an update: Z1 settles for Z, so the effect is pending on R at replay and fails its precondition on QQQ.
    The arbiter voids it with `lens: scope` (None: no lens key = every lens the effect touches; False: no void at all).
    `spy_ok`: the effect also carries an executable op on SPY."""
    script = F.lens_script()
    broken = F.tag([{"op": "p_move", "from": "D", "to": "A", "amount": 0.10}], "QQQ")
    if spy_ok:
        broken += F.tag([F.MOVE], "SPY")
    r_c1 = dict(F.R_C1, effect=dict(F.R_C1["effect"], if_true=broken))
    script["beat_reaper_r1_b1"] = [F.lens_final(1, [r_c1, F.R_C2], F.R_BY)]
    script["beat_reaper_r2_b1"] = [F.lens_final(2, [dict(r_c1, status="withdrawn"), F.R_C2_DEAD], F.R_BY, [F.UPD_R2])]
    if scope is not False:
        v = {"claim": "Z1", "side": "R", "outcome": "true", "reason": "R1 names node D, merged away on QQQ"}
        if scope is not None:
            v["lens"] = scope
        for k in ("provisional", "proposed", "ruling"):
            script[k][0]["voided"] = {"effects": [dict(v)]}
    return script


def run(script=None, until="signoff", **kw):
    """Drive one session in a fresh temp dir; -> (out, analysis doc or None, tmp). out["fatal"] = the release gate's
    FATALs on the analysis (re-run here, inside tmp)."""
    tmp = Path(tempfile.mkdtemp(prefix="lens_flow_"))
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        out = F.drive_lens_session(tmp, script, until=until, **kw)
        rc = out["results"].get("signoff") or {}
        doc = (json.loads((tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
               if rc.get("analysis_path") else None)
        out["session_json"] = json.loads(out["session"].state_path.read_text(encoding="utf-8"))
        out["meta"] = out["session"]._meta() if doc is not None else None
        out["fatal"] = vd.validate(copy.deepcopy(doc))[0] if doc is not None else None   # data_files resolve under tmp
    finally:
        os.chdir(cwd)
    return out, doc, tmp


def _clean(out, tmp, test):
    shutil.rmtree(tmp, ignore_errors=True)
    test.assertEqual(out["errors"], {}, "\n".join(out["tracebacks"].values()) or out["errors"])


class LensFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.doc, cls.tmp = run()
        if cls.out["errors"]:   # an unexpected exception or refusal anywhere in the base run fails the whole class
            raise AssertionError(f"base lens session failed: {cls.out['errors']}\n" + "\n".join(cls.out["tracebacks"].values()))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def by(self, doc=None):
        return (doc or self.doc)["by_lens"]

    # ---------------------------------------------------------------- end to end
    def test_end_to_end_divergent(self):
        self.assertEqual(self.out["errors"], {})
        self.assertEqual(self.out["tracebacks"], {})
        for st in ("rounds", "provisional", "proposed", "objection", "ruling", "signoff"):
            self.assertIn(st, self.out["results"], st)
        self.assertEqual(self.out["results"]["rounds"]["round"], 2)
        self.assertTrue(self.out["results"]["signoff"]["gate"]["ok"])
        self.assertEqual(self.out["fatal"], [])
        b = self.by()
        self.assertEqual(list(b), F.LENSES)
        self.assertEqual({L: family(b[L]["operation_type"]) for L in F.LENSES}, F.EXPECTED_OPS)
        self.assertEqual({L: round(b[L]["s_ref"], 1) for L in F.LENSES}, F.EXPECTED_S_REF)
        self.assertEqual(self.doc["composite_score"], 52)
        self.assertEqual(self.doc["operation_target"], "SPY=500; QQQ=400; DIA=300")
        self.assertEqual(self.doc["lenses"], F.LENSES)
        odds = self.out["session_json"]["odds"]
        self.assertEqual(list(odds["by_lens"]), F.LENSES)
        for L in F.LENSES:
            self.assertEqual(odds["by_lens"][L]["reference_price"], F.REF[L], L)
        self.assertEqual(self.out["meta"]["lenses"], F.LENSES)
        self.assertEqual(self.out["meta"]["lens_weights_default"], F.WEIGHTS)
        self.assertEqual(self.out["session_json"]["status"], "done")

    # ---------------------------------------------------------------- invariants
    def test_reflection_per_lens(self):
        out, doc, tmp = run(mirrored_script())
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        for L in F.LENSES:
            a, m = self.by()[L], self.by(doc)[L]
            self.assertEqual(family(m["operation_type"]), MIRROR[family(a["operation_type"])], L)
            self.assertEqual(m["operation_size"], a["operation_size"], L)
            self.assertAlmostEqual(m["scenario"]["derived"]["x_star"], a["scenario"]["derived"]["x_star"], places=6, msg=L)
            self.assertAlmostEqual(m["s_ref"], 100 - a["s_ref"], places=6, msg=L)
            self.assertLessEqual(abs(m["composite_score"] - (100 - a["composite_score"])), 1, L)
            self.assertAlmostEqual(m["operation_target"], 2 * F.REF[L] - a["operation_target"], places=6, msg=L)
            self.assertEqual(m["scenario"]["mixture"]["w"], 1 - a["scenario"]["mixture"]["w"], L)
        self.assertLessEqual(abs(doc["composite_score"] - (100 - self.doc["composite_score"])), 1)

    def test_reflection_per_lens_tilted_w(self):
        """w = 0.65 (no reason needed: |2w - 1| <= 0.5). Mirror = sides swapped with every scenario reflected about its
        lens's P0, and w -> 0.35."""
        base, bdoc, btmp = run(static_script(0.65, F.Z_BY, F.R_BY))
        _clean(base, btmp, self)
        self.assertEqual(base["fatal"], [])
        mz = {L: _mirror_sc(sc) for L, sc in F.R_BY.items()}
        mr = {L: _mirror_sc(sc) for L, sc in F.Z_BY.items()}
        out, doc, tmp = run(static_script(0.35, mz, mr))
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        for L in F.LENSES:
            a, m = bdoc["by_lens"][L], doc["by_lens"][L]
            self.assertEqual(a["scenario"]["mixture"]["w"], 0.65, L)
            self.assertEqual(m["scenario"]["mixture"]["w"], 0.35, L)
            self.assertEqual(family(m["operation_type"]), MIRROR[family(a["operation_type"])], L)
            self.assertEqual(m["operation_size"], a["operation_size"], L)
            self.assertAlmostEqual(m["scenario"]["derived"]["x_star"], a["scenario"]["derived"]["x_star"], places=6, msg=L)
            self.assertAlmostEqual(m["s_ref"], 100 - a["s_ref"], places=6, msg=L)
            self.assertLessEqual(abs(m["composite_score"] - (100 - a["composite_score"])), 1, L)
            self.assertAlmostEqual(m["operation_target"], 2 * F.REF[L] - a["operation_target"], places=6, msg=L)
        self.assertLessEqual(abs(doc["composite_score"] - (100 - bdoc["composite_score"])), 1)
        # non-trivial: at w 0.65 SPY tips to ENTRY, so the mirror carries both directions (TRIM / TRIM / ENTRY)
        self.assertEqual({L: family(bdoc["by_lens"][L]["operation_type"]) for L in F.LENSES},
                         {"SPY": "ENTRY", "QQQ": "ENTRY", "DIA": "TRIM_POSITION"})

    def test_lens_permutation_only_reorders(self):
        order = ["QQQ", "SPY", "DIA"]
        out, doc, tmp = run(lenses=order)
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        self.assertEqual(doc["lenses"], order)
        self.assertEqual(list(doc["by_lens"]), order)
        for L in F.LENSES:
            self.assertEqual(doc["by_lens"][L], self.by()[L], L)
        self.assertEqual(doc["operation_target"], "QQQ=400; SPY=500; DIA=300")
        q = doc["by_lens"]["QQQ"]
        for k in ("operation_type", "operation_size", "operation_execution", "scenario"):
            self.assertEqual(doc[k], q[k], k)
        self.assertEqual(doc["odds"]["unit_basis"]["data_symbol"], "QQQ")
        self.assertEqual(doc["composite_score"], self.doc["composite_score"])
        self.assertEqual(doc["lens_weights"], self.doc["lens_weights"])
        self.assertEqual(doc["lens_weights_default"], self.doc["lens_weights_default"])
        for k in ("s_ref", "s_ref_z", "s_ref_r"):
            self.assertAlmostEqual(doc[k], self.doc[k], places=6, msg=k)

    def test_unit_scaling_one_lens(self):
        out, doc, tmp = run(scaled_script("DIA", 10.0), ref={"DIA": F.REF["DIA"] * 10})
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        a, s = self.by()["DIA"], self.by(doc)["DIA"]
        self.assertEqual(s["operation_type"], a["operation_type"])
        self.assertEqual(s["operation_size"], a["operation_size"])
        self.assertAlmostEqual(s["scenario"]["derived"]["x_star"], a["scenario"]["derived"]["x_star"], places=6)
        self.assertEqual(s["composite_score"], a["composite_score"])
        self.assertAlmostEqual(s["s_ref"], a["s_ref"], places=6)
        self.assertAlmostEqual(s["operation_target"], a["operation_target"] * 10, places=6)
        self.assertAlmostEqual(s["operation_execution"]["stop"]["level"], a["operation_execution"]["stop"]["level"] * 10,
                               places=6)
        for L in ("SPY", "QQQ"):
            self.assertEqual(self.by(doc)[L], self.by()[L], L)
        self.assertEqual(doc["operation_target"], "SPY=500; QQQ=400; DIA=3000")
        self.assertEqual(doc["composite_score"], self.doc["composite_score"])

    def test_gap_through_stop_on_one_lens(self):
        """A7: a gap through DIA's stop changes DIA's derived execution and score input only; SPY and QQQ stay whole-dict
        equal, and the session passes the gate."""
        import odds_model as om
        out, doc, tmp = run(gap_script())
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        self.assertTrue(out["results"]["signoff"]["gate"]["ok"])
        for L in ("SPY", "QQQ"):
            self.assertEqual(self.by(doc)[L], self.by()[L], L)
        a, g = self.by()["DIA"], self.by(doc)["DIA"]
        stop = a["operation_execution"]["stop"]
        self.assertEqual(stop, {"level": 318.0, "semantics": "close"})
        sc = g["scenario"]
        leaf = next(n for n in sc["nodes"] if any(e["kind"] == "gap_open" for e in n["events"]))   # A's merged node
        sim = om.simulate_leaf(leaf, "underweight_now", {"stop": stop}, sc)
        self.assertTrue(sim["stopped"])
        self.assertEqual(sim["exit_price"], GAP_OPEN)   # the buy-back is at the gap price, past the level
        self.assertNotEqual(g["scenario"]["derived"]["x_star"], a["scenario"]["derived"]["x_star"])
        self.assertNotEqual((g["operation_type"], g.get("operation_execution")),
                            (a["operation_type"], a.get("operation_execution")))
        self.assertEqual(g["scenario"]["derived"]["operation_type"], g["operation_type"])

    def test_non_zero_cost_per_lens(self):
        """A8: distinct ATR and ADV per lens -> every lens is derived with its own cost and unit (never the primary's)."""
        out, doc, tmp = run(zero_cost=False, adv={"SPY": 400.0, "QQQ": 100.0, "DIA": 64.0})
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        odds = out["session_json"]["odds"]
        costs, units = {}, {}
        for L in F.LENSES:
            d = doc["by_lens"][L]["scenario"]["derived"]
            costs[L], units[L] = d["cost_bps"], d["unit_basis"]["s_s"]
            self.assertGreater(costs[L], 0, L)
            self.assertEqual(costs[L], odds["by_lens"][L]["cost_bps"], L)
            self.assertEqual(units[L], odds["by_lens"][L]["s_s"], L)
            self.assertAlmostEqual(units[L], F.s_s_of(L), places=6, msg=L)
        self.assertEqual(len(set(costs.values())), 3, costs)
        self.assertEqual(len(set(units.values())), 3, units)
        self.assertEqual(odds["cost_bps"], costs["SPY"])   # the top level is the primary lens's

    # ---------------------------------------------------------------- postcheck
    def test_postcheck_of_the_fixture(self):
        import pandas as pd
        from workflows import postcheck_quant as pq
        b = self.by()
        root = Path(tempfile.mkdtemp(prefix="lens_flow_pc_"))
        try:
            dates = pd.bdate_range(start="2026-09-01", periods=45)
            n_before = int((dates < pd.Timestamp("2026-09-30")).sum())
            for L in F.LENSES:
                P0 = F.REF[L]
                # flat into T; after T a 3 % dip over five sessions, then a recovery
                path = [P0] * n_before + [P0 * (1 - 0.006 * i) for i in range(6)] + \
                       [P0 * (0.97 + 0.004 * i) for i in range(len(dates) - n_before - 6)]
                d = root / L
                d.mkdir()
                pd.DataFrame({"date": dates, "open": path, "high": [c * 1.005 for c in path],
                              "low": [c * 0.995 for c in path], "close": path,
                              "volume": [1000] * len(dates)}).to_csv(d / f"{L}_5y_1d_20261101T000000Z.csv", index=False)
            old = pq.MARKET_DATA_ROOT
            pq.MARKET_DATA_ROOT = root
            try:
                dg = pq.compute_window_digest("GENERAL", self.doc["market_data_timestamp_utc"], 20, copy.deepcopy(self.doc))
            finally:
                pq.MARKET_DATA_ROOT = old
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertTrue(dg.get("lenswise"), dg)
        self.assertEqual(dg["primary_lens"], "SPY")
        by = {m["symbol"]: m for m in dg["members"]}
        self.assertEqual(list(by), F.LENSES)
        for L, m in by.items():
            self.assertNotIn("error", m, (L, m))
            self.assertEqual(m["role"], "operation", L)
            self.assertEqual(m["weight"], F.WEIGHTS[L], L)
            self.assertIn("forecast", m, L)
            self.assertNotIn("error", m["forecast"], (L, m["forecast"]))
            self.assertEqual(m["forecast"]["P0"], F.REF[L], L)
        # QQQ's MARKET_ENTRY executes at the reference price by definition: no resting-order fill to settle (R25, as
        # for any market entry); its level is the member's directional anchor and the immediate-entry row is there
        self.assertNotIn("entry", by["QQQ"])
        self.assertNotIn("trim_exec", by["QQQ"])
        self.assertEqual(by["QQQ"]["target"], b["QQQ"]["operation_target"])
        self.assertIn("alternatives", by["QQQ"])
        # DIA's TRIM_POSITION at its own level (300) executes on the first session after T
        self.assertEqual(by["DIA"]["trim_exec"]["exec_price"], b["DIA"]["operation_target"])
        self.assertIs(by["DIA"]["trim_exec"]["exec_price_reached"], True)
        self.assertNotIn("entry", by["DIA"])
        self.assertNotIn("entry", by["SPY"])
        self.assertNotIn("trim_exec", by["SPY"])
        self.assertEqual((by["QQQ"]["direction_call"], by["DIA"]["direction_call"]), ("看多", "看空"))
        self.assertEqual(dg["session_score_weights"], F.WEIGHTS)


class LensVoidTest(unittest.TestCase):
    """C11.4 positive case: a void scoped to one lens skips the failing op there only."""

    def test_unvoided_failing_op_names_qqq_only(self):
        out, _doc, tmp = run(void_script(False), until="provisional")
        shutil.rmtree(tmp, ignore_errors=True)
        err = out["errors"].get("provisional", "")
        self.assertEqual(out["tracebacks"], {})
        self.assertIn("lens QQQ:", err)
        self.assertIn("Z1#true", err)
        self.assertNotIn("lens SPY:", err)
        self.assertNotIn("lens DIA:", err)

    def test_unscoped_void_applies_on_the_lenses_the_effect_touches(self):
        """A3: the effect has ops on QQQ only and fails there: an unscoped void is passed to QQQ alone and accepted
        (the same report as the void scoped to QQQ), through the release gate."""
        out, doc, tmp = run(void_script(None))
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        scoped, sdoc, stmp = run(void_script("QQQ"))
        _clean(scoped, stmp, self)
        for L in F.LENSES:
            self.assertEqual(doc["by_lens"][L], sdoc["by_lens"][L], L)
        q = doc["by_lens"]["QQQ"]["scenario"]["derived"]["replay"]["R"]
        self.assertEqual([a["effect"] for a in q if a["status"] == "voided"], ["Z1#true"])

    def test_unscoped_void_refused_where_the_effect_executes(self):
        """A3: the effect also has an executable op on SPY: the unscoped void applies on SPY too and is refused there,
        naming SPY; DIA (no ops) is not tried."""
        out, _doc, tmp = run(void_script(None, spy_ok=True), until="provisional")
        shutil.rmtree(tmp, ignore_errors=True)
        err = out["errors"].get("provisional", "")
        self.assertEqual(out["tracebacks"], {})
        self.assertIn("lens SPY:", err)
        self.assertIn("effect is executable", err)
        self.assertNotIn("lens QQQ:", err)
        self.assertNotIn("lens DIA:", err)

    def test_void_scoped_to_qqq_skips_there_only(self):
        base, bdoc, btmp = run()
        _clean(base, btmp, self)
        out, doc, tmp = run(void_script("QQQ"))
        _clean(out, tmp, self)
        self.assertEqual(out["fatal"], [])
        q = doc["by_lens"]["QQQ"]["scenario"]
        voided = [a for a in q["derived"]["replay"]["R"] if a["status"] == "voided"]
        self.assertEqual(len(voided), 1, q["derived"]["replay"]["R"])
        self.assertEqual(voided[0]["effect"], "Z1#true")
        self.assertIn("D", voided[0]["failed"])
        # R' on QQQ is R's filed scenario, the failing op skipped (the redraft keeps the ruling: no revisions replayed)
        rq = {n["id"]: n["p"] for n in q["mixture"]["r_prime"]["nodes"]}
        self.assertEqual(rq, {n["id"]: n["p"] for n in F.R_BY["QQQ"]["nodes"]})
        for L in ("SPY", "DIA"):
            s, b0 = doc["by_lens"][L], bdoc["by_lens"][L]
            self.assertFalse([a for a in s["scenario"]["derived"]["replay"]["R"] if a["status"] == "voided"], L)
            for k in ("z_prime", "r_prime"):
                self.assertEqual(s["scenario"]["mixture"][k], b0["scenario"]["mixture"][k], (L, k))
            for k in ("operation_type", "operation_target", "operation_size", "composite_score", "s_ref"):
                self.assertEqual(s[k], b0[k], (L, k))


if __name__ == "__main__":
    unittest.main()
