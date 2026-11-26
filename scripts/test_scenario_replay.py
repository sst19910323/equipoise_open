"""scenario_replay (contracts C1 / C2 / C3 / C6.1-6.3, proposal-ruler_era §1): op grammar, update verification, registered
effects with freezing, settlements, deterministic replay, mixture A = w·Z′ + (1−w)·R′ and its envelope.

Module-level helpers (importable by later tasks): ev, node, scen, Z14, R14, record, P_MOVE."""
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("scenario_replay", ROOT / "scripts" / "scenario_replay.py")
sr = importlib.util.module_from_spec(spec); spec.loader.exec_module(sr)
import odds_model as om  # noqa: E402

P0 = 100.0


def ev(*triples):
    """(t, kind, price) triples → event list."""
    return [{"t": t, "kind": k, "price": float(p)} for t, k, p in triples]


def node(nid, p, P_T, events=None, label=None, refs=None, sub=None):
    """One scenario node; events default to a single day-15 close at P_T."""
    n = {"id": nid, "label": label or f"node {nid}", "p": p, "P_T": float(P_T), "refs": refs or ["Z1"],
         "events": ev((15, "close", P_T)) if events is None else events}
    if sub is not None:
        n["sub"] = sub
    return n


def scen(*nodes, P0=P0):
    """Schema-3.4 scenario from nodes."""
    return {"schema": "3.4", "T_days": 15, "P0": P0, "nodes": [copy.deepcopy(n) for n in nodes], "execution": {}}


Z14 = scen(node("A", 0.55, 110, label="beat and raise"), node("B", 0.30, 100, label="flat"),
           node("C", 0.15, 90, label="miss"))
R14 = scen(node("A", 0.25, 108, label="raise"), node("B", 0.35, 98, label="drift lower"),
           node("C", 0.40, 88, label="multiple compression"))


def P_MOVE(a, b, amt):
    return {"op": "p_move", "from": a, "to": b, "amount": amt}


def record(seq, side, rnd, claims=(), updates=(), scenario=None):
    """Store record as Task 3 will build it."""
    role = {"Z": "zealot", "R": "reaper"}[side]
    content = {"claims": [copy.deepcopy(c) for c in claims], "scenario_updates": [copy.deepcopy(u) for u in updates]}
    if scenario is not None:
        content["scenario"] = scenario
    return {"seq": seq, "id": f"R{rnd}.{side}", "role": role, "round": rnd, "content": content}


def claim(cid, text, if_true=(), if_false=(), kind="own", status="open", target=None, **extra):
    c = {"id": cid, "kind": kind, "claim": text, "evidence_tier": "T2", "status": status,
         "effect": {"if_true": list(if_true), "if_false": list(if_false)}}
    if target:
        c["target"] = target
    c.update(extra)
    return c


def p_of(sc, nid):
    return next(n["p"] for n in sc["nodes"] if n["id"] == nid)


# ------------------------------------------------------------------------------------------------------------------
# C1 ops
# ------------------------------------------------------------------------------------------------------------------

class TestOps(unittest.TestCase):
    def test_p_move_happy_and_input_untouched(self):
        before = sr.canonical(R14)
        out = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        self.assertAlmostEqual(p_of(out, "A"), 0.35)
        self.assertAlmostEqual(p_of(out, "C"), 0.30)
        self.assertEqual(sr.canonical(R14), before)

    def test_p_move_to_zero_removes_node(self):
        out = sr.apply_ops(Z14, [P_MOVE("C", "B", 0.15)])
        self.assertEqual([n["id"] for n in out["nodes"]], ["A", "B"])
        self.assertAlmostEqual(p_of(out, "B"), 0.45)

    def test_p_move_missing_node(self):
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [P_MOVE("C", "A", 0.05), P_MOVE("X", "A", 0.05)])
        self.assertEqual(cm.exception.index, 1)
        self.assertIn("X", cm.exception.reason)
        self.assertIn("missing", cm.exception.reason)

    def test_p_move_amount_too_large(self):
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [P_MOVE("C", "A", 0.20)])
        self.assertEqual(cm.exception.index, 0)
        self.assertIn("exceeds", cm.exception.reason)

    def test_p_move_non_positive_amount(self):
        with self.assertRaises(sr.OpError):
            sr.apply_ops(Z14, [P_MOVE("C", "A", 0.0)])

    def test_P_T_set_happy_rewrites_last_close(self):
        sc = scen(node("A", 0.6, 110, events=ev((3, "close", 104), (15, "close", 110))), node("B", 0.4, 95))
        out = sr.apply_ops(sc, [{"op": "P_T_set", "node": "A", "value": 108.0}])
        a = out["nodes"][0]
        self.assertEqual(a["P_T"], 108.0)
        self.assertEqual(a["events"], ev((3, "close", 104), (15, "close", 108)))

    def test_P_T_set_wrong_side(self):
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [{"op": "P_T_set", "node": "A", "value": 95.0}])
        self.assertIn("wrong side", cm.exception.reason)

    def test_P_T_set_non_positive(self):
        with self.assertRaises(sr.OpError):
            sr.apply_ops(Z14, [{"op": "P_T_set", "node": "A", "value": 0}])

    def test_path_set_happy(self):
        new = ev((2, "touch", 103), (15, "close", 110))
        out = sr.apply_ops(Z14, [{"op": "path_set", "node": "A", "events": new}])
        self.assertEqual(out["nodes"][0]["events"], new)

    def test_path_set_event_invariant(self):
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [{"op": "path_set", "node": "A", "events": ev((15, "close", 105))}])
        self.assertIn("last close", cm.exception.reason)

    def test_split_happy_copies_refs(self):
        op = {"op": "split", "node": "A", "amount": 0.10,
              "new_node": {"id": "A2", "label": "late rally", "P_T": 106.0, "events": ev((15, "close", 106))}}
        out = sr.apply_ops(Z14, [op])
        a2 = out["nodes"][-1]
        self.assertEqual((a2["id"], a2["p"], a2["refs"]), ("A2", 0.10, ["Z1"]))
        self.assertAlmostEqual(p_of(out, "A"), 0.45)

    def test_split_duplicate_id(self):
        op = {"op": "split", "node": "A", "amount": 0.10, "new_node": {"id": "B", "label": "x", "P_T": 106.0, "events": []}}
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [op])
        self.assertIn("already exists", cm.exception.reason)

    def test_split_amount_too_large(self):
        op = {"op": "split", "node": "C", "amount": 0.20, "new_node": {"id": "C2", "label": "x", "P_T": 80.0, "events": []}}
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(Z14, [op])
        self.assertIn("exceeds", cm.exception.reason)

    def test_split_whole_mass_removes_source(self):
        op = {"op": "split", "node": "C", "amount": 0.15,
              "new_node": {"id": "C2", "label": "gap down", "P_T": 90.0, "events": ev((1, "gap_open", 92), (15, "close", 90))}}
        out = sr.apply_ops(Z14, [op])
        self.assertEqual([n["id"] for n in out["nodes"]], ["A", "B", "C2"])

    def test_sub_addressing(self):
        sub = [{"id": "a", "label": "fast", "p_cond": 0.6, "P_T": 112.0, "events": ev((15, "close", 112))},
               {"id": "b", "label": "slow", "p_cond": 0.4, "P_T": 106.0, "events": ev((15, "close", 106))}]
        sc = scen(node("U", 0.6, 110, sub=sub), node("D", 0.4, 92))
        out = sr.apply_ops(sc, [P_MOVE("U/a", "U/b", 0.2), {"op": "P_T_set", "node": "U/b", "value": 107.0}])
        u = out["nodes"][0]["sub"]
        self.assertAlmostEqual(u[0]["p_cond"], 0.4)
        self.assertAlmostEqual(u[1]["p_cond"], 0.6)
        self.assertEqual(u[1]["P_T"], 107.0)
        self.assertEqual(u[1]["events"], ev((15, "close", 107)))

    def test_cross_parent_move(self):
        sub = [{"id": "a", "p_cond": 1.0, "P_T": 112.0, "events": []}]
        sc = scen(node("U", 0.6, 110, sub=sub), node("D", 0.4, 92))
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(sc, [P_MOVE("U/a", "D", 0.1)])
        self.assertIn("cross-parent move not expressible", cm.exception.reason)

    def test_atomic_failure_leaves_input(self):
        before = sr.canonical(Z14)
        with self.assertRaises(sr.OpError):
            sr.apply_ops(Z14, [P_MOVE("C", "A", 0.05), {"op": "bogus"}])
        self.assertEqual(sr.canonical(Z14), before)

    def test_p_move_within_tolerance_clamped(self):
        out = sr.apply_ops(Z14, [P_MOVE("C", "B", 0.155)])
        self.assertEqual([n["id"] for n in out["nodes"]], ["A", "B"])
        self.assertAlmostEqual(p_of(out, "B"), 0.45, places=12)

    def test_missing_node_each_op(self):
        for op in ({"op": "P_T_set", "node": "X", "value": 105.0},
                   {"op": "path_set", "node": "X", "events": []},
                   {"op": "split", "node": "X", "amount": 0.1, "new_node": {"id": "Y", "P_T": 105.0, "events": []}}):
            with self.assertRaises(sr.OpError) as cm:
                sr.apply_ops(Z14, [op])
            self.assertIn("node X missing", cm.exception.reason)

    def test_P_T_set_event_invariant(self):
        sc = scen(node("A", 0.6, 110, events=[{"t": 3, "kind": "spike", "price": 104.0}] + ev((15, "close", 110))),
                  node("B", 0.4, 95))
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(sc, [{"op": "P_T_set", "node": "A", "value": 108.0}])
        self.assertIn("kind", cm.exception.reason)

    def test_split_sub_leaf(self):
        sub = [{"id": "a", "label": "fast", "p_cond": 0.6, "P_T": 112.0, "events": ev((15, "close", 112))},
               {"id": "b", "label": "slow", "p_cond": 0.4, "P_T": 106.0, "events": ev((15, "close", 106))}]
        sc = scen(node("U", 0.6, 110, sub=sub), node("D", 0.4, 92))
        op = {"op": "split", "node": "U/a", "amount": 0.2,
              "new_node": {"id": "c", "label": "late", "P_T": 115.0, "events": ev((15, "close", 115))}}
        out = sr.apply_ops(sc, [op])
        u = out["nodes"][0]
        self.assertEqual([(x["id"], round(x["p_cond"], 9)) for x in u["sub"]], [("a", 0.4), ("b", 0.4), ("c", 0.2)])
        self.assertEqual(u["p"], 0.6)
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(sc, [dict(op, new_node=dict(op["new_node"], id="b"))])
        self.assertIn("already exists", cm.exception.reason)

    def test_canonical_rounds_and_drops(self):
        a = dict(Z14, derived={"x": 1}, mixture={"w": 0.5})
        b = copy.deepcopy(Z14); b["nodes"][0]["p"] = 0.55 + 1e-9
        self.assertEqual(sr.canonical(a), sr.canonical(b))
        self.assertNotIn("derived", sr.canonical(a))


# ------------------------------------------------------------------------------------------------------------------
# C3 update verification
# ------------------------------------------------------------------------------------------------------------------

class TestVerifyUpdate(unittest.TestCase):
    def test_reproduces_exact(self):
        this = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        this["execution"] = {"overweight": {"stop": None, "conditional": []}}   # execution is outside the op grammar
        ok, msg = sr.verify_update(R14, [{"claim": "Z1", "applies": None, "ops": [P_MOVE("C", "A", 0.10)], "why": "w"}], this)
        self.assertTrue(ok, msg)

    def test_names_divergent_node(self):
        this = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        this["nodes"][1]["P_T"] = 97.0
        ok, msg = sr.verify_update(R14, [{"claim": "Z1", "applies": None, "ops": [P_MOVE("C", "A", 0.10)]}], this)
        self.assertFalse(ok)
        self.assertIn("node B", msg)

    def test_unlogged_change_names_node(self):
        this = copy.deepcopy(R14); this["nodes"][2]["p"] = 0.30; this["nodes"][0]["p"] = 0.35
        ok, msg = sr.verify_update(R14, [], this)
        self.assertFalse(ok)
        self.assertIn("node A", msg)

    def test_integer_valued_filed_scenario(self):
        this = {"schema": "3.4", "T_days": 15, "P0": 100, "execution": {},
                "nodes": [{"id": "A", "label": "beat and raise", "p": 0.55, "P_T": 109, "refs": ["Z1"],
                           "events": [{"t": 15, "kind": "close", "price": 109}]},
                          {"id": "B", "label": "flat", "p": 0.3, "P_T": 100, "refs": ["Z1"],
                           "events": [{"t": 15, "kind": "close", "price": 100}]},
                          {"id": "C", "label": "miss", "p": 0.15, "P_T": 90, "refs": ["Z1"],
                           "events": [{"t": 15, "kind": "close", "price": 90}]}]}
        ok, msg = sr.verify_update(Z14, [{"claim": "Z1", "ops": [{"op": "P_T_set", "node": "A", "value": 109}]}], this)
        self.assertTrue(ok, msg)

    def test_op_failure_reported(self):
        ok, msg = sr.verify_update(R14, [{"claim": "Z1", "ops": [P_MOVE("C", "A", 0.9)]}], R14)
        self.assertFalse(ok)
        self.assertIn("update[0] op[0]", msg)

    def test_opening_no_updates(self):
        self.assertTrue(sr.verify_update(None, [], Z14)[0])
        self.assertFalse(sr.verify_update(None, [{"claim": "Z1", "ops": []}], Z14)[0])


# ------------------------------------------------------------------------------------------------------------------
# C2 registry + freezing, C3 applies
# ------------------------------------------------------------------------------------------------------------------

def _history():
    """Z owns Z1 (empty effect, 'already inside'); R challenges Z1 with C→A 0.10 if Z1 holds; R owns R2."""
    return [
        record(1, "Z", 1, claims=[claim("Z1", "guidance raised")]),
        record(2, "R", 1, claims=[claim("R1", "guidance is noise", kind="challenge", target="Z1",
                                        if_true=[P_MOVE("C", "A", 0.10)], if_false=[]),
                                  claim("R2", "multiple compresses", if_true=[], if_false=[P_MOVE("C", "B", 0.05)])]),
    ]


class TestRegistry(unittest.TestCase):
    def test_registry_both_sides(self):
        reg_r, fatal = sr.effects_registry_from_history(_history(), "R")
        self.assertEqual(fatal, [])
        self.assertEqual(reg_r["Z1"]["true"], [P_MOVE("C", "A", 0.10)])
        self.assertEqual(reg_r["Z1"]["false"], [])
        self.assertEqual((reg_r["Z1"]["owner"], reg_r["Z1"]["claim_rev"]), ("Z", "R1.Z"))
        self.assertEqual(reg_r["R2"]["false"], [P_MOVE("C", "B", 0.05)])
        reg_z, _ = sr.effects_registry_from_history(_history(), "zealot")
        self.assertEqual(reg_z["Z1"]["true"], [])
        self.assertEqual(reg_z["R2"], {"true": [], "false": [], "owner": "R", "claim_rev": "R1.R"})

    def test_text_change_without_revises_fatal(self):
        h = _history() + [record(3, "Z", 2, claims=[claim("Z1", "guidance raised twice")])]
        _, fatal = sr.effects_registry_from_history(h, "Z")
        self.assertTrue(any("Z1" in f and "revises" in f for f in fatal), fatal)

    def test_effect_change_same_text_fatal(self):
        h = _history() + [record(3, "R", 2, claims=[claim("R2", "multiple compresses", if_false=[P_MOVE("C", "B", 0.10)])])]
        _, fatal = sr.effects_registry_from_history(h, "R")
        self.assertTrue(any("effect frozen" in f for f in fatal), fatal)

    def test_proper_revision_new_claim_rev(self):
        h = _history() + [record(3, "R", 2, claims=[claim("R2", "multiple compresses by 1x", if_false=[P_MOVE("C", "B", 0.10)],
                                                          revises="R1.R")])]
        reg, fatal = sr.effects_registry_from_history(h, "R")
        self.assertEqual(fatal, [])
        self.assertEqual(reg["R2"]["claim_rev"], "R2.R")
        self.assertEqual(reg["R2"]["false"], [P_MOVE("C", "B", 0.10)])

    def test_revision_wrong_revises_fatal(self):
        h = _history() + [record(3, "R", 2, claims=[claim("R2", "new text", revises="R9.R")])]
        _, fatal = sr.effects_registry_from_history(h, "R")
        self.assertTrue(fatal)

    def test_verify_applies(self):
        reg, _ = sr.effects_registry_from_history(_history(), "R")
        good = [{"claim": "Z1", "applies": "Z1#true", "ops": [P_MOVE("C", "A", 0.10)]}]
        self.assertEqual(sr.verify_applies(good, reg), [])
        forged = [{"claim": "Z1", "applies": "Z1#true", "ops": [P_MOVE("C", "A", 0.05)]}]
        self.assertTrue(any("does not match" in f for f in sr.verify_applies(forged, reg)))
        self.assertTrue(sr.verify_applies([{"claim": "Q9", "applies": "Q9#true", "ops": []}], reg))
        self.assertTrue(any("already claimed" in f for f in sr.verify_applies(good + good, reg)))
        self.assertTrue(sr.verify_applies([{"claim": "Z1", "applies": "Z1#maybe", "ops": []}], reg))


# ------------------------------------------------------------------------------------------------------------------
# C6.1 settlements
# ------------------------------------------------------------------------------------------------------------------

class TestSettlements(unittest.TestCase):
    def test_owner_status_conceded_and_review(self):
        h = _history() + [
            record(3, "Z", 2, claims=[claim("Z1", "guidance raised", status="settled_for")]),
            record(4, "R", 2, claims=[claim("R1", "guidance is noise", kind="challenge", target="Z1",
                                            if_true=[P_MOVE("C", "A", 0.10)]),
                                      claim("R2", "multiple compresses", if_false=[P_MOVE("C", "B", 0.05)], status="conceded")]),
        ]
        s = sr.settlements(h, [])
        self.assertEqual(s, [("Z1", "true", 2, 3), ("R2", "false", 2, 4)])
        s2 = sr.settlements(h, [{"claim": "Z1", "claim_rev": "R1.Z", "verdict": "rejected", "reason": "x"}])
        self.assertIn(("Z1", "false", 2, 3), s2)

    def test_conceded_marks_challenges_true(self):
        """Owner concession settles its claim false; a challenge id is never a settlement key (2nd fix wave, G3)."""
        h = [record(1, "Z", 1, claims=[claim("Z1", "a")]),
             record(2, "R", 1, claims=[claim("R1", "b", kind="challenge", target="Z1")]),
             record(3, "Z", 2, claims=[claim("Z1", "a", status="conceded")])]
        self.assertEqual(sr.settlements(h, []), [("Z1", "false", 2, 3)])

    def test_challenger_concession_settles_the_target_true_once(self):
        """G3 (ledger ruling): `conceded` / `withdrawn` on one's own challenge settles the TARGET claim true, once;
        when the owner also settled it, the earlier record fixes it."""
        for st in ("conceded", "withdrawn"):
            h = [record(1, "Z", 1, claims=[claim("Z1", "a")]),
                 record(2, "R", 1, claims=[claim("R1", "b", kind="challenge", target="Z1")]),
                 record(3, "R", 2, claims=[claim("R1", "b", kind="challenge", target="Z1", status=st)]),
                 record(4, "Z", 2, claims=[claim("Z1", "a", status="settled_for")])]
            self.assertEqual(sr.settlements(h, []), [("Z1", "true", 2, 3)])

    def test_status_reopened_not_settled_and_review_only(self):
        h = [record(1, "Z", 1, claims=[claim("Z1", "a", status="settled_for")]),
             record(2, "Z", 2, claims=[claim("Z1", "a", status="open")]),
             record(3, "R", 2, claims=[claim("R2", "b")])]
        self.assertEqual(sr.settlements(h, []), [])
        s = sr.settlements(h, [{"claim": "R2", "claim_rev": "R1.R", "verdict": "adopted", "reason": ""}])
        self.assertEqual(s, [("R2", "true", 3, 4)])

    def test_first_setting_record_kept(self):
        h = [record(1, "Z", 1, claims=[claim("Z1", "a", status="settled_for")]),
             record(5, "Z", 3, claims=[claim("Z1", "a", status="settled_for")])]
        self.assertEqual(sr.settlements(h, []), [("Z1", "true", 1, 1)])


# ------------------------------------------------------------------------------------------------------------------
# C6.2 replay (proposal §5 f)
# ------------------------------------------------------------------------------------------------------------------

def _replay_fixture():
    """Z opening + one accepted update; registry for Z; two pending non-commuting settlements."""
    opening = scen(node("A", 0.50, 110), node("B", 0.30, 100), node("C", 0.20, 90))
    upd = [{"claim": "R1", "applies": "R1#true", "ops": [P_MOVE("A", "B", 0.10)], "why": "R1 concession"}]
    reg = {"R1": {"true": [P_MOVE("A", "B", 0.10)], "false": [], "owner": "R", "claim_rev": "R1.R"},
           "R2": {"true": [{"op": "split", "node": "C", "amount": 0.20,
                            "new_node": {"id": "C2", "label": "deep", "P_T": 85.0, "events": ev((15, "close", 85))}}],
                  "false": [], "owner": "R", "claim_rev": "R1.R"},
           "Z3": {"true": [P_MOVE("C2", "B", 0.05)], "false": [], "owner": "Z", "claim_rev": "R2.Z"}}
    settled = [("Z3", "true", 3, 9), ("R1", "true", 2, 5), ("R2", "true", 2, 6)]
    return opening, upd, reg, settled


class TestReplay(unittest.TestCase):
    def test_reproduces_hand_built_byte_for_byte(self):
        opening, upd, reg, settled = _replay_fixture()
        sc, applied, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertEqual(fatal, [])
        expected = scen(node("A", 0.40, 110), node("B", 0.45, 100),
                        node("C2", 0.15, 85, label="deep", refs=["Z1"]))
        self.assertEqual(sr.canonical(sc), sr.canonical(expected))
        self.assertEqual([a["effect"] for a in applied if a["status"] == "applied"], ["R2#true", "Z3#true"])
        self.assertEqual([a["effect"] for a in applied if a["status"] == "carried"], ["R1#true"])

    def test_carried_effect_applied_once(self):
        opening, upd, reg, settled = _replay_fixture()
        sc, _, _ = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertAlmostEqual(p_of(sc, "A"), 0.40)   # not 0.30: R1#true applied by the update only

    def test_forged_applies_fatal(self):
        opening, upd, reg, settled = _replay_fixture()
        upd[0]["ops"] = [P_MOVE("A", "B", 0.05)]
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertTrue(any("does not match" in f for f in fatal), fatal)

    def test_applies_conflicts_with_settlement(self):
        opening, upd, reg, settled = _replay_fixture()
        settled = [("R1", "false", 2, 5)]
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertTrue(any("conflicts" in f for f in fatal), fatal)

    def test_non_commuting_order(self):
        opening, upd, reg, settled = _replay_fixture()
        # Z3 needs C2, which only R2 creates: recorded order (R2 at round 2 before Z3 at round 3) works ...
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertEqual(fatal, [])
        # ... while the reverse order fails on the precondition
        _, _, fatal = sr.replay_side(opening, upd, [("Z3", "true", 1, 1), ("R2", "true", 2, 6)], reg, [], "Z")
        self.assertTrue(any("Z3#true" in f and "C2" in f for f in fatal), fatal)

    def test_precondition_failure_voided(self):
        opening, upd, reg, _ = _replay_fixture()
        settled = [("Z3", "true", 3, 9)]
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertTrue(fatal)
        sc, applied, fatal = sr.replay_side(opening, upd, settled, reg, [{"claim": "Z3", "outcome": "true"}], "Z")
        self.assertEqual(fatal, [])
        self.assertEqual([a["status"] for a in applied], ["carried", "voided"])
        self.assertAlmostEqual(p_of(sc, "B"), 0.40)

    def test_voided_only_for_named_side(self):
        z = scen(node("A", 0.5, 110), node("B", 0.3, 100), node("C", 0.2, 90))
        r = copy.deepcopy(z)
        reg_z = {"R3": {"true": [P_MOVE("C", "B", 0.1)], "false": [], "owner": "R", "claim_rev": "R2.R"}}
        reg_r = {"R3": {"true": [P_MOVE("D", "B", 0.1)], "false": [], "owner": "R", "claim_rev": "R2.R"}}   # D gone on R
        settled = [("R3", "true", 3, 7)]
        voided = [{"claim": "R3", "outcome": "true", "side": "R", "reason": "x"}]
        z_prime, z_applied, z_fatal = sr.replay_side(z, [], settled, reg_z, voided, "Z")
        r_prime, r_applied, r_fatal = sr.replay_side(r, [], settled, reg_r, voided, "R")
        self.assertEqual((z_fatal, r_fatal), ([], []))
        self.assertEqual([a["status"] for a in z_applied], ["applied"])
        self.assertAlmostEqual(p_of(z_prime, "C"), 0.1)
        self.assertEqual([a["status"] for a in r_applied], ["voided"])
        self.assertEqual(sr.canonical(r_prime), sr.canonical(r))
        # an entry without "side" applies to both sides — and on Z the effect is executable, so the void is rejected
        _, _, z_fatal = sr.replay_side(z, [], settled, reg_z, [{"claim": "R3", "outcome": "true"}], "zealot")
        self.assertTrue(any("void rejected: effect is executable" in f for f in z_fatal), z_fatal)

    def test_unregistered_claim_is_empty_effect(self):
        opening, _, reg, _ = _replay_fixture()
        sc, applied, fatal = sr.replay_side(opening, [], [("Q7", "true", 1, 1)], reg, [], "Z")
        self.assertEqual((fatal, applied), ([], []))
        self.assertEqual(sr.canonical(sc), sr.canonical(opening))


# ------------------------------------------------------------------------------------------------------------------
# C6.3 mixture + envelope (proposal §1.3-1.4, §5 e)
# ------------------------------------------------------------------------------------------------------------------

class TestMixture(unittest.TestCase):
    def test_worked_example_R_prime(self):
        r_prime = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        self.assertAlmostEqual(om.score(r_prime)["S_ref"], 39.4, delta=0.1)
        self.assertAlmostEqual(om.score(Z14)["S_ref"], 78.6, delta=0.1)
        A = sr.mixture(Z14, r_prime, 0.5)
        self.assertAlmostEqual(om.score(A)["S_ref"], 58.9, delta=0.1)
        self.assertEqual(len(A["nodes"]), 6)
        self.assertEqual([n["id"] for n in A["nodes"]], ["M1", "M2", "M3", "M4", "M5", "M6"])
        self.assertEqual(A["mixture"]["t"], 0.0)
        self.assertIsNone(A["mixture"]["reason"])
        self.assertEqual(sr.envelope_check(A, Z14, r_prime, 0.01), [])
        A75 = sr.mixture(Z14, r_prime, 0.75)
        self.assertAlmostEqual(om.score(A75)["S_ref"], 68.7, delta=0.1)

    def test_identical_atoms_merge_label_refs(self):
        z = scen(node("A", 0.6, 110, label="beat and raise", refs=["Z1"]), node("B", 0.4, 95, label="miss"))
        r = scen(node("X", 0.3, 110, label="beat", refs=["R1"]), node("Y", 0.7, 95, label="miss hard", refs=["R2"]))
        A = sr.mixture(z, r, 0.5)
        self.assertEqual(len(A["nodes"]), 2)
        hi = next(n for n in A["nodes"] if n["P_T"] == 110.0)
        self.assertAlmostEqual(hi["p"], 0.45)
        self.assertEqual((hi["label"], hi["refs"]), ("beat", ["R1", "Z1"]))
        self.assertNotIn("sub", json.dumps(A["nodes"]))

    def test_sub_expanded(self):
        sub = [{"id": "a", "label": "fast", "p_cond": 0.5, "P_T": 112.0, "events": ev((15, "close", 112))},
               {"id": "b", "label": "slow", "p_cond": 0.5, "P_T": 106.0, "events": ev((15, "close", 106))}]
        z = scen(node("U", 0.6, 110, sub=sub), node("D", 0.4, 92))
        A = sr.mixture(z, z, 0.3)
        self.assertEqual(sorted(n["P_T"] for n in A["nodes"]), [92.0, 106.0, 112.0])
        self.assertAlmostEqual(next(n["p"] for n in A["nodes"] if n["P_T"] == 112.0), 0.3)

    def test_reason_and_mismatch(self):
        self.assertEqual(sr.mixture(Z14, R14, 0.9, reason="R2 + Z.EV3")["mixture"]["reason"], "R2 + Z.EV3")
        with self.assertRaises(ValueError):
            sr.mixture(Z14, dict(R14, T_days=10), 0.5)
        with self.assertRaises(ValueError):
            sr.mixture(Z14, dict(R14, P0=101.0), 0.5)
        sr.mixture(Z14, dict(R14, P0=100), 0.5)   # int vs float of the same value is not a mismatch

    def test_swap_symmetry(self):
        r_prime = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        a = sr.mixture(Z14, r_prime, 0.65)
        b = sr.mixture(r_prime, Z14, 0.35)
        strip = lambda x: {k: v for k, v in x.items() if k != "mixture"}   # noqa: E731
        self.assertEqual(sr.canonical(strip(a)), sr.canonical(strip(b)))

    def test_envelope_outside_range_fatal(self):
        r_prime = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        A = sr.mixture(Z14, r_prime, 0.5)
        hi = next(n for n in A["nodes"] if n["P_T"] == 110.0)
        hi["p"] = 0.60   # > 0.55 and > 0.35: the proposal's §1.4 envelope example
        fatal = sr.envelope_check(A, Z14, r_prime, 0.01)
        self.assertTrue(any("outside" in f and "110" in f for f in fatal), fatal)

    def test_envelope_no_common_w_fatal(self):
        z = scen(node("a", 0.6, 110), node("b", 0.3, 100), node("c", 0.1, 90))
        r = scen(node("a", 0.2, 110), node("b", 0.2, 100), node("c", 0.6, 90))
        A = scen(node("a", 0.40, 110), node("b", 0.28, 100), node("c", 0.32, 90))
        fatal = sr.envelope_check(A, z, r, 0.01)
        self.assertFalse(any("outside" in f for f in fatal), fatal)
        self.assertTrue(any("no single w" in f for f in fatal), fatal)

    def test_envelope_stored_w_mismatch(self):
        A = sr.mixture(Z14, R14, 0.5)
        A["mixture"]["w"] = 0.8
        self.assertTrue(any("w = 0.8" in f for f in sr.envelope_check(A, Z14, R14, 0.01)))

    def test_rebuild_at_half(self):
        r_prime = sr.apply_ops(R14, [P_MOVE("C", "A", 0.10)])
        A = sr.mixture(Z14, r_prime, 0.9)
        half = sr.rebuild_at_half(A)
        self.assertNotIn("mixture", half)
        self.assertAlmostEqual(om.score(half)["S_ref"], 58.9, delta=0.1)
        self.assertEqual(sr.canonical(half), sr.canonical({k: v for k, v in sr.mixture(Z14, r_prime, 0.5).items()
                                                           if k != "mixture"}))

class TestPartlyRuling(unittest.TestCase):
    """v3.8: the ruling may settle a claim `partly` — it fails in part, and each side's registered if_false effect lands
    at the ruled share. Three production sessions (MU 11-09, MHVYF 11-10, HOCPY 11-10) paid a whole registered effect
    for a partial hit; the ruling had no way to say "half of it"."""

    def _fx(self):
        opening = scen(node("A", 0.50, 110), node("B", 0.30, 100), node("C", 0.20, 90))
        reg = {"R1": {"true": [], "false": [P_MOVE("C", "A", 0.08)], "owner": "R", "claim_rev": "R1.R"},
               "R2": {"true": [], "false": [{"op": "P_T_set", "node": "A", "value": 116.0}], "owner": "R", "claim_rev": "R1.R"}}
        return opening, reg

    def test_review_scales_and_settlement_direction(self):
        cr = [{"claim": "R1", "verdict": "partly", "scale": 0.5, "reason": "size only"},
              {"claim": "R2", "verdict": "partly", "scale": 1.0}, {"claim": "R3", "verdict": "partly"},
              {"claim": "R4", "verdict": "rejected", "scale": 0.5}]
        self.assertEqual(sr.review_scales(cr), {"R1": 0.5})           # only a share strictly inside (0, 1) is a share
        self.assertEqual(sr.review_scales(None), {})
        finals = [{"role": "R", "round": 1, "seq": 1, "id": "R1.R", "content": {"claims": [{"id": "R1", "status": "open"}]}}]
        self.assertEqual([(c, o) for c, o, _, _ in sr.settlements(finals, cr[:1])], [("R1", "false")])

    def test_scale_ops(self):
        opening, reg = self._fx()
        self.assertEqual(sr.scale_ops(opening, reg["R1"]["false"], 0.5), [P_MOVE("C", "A", 0.04)])
        self.assertEqual(sr.scale_ops(opening, reg["R2"]["false"], 0.25)[0]["value"], 111.5)   # a quarter of the way 110 → 116
        with self.assertRaises(ValueError):
            sr.scale_ops(opening, [{"op": "path_set", "node": "A", "events": []}], 0.5)

    def test_pending_effect_lands_at_the_ruled_share(self):
        opening, reg = self._fx()
        sc, applied, fatal = sr.replay_side(opening, [], [("R1", "false", 3, 7)], reg, [], "R", scales={"R1": 0.5})
        self.assertEqual(fatal, [])
        self.assertEqual((applied[0]["status"], applied[0]["scale"]), ("applied_scaled", 0.5))
        self.assertAlmostEqual(p_of(sc, "A"), 0.54); self.assertAlmostEqual(p_of(sc, "C"), 0.16)
        full, _, _ = sr.replay_side(opening, [], [("R1", "false", 3, 7)], reg, [], "R")
        self.assertAlmostEqual(p_of(full, "A"), 0.58)                 # without a share: the whole effect, as before

    def test_effect_carried_whole_is_taken_back_to_the_ruled_share(self):
        """The side conceded and applied the whole effect in a round; the ruling says half was hit."""
        opening, reg = self._fx()
        upd = [{"claim": "R1", "applies": "R1#false", "ops": [P_MOVE("C", "A", 0.08)], "why": "conceded"},
               {"claim": "R2", "applies": "R2#false", "ops": reg["R2"]["false"], "why": "conceded"},
               {"claim": "R9", "ops": [P_MOVE("B", "C", 0.02)], "why": "independent"}]
        setts = [("R1", "false", 2, 5), ("R2", "false", 2, 5)]
        sc, applied, fatal = sr.replay_side(opening, upd, setts, reg, [], "R", scales={"R1": 0.5, "R2": 0.5})
        self.assertEqual(fatal, [])
        self.assertEqual([a["status"] for a in applied], ["carried_scaled", "carried_scaled", "update"])
        self.assertAlmostEqual(p_of(sc, "A"), 0.54)                   # 0.50 + half of 0.08
        self.assertAlmostEqual(p_of(sc, "C"), 0.18)                   # 0.20 − 0.04 + the independent 0.02
        self.assertAlmostEqual(next(n["P_T"] for n in sc["nodes"] if n["id"] == "A"), 113.0)   # half of the way to 116
        whole, a2, f2 = sr.replay_side(opening, upd, setts, reg, [], "R")
        self.assertEqual((f2, [a["status"] for a in a2]), ([], ["carried", "carried", "update"]))
        self.assertAlmostEqual(p_of(whole, "A"), 0.58)

    def test_both_sides_registered_effects_scale_alike(self):
        opening, reg = self._fx()
        z_reg = {"R1": {"true": [], "false": [P_MOVE("B", "A", 0.06)], "owner": "R", "claim_rev": "R1.R"}}
        z, app, fatal = sr.replay_side(opening, [], [("R1", "false", 3, 7)], z_reg, [], "Z", scales={"R1": 0.5})
        self.assertEqual(fatal, [])
        self.assertAlmostEqual(p_of(z, "A"), 0.53)

    def test_effect_that_has_no_share_is_reported(self):
        opening, reg = self._fx()
        reg["R1"]["false"] = [{"op": "path_set", "node": "A", "events": ev((15, "close", 110))}]
        _, _, fatal = sr.replay_side(opening, [], [("R1", "false", 3, 7)], reg, [], "R", scales={"R1": 0.5})
        self.assertEqual(len(fatal), 1, fatal)
        self.assertIn("cannot be applied in part", fatal[0])


class TestCarriedConflict(unittest.TestCase):
    """SSNLF 2026-11-03T01:36Z: both sides conceded R2 in opposite directions, so the Zealot carried R2#true while the
    ruling settled R2 false — before this fix no void could repair it (not pending, not executable) and the session
    was stuck at the provisional ruling."""

    def test_conflict_names_the_remedy(self):
        opening, upd, reg, _ = _replay_fixture()
        settled = [("R1", "false", 2, 5)]
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
        self.assertEqual(len(fatal), 1, fatal)
        self.assertIn("conflicts with the settlement R1#false", fatal[0])
        self.assertIn("claim: R1, side: Z, outcome: true", fatal[0])

    def test_void_of_the_carried_outcome_skips_the_update(self):
        opening, upd, reg, _ = _replay_fixture()
        reg["R1"]["false"] = [P_MOVE("A", "C", 0.05)]
        settled = [("R1", "false", 2, 5)]
        voided = [{"claim": "R1", "outcome": "true", "side": "Z", "reason": "carried R1#true contradicts the settlement"}]
        sc, applied, fatal = sr.replay_side(opening, upd, settled, reg, voided, "Z")
        self.assertEqual(fatal, [])
        self.assertEqual([a["status"] for a in applied], ["void_carried", "applied"])
        self.assertEqual(applied[0]["settled"], "R1#false")
        self.assertAlmostEqual(p_of(sc, "A"), 0.45)   # opening 0.50, the carried A→B 0.10 skipped, settled A→C 0.05 applied
        self.assertAlmostEqual(p_of(sc, "B"), 0.30)
        self.assertAlmostEqual(p_of(sc, "C"), 0.25)

    def test_void_of_the_settled_outcome_is_still_rejected(self):
        opening, upd, reg, _ = _replay_fixture()
        reg["R1"]["false"] = [P_MOVE("A", "C", 0.05)]
        settled = [("R1", "false", 2, 5)]
        voided = [{"claim": "R1", "outcome": "false", "side": "Z", "reason": "x"}]
        _, _, fatal = sr.replay_side(opening, upd, settled, reg, voided, "Z")
        self.assertTrue(any("conflicts with the settlement" in f for f in fatal), fatal)
        self.assertTrue(any("void rejected: effect is executable" in f for f in fatal), fatal)

    def _dependent_fixture(self):
        """Drill GENERAL 2026-10-15: a carried split is voided at the ruling, and a later accepted update edits the node the
        split created — before this fix that update failed on a missing node and nothing could repair the replay."""
        opening, _, reg, _ = _replay_fixture()
        split = reg["R2"]["true"][0]
        upd = [{"claim": "R2", "applies": "R2#true", "ops": [dict(split, amount=0.10)], "why": "carried"},
               {"claim": "Z3", "ops": [P_MOVE("C2", "B", 0.05)], "why": "takes back half of the split"},
               {"claim": "Z4", "ops": [P_MOVE("A", "B", 0.02)], "why": "independent"}]
        reg["R2"]["true"] = [dict(split, amount=0.10)]
        voided = [{"claim": "R2", "outcome": "true", "side": "Z", "reason": "carried R2#true contradicts the settlement"}]
        return opening, upd, reg, voided

    def test_update_resting_on_a_voided_carry_goes_with_it(self):
        opening, upd, reg, voided = self._dependent_fixture()
        sc, applied, fatal = sr.replay_side(opening, upd, [("R2", "false", 3, 7)], reg, voided, "Z")
        self.assertEqual(fatal, [])
        self.assertEqual([a["status"] for a in applied], ["void_carried", "void_dependent", "update"])
        self.assertIn("C2 missing", applied[1]["failed"])
        self.assertAlmostEqual(p_of(sc, "A"), 0.48)   # only the independent update landed
        self.assertAlmostEqual(p_of(sc, "B"), 0.32)
        self.assertAlmostEqual(p_of(sc, "C"), 0.20)

    def test_update_that_never_executed_stays_fatal(self):
        opening, upd, reg, voided = self._dependent_fixture()
        upd.append({"claim": "Z5", "ops": [P_MOVE("NOPE", "B", 0.01)], "why": "bad address"})
        _, _, fatal = sr.replay_side(opening, upd, [("R2", "false", 3, 7)], reg, voided, "Z")
        self.assertEqual(len(fatal), 1, fatal)
        self.assertIn("accepted update[3]", fatal[0])

    def test_without_a_void_nothing_is_skipped(self):
        opening, upd, reg, _ = self._dependent_fixture()
        _, applied, fatal = sr.replay_side(opening, upd, [("R2", "true", 3, 7)], reg, [], "Z")
        self.assertEqual(fatal, [])
        self.assertEqual([a["status"] for a in applied], ["carried", "update", "update"])

    def test_other_side_unaffected(self):
        opening, upd, reg, _ = _replay_fixture()
        settled = [("R1", "false", 2, 5)]
        voided = [{"claim": "R1", "outcome": "true", "side": "Z", "reason": "x"}]
        _, applied, fatal = sr.replay_side(opening, [], settled, reg, voided, "R")   # R carried nothing
        self.assertEqual(fatal, [])
        self.assertEqual(applied, [])


if __name__ == "__main__":
    unittest.main()


class TestMixtureEventRemap(unittest.TestCase):
    """2026-11-06: the mixture renumbers nodes to M1..; event.folded_into named the sides' own ids and was never remapped."""

    def test_folded_into_is_remapped_to_mixture_ids(self):
        z = scen(node("A", 0.6, 90.0, events=ev((1, "close", 95.0))), node("B", 0.4, 110.0, events=ev((1, "close", 105.0))))
        r = scen(node("X", 0.5, 90.0, events=ev((1, "close", 95.0))), node("Y", 0.5, 120.0, events=ev((1, "close", 108.0))))
        z["event"] = {"name": "Q3 earnings", "date": "2026-11-17", "folded_into": ["A"], "source": "P01"}
        r["event"] = {"name": "Q3 earnings", "date": "2026-11-17", "folded_into": ["Y"], "source": "P01"}
        A = sr.mixture(z, r, 0.5)
        by_pt = {n["P_T"]: n["id"] for n in A["nodes"]}
        self.assertEqual(A["event"]["folded_into"], sorted([by_pt[90.0], by_pt[120.0]], key=lambda s: int(s[1:])))
        self.assertEqual(A["event"]["folded_into_sides"], {"Z": ["A"], "R": ["Y"]})
        z2 = scen(node("A", 1.0, 90.0, events=ev((1, "close", 95.0))))
        z2["event"] = {"name": "Q3 earnings", "date": "2026-11-17"}
        self.assertNotIn("folded_into", sr.mixture(z2, z2, 0.5)["event"])
