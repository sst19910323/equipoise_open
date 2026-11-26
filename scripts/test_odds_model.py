"""odds_model schema 3.4 (contracts C4 / C7, proposal-ruler_era §1.4, §3, §5 e/h/i/j/k): single neutral reference q_ref = 0.5,
deviation d in [-0.5, 0.5], five symmetric strategies on normalised net path payoffs, candidate union of both sides'
execution contracts, observable cost, session snapshot. Pure functions; no PG, no model calls.

Module-level helpers (importable by later tasks): CFG, leaf_events, ruler_scenario, two_node, reflect_scenario,
reflect_execution, worked_example_A."""
import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("odds_model", ROOT / "scripts" / "odds_model.py")
om = importlib.util.module_from_spec(spec); spec.loader.exec_module(om)

CFG = dict(om.DEFAULT_CONFIG); CFG["gamma_sensitivity"] = []
P0 = 100.0


def leaf_events(*pairs):
    """pairs of (t, kind, price) → event list."""
    return [{"t": t, "kind": k, "price": float(p)} for t, k, p in pairs]


def ruler_scenario(nodes, P0=P0, execution=None, event=None):
    """nodes = [(id, p, P_T, events or None)] → schema-3.4 scenario (no baseline, no cost_bps)."""
    sc = {"schema": "3.4", "T_days": 15, "P0": P0,
          "nodes": [{"id": i, "p": p, "P_T": float(pt), "refs": ["Z1"],
                     "events": ev if ev is not None else leaf_events((15, "close", pt))} for i, p, pt, ev in nodes],
          "execution": execution or {}}
    if event:
        sc["event"] = event
    return sc


def two_node(p_up, up, down, P0=P0, up_events=None, down_events=None):
    return ruler_scenario([("U", p_up, up, up_events), ("D", 1 - p_up, down, down_events)], P0=P0)


def _refl(price, P0=P0):
    return 2 * P0 - float(price)


_OP_REFL = {">": "<", "<": ">", ">=": "<=", "<=": ">="}


def reflect_scenario(sc):
    """Reflect every price about P0 (P_T and event prices); kinds and days unchanged."""
    P0_ = float(sc["P0"])
    out = dict(sc)
    out["nodes"] = [dict(n, P_T=_refl(n["P_T"], P0_),
                         events=[dict(e, price=_refl(e["price"], P0_)) for e in n.get("events") or []]) for n in sc["nodes"]]
    return out


def reflect_execution(ex, P0_=P0):
    """overweight ↔ underweight, levels reflected, comparison operators flipped."""
    def refl_dir(d):
        if not d:
            return d
        o = {}
        if d.get("stop"):
            o["stop"] = dict(d["stop"], level=_refl(d["stop"]["level"], P0_))
        else:
            o["stop"] = None
        conds = []
        for c in d.get("conditional") or []:
            kind, op, lvl = c["observe"].split()
            conds.append(dict(c, observe=f"{kind} {_OP_REFL[op]} {_refl(lvl, P0_):g}"))
        o["conditional"] = conds
        return o
    return {"overweight": refl_dir(ex.get("underweight")), "underweight": refl_dir(ex.get("overweight"))}


def worked_example_A():
    """Proposal §1.4 at w = 0.5: A = ½Z + ½R′ on six atoms (Z: 0.55→110, 0.30→100, 0.15→90; R′: 0.35→108, 0.35→98, 0.30→88)."""
    z = [(0.55, 110), (0.30, 100), (0.15, 90)]
    r = [(0.35, 108), (0.35, 98), (0.30, 88)]
    nodes = [(f"Z{i}", 0.5 * p, pt, None) for i, (p, pt) in enumerate(z)] + \
            [(f"R{i}", 0.5 * p, pt, None) for i, (p, pt) in enumerate(r)]
    return ruler_scenario(nodes)


def _cfg(**kw):
    c = dict(CFG); c.update(kw); return c


# ----------------------------------------------------------------------------------------------------------------

class ConfigAndSnapshotTest(unittest.TestCase):
    def test_default_config(self):
        self.assertEqual(om.DEFAULT_CONFIG["schema"], "3.4")
        self.assertEqual(om.DEFAULT_CONFIG["gamma"], 6.25)
        self.assertEqual(om.DEFAULT_CONFIG["sigma_ref"], 0.08)
        self.assertEqual(om.DEFAULT_CONFIG["cost"]["base_bps"], {"US": 2, "EU": 4, "JP": 4, "KR": 5})
        self.assertNotIn("tie_order", om.DEFAULT_CONFIG)
        for gone in ("DEFAULT_CONFIG_34", "load_config_34", "FLAT_STRATEGIES", "binary_closed_form"):
            self.assertFalse(hasattr(om, gone), gone)

    def test_yaml_is_34(self):
        cfg = om.load_config(ROOT)
        self.assertEqual(cfg["schema"], "3.4"); self.assertEqual(cfg["gamma"], 6.25); self.assertEqual(cfg["sigma_ref"], 0.08)
        self.assertEqual(cfg["cost"]["adr_surcharge_bps"], 3); self.assertEqual(cfg["cost"]["k"], 10)
        self.assertEqual(cfg["cost"]["fx_to_usd"]["KRW"], 0.00072)
        self.assertNotIn("tie_order", cfg); self.assertEqual(cfg["coverage_nominal"], 0.8)

    def test_snapshot_round_trip(self):
        c, basis = om.cost_bps_for(3.1, 30000.0, "US", False, CFG)
        snap = om.snapshot_from_config(CFG, cost_bps=c, unit_basis=basis, commit="abc123")
        for k in ("schema", "T_days", "gamma", "sigma_ref", "p_tolerance", "z_flag", "tie_threshold", "size_display_bounds",
                  "size_enum", "baseline", "coverage_nominal", "cost", "cost_bps", "unit_basis", "provenance"):
            self.assertIn(k, snap)
        self.assertEqual(snap["provenance"], {"commit": "abc123"})
        self.assertAlmostEqual(snap["unit_basis"]["s_s"], 0.031 * math.sqrt(15), 6)
        cfg = om.config_from_snapshot(snap)
        self.assertEqual(cfg["gamma"], 6.25); self.assertEqual(cfg["cost_bps"], c); self.assertEqual(cfg["schema"], "3.4")


class CostTest(unittest.TestCase):
    def test_formula_examples(self):
        self.assertAlmostEqual(om.cost_bps_for(3.0, 30000, "US", False, CFG)[0], 2.17, 2)
        self.assertAlmostEqual(om.cost_bps_for(4.0, 50, "US", False, CFG)[0], 7.66, 2)
        self.assertAlmostEqual(om.cost_bps_for(4.0, 50, "KR", False, CFG)[0], 10.66, 2)
        self.assertAlmostEqual(om.cost_bps_for(4.0, 50, "US", True, CFG)[0], 10.66, 2)   # ADR surcharge 3
        c, basis = om.cost_bps_for(3.0, 30000, "US", False, CFG)
        self.assertEqual(basis["venue"], "US"); self.assertFalse(basis["adr"]); self.assertNotIn("fallback", basis)
        self.assertIn("reference order size", basis["reference_size"])

    def test_fallback_when_adv_or_atr_missing(self):
        c, basis = om.cost_bps_for(3.0, None, "EU", False, CFG)
        self.assertEqual(c, 9.0); self.assertIn("adv", basis["fallback"])
        c, basis = om.cost_bps_for(None, 100.0, "US", False, CFG)
        self.assertEqual(c, 7.0); self.assertIn("atr", basis["fallback"]); self.assertIsNone(basis["s_s"])
        c, basis = om.cost_bps_for(3.0, 0.0, "JP", False, CFG)
        self.assertEqual(c, 9.0); self.assertIn("fallback", basis)


# ----------------------------------------------------------------------------------------------------------------

class ExecutionValidityTest(unittest.TestCase):
    def setUp(self):
        self.sc = two_node(0.5, 110, 90, up_events=leaf_events((3, "close", 104), (4, "open", 104.5), (15, "close", 110)),
                           down_events=leaf_events((2, "touch", 95), (15, "close", 90)))

    def test_cost_bps_is_fatal(self):
        fatal, _ = om.validate_execution({"cost_bps": 5, "overweight": {}}, self.sc)
        self.assertTrue(any("cost_bps" in f for f in fatal))

    def test_invalidities(self):
        ex = {"overweight": {"stop": {"level": 96, "semantics": "close"},
                             "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"},
                                             {"name": "far", "observe": "close > 150", "fill": "next_open"},
                                             {"name": "junk", "observe": "breaks out", "fill": "next_open"},
                                             {"name": "ev", "observe": "close > 103", "fill": "next_open", "event": "FOMC"}]},
              "underweight": {"stop": {"level": 97, "semantics": "touch"}, "conditional": []}}
        fatal, raw = om.validate_execution(ex, self.sc)
        self.assertEqual(fatal, [])
        by = {(r["strategy"], r.get("name")): r["invalid"] for r in raw}
        self.assertEqual(by[("overweight_on_condition", "brk")], [])
        self.assertEqual(by[("overweight_on_condition", "far")], ["level_unreachable"])
        self.assertEqual(by[("overweight_on_condition", "junk")], ["unparseable"])
        self.assertEqual(by[("overweight_on_condition", "ev")], ["unknown_event"])
        self.assertEqual(by[("overweight_now", None)], [])
        self.assertEqual(by[("underweight_now", None)], ["stop_wrong_side"])   # buy-back stop below P0


class CandidatesTest(unittest.TestCase):
    """(h) one-sided contracts, same entry with different stops, voiding only for documented invalidity."""
    def setUp(self):
        self.sc = two_node(0.5, 110, 90, up_events=leaf_events((3, "close", 104), (4, "open", 104.5), (15, "close", 110)))

    def test_one_sided(self):
        z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"}]}}
        cands = om.build_candidates(z, {}, self.sc, [])
        names = [c["strategy"] for c in cands]
        self.assertIn("overweight_now", names); self.assertIn("underweight_now", names)
        self.assertEqual(names.count("overweight_on_condition"), 1); self.assertNotIn("underweight_on_condition", names)
        now = {c["strategy"]: c for c in cands if c["strategy"].endswith("_now")}
        self.assertIsNone(now["overweight_now"]["stop"]); self.assertIsNone(now["underweight_now"]["stop"])
        cond = [c for c in cands if c["strategy"] == "overweight_on_condition"][0]
        self.assertEqual(cond["provenance"][0]["side"], "Z")

    def test_identical_merge_and_different_stops(self):
        z = {"overweight": {"stop": {"level": 95, "semantics": "close"},
                            "conditional": [{"name": "zb", "observe": "close > 103", "fill": "next_open"}]}}
        r_same = {"overweight": {"stop": {"level": 95, "semantics": "close"},
                                 "conditional": [{"name": "rb", "observe": "close > 103", "fill": "next_open"}]}}
        cands = om.build_candidates(z, r_same, self.sc, [])
        conds = [c for c in cands if c["strategy"] == "overweight_on_condition"]
        self.assertEqual(len(conds), 1); self.assertEqual({p["side"] for p in conds[0]["provenance"]}, {"Z", "R"})
        r_diff = {"overweight": {"stop": {"level": 92, "semantics": "close"},
                                 "conditional": [{"name": "rb", "observe": "close > 103", "fill": "next_open"}]}}
        cands = om.build_candidates(z, r_diff, self.sc, [])
        conds = [c for c in cands if c["strategy"] == "overweight_on_condition"]
        self.assertEqual(len(conds), 2); self.assertEqual(sorted(c["stop"]["level"] for c in conds), [92.0, 95.0])
        now = [c for c in cands if c["strategy"] == "overweight_now"][0]
        self.assertEqual(now["stop"]["level"], 95.0); self.assertEqual(now["stop_source"], "Z")   # closer to P0

    def test_voids(self):
        z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"}]},
             "underweight": {"stop": None, "conditional": [{"name": "bad", "observe": "close < 97", "fill": "next_open"}]}}
        r = {"underweight": {"stop": {"level": 99, "semantics": "close"},
                             "conditional": [{"name": "rs", "observe": "close > 103", "fill": "next_open"}]}}
        first = om.build_candidates(z, r, self.sc, [])
        ow = [c for c in first if c["strategy"] == "overweight_on_condition"][0]
        warn = []
        cands = om.build_candidates(z, r, self.sc, [{"id": ow["id"], "reason": "unprofitable"}], warn=warn)
        self.assertIn(ow["id"], [c["id"] for c in cands])
        self.assertTrue(any("ignored" in w and ow["id"] in w for w in warn))
        # R's underweight conditional fills at ~104 with a stop at 99: stop on the wrong side → invalid, a matching void is honoured
        uw_ids_all = [c["id"] for c in om.build_candidates(z, r, self.sc, [], include_invalid=True)
                      if c["strategy"] == "underweight_on_condition" and "stop_wrong_side" in c.get("invalid", [])]
        self.assertEqual(len(uw_ids_all), 1)
        warn = []
        cands = om.build_candidates(z, r, self.sc, [{"id": uw_ids_all[0], "reason": "stop_wrong_side: stop 99 below fill 103"}], warn=warn)
        self.assertNotIn(uw_ids_all[0], [c["id"] for c in cands])
        self.assertFalse(any("ignored" in w for w in warn))

    def test_near_stop_is_flagged_and_only_a_void_drops_it(self):
        """NOVO 2026-11-09: the immediate entry carried a stop 0.83 ATR away, both debaters objected, and the arbiter had
        no interface to drop it. The script reports the distance; the arbiter's void decides."""
        z = {"overweight": {"stop": {"level": 99, "semantics": "close"}, "conditional": []}}
        r = {"overweight": {"stop": {"level": 95, "semantics": "close"}, "conditional": []}}
        now = lambda cs: [c for c in cs if c["id"] == "overweight_now"][0]  # noqa: E731
        warn = []
        self.assertEqual(now(om.build_candidates(z, r, self.sc, [], warn=warn, atr_abs=2.0))["stop"]["level"], 99.0)
        self.assertTrue(any("overweight_now stop 99 is 0.50 ATR" in w and "stop_too_near" in w for w in warn), warn)
        warn = []
        got = now(om.build_candidates(z, r, self.sc, [{"id": "overweight_now", "reason": "stop_too_near: 0.5 ATR"}],
                                      warn=warn, atr_abs=2.0))
        self.assertEqual((got["stop"]["level"], got["stop_source"]), (95.0, "R"))   # the other side's stop takes its place
        self.assertFalse(any("ignored" in w for w in warn), warn)
        got = now(om.build_candidates(z, {}, self.sc, [{"id": "overweight_now", "reason": "stop_too_near"}], atr_abs=2.0))
        self.assertIsNone(got["stop"])
        warn = []   # a stop that is not near cannot be voided this way; without an ATR nothing is confirmed
        got = now(om.build_candidates({}, r, self.sc, [{"id": "overweight_now", "reason": "stop_too_near"}], warn=warn, atr_abs=2.0))
        self.assertEqual(got["stop"]["level"], 95.0)
        self.assertTrue(any("not confirmed" in w for w in warn), warn)
        got = now(om.build_candidates(z, {}, self.sc, [{"id": "overweight_now", "reason": "stop_too_near"}]))
        self.assertEqual(got["stop"]["level"], 99.0)

    def test_near_stop_on_a_conditional(self):
        z = {"overweight": {"stop": {"level": 102, "semantics": "close"},
                            "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"}]}}
        warn = []
        first = om.build_candidates(z, {}, self.sc, [], warn=warn, atr_abs=2.0)
        cid = [c["id"] for c in first if c["strategy"] == "overweight_on_condition"][0]
        self.assertTrue(any(cid in w and "0.50 ATR" in w for w in warn), warn)
        cands = om.build_candidates(z, {}, self.sc, [{"id": cid, "reason": "stop_too_near"}], atr_abs=2.0)
        self.assertNotIn(cid, [c["id"] for c in cands])


# ----------------------------------------------------------------------------------------------------------------

class SimulationAndCostTest(unittest.TestCase):
    """(j) cost fixture: held pays |d|·c, stopped 2|d|·c, unfilled 0; normalised payoff halves when s_s doubles."""
    def test_leaf_paths(self):
        sc = two_node(0.5, 105, 92, down_events=leaf_events((2, "close", 96), (3, "gap_open", 95.5), (15, "close", 92)))
        cands = om.build_candidates({"overweight": {"stop": {"level": 97, "semantics": "close"}, "conditional": []}}, {}, sc, [])
        now = [c for c in cands if c["strategy"] == "overweight_now"][0]
        leaves = om.expand_leaves(sc)
        held = om.simulate_leaf(leaves[0], "overweight_now", now, sc, CFG)
        stopped = om.simulate_leaf(leaves[1], "overweight_now", now, sc, CFG)
        self.assertEqual(held["n_tx"], 1); self.assertAlmostEqual(held["r_path"], 0.05, 9); self.assertTrue(held["filled"])
        self.assertEqual(stopped["n_tx"], 2); self.assertTrue(stopped["stopped"])
        self.assertAlmostEqual(stopped["r_path"], -0.045, 9)    # exit at the next open after the close stop
        self.assertEqual(stopped["exposure_end"], 0.0)
        # cost charges (s_s = sigma_ref → factor 1)
        w0 = om.leaf_payoff(0.3, held, 0.0, 0.08, 0.08); w20 = om.leaf_payoff(0.3, held, 20.0, 0.08, 0.08)
        self.assertAlmostEqual(w0, 0.015, 12); self.assertAlmostEqual(w0 - w20, 0.3 * 0.002, 12)
        s0 = om.leaf_payoff(0.3, stopped, 0.0, 0.08, 0.08); s20 = om.leaf_payoff(0.3, stopped, 20.0, 0.08, 0.08)
        self.assertAlmostEqual(s0 - s20, 2 * 0.3 * 0.002, 12)
        self.assertAlmostEqual(om.leaf_payoff(0.3, held, 20.0, 0.16, 0.08), w20 / 2, 12)

    def test_unfilled_pays_nothing_and_underweight_buyback(self):
        sc = two_node(0.5, 108, 92, up_events=leaf_events((2, "close", 103), (3, "gap_open", 104), (15, "close", 108)))
        r = {"underweight": {"stop": {"level": 102, "semantics": "close"},
                             "conditional": [{"name": "brk", "observe": "close < 90", "fill": "next_open"}]}}
        cands = om.build_candidates({}, r, sc, [])
        leaves = om.expand_leaves(sc)
        cond = [c for c in cands if c["strategy"] == "underweight_on_condition"]
        self.assertEqual(cond, [])          # close < 90 is unreachable (min path 92) → excluded
        r["underweight"]["conditional"][0]["observe"] = "close < 93"
        sc["nodes"][1]["events"] = leaf_events((5, "close", 92.5), (6, "open", 92.4), (15, "close", 92))
        cands = om.build_candidates({}, r, sc, [])
        cond = [c for c in cands if c["strategy"] == "underweight_on_condition"][0]
        up = om.simulate_leaf(leaves[0], "underweight_on_condition", cond, sc, CFG)
        self.assertFalse(up["filled"]); self.assertEqual(up["n_tx"], 0); self.assertEqual(up["r_path"], 0.0)
        self.assertAlmostEqual(up["avoided"], 0.08, 9)
        self.assertEqual(om.leaf_payoff(-0.4, up, 20.0, 0.08, 0.08), 0.0)
        now = [c for c in cands if c["strategy"] == "underweight_now"][0]
        stopped = om.simulate_leaf(leaves[0], "underweight_now", now, sc, CFG)
        self.assertTrue(stopped["stopped"]); self.assertEqual(stopped["n_tx"], 2)
        self.assertAlmostEqual(stopped["r_path"], 0.04, 9)     # buy back at the post-gap 104
        self.assertAlmostEqual(om.leaf_payoff(-0.5, stopped, 0.0, 0.08, 0.08), -0.02, 12)

    def test_cost_selects_neutral_then_conditional(self):
        c_bps = 20.0      # c = 0.002; μ = +0.001 = 0.5·c
        sc = two_node(0.5, 105.1, 95.1)
        cfg = _cfg(cost_bps=c_bps)
        out = om.compare_strategies(sc, om.build_candidates({}, {}, sc, []), cfg, s_s=0.08)
        self.assertEqual(out["best"], "neutral")
        self.assertEqual(out["x_opt"], 0.0)                       # no action has a positive CE even without the 0.5 cap
        self.assertIn(out["x_opt_strategy"], ("overweight_now", "underweight_now"))
        self.assertEqual(out["x_opt"], out["strategies"][out["x_opt_strategy"]]["x_opt"])
        self.assertLessEqual(out["strategies"]["overweight_now"]["CE"], 0.0)
        sc2 = two_node(0.5, 105.1, 95.1, up_events=leaf_events((1, "close", 102), (2, "open", 102), (15, "close", 105.1)))
        z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 101.5", "fill": "next_open"}]}}
        out = om.compare_strategies(sc2, om.build_candidates(z, {}, sc2, []), cfg, s_s=0.08)
        best = out["strategies"][out["best"]]
        self.assertTrue(out["best"].startswith("overweight_on_condition"))
        self.assertGreater(best["CE"], 0.0); self.assertAlmostEqual(best["x"], 1.0, 6)


class XOptAndWarningsTest(unittest.TestCase):
    def test_x_opt_belongs_to_the_chosen_strategy(self):
        A = worked_example_A()
        out = om.compare_strategies(A, om.build_candidates({}, {}, A, []), _cfg(cost_bps=0.0), s_s=0.08)
        self.assertEqual(out["x_opt_strategy"], out["best"])
        self.assertEqual(out["x_opt"], out["strategies"]["overweight_now"]["x_opt"])
        self.assertAlmostEqual(out["x_opt"], 0.583, delta=0.01)
        # the tie rule selects a strategy other than the CE argmax: x_opt follows the selected one
        sc = two_node(0.9, 115, 95, up_events=leaf_events((1, "close", 103), (2, "open", 103), (15, "close", 115)))
        z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 102", "fill": "next_open"}]}}
        cfg = _cfg(cost_bps=0.0, tie_threshold=1.0)               # every positive action inside the threshold
        out = om.compare_strategies(sc, om.build_candidates(z, {}, sc, []), cfg, s_s=0.08)
        argmax = max(out["strategies"], key=lambda n: out["strategies"][n]["CE"])
        self.assertTrue(out["tie_selected"]); self.assertNotEqual(out["best"], argmax)
        self.assertEqual(out["x_opt_strategy"], out["best"])
        self.assertEqual(out["x_opt"], out["strategies"][out["best"]]["x_opt"])
        d = om.derive_for_report(sc, om.build_candidates(z, {}, sc, []), cfg, s_s=0.08)
        self.assertEqual(d["x_opt_strategy"], d["chosen"])

    def test_missing_cost_bps_warns(self):
        sc = two_node(0.6, 110, 95)
        out = om.compare_strategies(sc, [], CFG, s_s=0.08)
        self.assertTrue(any("cost_bps" in w for w in out["warn"]))
        out = om.compare_strategies(sc, [], _cfg(cost_bps=2.0), s_s=0.08)
        self.assertFalse(any("cost_bps" in w for w in out["warn"]))

    def test_void_with_documented_token_on_valid_contract_is_ignored(self):
        sc = two_node(0.5, 110, 90, up_events=leaf_events((3, "close", 104), (4, "open", 104.5), (15, "close", 110)))
        z = {"overweight": {"stop": {"level": 95, "semantics": "close"},
                            "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"}]}}
        cid = [c["id"] for c in om.build_candidates(z, {}, sc, []) if c["strategy"] == "overweight_on_condition"][0]
        warn = []
        cands = om.build_candidates(z, {}, sc, [{"id": cid, "reason": "level_unreachable"}], warn=warn)
        self.assertIn(cid, [c["id"] for c in cands])
        self.assertTrue(any("ignored" in w and "not confirmed" in w for w in warn))


class TieTest(unittest.TestCase):
    def test_k_action_with_positive_ce_beats_neutral_inside_threshold(self):
        sc = two_node(0.5, 103, 97.2)          # μ = +0.1%, CE of the best action ≈ 1e-4 < tie_threshold
        out = om.compare_strategies(sc, om.build_candidates({}, {}, sc, []), _cfg(cost_bps=0.0), s_s=0.08)
        self.assertEqual(out["best"], "overweight_now")
        self.assertGreater(out["strategies"]["overweight_now"]["CE"], 0.0)
        self.assertLess(out["strategies"]["overweight_now"]["CE"], CFG["tie_threshold"])
        self.assertTrue(out["tie_selected"])

    def test_e_self_symmetric_reflection(self):
        sc = two_node(0.6, 108, 94, up_events=leaf_events((2, "close", 103), (3, "open", 103.2), (15, "close", 108)),
                      down_events=leaf_events((1, "touch", 98), (4, "close", 96), (5, "gap_open", 95.5), (15, "close", 94)))
        z = {"overweight": {"stop": {"level": 97, "semantics": "close"},
                            "conditional": [{"name": "brk", "observe": "close > 102", "fill": "next_open"}]}}
        r = {"underweight": {"stop": {"level": 104, "semantics": "touch"},
                             "conditional": [{"name": "dip", "observe": "touch < 98.5", "fill": "at_level"}]}}
        cfg = _cfg(cost_bps=5.0)
        a = om.compare_strategies(sc, om.build_candidates(z, r, sc, []), cfg, s_s=0.08)
        scr = reflect_scenario(sc)
        b = om.compare_strategies(scr, om.build_candidates(reflect_execution(r), reflect_execution(z), scr, []), cfg, s_s=0.08)

        def tie_set(out, flip):
            top = max(v["CE"] for v in out["strategies"].values())
            rows = set()
            for name, v in out["strategies"].items():
                if top - v["CE"] <= cfg["tie_threshold"]:
                    base = name.split("[")[0]
                    if flip:
                        base = {"overweight_now": "underweight_now", "underweight_now": "overweight_now",
                                "overweight_on_condition": "underweight_on_condition",
                                "underweight_on_condition": "overweight_on_condition"}.get(base, base)
                    rows.add((base, round(v["CE"], 9), round(v["x"], 4)))
            return rows
        self.assertEqual(tie_set(a, False), tie_set(b, True))
        full_a = sorted((round(v["CE"], 9), round(v["x"], 4)) for v in a["strategies"].values())
        full_b = sorted((round(v["CE"], 9), round(v["x"], 4)) for v in b["strategies"].values())
        self.assertEqual(full_a, full_b)
        qa = a["strategies"][a["best"]]["q"]; qb_set = {v["q"] for v in b["strategies"].values()}
        self.assertTrue(any(abs((1 - qa) - q) < 1e-6 for q in qb_set))


class WorkedExampleTest(unittest.TestCase):
    def test_section_1_4_w_half(self):
        A = worked_example_A()
        self.assertAlmostEqual(om.score(A)["S_ref"], 58.9, delta=0.1)
        self.assertAlmostEqual(om.score(A)["mu"], 0.0125, 6)
        out = om.compare_strategies(A, om.build_candidates({}, {}, A, []), _cfg(cost_bps=0.0), s_s=0.08)
        self.assertEqual(out["best"], "overweight_now")
        best = out["strategies"]["overweight_now"]
        self.assertAlmostEqual(best["q"], 0.79, delta=0.01)
        self.assertAlmostEqual(best["x"], 0.58, delta=0.01)


# ----------------------------------------------------------------------------------------------------------------

class DeriveTest(unittest.TestCase):
    """(i) derived enum per strategy; authored operation_type cannot be passed in."""
    def _derive(self, sc, z=None, r=None):
        return om.derive_for_report(sc, om.build_candidates(z or {}, r or {}, sc, []), _cfg(cost_bps=0.0), s_s=0.08)

    def test_signature_has_no_operation_type(self):
        sc = two_node(0.5, 110, 95)
        with self.assertRaises(TypeError):
            om.derive_for_report(sc, [], CFG, operation_type="LIQUIDATE_NOW")

    def test_neutral(self):
        d = self._derive(two_node(0.5, 105, 95))
        self.assertTrue(d["ok"]); self.assertEqual(d["chosen"], "neutral")
        self.assertEqual(d["operation_type"], "HOLD_NEUTRAL"); self.assertEqual(d["operation_target"], P0)
        self.assertEqual(d["display"], "neutral; reference target exposure 50% of the standard unit")
        self.assertEqual(d["q_star"], 0.5); self.assertEqual(d["x_star"], 0.0); self.assertEqual(d["operation_size"], "N/A")

    def test_overweight_now(self):
        d = self._derive(two_node(0.6, 110, 95))
        self.assertEqual(d["chosen"], "overweight_now"); self.assertEqual(d["operation_type"], "ENTRY")
        self.assertEqual(d["operation_target"], P0); self.assertGreater(d["q_star"], 0.5)
        self.assertEqual(d["score"]["S_ref"], om.score(two_node(0.6, 110, 95))["S_ref"])

    def test_underweight_now_trim_and_liquidate(self):
        d = self._derive(two_node(0.48, 103, 97))
        self.assertEqual(d["chosen"], "underweight_now"); self.assertLess(d["x_star"], 1.0)
        self.assertEqual(d["operation_type"], "TRIM_POSITION")
        d = self._derive(two_node(0.2, 103, 90))
        self.assertEqual(d["chosen"], "underweight_now"); self.assertAlmostEqual(d["x_star"], 1.0, 9)
        self.assertEqual(d["operation_type"], "LIQUIDATE_NOW"); self.assertEqual(d["q_star"], 0.0)

    def test_on_condition_both_directions(self):
        up_ev = leaf_events((1, "close", 103), (2, "open", 103), (15, "close", 115))
        sc = two_node(0.5, 115, 90, up_events=up_ev)
        z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 102", "fill": "next_open"}]}}
        d = self._derive(sc, z=z)
        self.assertEqual(d["chosen"].split("[")[0], "overweight_on_condition")
        self.assertEqual(d["operation_type"], "ENTRY"); self.assertEqual(d["operation_target"], 102.0)
        self.assertEqual(d["operation_execution"]["conditional"]["observe"], "close > 102")
        scr = reflect_scenario(sc)
        d = self._derive(scr, r=reflect_execution(z))
        self.assertEqual(d["chosen"].split("[")[0], "underweight_on_condition")
        self.assertAlmostEqual(d["x_star"], 1.0, 9)
        self.assertEqual(d["operation_type"], "TRIM_POSITION")          # never LIQUIDATE_NOW
        self.assertEqual(d["operation_target"], 98.0)
        self.assertEqual(d["operation_execution"]["conditional"]["observe"], "close < 98")

    def test_invalid_scenario(self):
        sc = two_node(0.5, 110, 95); sc["nodes"][0]["p"] = 0.9
        d = om.derive_for_report(sc, [], CFG)
        self.assertFalse(d["ok"]); self.assertTrue(d["fatal"])


# ---------------------------------------------------------------- generic cases (validation, score, size, forecast)

def binary(p, U, D, P0=100.0, events=True):
    """Two-node scenario: +U with p, −D with 1−p (decimals)."""
    up, dn = P0 * (1 + U), P0 * (1 - D)
    def ev(price):
        return [{"t": 15, "kind": "close", "price": price}] if events else []
    return {"schema": om.DEFAULT_CONFIG["schema"], "T_days": 15, "P0": P0,
            "nodes": [{"id": "U", "p": p, "P_T": up, "refs": ["Z1"], "events": ev(up)},
                      {"id": "D", "p": 1 - p, "P_T": dn, "refs": ["R1"], "events": ev(dn)}],
            "execution": {}}


class ValidationTest(unittest.TestCase):
    def test_valid_and_normalized_within_tolerance(self):
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["p"] = 0.604   # Σ = 1.004
        fatal, warn, norm = om.validate_scenario(sc, CFG)
        self.assertEqual(fatal, []); self.assertAlmostEqual(sum(n["p"] for n in norm["nodes"]), 1.0, 9)
        self.assertTrue(norm["derived"]["p_normalization"]["normalized"]); self.assertEqual(norm["nodes"][0]["p_raw"], 0.604)

    def test_invalid_probabilities_are_fatal_not_normalized(self):
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["p"] = 0.7       # Σ = 1.1
        fatal, _, _ = om.validate_scenario(sc, CFG)
        self.assertTrue(any("outside tolerance" in f for f in fatal))
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][1]["p"] = -0.1
        self.assertTrue(om.validate_scenario(sc, CFG)[0])
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][1]["id"] = "U"
        self.assertTrue(any("duplicated" in f for f in om.validate_scenario(sc, CFG)[0]))

    def test_refs_and_one_sided_are_warnings_unknown_refs_fatal(self):
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["refs"] = []
        fatal, warn, _ = om.validate_scenario(sc, CFG)
        self.assertEqual(fatal, []); self.assertTrue(any("no refs" in w for w in warn))
        fatal, _, _ = om.validate_scenario(binary(0.6, 0.04, 0.04), CFG, claim_ids={"Z1"})
        self.assertTrue(any("unknown claims" in f for f in fatal))
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][1]["P_T"] = 101.0; sc["nodes"][1]["events"] = [{"t": 15, "kind": "close", "price": 101.0}]
        _, warn, _ = om.validate_scenario(sc, CFG)
        self.assertTrue(any("one-sided" in w for w in warn))

    def test_events_contract(self):
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["events"] = [{"t": 15, "kind": "close", "price": 99.0}]
        self.assertTrue(any("path and end price contradict" in f for f in om.validate_scenario(sc, CFG)[0]))
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["events"] = [{"t": 3, "kind": "close", "price": 101}, {"t": 3, "kind": "close", "price": 104}]
        self.assertTrue(any("two close events" in f for f in om.validate_scenario(sc, CFG)[0]))
        sc = binary(0.6, 0.04, 0.04); sc["nodes"][0]["events"] = [{"t": 1, "kind": "bogus", "price": 101}]
        self.assertTrue(om.validate_scenario(sc, CFG)[0])

    def test_sub_leaves_need_own_paths_and_conditional_sum(self):
        sc = binary(0.6, 0.04, 0.04)
        sc["nodes"][1]["sub"] = [{"id": "D1", "p_cond": 0.7, "P_T": 96.0, "events": [{"t": 15, "kind": "close", "price": 96.0}]},
                                 {"id": "D2", "p_cond": 0.3, "P_T": 90.0}]
        self.assertTrue(any("no events" in f for f in om.validate_scenario(sc, CFG)[0]))
        sc["nodes"][1]["sub"][1]["events"] = [{"t": 15, "kind": "close", "price": 90.0}]
        self.assertEqual(om.validate_scenario(sc, CFG)[0], [])
        leaves = om.expand_leaves(sc)
        self.assertEqual([l["id"] for l in leaves], ["U", "D/D1", "D/D2"])
        self.assertAlmostEqual(sum(l["p"] for l in leaves), 1.0, 9); self.assertAlmostEqual(leaves[2]["p"], 0.12, 9)
        sc["nodes"][1]["sub"][1]["p_cond"] = 0.5
        self.assertTrue(any("Σ p_cond" in f for f in om.validate_scenario(sc, CFG)[0]))

    def test_z_flag(self):
        sc = binary(0.6, 0.40, 0.04)   # +40% with ATR 2%: z = 0.4 / (0.02*sqrt(15)) = 5.2
        _, warn, norm = om.validate_scenario(sc, CFG, atr_pct=2.0)
        self.assertTrue(any("|z| = 5.2" in w for w in warn))


class ScoreTest(unittest.TestCase):
    def test_worked_examples(self):
        self.assertEqual(om.score(binary(0.55, 0.04, 0.04))["S_ref"], 55.0)
        self.assertEqual(om.score(binary(0.60, 0.04, 0.04))["S_ref"], 60.0)
        self.assertEqual(om.score(binary(0.75, 0.12, 0.04))["S_ref"], 90.0)   # G=9%, L=1%
        self.assertEqual(om.score(binary(0.25, 0.04, 0.12))["S_ref"], 10.0)   # mirror
        self.assertEqual(om.score(binary(0.50, 0.20, 0.20))["S_ref"], 50.0)
        hx = binary(0.60, 0.0843, 0.10, P0=1293000)
        self.assertAlmostEqual(om.score(hx)["S_ref"], 55.8, 1)

    def test_mirror_and_unit_invariance_and_fsd(self):
        a = binary(0.6, 0.05, 0.03); b = binary(0.4, 0.03, 0.05)
        self.assertAlmostEqual(om.score(a)["S_ref"] + om.score(b)["S_ref"], 100.0, 6)
        c = binary(0.6, 0.05, 0.03, P0=1_000_000.0)
        self.assertAlmostEqual(om.score(a)["S_ref"], om.score(c)["S_ref"], 6)
        better = binary(0.6, 0.06, 0.03)     # upside node improved
        self.assertGreaterEqual(om.score(better)["S_ref"], om.score(a)["S_ref"])
        zero = binary(0.5, 0.0, 0.0)
        self.assertEqual(om.score(zero)["S_ref"], 50.0)


class SizeTest(unittest.TestCase):
    def test_size_mapping(self):
        self.assertEqual(om.size_from_x(0.0, CFG), ("none", "N/A"))
        self.assertEqual(om.size_from_x(0.25, CFG), ("light", "PILOT_SIZE"))
        self.assertEqual(om.size_from_x(0.5, CFG), ("standard", "STANDARD_SIZE"))
        self.assertEqual(om.size_from_x(1.0, CFG), ("heavy", "AGGRESSIVE_SIZE"))


class ForecastScoringTest(unittest.TestCase):
    def test_mixture_variance_decomposition(self):
        mu, sig = om.mixture_moments([(0.5, 0.20, 0.0), (0.5, -0.20, 0.0)])
        self.assertAlmostEqual(mu, 0.0, 9); self.assertAlmostEqual(sig, 0.20, 9)
        mu2, sig2 = om.mixture_moments([(0.6, 0.04, 0.02), (0.4, -0.04, 0.02)])
        self.assertGreater(sig2, 0.02)

    def test_crps_and_interval(self):
        leaves = [(0.6, 0.04), (0.4, -0.04)]
        self.assertLess(om.crps_discrete(leaves, 0.04), om.crps_discrete(leaves, 0.20))
        self.assertAlmostEqual(om.crps_discrete([(1.0, 0.03)], 0.03), 0.0, 9)
        lo, hi, cov = om.quantile_interval([(0.1, -0.1), (0.8, 0.0), (0.1, 0.1)], 0.8)
        self.assertEqual((lo, hi), (-0.1, 0.1)); self.assertGreaterEqual(cov, 0.8)


class TestTouchObserveAnyEvent(unittest.TestCase):
    """A resting order (`touch` observe) is filled by any event at or beyond its level, not only by a `touch` event:
    a down node recorded as closes and a gap only still passes through the level (stop semantics; postcheck high/low)."""

    def _sc(self):
        return two_node(0.6, 108, 94, up_events=leaf_events((2, "close", 103), (15, "close", 108)),
                        down_events=leaf_events((2, "close", 97), (3, "gap_open", 95.5), (15, "close", 94)))

    def test_dip_touch_fills_on_close_through_level(self):
        sc = self._sc()
        cand = {"strategy": "overweight_on_condition", "direction": "overweight", "kind": "touch", "op": "<=", "level": 98.5,
                "fill": "at_level", "stop": None}
        sim = om.simulate_leaf(om.expand_leaves(sc)[1], "overweight_on_condition", cand, sc, CFG)
        self.assertTrue(sim["filled"])
        self.assertEqual(sim["fill_price"], 97.0)          # the triggering event's price, not the level
        self.assertAlmostEqual(sim["r_path"], (94.0 - 97.0) / P0)

    def test_close_observe_still_needs_a_close(self):
        sc = two_node(0.6, 108, 94, down_events=leaf_events((2, "touch", 97), (3, "gap_open", 95.5), (4, "close", 96),
                                                            (5, "open", 95.8), (15, "close", 94)))
        cand = {"strategy": "overweight_on_condition", "direction": "overweight", "kind": "close", "op": "<", "level": 98.5,
                "fill": "next_open", "stop": None}
        sim = om.simulate_leaf(om.expand_leaves(sc)[1], "overweight_on_condition", cand, sc, CFG)
        self.assertTrue(sim["filled"])
        self.assertEqual(sim["fill_price"], 95.8)         # the touch 97 and the gap 95.5 did not trigger; the close 96 did

    def test_mirror_trim_high_touch_fills_on_close_through_level(self):
        sc = reflect_scenario(self._sc())
        cand = {"strategy": "underweight_on_condition", "direction": "underweight", "kind": "touch", "op": ">=", "level": _refl(98.5),
                "fill": "at_level", "stop": None}
        sim = om.simulate_leaf(om.expand_leaves(sc)[1], "underweight_on_condition", cand, sc, CFG)
        self.assertTrue(sim["filled"])
        self.assertEqual(sim["fill_price"], _refl(97.0))


class StopFillWithoutOpenTest(unittest.TestCase):
    """A close stop whose path writes no later open: legacy snapshots exit at the triggering close; next_price exits at
    the next later-day event, else the leaf's P_T. Mirror-symmetric for the buy-back."""
    def _leaf(self, events, PT):
        sc = ruler_scenario([("D", 1.0, PT, events)])
        return sc, om.expand_leaves(sc)[0]

    def test_legacy_default_keeps_trigger_price(self):
        sc, leaf = self._leaf(leaf_events((3, "close", 96), (15, "close", 90)), 90)
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        self.assertEqual(om.simulate_leaf(leaf, "overweight_now", now, sc, CFG)["exit_price"], 96.0)

    def test_next_price_proxy_never_better_than_the_trigger(self):
        cfg = dict(CFG, stop_fill_without_open="next_price")
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        sc, leaf = self._leaf(leaf_events((3, "close", 96), (5, "close", 93), (15, "close", 90)), 90)
        sim = om.simulate_leaf(leaf, "overweight_now", now, sc, cfg)
        self.assertTrue(sim["stopped"]); self.assertEqual(sim["exit_price"], 93.0); self.assertEqual(sim["stop_fill"], "proxy")
        # a V-shaped leaf: the later rebound is not handed to the stopped-out position
        sc2, leaf2 = self._leaf(leaf_events((3, "close", 96), (10, "close", 105), (15, "close", 110)), 110)
        self.assertEqual(om.simulate_leaf(leaf2, "overweight_now", now, sc2, cfg)["exit_price"], 96.0)

    def test_valuation_day_close_is_not_an_exit(self):
        cfg = dict(CFG, stop_fill_without_open="next_price")
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        sc, leaf = self._leaf(leaf_events((5, "close", 99), (15, "close", 90)), 90)
        sim = om.simulate_leaf(leaf, "overweight_now", now, sc, cfg)
        self.assertFalse(sim["stopped"]); self.assertEqual(sim["n_tx"], 1); self.assertEqual(sim["exit_price"], 90.0)
        self.assertEqual(sim["stop_fill"], "valuation")

    def test_unknown_mode_is_an_error(self):
        sc, leaf = self._leaf(leaf_events((3, "close", 96), (15, "close", 90)), 90)
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        with self.assertRaises(ValueError):
            om.simulate_leaf(leaf, "overweight_now", now, sc, dict(CFG, stop_fill_without_open="next_prcie"))

    def test_written_open_still_wins_and_mirror(self):
        cfg = dict(CFG, stop_fill_without_open="next_price")
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        sc, leaf = self._leaf(leaf_events((3, "close", 96), (4, "gap_open", 94), (15, "close", 90)), 90)
        self.assertEqual(om.simulate_leaf(leaf, "overweight_now", now, sc, cfg)["exit_price"], 94.0)
        scr = reflect_scenario(ruler_scenario([("D", 1.0, 90, leaf_events((3, "close", 96), (5, "close", 93), (15, "close", 90)))]))
        short = {"id": "underweight_now", "strategy": "underweight_now", "direction": "underweight",
                 "stop": {"level": _refl(97), "semantics": "close"}}
        sim = om.simulate_leaf(om.expand_leaves(scr)[0], "underweight_now", short, scr, cfg)
        self.assertEqual(sim["exit_price"], _refl(93))

    def test_snapshot_round_trip_carries_the_key(self):
        snap = om.snapshot_from_config(dict(CFG, stop_fill_without_open="next_price"))
        self.assertEqual(om.config_from_snapshot(snap)["stop_fill_without_open"], "next_price")
        self.assertEqual(om.config_from_snapshot({})["stop_fill_without_open"], "trigger_price")


class SimpleCardTest(unittest.TestCase):
    """operation_simple: same distribution, only neutral / now / a touch-at-level order, no stop."""
    def test_candidates_keep_now_and_resting_limits_only(self):
        cands = [{"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 95}},
                 {"id": "c1", "strategy": "overweight_on_condition", "direction": "overweight", "kind": "close", "fill": "next_open",
                  "op": ">=", "level": 103, "stop": {"level": 95}},
                 {"id": "brk", "strategy": "overweight_on_condition", "direction": "overweight", "kind": "touch", "fill": "at_level",
                  "op": ">=", "level": 103, "stop": None},                       # a breakout (buy stop): not in the simple card
                 {"id": "c2", "strategy": "underweight_on_condition", "direction": "underweight", "kind": "touch", "fill": "at_level",
                  "op": ">=", "level": 104, "stop": {"level": 108}},
                 {"id": "c2b", "strategy": "underweight_on_condition", "direction": "underweight", "kind": "touch", "fill": "at_level",
                  "op": ">=", "level": 104, "stop": {"level": 110}}]              # same order once the stop is gone
        got = om.simple_candidates(cands)
        self.assertEqual([c["id"] for c in got], ["overweight_now", "c2"])
        self.assertTrue(all(c["stop"] is None for c in got))
        self.assertEqual(got[1]["fill"], "limit")
        self.assertEqual(cands[0]["stop"], {"level": 95})   # inputs untouched

    def test_limit_fills_at_its_level_or_a_better_open(self):
        cand = {"id": "lim", "strategy": "overweight_on_condition", "direction": "overweight", "kind": "touch", "op": "<=",
                "level": 95.0, "fill": "limit", "stop": None}
        sc = ruler_scenario([("D", 1.0, 90, leaf_events((4, "close", 92), (15, "close", 90)))])
        self.assertEqual(om.simulate_leaf(om.expand_leaves(sc)[0], "overweight_on_condition", cand, sc, CFG)["fill_price"], 95.0)
        sc2 = ruler_scenario([("D", 1.0, 90, leaf_events((4, "gap_open", 93), (4, "close", 92), (15, "close", 90)))])
        self.assertEqual(om.simulate_leaf(om.expand_leaves(sc2)[0], "overweight_on_condition", cand, sc2, CFG)["fill_price"], 93.0)

    def test_limit_on_the_wrong_side_of_P0_is_excluded_and_limits_are_inclusive(self):
        mk = lambda d, op, lvl: {"id": f"{d}{op}{lvl}", "strategy": f"{d}_on_condition", "direction": d, "kind": "touch",
                                  "fill": "at_level", "op": op, "level": lvl, "stop": None}
        got = om.simple_candidates([mk("overweight", "<=", 105.0), mk("overweight", "<=", 100.0), mk("overweight", "<", 95.0),
                                    mk("underweight", ">=", 96.0), mk("underweight", ">", 104.0)], P0=100.0)
        self.assertEqual([(c["direction"], c["op"], c["level"]) for c in got],
                         [("overweight", "<=", 95.0), ("underweight", ">=", 104.0)])
        # a low exactly at the limit fills (inclusive), and the mirror on the sell side
        buy = got[0]
        sc = ruler_scenario([("D", 1.0, 98, leaf_events((4, "touch", 95), (15, "close", 98)))])
        self.assertTrue(om.simulate_leaf(om.expand_leaves(sc)[0], "overweight_on_condition", buy, sc, CFG)["filled"])
        scr = reflect_scenario(sc)
        sell = got[1]
        self.assertTrue(om.simulate_leaf(om.expand_leaves(scr)[0], "underweight_on_condition", sell, scr, CFG)["filled"])

    def test_late_open_is_a_proxy_not_the_next_day_open(self):
        cfg = dict(CFG, stop_fill_without_open="next_price")
        now = {"id": "overweight_now", "strategy": "overweight_now", "direction": "overweight", "stop": {"level": 97, "semantics": "close"}}
        sc = ruler_scenario([("D", 1.0, 99, leaf_events((3, "close", 96), (8, "open", 99), (15, "close", 99)))])
        sim = om.simulate_leaf(om.expand_leaves(sc)[0], "overweight_now", now, sc, cfg)
        self.assertEqual(sim["exit_price"], 96.0); self.assertEqual(sim["stop_fill"], "proxy")

    def test_simple_card_ignores_the_stop_that_drives_the_full_size(self):
        # deep loss node truncated by a tight stop: the full card sizes up, the simple card does not
        down_ev = leaf_events((2, "close", 97), (3, "open", 96.5), (15, "close", 85))
        sc = ruler_scenario([("U", 0.6, 106, None), ("D", 0.4, 85, down_ev)])
        z = {"overweight": {"stop": {"level": 97.5, "semantics": "close"}, "conditional": []}}
        cands = om.build_candidates(z, {}, sc, [])
        d = om.derive_for_report(sc, cands, CFG, s_s=0.08)
        self.assertTrue(d["ok"])
        self.assertIn("simple", d)
        self.assertLess(d["simple"]["x_star"], d["x_star"])
        self.assertIn("no stop", d["simple"]["display"])

    def test_mirror(self):
        sc = two_node(0.35, 108, 95)
        a = om.derive_for_report(sc, om.build_candidates({}, {}, sc, []), CFG, s_s=0.08)["simple"]
        scr = reflect_scenario(sc)
        b = om.derive_for_report(scr, om.build_candidates({}, {}, scr, []), CFG, s_s=0.08)["simple"]
        self.assertAlmostEqual(a["x_star"], b["x_star"], places=6)
        self.assertEqual(a["chosen"].split("_")[0] == "overweight", b["chosen"].split("_")[0] == "underweight")


if __name__ == "__main__":
    unittest.main()
