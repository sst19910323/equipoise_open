"""v3.4 whole-branch review fixes (final-review.md: C1, M1, M5, I1-I6, M2, M8, ledger T5 / T7), one regression test each.

Most tests run on the §1.4 mock session of test_v34_flow (`run_ruler_session`); I1 runs the same script as a GENERAL session
(lenses SPY / QQQ / DIA, P0 = the primary lens SPY's opening price) end to end: open → rounds → ruling → gate."""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import odds_model as om                         # noqa: E402
import ruling as rlg                         # noqa: E402
import run_step as rs                           # noqa: E402
import scenario_replay as sr                    # noqa: E402
import test_v34_flow as t_flow                  # noqa: E402
import validate_debate as vd                    # noqa: E402
from ruling import RulingError, derive_ruling   # noqa: E402
from run_step import MockTools, Session         # noqa: E402
from validate_role_record import ingest      # noqa: E402

TS = "20260930T000000Z"


def _run(script, general=False, zero_cost=True):
    tmp = Path(tempfile.mkdtemp(prefix="v34_review_"))
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        if general:
            rc, ts, routes = _run_general(tmp, script)
            sym = "GENERAL"
        else:
            rc, ts, routes = t_flow.run_ruler_session(tmp, script, zero_cost=zero_cost)
            sym = "TEST"
        s = Session(sym, ts, tmp)
        a = json.loads((tmp / rc["analysis_path"]).read_text(encoding="utf-8")) if rc.get("analysis_path") else {}
    finally:
        os.chdir(cwd)
    return {"rc": rc, "s": s, "a": a, "routes": routes, "tmp": tmp}


def _run_general(tmp: Path, script: dict):
    """test_v34_flow.run_ruler_session for a GENERAL session: the mock root gains the three US lenses' technical snapshots
    (SPY at 100 with s_s = 0.08, as the §1.4 example), the snapshot cost is zeroed like the TEST fixture."""
    t_flow.dd._mock_root(tmp)
    (tmp / "config" / "odds.yaml").write_text((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp / "config" / "symbols.yaml").write_text(json.dumps({
        "analysis_targets": {"stocks": [], "etfs": [], "generals": ["GENERAL"], "benchmarks": []},
        "symbol_contracts": {**{s: {"symbol": s, "region": "US", "currency": "USD"} for s in ("SPY", "QQQ", "DIA")},
                             "GENERAL": {"symbol": "GENERAL", "region": "US", "currency": "USD",
                                         "lenses": [{"symbol": "SPY", "weight": 0.5}, {"symbol": "QQQ", "weight": 0.3},
                                                    {"symbol": "DIA", "weight": 0.2}]}}}),
        encoding="utf-8")
    for sym, px in (("SPY", 100.0), ("QQQ", 400.0), ("DIA", 350.0)):
        d = tmp / "data" / "technical" / sym
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sym}_technical_{TS}.json").write_text(json.dumps({
            f"{sym}_current_snapshot_last_price": px, f"{sym}_daily_atr_pct_14": 8 / 15 ** 0.5,
            f"{sym}_latest_data_timestamp": "2026-09-30T00:00:00+00:00",
            f"{sym}_session_context_current_session": "closed"}), encoding="utf-8")
    s = Session.open("GENERAL", tmp, engine="mock", model="mock-judge", tools=MockTools(tmp))
    s.state["odds"]["cost_bps"] = 0.0
    s.state.pop("lenses", None)   # these G-tests exercise the single-lens (primary-lens) GENERAL path; multi-lens finals are tested in test_lens_ingest
    s.save()
    captured = []
    orig = Session.route

    def spy(self, *a, **k):
        rc = orig(self, *a, **k)
        captured.append(rc)
        return rc
    with mock.patch.object(Session, "route", spy):
        drv = t_flow.dd.Driver("GENERAL", tmp, t_flow.dd.MockEngine(script), "mock-judge", "mock-worker", tools=MockTools(tmp))
        rc = drv.run()
    return rc, s.ts, captured


def _validate(fl, doc):
    cwd = os.getcwd()
    os.chdir(fl["tmp"])
    try:
        return vd.validate(doc)
    finally:
        os.chdir(cwd)


@pytest.fixture(scope="module")
def flow():
    return _run(t_flow.ruler_script())


# ============================================================================================ C1 + M1 + M5 + T5 + T7

def test_c1_gate_re_derives_operation_size_target_strategy_and_ruler(flow):
    a = flow["a"]
    assert _validate(flow, copy.deepcopy(a))[0] == []
    sizes = [x for x in ("LIGHT_SIZE", "STANDARD_SIZE", "AGGRESSIVE_SIZE", "N/A") if x != a["operation_size"]]
    edits = {
        "operation_type HOLD_NEUTRAL": lambda d: d.update(operation_type="HOLD_NEUTRAL"),
        "operation_type TRIM_POSITION": lambda d: d.update(operation_type="TRIM_POSITION"),
        "operation_size": lambda d: d.update(operation_size=sizes[0]),
        "operation_target": lambda d: d.update(operation_target="101"),
        "derived.chosen": lambda d: d["scenario"]["derived"].update(chosen="neutral"),
        "derived.q_star": lambda d: d["scenario"]["derived"].update(q_star=d["scenario"]["derived"]["q_star"] - 0.1),
        "derived.x_star": lambda d: d["scenario"]["derived"].update(x_star=d["scenario"]["derived"]["x_star"] + 0.1),
        "odds.gamma": lambda d: d["odds"].update(gamma=50),
        "odds.cost_bps": lambda d: d["odds"].update(cost_bps=500),
    }
    for name, edit in edits.items():
        doc = copy.deepcopy(a)
        edit(doc)
        fatal, _ = _validate(flow, doc)
        assert any("C9 re-derivation" in f for f in fatal), (name, fatal)


def test_m1_one_replay_glue_for_the_arbiter_stages_and_the_gate(flow):
    s, a = flow["s"], flow["a"]
    calls = []
    real = rlg.replay_primes

    def spy(*args, **kw):
        calls.append(args[3])
        return real(*args, **kw)
    with mock.patch.object(rlg, "replay_primes", spy):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, objection_dispositions=[]), "ruling")
        assert _validate(flow, copy.deepcopy(a))[0] == []
    assert len(calls) == 2, calls


def test_t5_ruling_session_cfg_is_strict():
    with pytest.raises(RulingError, match="session opened before v3.4"):
        rlg.session_cfg({}, ROOT)
    assert rlg.session_cfg({"odds": {"schema": "3.4", "gamma": 7.0}})["gamma"] == 7.0


def test_m5_gate_compares_the_reports_ruler_with_the_session_snapshot(flow):
    a, tmp = flow["a"], flow["tmp"]
    path = tmp / flow["rc"]["analysis_path"]
    assert not any("odds" in e for e in rs.gate_extras(copy.deepcopy(a), path, tmp))
    for k, v in (("gamma", 50), ("cost_bps", 500), ("sigma_ref", 0.0801)):
        doc = copy.deepcopy(a)
        doc["odds"][k] = v
        assert any("odds differs from the session's _session.json.odds" in e for e in rs.gate_extras(doc, path, tmp)), k


def test_t7_gate_passes_a_session_whose_ruling_folds_in_the_objection_revision():
    script = t_flow.ruler_script()
    script["ruling"] = [dict(t_flow.ARBITER, mixture={"w": 0.6, "reason": None}, composite_score=95,
                             objection_dispositions=[])]
    fl = _run(script)
    assert fl["rc"]["ok"] and fl["rc"]["gate"]["ok"], fl["rc"]
    sc = fl["a"]["scenario"]
    assert sc["derived"]["replay"]["revisions_included"] is True
    assert sr.canonical(sc["mixture"]["r_prime"]) == sr.canonical(sr.apply_ops(t_flow.R14_AFTER_Z1, t_flow.R_OBJ_REV["ops"]))
    assert _validate(fl, copy.deepcopy(fl["a"]))[0] == []


# ============================================================================================ I1 GENERAL end to end

def test_i1_general_session_end_to_end_names_the_primary_lens():
    from assemble_debate_artifacts import _derive_execution  # noqa: F401  (the path the report went through)
    from workflows.postcheck_quant import _parse_price_map
    fl = _run(t_flow.ruler_script(), general=True)
    rc, a, s = fl["rc"], fl["a"], fl["s"]
    assert rc["ok"] and rc["gate"]["ok"], rc
    assert s.state["odds"]["unit_basis"]["data_symbol"] == "SPY"
    for oid in ("PROV", "PROPOSED", "RULING.v1"):
        assert s.store.obj(oid)["content"]["operation_target"] == "SPY=100", oid
    assert a["operation_target"] == "SPY=100"
    assert a["operation_type"] in ("MARKET_ENTRY", "LIMIT_ENTRY")
    assert a["operation_type"] == "MARKET_ENTRY"                       # SPY=100 against SPY's reference price 100
    assert a["operation_execution"]["derived"] == "MARKET_ENTRY"
    assert vd.operation_target_error(None, a["operation_target"]) is None
    assert vd.operation_target_error("GENERAL", a["operation_target"]) is None
    assert _parse_price_map(a["operation_target"]) == {"SPY": 100.0}
    assert a["composite_score"] == 59


# ============================================================================================ I2 claims_review

@pytest.mark.parametrize("entry, why", [
    ({"claim": "R1", "claim_rev": "R1.R", "verdict": "adopted", "reason": "R's challenge holds"}, "is a challenge — rule on the target claim Z1"),
    ({"claim": "Z9", "claim_rev": "R1.Z", "verdict": "adopted", "reason": "x"}, "Z9 is not a claim of this session"),
    ({"claim": "Z1", "claim_rev": "R2.Z", "verdict": "adopted", "reason": "x"}, "is not Z1's current rev 'R1.Z'"),
    ({"claim": "Z1", "verdict": "adopted", "reason": "x"}, "is not Z1's current rev 'R1.Z'"),
])
def test_i2_claims_review_must_name_a_registered_claim_at_its_current_rev(flow, entry, why):
    s = flow["s"]
    with pytest.raises(RulingError, match="claims_review") as exc:
        derive_ruling(s.store, s, dict(t_flow.ARBITER, claims_review=[entry]), "proposed")
    assert why in str(exc.value)
    doc = copy.deepcopy(flow["a"])
    doc["claims_review"] = [entry]
    assert any(why in f for f in _validate(flow, doc)[0])


def test_i2_final_format_tells_the_arbiter_to_rule_on_the_target():
    txt = (ROOT / "prompts" / "agent" / "formats" / "final.md").read_text(encoding="utf-8")
    assert "Rule on the target claim, never on a challenge" in txt


# ============================================================================================ I3 / I4 ingestion

def _cfg(flow):
    return om.config_from_snapshot(flow["s"].state["odds"])


def test_i3_revision_of_a_carried_effect_rejected_at_ingestion(flow):
    """The review probe: R carried `Z1#true` (its challenge R1's if_true) in round 2, then revises R1 in round 3 with a
    new effect. Accepted, the replay of every later stage would FATAL; now ingestion refuses it with retry."""
    s = flow["s"]
    history = rlg.final_records(s.store)
    r1_rev = t_flow.ruler_claim("R1", "guidance raise is priced, and then some", kind="challenge", target="Z1",
                            status="withdrawn", if_true=[{"op": "p_move", "from": "C", "to": "A", "amount": 0.05}],
                            revises="R1.R")
    rec = t_flow.ruler_final(2, [r1_rev, t_flow.R_C2_DEAD], t_flow.R14_AFTER_Z1)
    errors, _ = ingest(rec, "Reaper", 3, history, _cfg(flow), ref_price=100.0)
    assert any("claim R1: effect already carried by update R2.R.scenario_updates[0] (applies Z1#true); "
               "file a new claim instead" in e for e in errors), errors
    # what acceptance would have caused: no stage could replay R
    fake = _FakeStore(s.store, extra={"id": "R3.R", "by": "Reaper", "round": 3, "content": rec})
    with pytest.raises(RulingError, match="replay failed"):
        derive_ruling(fake, s, dict(t_flow.ARBITER), "proposed")
    # refused, the record never enters: the ruling derives as before
    assert derive_ruling(s.store, s, dict(t_flow.ARBITER), "proposed")["composite_score"] == 59
    # a revision of a claim whose effect nobody carried is still a plain revision
    r2_rev = dict(t_flow.R_C2_DEAD, claim="multiple compression, sector-wide", revises="R1.R")
    errors, _ = ingest(t_flow.ruler_final(2, [t_flow.R_C1_DONE, r2_rev], t_flow.R14_AFTER_Z1), "Reaper", 3, history,
                          _cfg(flow), ref_price=100.0)
    assert errors == [], errors


class _FakeStore:
    def __init__(self, store, extra):
        self.store, self.extra = store, extra

    def rows(self):
        return self.store.rows() + [{"id": self.extra["id"], "kind": "final", "by": self.extra["by"], "round": self.extra["round"]}]

    def obj(self, oid):
        return copy.deepcopy(self.extra) if oid == self.extra["id"] else self.store.obj(oid)

    def ids(self):
        return set(self.store.ids()) | {self.extra["id"]}


def test_i4_scenario_P0_and_T_days_pinned_at_ingestion(flow):
    cfg = _cfg(flow)

    def opening(**kw):
        return t_flow.ruler_final(50, [t_flow.Z_C1], dict(t_flow.Z14, **kw))
    errors, _ = ingest(opening(P0=100.4), "Zealot", 1, [], cfg, ref_price=100.0)
    assert any("scenario.P0 100.4 ≠ the session reference price 100" in e for e in errors), errors
    errors, _ = ingest(opening(T_days=20), "Zealot", 1, [], cfg, ref_price=100.0)
    assert any("scenario.T_days 20 ≠ the session's T_days 15" in e for e in errors), errors
    errors, out = ingest(opening(), "Zealot", 1, [], cfg, ref_price=100.0)
    assert errors == [] and out["scenario"]["P0"] == 100.0
    errors, out = ingest(opening(P0=100.04), "Zealot", 1, [], cfg, ref_price=100.0)   # display rounding: stored canonical
    assert errors == [] and out["scenario"]["P0"] == 100.0
    # the store passes the session reference price (the opening snapshot's) to ingestion
    from odds_snapshot import reference_price
    assert reference_price(flow["s"].state, "TEST", flow["tmp"]) == 100.0


def test_i4_store_rejects_a_mismatched_P0_and_mints_nothing(flow):
    """debate_store.put passes the session reference price to ingestion: a final whose P0 is off is refused before
    anything is minted (route() turns the StoreError into `retry: true`, as for the forged applies)."""
    from debate_store import StoreError
    store = flow["s"].store
    n = len(store.rows())
    bad = t_flow.ruler_final(2, [t_flow.R_C1_DONE, t_flow.R_C2_DEAD], dict(t_flow.R14_AFTER_Z1, P0=100.4))
    with pytest.raises(StoreError, match="scenario.P0 100.4"):
        store.put("final", bad, by="Reaper", round_=3)
    assert len(store.rows()) == n


# ============================================================================================ I5 touch stops

def test_i5_touch_stop_fires_on_a_close_beyond_the_level():
    leaf = {"id": "L", "p": 1.0, "P_T": 88.0, "refs": [], "events": [{"t": 15, "kind": "close", "price": 88.0}]}
    touch = {"stop": {"level": 95.0, "semantics": "touch"}}
    sim = om.simulate_leaf(leaf, "overweight_now", touch, {"P0": 100.0})
    assert sim["stopped"] is True and sim["n_tx"] == 2 and sim["exit_price"] == 88.0
    gap = dict(leaf, events=[{"t": 3, "kind": "gap_open", "price": 93.0}, {"t": 15, "kind": "close", "price": 88.0}])
    sim = om.simulate_leaf(gap, "overweight_now", touch, {"P0": 100.0})
    assert sim["stopped"] and sim["exit_price"] == 93.0
    short = dict(leaf, P_T=112.0, events=[{"t": 15, "kind": "close", "price": 112.0}])
    sim = om.simulate_leaf(short, "underweight_now", {"stop": {"level": 106.0, "semantics": "touch"}}, {"P0": 100.0})
    assert sim["stopped"] and sim["n_tx"] == 2
    assert om._stop_hit({"t": 15, "kind": "close", "price": 88.0}, touch["stop"]) is True
    assert om._stop_hit({"t": 15, "kind": "touch", "price": 96.0}, touch["stop"]) is False


# ============================================================================================ I6 conditional entry carried

def test_i6_derive_execution_keeps_the_conditional_contract():
    from assemble_debate_artifacts import _derive_execution
    cond = {"conditional": {"observe": "close > 105", "fill": "next_open"}, "stop": {"level": 98.0, "semantics": "close"}}
    rep = {"symbol": "TEST", "operation_type": "ENTRY", "operation_target": "105", "operation_execution": copy.deepcopy(cond),
           "reference_prices": [{"symbol": "TEST", "price": 100.0}]}
    _derive_execution(rep)
    assert rep["operation_type"] == "LIMIT_ENTRY"
    assert rep["operation_execution"]["conditional"] == cond["conditional"]
    assert rep["operation_execution"]["stop"] == cond["stop"]
    plain = {"symbol": "TEST", "operation_type": "ENTRY", "operation_target": "100",
             "reference_prices": [{"symbol": "TEST", "price": 100.0}]}
    _derive_execution(plain)
    assert set(plain["operation_execution"]) == {"declared", "derived", "reference", "tolerance_pct", "basis"}


# ============================================================================================ M2 / M8

def test_m2_a_normal_35_session_emits_no_34_ledger_warnings(flow):
    warns = flow["rc"]["gate"]["warnings"] + _validate(flow, copy.deepcopy(flow["a"]))[1]
    assert not any("placed nowhere" in w for w in warns), warns
    assert not any("zero responses all session" in w for w in warns), warns


def test_m2_zero_responses_still_reported_in_35_when_a_side_never_answers():
    from ledger_tally import tally
    z = t_flow.ruler_final(50, [t_flow.Z_C1], t_flow.Z14)
    r = t_flow.ruler_final(50, [t_flow.ruler_claim("R2", "a"), t_flow.ruler_claim("R3", "b")], t_flow.R14)
    entries = [{"round": n, "role": role, "content": c} for n in ("Init", "回合1") for role, c in (("Zealot", z), ("Reaper", r))]
    res = tally(entries, ruler=True)
    assert any("Zealot: zero responses all session" in f for f in res["forensics"]), res["forensics"]


def test_m8_35_report_without_scenario_gets_the_35_message(flow):
    doc = copy.deepcopy(flow["a"])
    del doc["scenario"]
    fatal, _ = _validate(flow, doc)
    assert any("scenario (A) missing" in f for f in fatal), fatal
    assert not any("ledger_score" in f for f in fatal), fatal
