"""v3.4 end-to-end on the mock engine (contracts C2 / C3 / C6 / C8; proposal §1.4 worked example).

Z: A .55→110 / B .30→100 / C .15→90; R: A .25→108 / B .35→98 / C .40→88. Z1 (guidance raised) settles for Z and is carried by
R's `applies: "Z1#true"` update (R's registered effect = its challenge R1's if_true, p_move C→A .10); R2 (multiple compression)
is deadlocked with a complete closing record (Z.EV1 targets R2, R's round-2 update refs it); the arbiter sets w = 0.5 →
A has S_ref 58.9 and composite 59 at provisional, proposed and ruling. Reaper files a scenario_revision at the objection
stage (endorsement only: the redraft keeps claims_review and mixture), Zealot a signoff-only revision (fixture o). Wrong
authored scores everywhere (fixture l) are overwritten and kept as authored_score. A forged `applies` and a debater
`cost_bps` are rejected at ingestion with retry: true.

Module-level helpers (importable by later tasks): Z14, R14, ruler_sc, ruler_claim, ruler_final, ARBITER, ruler_script, run_ruler_session."""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("debate_driver", ROOT / "scripts" / "debate_driver.py")
dd = importlib.util.module_from_spec(spec); spec.loader.exec_module(dd)
from run_step import Session, MockTools  # noqa: E402
from debate_store import role_record     # noqa: E402
import odds_model as om                   # noqa: E402
import scenario_replay as sr             # noqa: E402


# the mock ruler session lives in debate_driver (its --self-test runs it); re-exported here for the fixture suites
from debate_driver import (Z_EXEC, R_EXEC, Z14, R14, MOVE_Z1, R14_AFTER_Z1, Z_C1, R_C1, R_C2, Z_FIX_SETTLED,  # noqa: E402,F401
                           R_C1_DONE, R_C2_DEAD, UPD_Z1, UPD_R2, FORGED, CLOSING_R2, Z_SIGN_REV, R_OBJ_REV, _node,
                           ruler_sc as ruler_sc, ruler_claim as ruler_claim, ruler_final as ruler_final, ARBITER as ARBITER,
                           ruler_script as ruler_script, run_ruler_session as run_ruler_session)


def s_ref(sc) -> float:
    return om.score(om.validate_scenario(sc, om.DEFAULT_CONFIG)[2])["S_ref"]


class RulerFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="v34_test_"))
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.rc, cls.ts, cls.routes = run_ruler_session(cls.tmp, ruler_script())
        cls.s = Session("TEST", cls.ts, cls.tmp)
        cls.a = json.loads((cls.tmp / cls.rc["analysis_path"]).read_text(encoding="utf-8")) if cls.rc.get("analysis_path") else {}

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)

    def obj(self, oid):
        return self.s.store.obj(oid)["content"]

    def test_session_passes_the_gate(self):
        self.assertTrue(self.rc["ok"], self.rc)
        self.assertTrue(self.rc["gate"]["ok"], self.rc["gate"])
        self.assertEqual(self.s.state["status"], "done")
        self.assertEqual(self.s.state.get("deadlocked"), ["R2"])
        self.assertEqual(self.a["schema_version"], "3.4")

    def test_forged_applies_and_cost_bps_rejected_with_retry(self):
        rejected = [r for r in self.routes if not r["ok"]]
        self.assertEqual(len(rejected), 1, self.routes)
        self.assertTrue(rejected[0]["retry"])
        why = {a["role"]: a["rejected"] for a in rejected[0]["archived"] if a.get("rejected")}
        self.assertIn("applies does not match the registered effect", why["Reaper"])
        self.assertIn("cost_bps", why["Zealot"])
        self.assertEqual(self.s.state["rejects"], {"R2.Zealot": 1, "R2.Reaper": 1})

    def test_debater_scores_derived_authored_kept(self):
        for oid, sc, authored in (("R1.Z", Z14, 99), ("R1.R", R14, 1), ("R2.Z", Z14, 98), ("R2.R", R14_AFTER_Z1, 2)):
            c = self.obj(oid)
            self.assertEqual(c["composite_score"], round(s_ref(sc)), oid)
            self.assertEqual(c["authored_score"], authored, oid)
        self.assertEqual(self.obj("R2.R")["composite_score"], 39)
        for e in self.a["debate_history"]:
            self.assertNotIn(e["content"]["composite_score"], (1, 2, 98, 99))

    def test_report_carries_the_simple_card(self):
        self.assertIn("operation_simple", self.a)
        self.assertEqual(self.a["operation_simple"], self.obj("RULING.v1")["operation_simple"])

    def test_arbiter_stages_derive_from_A(self):
        for oid, authored in (("PROV", 97), ("PROPOSED", 96), ("RULING.v1", 95)):
            c = self.obj(oid)
            A = c["scenario"]
            self.assertEqual(A["mixture"]["w"], 0.5, oid)
            self.assertAlmostEqual(om.score(A)["S_ref"], 58.9, delta=0.05)
            self.assertEqual(c["composite_score"], round(om.score(A)["S_ref"]), oid)
            self.assertEqual(c["composite_score"], 59, oid)
            self.assertEqual(c["authored_score"], authored, oid)
            self.assertEqual(c["operation_type"], "ENTRY", oid)
            self.assertEqual(A["derived"]["chosen"], "overweight_now", oid)
            self.assertAlmostEqual(A["derived"]["q_star"], 0.791, delta=0.01)
            self.assertEqual(c["operation_size"], A["derived"]["operation_size"])
            self.assertEqual(c["operation_simple"]["x_star"], A["derived"]["simple"]["x_star"], oid)
            self.assertEqual(sr.canonical(A["mixture"]["z_prime"]), sr.canonical(Z14))
            self.assertEqual(sr.canonical(A["mixture"]["r_prime"]), sr.canonical(R14_AFTER_Z1))

    def test_report_fields(self):
        a = self.a
        self.assertEqual(a["composite_score"], 59)
        self.assertEqual(a["authored_score"], 95)
        self.assertAlmostEqual(a["s_ref"], 58.9, delta=0.05)
        self.assertEqual(a["scenario"]["mixture"]["w"], 0.5)
        self.assertEqual(a["operation_type"], "MARKET_ENTRY")
        self.assertEqual(a["provisional_ruling"]["composite_score"], 59)
        self.assertEqual(a["provisional_ruling"]["authored_score"], 97)
        self.assertNotIn("baseline", a)
        self.assertNotIn("baseline", a["scenario"])
        self.assertEqual(a["odds"]["schema"], "3.4")
        self.assertNotIn("fatal", a["scenario"]["derived"])
        for c in a["scenario"]["derived"]["candidates"]:
            self.assertIn("provenance", c)
        # release-gate carry-overs (Task 3): closing records, evidence ids / owners / filings, spawns
        self.assertEqual(a["arbiter_log"][-1]["terminal_open"], {"R2": CLOSING_R2})
        ev = next(e for e in a["evidence"] if e.get("evidence_id") == "Z.EV1")
        self.assertEqual(ev["requested_by"], "Zealot")
        self.assertEqual(ev["request"]["target_claim"], "R2")
        self.assertTrue(all(e.get("evidence_id") or e.get("model_call_id") for e in a["evidence"]))
        self.assertIn("R2.R", a["spawns"])

    def test_objection_revision_endorsement_and_dissent(self):
        obj = self.obj("OBJ1.R")
        endorsed_r = sr.apply_ops(R14_AFTER_Z1, R_OBJ_REV["ops"])
        self.assertEqual(sr.canonical(obj["endorsed_scenario"]), sr.canonical(endorsed_r))
        d = self.a["dissent"]
        self.assertEqual(d["reaper_composite_score"], round(s_ref(endorsed_r)))
        self.assertEqual(d["reaper_composite_score"], 42)
        self.assertNotEqual(d["reaper_composite_score"], round(s_ref(R14_AFTER_Z1)))
        self.assertEqual(sr.canonical(d["reaper_endorsed_scenario"]), sr.canonical(endorsed_r))
        self.assertEqual(self.obj("SIGN1.R")["authored_score"], 12)
        self.assertEqual(self.a["s_ref_r"], s_ref(endorsed_r))

    def test_signoff_only_revision_leaves_the_mixture_inputs_untouched(self):
        endorsed_z = sr.apply_ops(Z14, Z_SIGN_REV["ops"])
        d = self.a["dissent"]
        self.assertEqual(d["zealot_composite_score"], round(s_ref(endorsed_z)))
        self.assertEqual(d["zealot_composite_score"], 77)
        self.assertEqual(self.obj("SIGN1.Z")["authored_score"], 11)
        self.assertEqual(self.obj("SIGN1.Z")["endorsed_composite_score"], 77)
        sealed = self.obj("RULING.v1")["scenario"]["mixture"]
        for k in ("z_prime", "r_prime"):
            self.assertEqual(json.dumps(self.a["scenario"]["mixture"][k], sort_keys=True), json.dumps(sealed[k], sort_keys=True))
        self.assertEqual(sr.canonical(self.a["scenario"]["mixture"]["z_prime"]), sr.canonical(Z14))

    def test_tally_lines_show_the_per_side_ruler(self):
        txt = (self.s.store.dir / "_tally_R2.txt").read_text(encoding="utf-8")
        self.assertIn("S_ref 78.6", txt)
        self.assertIn("S_ref 39.4", txt)
        self.assertIn("q*", txt)
        self.assertIn("x*", txt)
        self.assertIn("best overweight_now", txt)

    def test_tally_shows_what_pending_effects_can_take_back(self):
        """v3.7: per side, the lean a pending registered effect could take back and the lean no registered effect reaches."""
        r1 = (self.s.store.dir / "_tally_R1.txt").read_text(encoding="utf-8")
        z = next(ln for ln in r1.splitlines() if ln.strip().startswith("Z short-term"))
        r = next(ln for ln in r1.splitlines() if ln.strip().startswith("R short-term"))
        self.assertIn("pending registered effects none", z)            # Z1 registers no effect: the whole lean is unstaked
        self.assertRegex(z, r"settlement can take back 0\.0; \d+\.\d rests on no registered effect")
        self.assertIn("Z1#true +", r)                                   # R's challenge of Z1 moves its tree up if Z1 holds
        self.assertRegex(r, r"settlement can take back \d+\.\d;")
        r2 = (self.s.store.dir / "_tally_R2.txt").read_text(encoding="utf-8")
        r_after = next(ln for ln in r2.splitlines() if ln.strip().startswith("R short-term"))
        self.assertIn("pending registered effects none", r_after)      # carried in round 2: no longer pending
        # v3.8 candidate: what the round's updates did — the registered effect applied whole, and the node it passed over
        self.assertIn("this round's updates: Z1#true registered effect applied whole, C→A past B", r_after)
        self.assertIn("for the arbiter to check against the hit", r_after)
        self.assertNotIn("this round's updates", r)                    # round 1: the opening files no update

    def test_neutral_tree_still_shows_where_its_effects_land(self):
        """Drill MU 2026-11-09 (v38, round 1): the sceptic opened at 49.1 with every registered effect moving mass up and
        past a node, and the line said nothing because the lean was under one point."""
        import scenario_replay as sr
        sc = ruler_sc([_node("A", 0.30, 90.0, "down", ["R1"]), _node("B", 0.40, 100.5, "flat", ["R1"]), _node("C", 0.30, 110.0, "up", ["R1"])])
        s = self.s
        old = s._pending_effects
        s._pending_effects = lambda lens, role: [("R1#false", [{"op": "p_move", "from": "A", "to": "C", "amount": 0.05}])]
        try:
            text = s._stake_part(sc, None, "Reaper", 50.4)
        finally:
            s._pending_effects = old
        self.assertIn("(at neutral): pending registered effects R1#false +", text)
        self.assertIn("every registered effect moves a neutral tree the same way", text)
        self.assertIn("R1#false A→C past B", text)
        self.assertNotIn("rests on no registered effect", text)

    def test_ruling_sheet_accepts_partly_only_with_a_share_and_a_reason(self):
        """v3.8: `partly` = the claim failed in part; the share of the registered effect and what was hit are required."""
        import ruling as rl
        base = {"mixture": {"w": 0.5, "reason": None}, "claims_review": []}
        ok = dict(base, claims_review=[{"claim": "Z1", "verdict": "partly", "scale": 0.5, "reason": "size hit, mechanism stands"}])
        self.assertEqual(rl._authored_checks(ok)[0], 0.5)
        for bad in ({"claim": "Z1", "verdict": "partly", "reason": "x"}, {"claim": "Z1", "verdict": "partly", "scale": 1.0, "reason": "x"},
                    {"claim": "Z1", "verdict": "partly", "scale": 0.5}, {"claim": "Z1", "verdict": "half"}):
            with self.assertRaises(rl.RulingError):
                rl._authored_checks(dict(base, claims_review=[bad]))

    def test_release_gate_rejects_a_tampered_score(self):
        from validate_debate import validate
        doc = copy.deepcopy(self.a)
        doc["composite_score"] = 60
        fatal, _ = validate(doc)
        self.assertTrue(any("composite_score" in f and "derivation" in f for f in fatal), fatal)
        doc = copy.deepcopy(self.a)
        doc["scenario"]["nodes"][0]["p"] += 0.05
        doc["scenario"]["nodes"][1]["p"] -= 0.05
        fatal, _ = validate(doc)
        self.assertTrue(any("mixture" in f for f in fatal), fatal)

    def test_arbiter_may_not_author_derived_fields(self):
        from ruling import RulingError, derive_ruling
        for k, v in (("operation_type", "ENTRY"), ("scenario", Z14), ("operation_target", "100"), ("operation_size", "N/A")):
            with self.assertRaises(RulingError) as cm:
                derive_ruling(self.s.store, self.s, dict(ARBITER, **{k: v}), "proposed")
            self.assertIn("derived field authored", str(cm.exception))
        with self.assertRaises(RulingError) as cm:
            derive_ruling(self.s.store, self.s, dict(ARBITER, mixture={"w": 0.9, "reason": None}), "proposed")
        self.assertIn("mixture.reason", str(cm.exception))
        rec = derive_ruling(self.s.store, self.s, dict(ARBITER, mixture={"w": 0.9, "reason": "R2 + Z.EV1"}), "proposed")
        self.assertAlmostEqual(om.score(rec["scenario"])["S_ref"], 74.6, delta=0.1)   # proposal §1.4 table, w = 0.9
        self.assertNotIn("authored_score", rec)

    def test_objection_revision_enters_the_replay_when_the_redraft_changes(self):
        from ruling import derive_ruling
        same = derive_ruling(self.s.store, self.s, dict(ARBITER, objection_dispositions=[]), "ruling")
        self.assertFalse(same["scenario"]["derived"]["replay"]["revisions_included"])
        moved = derive_ruling(self.s.store, self.s, dict(ARBITER, mixture={"w": 0.6, "reason": None},
                                                             objection_dispositions=[]), "ruling")
        self.assertTrue(moved["scenario"]["derived"]["replay"]["revisions_included"])
        self.assertEqual(sr.canonical(moved["scenario"]["mixture"]["r_prime"]),
                         sr.canonical(sr.apply_ops(R14_AFTER_Z1, R_OBJ_REV["ops"])))
        self.assertEqual(sr.canonical(moved["scenario"]["mixture"]["z_prime"]), sr.canonical(Z14))

    def test_revision_predecessor_and_ops_verified(self):
        from run_step import StepError
        with self.assertRaises(StepError) as cm:
            self.s._with_endorsement("Zealot", {"scenario_revision": dict(Z_SIGN_REV, predecessor="R1.Z")}, signoff=True)
        self.assertIn("predecessor", str(cm.exception))
        with self.assertRaises(StepError) as cm:   # Reaper's head is its objection-stage revision, not R2.R
            self.s._with_endorsement("Reaper", {"scenario_revision": dict(R_OBJ_REV, predecessor="R2.R")}, signoff=True)
        self.assertIn("OBJ1.R", str(cm.exception))
        bad = {"predecessor": "OBJ1.R", "ops": [{"op": "p_move", "from": "C", "to": "A", "amount": 0.9}], "why": "w"}
        with self.assertRaises(StepError) as cm:
            self.s._with_endorsement("Reaper", {"scenario_revision": bad}, signoff=True)
        self.assertIn("op[0]", str(cm.exception))
        ok = self.s._with_endorsement("Reaper", {"reservation": "r"}, signoff=True)
        self.assertEqual(ok["endorsed_composite_score"], 42)
        self.assertNotIn("authored_score", ok)

    # ---------------------------------------------------------------- review fix round 1

    def test_report_records_carry_store_seq_and_raw_spawns(self):
        import closure_check as cc
        seqs = {r["id"]: i for i, r in enumerate(self.s.store.rows(), 1)}
        recs = cc.records_from_report(self.a)
        self.assertTrue(recs)
        for r in recs:
            self.assertEqual(r["seq"], seqs[r["id"]], r["id"])
        self.assertEqual([r["seq"] for r in recs], sorted(r["seq"] for r in recs))
        self.assertEqual(self.a["spawns"], self.s.state["spawns"])
        for e in self.a["debate_history"] + self.a["evidence"] + self.a["arbiter_log"]:
            self.assertIsInstance(e.get("seq"), int)

    def test_release_gate_rejects_a_tampered_simple_card(self):
        from validate_debate import validate
        for mutate in (lambda d: d["operation_simple"].__setitem__("q_star", 123),
                       lambda d: d["operation_simple"].__setitem__("operation_type", "LIQUIDATE_NOW"),
                       lambda d: d["scenario"]["derived"]["simple"].__setitem__("x_star", 0.9),
                       lambda d: d.pop("operation_simple"),
                       lambda d: d["operation_simple"].update(q_star=123, primary_lens="X"),   # a forged lens-summary marker
                       lambda d: (d.pop("operation_simple"), d["scenario"]["derived"].pop("simple"))):   # both removed
            doc = copy.deepcopy(self.a)
            mutate(doc)
            fatal, _ = validate(doc)
            self.assertTrue(any("simple" in f for f in fatal), fatal)

    def test_gate_rebuilds_candidates(self):
        from validate_debate import validate
        doc = copy.deepcopy(self.a)
        doc["scenario"]["derived"]["candidates"][0]["provenance"] = [{"side": "R", "round": 1}]
        fatal, _ = validate(doc)
        self.assertTrue(any("candidates differ" in f for f in fatal), fatal)
        doc = copy.deepcopy(self.a)
        doc["scenario"]["derived"]["candidates"].append(dict(doc["scenario"]["derived"]["candidates"][0], id="forged"))
        fatal, _ = validate(doc)
        self.assertTrue(any("candidates differ" in f for f in fatal), fatal)

    def test_gate_requires_the_scenarios_scores_derive_from(self):
        from validate_debate import validate
        for path, want in ((("provisional_ruling", "scenario"), "provisional_ruling.scenario missing"),
                           (("dissent", "reaper_endorsed_scenario"), "dissent.reaper_endorsed_scenario missing")):
            doc = copy.deepcopy(self.a)
            del doc[path[0]][path[1]]
            fatal, _ = validate(doc)
            self.assertTrue(any(want in f for f in fatal), (want, fatal))
        doc = copy.deepcopy(self.a)
        del doc["debate_history"][0]["content"]["scenario"]
        fatal, _ = validate(doc)
        self.assertTrue(any("history[0]: scenario missing" in f for f in fatal), fatal)
        doc = copy.deepcopy(self.a)
        del doc["dissent"]["reaper_endorsed_scenario"]
        fatal, _ = validate(doc, pre_signoff=True)
        self.assertFalse(any("endorsed_scenario missing" in f for f in fatal), fatal)

    def test_gate_reads_the_reports_ruler(self):
        import validate_debate as vd
        doc = copy.deepcopy(self.a)
        doc["odds"]["p_tolerance"] = 0.0123
        self.assertEqual(vd._doc_cfg(doc)["p_tolerance"], 0.0123)
        doc.pop("odds")
        self.assertIsNone(vd._doc_cfg(doc))
        fatal, _ = vd.validate(doc)
        self.assertTrue(any("3.4 report without odds snapshot" in f for f in fatal), fatal)

    def test_general_bare_level_refused(self):
        """Review I1: a GENERAL 3.4 report names the lens its level belongs to (`<lens>=<level>`); a bare level is a
        re-derivation mismatch at the gate (end-to-end GENERAL session: RulerGeneralFlowTest)."""
        from validate_debate import validate
        doc = copy.deepcopy(self.a)
        doc["symbol"] = "GENERAL"
        self.assertTrue(any("operation_target" in f and "TEST=100" in f for f in validate(doc)[0]))

    def test_no_3_4_ledger_drift_warning(self):
        self.assertFalse(any("ledger arithmetic" in w for w in self.rc["gate"].get("warnings", [])), self.rc["gate"])

    def test_side_without_execution(self):
        from ruling import derive_ruling

        class FakeStore:
            def __init__(self, recs):
                self.recs = recs

            def rows(self):
                return [{"id": r["id"], "kind": "final", "by": r["by"], "round": r["round"]} for r in self.recs]

            def obj(self, oid):
                return next(r for r in self.recs if r["id"] == oid)

            def ids(self):
                return {r["id"] for r in self.recs}

        z = copy.deepcopy(Z14)
        del z["execution"]
        store = FakeStore([{"id": "R1.Z", "by": "Zealot", "round": 1, "content": ruler_final(50, [Z_C1], z)},
                           {"id": "R1.R", "by": "Reaper", "round": 1, "content": ruler_final(50, [R_C1, R_C2], R14)}])
        session = type("S", (), {"state": {"odds": self.s.state["odds"]}, "root": ROOT})()   # the ruler is the snapshot
        rec = derive_ruling(store, session, dict(ARBITER, claims_review=[]), "proposed")
        nows = {c["id"]: c for c in rec["scenario"]["derived"]["candidates"]}
        self.assertIsNone(nows["overweight_now"]["stop"])
        self.assertEqual(nows["underweight_now"]["stop_source"], "R")


if __name__ == "__main__":
    unittest.main()
