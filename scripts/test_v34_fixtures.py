"""v3.4 frozen-material acceptance run (proposal-ruler_era §⑤ fixtures (a)-(o) + the three GPT round-7 checks).

One command proves the acceptance list: `python -m pytest scripts/test_v34_fixtures.py -q`.

Each `test_<letter>_...` runs the task modules' own fixture tests by name (`_passes`: the unittest cases are run through
a suite, so class fixtures such as the shared mock session of `test_v34_flow.RulerFlowTest` are set up and torn down) and
then asserts one concrete expected value of its own, built from the task modules' helpers (no fixture body is copied).
The three extra checks (`test_x1_...`, `test_x2_...`, `test_x3_...`) and the cost > 0 end-to-end session
(`test_x4_...`, residue R6) run on one mock 3.4 session (`test_v34_flow.run_ruler_session`).

The task test modules are imported as modules (never `from … import <TestCase>`), so pytest does not collect their
test classes a second time here."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import closure_check as cc                      # noqa: E402
import odds_model as om                         # noqa: E402
import odds_snapshot as osn                     # noqa: E402
import scenario_replay as sr                    # noqa: E402
import test_closure_check as t_cc               # noqa: E402
import test_corpus_sync as t_corpus             # noqa: E402
import test_odds_model as t_om               # noqa: E402
import test_run_step as t_rs                    # noqa: E402
import test_scenario_replay as t_sr             # noqa: E402
import test_v34_flow as t_flow                  # noqa: E402
import test_v34_snapshot as t_snap              # noqa: E402
import test_validate_role_record as t_vrr       # noqa: E402
import validate_debate as vd                    # noqa: E402
from ruling import RulingError, derive_ruling   # noqa: E402
from run_step import Session, StepError         # noqa: E402


def _passes(cls, *names):
    """Run the named tests of a unittest.TestCase through a suite (class fixtures included); assert all ran and passed."""
    suite = unittest.TestSuite(cls(n) for n in names)
    res = unittest.TestResult()
    cwd = os.getcwd()
    try:
        suite.run(res)
    finally:
        os.chdir(cwd)
    problems = [(str(t), tb) for t, tb in res.failures + res.errors] + [(str(t), why) for t, why in res.skipped]
    assert problems == [], problems
    assert res.testsRun == len(names), (res.testsRun, names)
    return res.testsRun


class _ViewStore:
    """A read-only view of a session store for derive_ruling: `swap` exchanges the debaters' roles on every record,
    `edit(content) -> content` rewrites each debater final's content. rows() / obj() / ids() as the Store."""
    SWAP = {"Zealot": "Reaper", "Reaper": "Zealot"}

    def __init__(self, store, swap=False, edit=None):
        self.store, self.swap, self.edit = store, swap, edit

    def rows(self):
        out = []
        for r in self.store.rows():
            r = dict(r)
            if self.swap and r.get("by") in self.SWAP:
                r["by"] = self.SWAP[r["by"]]
            out.append(r)
        return out

    def obj(self, oid):
        rec = copy.deepcopy(self.store.obj(oid))
        if rec.get("by") in self.SWAP:
            if self.swap:
                rec["by"] = self.SWAP[rec["by"]]
            kind = next((r.get("kind") for r in self.store.rows() if r["id"] == oid), None)
            if self.edit and kind == "final" and isinstance(rec.get("content"), dict):
                rec["content"] = self.edit(rec["content"])
        return rec

    def ids(self):
        return self.store.ids()


def _run_flow(zero_cost=True):
    tmp = Path(tempfile.mkdtemp(prefix="v34_fixtures_"))
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        rc, ts, routes = t_flow.run_ruler_session(tmp, t_flow.ruler_script(), zero_cost=zero_cost)
        s = Session("TEST", ts, tmp)
        a = json.loads((tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
    finally:
        os.chdir(cwd)
    return {"rc": rc, "s": s, "a": a, "routes": routes, "tmp": tmp}


@pytest.fixture(scope="module")
def flow():
    """The §1.4 mock session of test_v34_flow (cost zeroed, as the worked example has no fees)."""
    return _run_flow(zero_cost=True)


@pytest.fixture(scope="module")
def flow_cost():
    """The same session with the snapshot's real cost (mock root without a daily CSV → the base + 5 fallback)."""
    return _run_flow(zero_cost=False)


def _validate(fl, doc):
    """validate_debate.validate inside the session's mock root (the report's data_files are relative to it)."""
    cwd = os.getcwd()
    os.chdir(fl["tmp"])
    try:
        return vd.validate(doc)
    finally:
        os.chdir(cwd)


def _sorted_atoms(sc):
    return sorted((round(n["p"], 9), round(float(n["P_T"]), 6), json.dumps(n.get("events"), sort_keys=True))
                  for n in sc["nodes"])


# ===================================================================================================== (a)-(d)

def test_a_closing_record_tried_engaged_disposed_stale_same_beat():
    n = _passes(t_cc.FixtureA, "test_empty_tried_rejected", "test_non_returned_evidence_rejected",
                "test_non_targeting_evidence_rejected", "test_engaged_by_opposing_side_accepted",
                "test_engaged_by_naming_a_non_engaging_record_rejected", "test_arbiter_disposition_accepted",
                "test_disposition_on_stale_rev_does_not_count", "test_stale_claim_rev_rejected",
                "test_same_beat_engagement_via_view_seq", "test_later_challenge_naming_other_evidence_does_not_discharge",
                "test_filed_next_keeps_the_claim_open")
    assert n == 11
    recs, spawns = t_cc.base_world()
    ok = t_cc.run(recs, spawns, {"R2": t_cc.closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
    assert (ok["ok"], ok["closable"], ok["open"]) == (True, ["R2"], [])
    empty = t_cc.run(recs, spawns, {"R2": t_cc.closing("R1.R", [])})
    assert (empty["ok"], empty["open"]) == (False, ["R2"])


def test_b_arbiter_unresolved_rejected_before_the_cap():
    _passes(t_cc.FixtureB, "test_arbiter_unresolved_is_fatal", "test_bare_label_is_fatal",
            "test_cap_reports_what_is_still_open_as_unresolved")
    _passes(t_rs.CloseRoundTest, "test_close_round_rejects_arbiter_unresolved_and_bare_labels")
    recs, spawns = t_cc.base_world()
    out = t_cc.run(recs, spawns, {"R2": {"state": "unresolved"}}, max_rounds=20, round_=3)
    assert out["ok"] is False
    assert any("unresolved" in f for f in out["fatal"])
    assert out["closable"] == []


def test_c_debater_deadlocked_stays_open_and_report_lists_fatal_at_release():
    _passes(t_cc.FixtureC, "test_debater_deadlocked_stays_open", "test_release_rebuilds_from_notes_and_ignores_report_lists",
            "test_unmarked_doc_with_35_finals_takes_the_35_gate")
    recs, _ = t_cc.base_world()
    t = t_cc.tally_of(recs)
    assert "R2" in t["open_total"]
    assert t["reaper"]["deadlock_requested"] == ["R2"]
    recs.append(t_cc.notes(11, 3))
    doc = t_cc.report_from_records(recs)
    doc["schema_version"] = "3.4"
    doc["deadlocked"] = ["R2"]
    fatal, _ = vd.closure(doc, t)
    assert any("R2" in f for f in fatal), fatal


def test_d_unengaged_evidence_backed_challenge_blocks_deadlocked():
    _passes(t_cc.FixtureD, "test_unengaged_challenge_blocks", "test_text_mention_does_not_unblock",
            "test_counter_challenge_answering_the_evidence_unblocks")
    _passes(t_cc.EngagementPredicate, "test_held_text_mention_never_counts")
    recs, spawns = t_cc.base_world()
    recs[-1] = t_cc.final(10, "R3.R", [t_cc.claim("R2", status="deadlocked", text="multiples hold")],
                          held=[{"id": "R2", "text": "Z.EV1 does not move R2"}])
    assert cc.engaged("Z.EV1", "R2", recs, spawns) == []      # a text mention alone engages nothing


# ===================================================================================================== (e)-(k)

def test_e_envelope_and_exchange_symmetry_of_the_mixture():
    _passes(t_sr.TestMixture, "test_envelope_outside_range_fatal", "test_envelope_no_common_w_fatal",
            "test_swap_symmetry", "test_worked_example_R_prime")
    r_prime = t_flow.R14_AFTER_Z1
    a = sr.mixture(t_flow.Z14, r_prime, 0.65)
    b = sr.mixture(r_prime, t_flow.Z14, 0.35)
    assert _sorted_atoms(a) == _sorted_atoms(b)
    assert round(om.score(a)["S_ref"], 1) == round(om.score(b)["S_ref"], 1) == 64.7
    A = sr.mixture(t_flow.Z14, r_prime, 0.5)
    hi = next(n for n in A["nodes"] if n["P_T"] == 110.0)
    assert hi["p"] == pytest.approx(0.275)                     # ½·0.55: inside [0.35·0, 0.55] per atom
    hi["p"] = 0.60                                              # the proposal's §1.4 envelope example
    assert any("outside" in f for f in sr.envelope_check(A, t_flow.Z14, r_prime, 0.01))


def test_f_replay_reproduces_and_applies_once_and_forged_fatal_and_order():
    _passes(t_sr.TestReplay, "test_reproduces_hand_built_byte_for_byte", "test_carried_effect_applied_once",
            "test_forged_applies_fatal", "test_non_commuting_order", "test_precondition_failure_voided",
            "test_applies_conflicts_with_settlement")
    _passes(t_vrr.Ingest35Test, "test_forged_unknown_and_double_applies")
    _passes(t_flow.RulerFlowTest, "test_forged_applies_and_cost_bps_rejected_with_retry")
    opening, upd, reg, settled = t_sr._replay_fixture()
    sc, applied, fatal = sr.replay_side(opening, upd, settled, reg, [], "Z")
    assert fatal == []
    assert [x["effect"] for x in applied if x["status"] == "applied"] == ["R2#true", "Z3#true"]
    assert t_sr.p_of(sc, "A") == pytest.approx(0.40)           # R1#true carried by the update, applied once


def test_g_mentality_enum_and_no_position_baseline_strings():
    _passes(t_corpus.MentalityTableSyncTest, "test_table_matches_validator_enum")
    _passes(t_corpus.FormatEnumSyncTest, "test_ruler_no_position_baseline_strings")
    assert vd.MENTALITY_V34 == ("with_trend", "against_trend", "realizing", "thesis_exit", "exposure_adjustment",
                                "continuing", "waiting")
    agent = ROOT / "prompts" / "agent"
    for f in [agent / "manuals/mentality/mentality.md", agent / "principles.md"] + sorted((agent / "formats").glob("*.md")):
        txt = f.read_text(encoding="utf-8")
        assert ("long_1" in txt, "cost_bps" in txt) == (False, False), f.name


def test_h_reflection_one_sided_contracts_same_entry_two_stops_voids():
    _passes(t_om.TieTest, "test_e_self_symmetric_reflection")
    _passes(t_om.CandidatesTest, "test_one_sided", "test_identical_merge_and_different_stops", "test_voids")
    sc = t_om.two_node(0.5, 110, 90, up_events=t_om.leaf_events((3, "close", 104), (4, "open", 104.5), (15, "close", 110)))
    z = {"overweight": {"stop": None, "conditional": [{"name": "brk", "observe": "close > 103", "fill": "next_open"}]}}
    names = sorted(c["strategy"] for c in om.build_candidates(z, {}, sc, []))
    assert names == ["overweight_now", "overweight_on_condition", "underweight_now"]


def test_i_authored_operation_type_changes_nothing(flow):
    _passes(t_om.DeriveTest, "test_signature_has_no_operation_type")
    _passes(t_flow.RulerFlowTest, "test_arbiter_may_not_author_derived_fields")
    s = flow["s"]
    base = derive_ruling(s.store, s, dict(t_flow.ARBITER), "proposed")
    assert s.store.obj("R2.R")["content"]["operation_type"] == "HOLD_NEUTRAL"   # as authored in the session
    for op in ("LIQUIDATE_NOW", "ENTRY", "TRIM_POSITION"):
        edited = _ViewStore(s.store, edit=lambda c, op=op: dict(c, operation_type=op))
        assert edited.obj("R2.R")["content"]["operation_type"] == op
        rec = derive_ruling(edited, s, dict(t_flow.ARBITER), "proposed")
        for k in ("chosen", "q_star", "x_star", "operation_type"):
            assert rec["scenario"]["derived"][k] == base["scenario"]["derived"][k], (op, k)
        assert rec["operation_type"] == base["operation_type"] == "ENTRY"
    assert base["scenario"]["derived"]["chosen"] == "overweight_now"
    with pytest.raises(RulingError, match="derived field authored"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, operation_type="LIQUIDATE_NOW"), "proposed")


def test_j_cost_charges_normalisation_and_cost_selected_strategy():
    _passes(t_om.SimulationAndCostTest, "test_leaf_paths", "test_unfilled_pays_nothing_and_underweight_buyback",
            "test_cost_selects_neutral_then_conditional")
    sc = t_om.two_node(0.5, 105.1, 95.1)
    out = om.compare_strategies(sc, om.build_candidates({}, {}, sc, []), t_om._cfg(cost_bps=20.0), s_s=0.08)
    assert (out["best"], out["x_opt"]) == ("neutral", 0.0)


def test_k_tie_threshold_never_selects_neutral_over_positive_ce():
    _passes(t_om.TieTest, "test_k_action_with_positive_ce_beats_neutral_inside_threshold")
    sc = t_om.two_node(0.5, 103, 97.2)
    out = om.compare_strategies(sc, om.build_candidates({}, {}, sc, []), t_om._cfg(cost_bps=0.0), s_s=0.08)
    assert out["best"] == "overweight_now"
    assert 0.0 < out["strategies"]["overweight_now"]["CE"] < t_om.CFG["tie_threshold"]


# ===================================================================================================== (l)-(o)

def test_l_wrong_authored_scores_overwritten_and_kept(flow):
    _passes(t_flow.RulerFlowTest, "test_debater_scores_derived_authored_kept", "test_arbiter_stages_derive_from_A",
            "test_report_fields")
    s, a = flow["s"], flow["a"]
    got = {oid: (s.store.obj(oid)["content"]["composite_score"], s.store.obj(oid)["content"]["authored_score"])
           for oid in ("R1.Z", "R1.R", "R2.Z", "R2.R", "PROV", "PROPOSED", "RULING.v1")}
    assert got == {"R1.Z": (79, 99), "R1.R": (27, 1), "R2.Z": (79, 98), "R2.R": (39, 2),
                   "PROV": (59, 97), "PROPOSED": (59, 96), "RULING.v1": (59, 95)}
    assert (a["composite_score"], a["authored_score"]) == (59, 95)
    assert (s.store.obj("SIGN1.Z")["content"]["endorsed_composite_score"], s.store.obj("SIGN1.Z")["content"]["authored_score"]) == (77, 11)
    assert (s.store.obj("SIGN1.R")["content"]["endorsed_composite_score"], s.store.obj("SIGN1.R")["content"]["authored_score"]) == (42, 12)


def test_m_session_opened_before_the_ruler_is_refused():
    import run_step as rs
    _passes(t_snap.SessionBeforeRulerTest, "test_session_without_odds_is_refused")
    assert rs.SESSION_BEFORE_RULER.startswith("session opened before v3.4")
    for gone in ("reconstruct_34_snapshot",):
        assert not hasattr(osn, gone), gone




def test_o_revisions_signoff_endorsement_only_and_objection_rebuilds_A(flow):
    _passes(t_flow.RulerFlowTest, "test_signoff_only_revision_leaves_the_mixture_inputs_untouched",
            "test_objection_revision_enters_the_replay_when_the_redraft_changes",
            "test_objection_revision_endorsement_and_dissent")
    s, a = flow["s"], flow["a"]
    # the Zealot objected to nothing; its signoff-only revision gives the endorsed score, the sealed mixture is untouched
    assert s.store.obj("OBJ1.Z")["content"]["objections"] == []
    assert a["dissent"]["zealot_composite_score"] == 77
    sealed = s.store.obj("RULING.v1")["content"]["scenario"]["mixture"]
    assert json.dumps(a["scenario"]["mixture"], sort_keys=True) == json.dumps(sealed, sort_keys=True)
    # a revision filed before sealing and incorporated (the redraft changed w): A and the strategy table are rebuilt
    moved = derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture={"w": 0.6, "reason": None},
                                              objection_dispositions=[]), "ruling")
    r_rev = sr.apply_ops(t_flow.R14_AFTER_Z1, t_flow.R_OBJ_REV["ops"])
    want = sr.mixture(t_flow.Z14, r_rev, 0.6)
    without = sr.mixture(t_flow.Z14, t_flow.R14_AFTER_Z1, 0.6)
    assert _sorted_atoms(moved["scenario"]) == _sorted_atoms(want)
    assert moved["scenario"]["derived"]["s_ref"] == om.score(want)["S_ref"] != om.score(without)["S_ref"]
    assert moved["scenario"]["derived"]["replay"]["revisions_included"] is True


# ===================================================================================================== extra checks

def test_x1_exchange_invariance_through_the_finalize_path(flow):
    """Swap the sides' records (Zealot ↔ Reaper on every record), w → 1 − w: the same A, strategy and score."""
    s = flow["s"]
    for w in (0.5, 0.3, 0.7):
        base = derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture={"w": w, "reason": None}), "proposed")
        swapped = derive_ruling(_ViewStore(s.store, swap=True), s,
                                   dict(t_flow.ARBITER, mixture={"w": round(1 - w, 10), "reason": None}), "proposed")
        A, B = base["scenario"], swapped["scenario"]
        assert sr.canonical(A["mixture"]["z_prime"]) == sr.canonical(B["mixture"]["r_prime"])
        assert sr.canonical(A["mixture"]["r_prime"]) == sr.canonical(B["mixture"]["z_prime"])
        assert _sorted_atoms(A) == _sorted_atoms(B), w
        for k in ("chosen", "q_star", "x_star", "operation_size", "operation_type", "s_ref"):
            assert A["derived"][k] == B["derived"][k], (w, k)
        assert sorted(c["id"] for c in A["derived"]["candidates"]) == sorted(c["id"] for c in B["derived"]["candidates"])
        assert (base["composite_score"], base["operation_target"]) == (swapped["composite_score"], swapped["operation_target"])
    assert derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture={"w": 0.7, "reason": None}),
                            "proposed")["composite_score"] == 67


def test_x2_hand_edited_scenario_in_a_ruling_json_rejected(flow):
    s = flow["s"]
    ruling = copy.deepcopy(s.store.obj("RULING.v1")["content"])
    for k in ("operation_type", "operation_target", "operation_size", "operation_execution", "composite_score"):
        ruling.pop(k, None)
    ruling["scenario"]["nodes"][0]["p"] += 0.05
    ruling["scenario"]["nodes"][1]["p"] -= 0.05
    with pytest.raises(StepError) as exc:
        s._derive(ruling, "ruling")
    assert "derived field authored: scenario" in str(exc.value)
    assert "nothing archived" in str(exc.value)


def test_x3_applies_on_a_claim_rejected_by_claims_review_is_fatal_at_release(flow):
    """R's update carries `applies: "Z1#true"`; a report whose claims_review rejects Z1 FATALs at the release gate."""
    a = flow["a"]
    fatal, _ = _validate(flow, copy.deepcopy(a))
    assert fatal == [], fatal
    doc = copy.deepcopy(a)
    doc["claims_review"] = [dict(doc["claims_review"][0], verdict="rejected")]
    fatal, _ = _validate(flow, doc)
    assert any("replay R′" in f and "Z1#true" in f and "conflicts" in f for f in fatal), fatal
    # the same input is refused at the arbiter stage before anything is archived
    s = flow["s"]
    with pytest.raises(RulingError, match="replay failed"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, claims_review=doc["claims_review"]), "proposed")
    # and a tampered Z′ that still mixes into A is not reproduced by the replay
    doc = copy.deepcopy(a)
    doc["scenario"]["mixture"]["z_prime"]["nodes"][0]["label"] = "edited"
    fatal, _ = _validate(flow, doc)
    assert any("z_prime is not reproduced" in f for f in fatal), fatal


def test_x3b_duplicate_claims_review_entry_is_fatal(flow):
    """Residue R3: two claims_review entries for one claim."""
    s = flow["s"]
    dup = list(t_flow.ARBITER["claims_review"]) * 2
    with pytest.raises(RulingError, match="duplicate claims_review entry for Z1"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, claims_review=dup), "proposed")
    doc = copy.deepcopy(flow["a"])
    doc["claims_review"] = dup
    assert any("duplicate claims_review entry for Z1" in f for f in _validate(flow, doc)[0])


def test_x4_cost_above_zero_end_to_end(flow, flow_cost):
    """Residue R6: the same session with the snapshot's real cost: the gate passes, q* is below the zero-cost q*, and
    scenario.derived.cost_bps equals the snapshot's."""
    a0, a1 = flow["a"], flow_cost["a"]
    assert flow_cost["rc"]["ok"] and flow_cost["rc"]["gate"]["ok"], flow_cost["rc"]
    snap = flow_cost["s"].state["odds"]
    assert snap["cost_bps"] == 7.0                                 # US base 2 + 5: the mock root has no daily CSV
    assert "no daily CSV" in snap["unit_basis"]["fallback"]
    assert a1["scenario"]["derived"]["cost_bps"] == snap["cost_bps"] == a1["odds"]["cost_bps"]
    assert a0["scenario"]["derived"]["cost_bps"] == 0.0
    q0, q1 = a0["scenario"]["derived"]["q_star"], a1["scenario"]["derived"]["q_star"]
    assert q0 == pytest.approx(0.791, abs=0.01)
    assert q1 < q0
    # 0.761 under trigger_price; next_price (2026-11) no longer charges a second fill for a valuation-day close stop
    assert q1 == pytest.approx(0.775, abs=0.01)
    assert a1["composite_score"] == a0["composite_score"] == 59      # the score is S_ref of A: cost does not move it


def test_x5_35_receipts_ask_for_the_ruling_sheet_not_the_scenario(flow):
    """Residue R1: the provisional / audit receipts of a 3.4 session."""
    import run_step as rs
    for txt in (rs.PROV_NEXT, rs.AUDIT_NEXT):
        assert "mixture" in txt and "claims_review" in txt
        assert "adopted `scenario`" not in txt and "write the adopted scenario" not in txt
    st = flow["s"]
    assert st.schema() == "3.4"
    cwd = os.getcwd()
    os.chdir(flow["tmp"])
    try:
        prov = st.finalize("provisional")
        audit = st.finalize("audit")
    finally:
        os.chdir(cwd)
    assert (prov["next"], audit["next"]) == (rs.PROV_NEXT, rs.AUDIT_NEXT)


def _c12_boundary_checks(C, c11, c12):
    """C12 follows C11; its boundary is either unset ("TBD" on both sides: C11 stays open, C12 matches nothing) or one
    deploy minute (C11.to == C12.from, a valid ISO minute: C11 before it, C12 from it on)."""
    from datetime import datetime, timedelta
    assert (c12["to"], c12["era"]) == (None, "v3.4")
    assert C.era_of("C12") == "v3.4"
    if c12["from"] == "TBD":
        assert c11["to"] == "TBD"
        assert C.cohort_of("2026-10-31T10:00Z") == "C11"
        assert C.cohort_of("2027-06-01T00:00Z") == "C11"
        return "unset"
    cut = c12["from"]
    assert c11["to"] == cut, (c11["to"], cut)
    dt = datetime.strptime(cut, "%Y-%m-%dT%H:%MZ")                       # a deploy minute, nothing else
    assert dt > datetime(2026, 10, 31, 0, 0)                               # after the withdrawn 10-29 deployment
    before = (dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%MZ")
    assert (C.cohort_of(before), C.cohort_of(cut), C.cohort_of("2099-01-01T00:00Z")) == ("C11", "C12", "C12")
    return "set"


def test_x6_cohort_c12_present_and_unset():
    """Residue R2 (+ 2nd fix wave G8, retconned 10-31): C12 (v3.4) follows C11; the withdrawn 10-29 line is gone; the repo
    may carry the boundary unset or as the deploy minute (§5 of the operators page writes it) — both parse. The unset and
    the populated cases each run on a fixture."""
    from datalayer import cohorts as C
    ids = [c["id"] for c in C.COHORTS]
    k = ids.index("C12")
    assert ids[k - 1] == "C11"
    assert "2026-10-29T02:55Z" not in json.dumps(C.COHORTS)
    if k + 1 < len(ids):   # a later cohort (C13 = v3.5) opens exactly where C12 closes
        assert C.COHORTS[k]['to'] == C.COHORTS[k + 1]['from']
    as_was = list(C.COHORTS[:k]) + [dict(C.COHORTS[k], to=None)]   # the C11/C12 boundary while C12 was the latest
    with mock.patch.object(C, "COHORTS", as_was):
        c11, c12 = C.COHORTS[-2], C.COHORTS[-1]
        assert "withdrawn" in c12["label"]
        assert _c12_boundary_checks(C, c11, c12) in ("unset", "set")           # whatever the repo carries
        unset = [dict(c11, to="TBD"), dict(c12, **{"from": "TBD"})]
        with mock.patch.object(C, "COHORTS", list(C.COHORTS[:-2]) + unset):
            assert _c12_boundary_checks(C, *unset) == "unset"
        cut = "2026-11-02T09:00Z"
        populated = [dict(c11, to=cut), dict(c12, **{"from": cut})]
        with mock.patch.object(C, "COHORTS", list(C.COHORTS[:-2]) + populated):
            assert _c12_boundary_checks(C, *populated) == "set"
            assert (C.cohort_of("2026-11-02T08:59Z"), C.cohort_of("2026-11-02T09:00Z")) == ("C11", "C12")


def test_x7_audit_correction_kind_counts_as_the_audit_entry(flow):
    """Residue R4: final.md's 3.4 basis item {kind: "audit correction", ...} is the audit entry the gate looks for when
    the first ruling and the final differ (the WARN used to look only for a 3.4 `audit` key)."""
    doc = copy.deepcopy(flow["a"])
    doc["provisional_ruling"]["composite_score"] = 55              # pretend the first ruling differed
    msg = "residual_adjustment.basis has no audit entry"
    _, warn = _validate(flow, doc)
    assert any(msg in w for w in warn), warn
    doc["residual_adjustment"] = {"basis": [{"kind": "audit correction", "issue": "Z1",
                                             "reason": "Z1's registered effect was missing from R′ in PROV"}]}
    _, warn = _validate(flow, doc)
    assert not any(msg in w for w in warn), warn
