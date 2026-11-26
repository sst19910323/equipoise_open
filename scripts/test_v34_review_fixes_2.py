"""v3.4 second fix wave (GPT round 8, council/20261030-0900-v34-closing-rules/gpt-8.md: G1-G9 + corpus sync), one
regression test (or a few) per item. Most tests run on the §1.4 mock session of test_v34_flow (`run_ruler_session`)."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import odds_model as om                         # noqa: E402
import ruling as rlg                         # noqa: E402
import scenario_replay as sr                    # noqa: E402
import test_v34_flow as t_flow                  # noqa: E402
import test_v34_review_fixes as t_rev           # noqa: E402
from ruling import RulingError, derive_ruling   # noqa: E402

_run, _validate = t_rev._run, t_rev._validate


@pytest.fixture(scope="module")
def flow():
    return _run(t_flow.ruler_script())


def _c9(fatal):
    return [f for f in fatal if "C9 re-derivation" in f]


# ============================================================================================ G1

def test_g1_authored_operation_execution_refused_at_the_arbiter_stage(flow):
    s = flow["s"]
    ruling = dict(t_flow.ARBITER, objection_dispositions=[],
                  operation_execution={"conditional": {"observe": "close > 999", "fill": "next_open"}, "stop": None})
    with pytest.raises(RulingError, match="derived field authored: operation_execution"):
        derive_ruling(s.store, s, ruling, "ruling")
    rec = derive_ruling(s.store, s, dict(t_flow.ARBITER, objection_dispositions=[]), "ruling")
    assert rec["scenario"]["derived"]["chosen"] == "overweight_now"
    assert rec["operation_execution"] == {"stop": {"level": 95.0, "semantics": "close"}}   # derived: the _now stop


def test_g1_gate_verifies_operation_execution(flow):
    a = flow["a"]
    assert a["scenario"]["derived"]["chosen"] == "overweight_now"
    assert _validate(flow, copy.deepcopy(a))[0] == []
    edits = {
        "inject close > 999 into the _now report": lambda d: d["operation_execution"].update(
            conditional={"observe": "close > 999", "fill": "next_open"}),
        "inject into scenario.derived": lambda d: d["scenario"]["derived"].update(
            operation_execution={"conditional": {"observe": "close > 999", "fill": "next_open"}, "stop": None}),
        "alter the stop": lambda d: d["operation_execution"].update(stop={"level": 90.0, "semantics": "close"}),
        "touch semantics": lambda d: d["operation_execution"].update(stop={"level": 95.0, "semantics": "touch"}),
        "remove the stop": lambda d: d["operation_execution"].pop("stop"),
        "remove operation_execution": lambda d: d.pop("operation_execution"),
    }
    for name, edit in edits.items():
        doc = copy.deepcopy(a)
        edit(doc)
        assert any("operation_execution" in f for f in _c9(_validate(flow, doc)[0])), name


def test_g1_gate_verifies_a_conditional_contract():
    """A conditional ruling (overweight_on_condition): alter / remove the condition → FATAL."""
    sc = t_flow.ruler_sc([t_flow._node("A", 0.6, 112, "up", []), t_flow._node("B", 0.4, 90, "down", [])])
    sc["nodes"][0]["events"] = [{"t": 2, "kind": "close", "price": 103.0}, {"t": 3, "kind": "open", "price": 103.0},
                                {"t": 15, "kind": "close", "price": 112.0}]
    cfg = om.config_from_snapshot({"schema": "3.4", "cost_bps": 0.0, "unit_basis": {"s_s": 0.08}})
    ex = {"overweight": {"stop": {"level": 95.0, "semantics": "close"},
                         "conditional": [{"name": "breakout", "observe": "close > 102", "fill": "next_open"}]}}
    cands = om.build_candidates(ex, {}, sc, [], warn=[])
    d = om.derive_for_report(sc, cands, cfg)
    assert d["chosen"].startswith("overweight_on_condition")   # the now-fill is stopped out at 90 on B
    A = dict(sc, derived=dict(d, s_ref=d["score"]["S_ref"], unit_basis=cfg.get("unit_basis")))
    rep = {"symbol": "TEST", **rlg.operation_fields(d, "TEST", None), "s_ref": d["score"]["S_ref"],
           "operation_simple": rlg.simple_fields(d, "TEST", None)}
    rep["operation_execution"] = dict(rep["operation_execution"], declared="ENTRY", derived="LIMIT_ENTRY")
    assert rlg.recompute_check(A, cfg, rep) == []
    for edit in (lambda r: r["operation_execution"]["conditional"].update(observe="close > 999"),
                 lambda r: r["operation_execution"]["conditional"].update(fill="at_level"),
                 lambda r: r["operation_execution"].pop("conditional"),
                 lambda r: r["operation_execution"].update(stop=None)):
        bad = copy.deepcopy(rep)
        edit(bad)
        assert any("operation_execution" in f for f in rlg.recompute_check(A, cfg, bad))


def test_g1_gate_verifies_score_aliases_and_unit_basis(flow):
    a = flow["a"]
    edits = {
        "s_ref": (lambda d: d.update(s_ref=d["s_ref"] + 5), "s_ref"),
        "s_ref removed": (lambda d: d.pop("s_ref"), "s_ref"),
        "derived.s_ref": (lambda d: d["scenario"]["derived"].update(s_ref=99.0), "scenario.derived.s_ref"),
        "derived.score.S_ref": (lambda d: d["scenario"]["derived"]["score"].update(S_ref=99.0), "score.S_ref"),
        "unit_basis.s_s": (lambda d: d["scenario"]["derived"]["unit_basis"].update(s_s=0.5), "unit_basis"),
    }
    for name, (edit, needle) in edits.items():
        doc = copy.deepcopy(a)
        edit(doc)
        assert any(needle in f for f in _c9(_validate(flow, doc)[0])), name


# ============================================================================================ G2

def test_g2_unseen_evidence_fails_live_at_release_and_without_spawns():
    import closure_check as cc
    import test_closure_check as t_cc
    import validate_debate as vd
    W = t_cc.OutOfOrderEvidenceTest
    recs, spawns, rec = W.world(4)                         # R3.Z's material never saw R.EV1 (seq 5)
    live = cc.check({"Z1": rec}, t_cc.tally_of(recs[:-1]), recs, spawns, None, 20, 3)
    assert "Z1" in live["open"]
    doc = t_cc.report_from_records(recs)
    doc["schema_version"] = "3.4"
    res = t_cc.tally_of(recs)
    for label, sp in (("real view_seq", spawns), ("spawns deleted", None), ("spawns empty", {})):
        d = copy.deepcopy(doc)
        if sp is not None:
            d["spawns"] = sp
        fatal, _ = vd.closure(d, res)
        assert any("Z1" in f for f in fatal), (label, fatal)
        if sp in (None, {}):
            assert any("spawns missing; cannot establish access" in f for f in fatal), (label, fatal)
    with pytest.raises(cc.ClosureInputError, match="spawns missing"):
        cc.spawns_from_report(doc, cc.records_from_report(doc))
    ok = copy.deepcopy(doc)
    ok["spawns"] = W.world(7)[1]                            # the seen variant still passes
    assert vd.closure(ok, res)[0] == []


def test_g2_gate_compares_report_spawns_with_the_session(flow):
    import run_step as rs
    a = flow["a"]
    path = flow["tmp"] / flow["rc"]["analysis_path"]
    assert rs.gate_extras(copy.deepcopy(a), path, flow["tmp"]) == []
    assert a.get("spawns")
    doc = copy.deepcopy(a)
    k = sorted(doc["spawns"])[-1]
    doc["spawns"][k] = dict(doc["spawns"][k], view_seq=doc["spawns"][k]["view_seq"] + 50)
    assert any("spawns differs" in e for e in rs.gate_extras(doc, path, flow["tmp"]))
    doc = copy.deepcopy(a)
    doc.pop("spawns")
    assert any("spawns differs" in e for e in rs.gate_extras(doc, path, flow["tmp"]))
    assert any("spawns missing" in f for f in _validate(flow, doc)[0])


# ============================================================================================ G3

def script_concession():
    """§1.4 with R conceding Z1 through its own challenge only: R1 withdrawn, no `applies` update, no claims_review."""
    sc = t_flow.ruler_script()
    sc["beat_reaper_r2_b1_fix"] = [t_flow.ruler_final(2, [t_flow.R_C1_DONE, t_flow.R_C2_DEAD], t_flow.R14, [t_flow.UPD_R2])]
    for k in ("provisional", "proposed", "ruling"):
        for x in sc[k]:
            x["claims_review"] = []
    sc["objection_reaper_1"] = [{"objections": [], "replies": [], "needs_new_evidence": []}]
    return sc


@pytest.fixture(scope="module")
def conc():
    return _run(script_concession())


def _fin(seq, rid, claims, scenario, updates=()):
    rnd, side = rid.split(".")
    return {"seq": seq, "id": rid, "role": side, "round": int(rnd[1:]),
            "content": {"claims": copy.deepcopy(list(claims)), "scenario": copy.deepcopy(scenario),
                        "scenario_updates": copy.deepcopy(list(updates))}}


Z_UP = [{"op": "p_move", "from": "B", "to": "A", "amount": 0.05}]


def _concession_finals(challenge_effect=None, updates=()):
    z1 = t_flow.ruler_claim("Z1", "guidance raised", if_true=Z_UP)
    r1 = t_flow.ruler_claim("R1", "guidance raise is already priced", kind="challenge", target="Z1",
                        if_true=challenge_effect if challenge_effect is not None else t_flow.MOVE_Z1)
    return [_fin(1, "R1.Z", [z1], t_flow.Z14), _fin(2, "R1.R", [r1], t_flow.R14),
            _fin(3, "R2.Z", [z1], t_flow.Z14), _fin(4, "R2.R", [dict(r1, status="withdrawn")], t_flow.R14, updates)]


def test_g3_opponent_concession_applies_the_targets_true_effects_once_on_both_sides():
    primes, applied, fatal = rlg.replay_primes(_concession_finals(), [], [], None, sr.P_TOL)
    assert fatal == []
    assert [a["effect"] for a in applied["Z"] if a["status"] == "applied"] == ["Z1#true"]   # owner's if_true
    assert [a["effect"] for a in applied["R"] if a["status"] == "applied"] == ["Z1#true"]   # challenge's if_true
    assert sr.canonical(primes["Z"]) == sr.canonical(sr.apply_ops(t_flow.Z14, Z_UP))
    assert sr.canonical(primes["R"]) == sr.canonical(t_flow.R14_AFTER_Z1)
    assert round(t_flow.s_ref(primes["R"]), 1) == 39.4
    # carried by an `applies` update on R: applied once (no second move)
    upd = {"claim": "Z1", "applies": "Z1#true", "ops": t_flow.MOVE_Z1, "why": "conceded"}
    fins = _concession_finals(updates=[upd])
    fins[-1]["content"]["scenario"] = copy.deepcopy(t_flow.R14_AFTER_Z1)
    primes, applied, fatal = rlg.replay_primes(fins, [], [], None, sr.P_TOL)
    assert fatal == [] and sr.canonical(primes["R"]) == sr.canonical(t_flow.R14_AFTER_Z1)
    assert [a["status"] for a in applied["R"]] == ["carried"]


def test_g3_concession_end_to_end_gives_R_prime_39_4(conc):
    a = conc["a"]
    assert _validate(conc, copy.deepcopy(a))[0] == []
    assert round(t_flow.s_ref(a["scenario"]["mixture"]["r_prime"]), 1) == 39.4
    assert {"effect": "Z1#true", "status": "applied", "round": 2, "seq": 8} in a["scenario"]["derived"]["replay"]["R"]


VOID_R_Z1 = {"claim": "Z1", "side": "R", "outcome": "true", "reason": "not profitable"}


def test_g3_void_on_an_executable_effect_rejected_at_derivation_and_gate(conc):
    s = conc["s"]
    ruling = dict(t_flow.ARBITER, claims_review=[], objection_dispositions=[], voided={"effects": [VOID_R_Z1]})
    with pytest.raises(RulingError, match="void rejected: effect is executable"):
        derive_ruling(s.store, s, ruling, "ruling")
    doc = copy.deepcopy(conc["a"])
    doc["voided"] = {"effects": [VOID_R_Z1], "candidates": []}
    assert any("void rejected: effect is executable" in f for f in _validate(conc, doc)[0])


def test_g3_void_on_a_failing_effect_accepted_and_void_inputs_validated():
    broken = [{"op": "p_move", "from": "D", "to": "A", "amount": 0.10}]    # R has no node D: precondition fails
    fins = _concession_finals(challenge_effect=broken)
    _p, _a, fatal = rlg.replay_primes(fins, [], [], None, sr.P_TOL)
    assert any("Z1#true" in f and "D" in f for f in fatal), fatal
    void = dict(VOID_R_Z1, reason="node D was merged away; the effect cannot apply")
    primes, applied, fatal = rlg.replay_primes(fins, [], [void], None, sr.P_TOL)
    assert fatal == []
    assert [a["status"] for a in applied["R"]] == ["voided"] and "D" in applied["R"][0]["failed"]
    assert sr.canonical(primes["R"]) == sr.canonical(t_flow.R14)
    for bad, needle in ((dict(void, side="X"), "side 'X' must be Z or R"), (dict(void, side=None), "must be Z or R"),
                        (dict(void, claim="Q9"), "claim 'Q9' is not a registered claim")):
        _p, _a, fatal = rlg.replay_primes(fins, [], [bad], None, sr.P_TOL)
        assert any(needle in f for f in fatal), (bad, fatal)
    # a void on an effect that is not pending on that side (Z's own Z1#false never settles) is rejected too
    _p, _a, fatal = rlg.replay_primes(fins, [], [void, dict(void, side="Z", outcome="false")], None, sr.P_TOL)
    assert any("void rejected: Z1#false is not a pending effect on side Z" in f for f in fatal), fatal


# ============================================================================================ G4

def script_event(event_name):
    """§1.4 with Z filing an earnings-linked conditional in a scenario that names the event; R's scenario names none."""
    sc = t_flow.ruler_script()
    ex = {"overweight": {"stop": {"level": 95.0, "semantics": "close"},
                         "conditional": [{"name": "post-earnings", "observe": "close > 105", "fill": "next_open",
                                          "event": event_name}]}}
    for k, v in sc.items():
        if k.startswith("beat_zealot"):
            for f in v:
                f["scenario"]["event"] = {"name": "Q3 earnings", "t": 3}
                extra = {"cost_bps": 10} if "cost_bps" in f["scenario"]["execution"] else {}   # keep the rejected-final beat
                f["scenario"]["execution"] = dict(copy.deepcopy(ex), **extra)
    return sc


COND_ID = "overweight_on_condition[close>105|next_open|stop:95close]"


def test_g4_event_linked_conditional_survives_derivation_and_release():
    fl = _run(script_event("Q3 earnings"))
    a = fl["a"]
    assert fl["rc"]["gate"]["ok"] is True
    assert COND_ID in [c["id"] for c in a["scenario"]["derived"]["candidates"]]
    assert COND_ID in [c["id"] for c in fl["s"].store.obj("RULING.v1")["content"]["scenario"]["derived"]["candidates"]]
    assert _validate(fl, copy.deepcopy(a))[0] == []
    assert "event" not in a["scenario"]          # R′ names no event: A carries none, the filer's scenario validates it


def test_g4_unknown_event_stays_excluded():
    fl = _run(script_event("FOMC"))
    a = fl["a"]
    assert COND_ID not in [c["id"] for c in a["scenario"]["derived"]["candidates"]]
    assert any("unknown_event" in w for w in fl["rc"]["gate"]["warnings"])
    assert _validate(fl, copy.deepcopy(a))[0] == []


def test_g4_mixture_carries_an_event_both_sides_name():
    z = dict(t_flow.Z14, event={"name": "Q3 earnings", "t": 3})
    r = dict(t_flow.R14, event={"name": "Q3 earnings", "t": 3})
    assert sr.mixture(z, r, 0.5)["event"] == {"name": "Q3 earnings", "t": 3}
    assert "event" not in sr.mixture(z, dict(r, event={"name": "FOMC"}), 0.5)
    assert "event" not in sr.mixture(z, t_flow.R14, 0.5)


# ============================================================================================ G5

def _opening(**kw):
    return t_flow.ruler_final(50, [t_flow.Z_C1], dict(t_flow.Z14, **kw))


def test_g5_reference_price_is_mandatory_and_pinned(flow):
    from validate_role_record import ingest
    cfg = rlg.session_cfg(flow["s"].state)
    for bad in (None, "100", 0, -1.0, True, float("nan")):
        errors, _ = ingest(_opening(), "Zealot", 1, [], cfg, ref_price=bad)
        assert any("reference price missing; session cannot ingest" in e for e in errors), (bad, errors)
    errors, out = ingest(_opening(), "Zealot", 1, [], cfg, ref_price=100.0)
    assert errors == [] and out["scenario"]["P0"] == 100.0
    for p0, ok in ((100.05, True), (100.0501, False), (99.95, True), (99.9499, False)):   # 0.05% of 100 = 0.05
        errors, out = ingest(_opening(P0=p0), "Zealot", 1, [], cfg, ref_price=100.0)
        assert (errors == []) is ok, (p0, errors)
        if ok:
            assert out["scenario"]["P0"] == 100.0


def test_g5_snapshot_freezes_the_reference_price_and_the_store_reads_it(flow):
    from debate_store import StoreError
    s = flow["s"]
    assert s.state["odds"]["reference_price"] == 100.0
    store = s.store
    n = len(store.rows())
    state = json.loads(s.state_path.read_text(encoding="utf-8"))
    saved = copy.deepcopy(state)
    try:
        state["odds"]["reference_price"] = None
        s.state_path.write_text(json.dumps(state), encoding="utf-8")
        with pytest.raises(StoreError, match="reference price missing; session cannot ingest"):
            store.put("final", t_flow.ruler_final(2, [t_flow.R_C1_DONE, t_flow.R_C2_DEAD], t_flow.R14_AFTER_Z1),
                      by="Reaper", round_=3)
        assert len(store.rows()) == n
    finally:
        s.state_path.write_text(json.dumps(saved), encoding="utf-8")


def test_g5_general_freezes_the_primary_lens_price():
    fl = _run(t_flow.ruler_script(), general=True)
    odds = fl["s"].state["odds"]
    assert odds["unit_basis"]["data_symbol"] == "SPY"
    assert odds["reference_price"] == 100.0           # SPY, not QQQ 400 / DIA 350
    assert fl["a"]["odds"]["reference_price"] == 100.0


# ============================================================================================ G6

def test_g6_general_35_digest_clears_only_the_chosen_lens_and_scores_the_forecast(tmp_path):
    import pandas as pd
    from workflows import postcheck_quant as pq
    old = pq.MARKET_DATA_ROOT
    pq.MARKET_DATA_ROOT = tmp_path
    try:
        for sym, base in (("SPY", 100.0), ("QQQ", 400.0), ("DIA", 350.0)):
            d = tmp_path / sym
            d.mkdir()
            dates = pd.bdate_range(start="2026-09-10", periods=40)
            closes = [base * (1 - 0.002 * i) if i < 10 else base * (0.98 + 0.003 * (i - 10)) for i in range(40)]
            pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                          "low": [c * 0.99 for c in closes], "close": closes,
                          "volume": [1000] * 40}).to_csv(d / f"{sym}_5y_1d_20260930T000000Z.csv", index=False)

        def sc(atoms):
            return {"schema": "3.4", "T_days": 15, "P0": 100.0, "execution": {},
                    "nodes": [{"id": f"N{i}", "p": p, "P_T": pt, "refs": ["Z1"], "events": []}
                              for i, (p, pt) in enumerate(atoms)]}
        A = sr.mixture(sc([(0.7, 108.0), (0.3, 96.0)]), sc([(0.3, 104.0), (0.7, 94.0)]), 0.6, reason=None)
        odds = om.snapshot_from_config(om.DEFAULT_CONFIG, unit_basis={"data_symbol": "SPY", "s_s": 0.08})
        rep = {"schema_version": "3.4", "symbol": "GENERAL", "operation_type": "LIMIT_ENTRY", "operation_target": "SPY=99",
               "short_term": {"direction": "上行", "target": "SPY=104; QQQ=450; DIA=390",
                              "invalid": "SPY=95; QQQ=380; DIA=330", "horizon_days": 10},
               "reference_prices": [{"symbol": s, "price": p} for s, p in (("SPY", 100.0), ("QQQ", 400.0), ("DIA", 350.0))],
               "scenario": A, "atr_pct_14": 2.0, "odds": odds}
        dg = pq.compute_window_digest("GENERAL", "2026-09-15T20:00:00Z", 20, rep)
    finally:
        pq.MARKET_DATA_ROOT = old
    ops = [m["symbol"] for m in dg["members"] if "entry" in m or "trim_exec" in m]
    assert ops == ["SPY"], ops
    assert dg["primary_lens"] == "SPY"
    by = {m["symbol"]: m for m in dg["members"]}
    assert (by["SPY"]["role"], by["QQQ"]["role"], by["DIA"]["role"]) == ("operation", "analysis", "analysis")
    assert by["SPY"]["entry"]["entry_price"] == 99.0                          # the chosen lens's level, not 450 / 390
    assert by["QQQ"]["target"] == 450.0 and by["DIA"]["target"] == 390.0       # path analysis vs the short-term target
    fc = dg["forecast"]
    assert fc["status"] == "scored" and "delta_crps" in fc, fc
    assert fc["P0"] == 100.0


# ============================================================================================ G7

def _cost_cfg():
    return om.load_config(ROOT)


def test_g7_configured_usd_adrs_get_the_surcharge():
    import odds_snapshot as osn
    cfg = _cost_cfg()
    for sym in ("TSM", "ASML"):
        u = osn.unit_inputs({}, sym, ROOT, cfg)
        assert u["adr"] is True and u["venue"] == "US", (sym, u)
        with_s, _ = om.cost_bps_for(2.0, 5000.0, "US", u["adr"], cfg)
        without, _ = om.cost_bps_for(2.0, 5000.0, "US", False, cfg)
        assert round(with_s - without, 6) == cfg["cost"]["adr_surcharge_bps"]
    assert osn.unit_inputs({}, "AAPL", ROOT, cfg)["adr"] is False          # a US common
    assert osn.is_adr("KR", {"currency": "USD"}) is True                    # a KR name's OTC line in USD
    assert osn.is_adr("JP", {"currency": "USD"}) is True
    assert osn.is_adr("US", {"currency": "JPY"}) is True
    assert osn.is_adr("KR", {"currency": "KRW"}) is False                   # the local KSE line
    assert osn.is_adr("US", {"currency": "USD"}) is False


def test_g7_real_aapl_bars_confirm_the_lot_unit():
    """The repo's AAPL daily CSV (IBKR feed): ~2 'volume' per trade is impossible for shares; ×100 gives AAPL's ADV."""
    import pandas as pd
    import odds_snapshot as osn
    df = pd.read_csv(ROOT / "data" / "market_data" / "AAPL" / "AAPL_5y_1d_20251125T000000Z.csv").tail(20)
    assert (df["volume"] / df["barCount"]).median() < 5
    raw, _ = osn.adv_usd_m(df)
    df = df.assign(volume=df["volume"] * 100)
    adv, _ = osn.adv_usd_m(df)
    assert 5000 < adv < 30000, adv                                          # ~$6-15 B/day, AAPL's known order
    assert round(adv / raw) == 100


def _snap_root(tmp, lot_us, volume=150000, close=230.0, atr=2.0):
    import pandas as pd
    import yaml
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"))
    cfg["cost"]["volume_lot"]["US"] = lot_us
    (tmp / "config" / "odds.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp / "config" / "symbols.yaml").write_text(json.dumps({"symbol_contracts": {
        "BIG": {"symbol": "BIG", "region": "US", "currency": "USD"}}}), encoding="utf-8")
    d = tmp / "data" / "market_data" / "BIG"
    d.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(start="2026-09-01", periods=25)
    pd.DataFrame({"date": dates, "open": close, "high": close, "low": close, "close": close,
                  "volume": volume}).to_csv(d / "BIG_5y_1d_20260930T000000Z.csv", index=False)
    t = tmp / "data" / "technical" / "BIG"
    t.mkdir(parents=True, exist_ok=True)
    (t / "BIG_technical_X.json").write_text(json.dumps({"BIG_daily_atr_pct_14": atr,
                                                        "BIG_current_snapshot_last_price": close}), encoding="utf-8")
    return {"schema": "3.4", "region": "US",
            "data_files": {"symbol": {"technical": "data/technical/BIG/BIG_technical_X.json"}}}


def test_g7_volume_lot_basis_recorded_and_adv_sanity_warning(tmp_path):
    import odds_snapshot as osn
    lotless = tmp_path / "lotless"
    snap = osn.build_odds_snapshot(_snap_root(lotless, 1), "BIG", lotless)   # lot-encoded volume read as shares
    ub = snap["unit_basis"]
    assert ub["adv_usd_m"] < 50 and ub["warning"] == "ADV implausibly low — check volume units"
    assert ub["volume_lot_basis"] == osn.VOLUME_LOT_BASIS_US
    ok = tmp_path / "ok"
    snap = osn.build_odds_snapshot(_snap_root(ok, 100), "BIG", ok)
    assert snap["unit_basis"]["adv_usd_m"] > 50 and "warning" not in snap["unit_basis"]
    assert snap["reference_price"] == 230.0
    wild = tmp_path / "wild"                                                  # an abnormal ATR does not trigger it
    snap = osn.build_odds_snapshot(_snap_root(wild, 1, atr=25.0), "BIG", wild)
    assert "warning" not in snap["unit_basis"]


# ============================================================================================ G9

def test_g9_self_settled_claim_stays_open_under_an_unanswered_challenge():
    import closure_check as cc
    import test_closure_check as t_cc
    C = t_cc.claim
    recs = [t_cc.final(1, "R1.Z", [C("Z1")]),
            t_cc.final(2, "R1.R", [C("R1", kind="challenge", target="Z1")]),
            t_cc.notes(3, 1),
            t_cc.final(4, "R2.Z", [C("Z1", status="settled_for")]),          # the owner self-reports a win
            t_cc.final(5, "R2.R", [C("R1", kind="challenge", target="Z1")])]  # the challenge is never answered
    t = t_cc.tally_of(recs)
    assert "Z1" in t["open_total"] and t["zealot"]["self_settled_pending"] == ["Z1"]
    out = cc.check({}, t, recs, {}, None, 20, 2)
    assert "Z1" in out["open"] and out["ok"] is False                       # TERMINATE cannot stand
    from ledger_tally import brief, tally
    assert "self-settled by Z: Z1" in brief(t)
    t = tally(cc.entries_from_records(recs), ruler=True, named={"Z1"})    # the arbiter ruled on it
    assert "Z1" not in t["open_total"]
    # the challenger concedes (withdraws its challenge): closed without the arbiter
    conceded = recs[:-1] + [t_cc.final(5, "R2.R", [C("R1", kind="challenge", target="Z1", status="withdrawn")])]
    assert "Z1" not in t_cc.tally_of(conceded)["open_total"]
    # uncontested: honoured only when no challenge ever targeted the claim
    unc = recs[:3] + [t_cc.final(4, "R2.Z", [C("Z1", status="uncontested")]), recs[4]]
    assert "Z1" in t_cc.tally_of(unc)["open_total"]
    alone = [t_cc.final(1, "R1.Z", [C("Z1")]), t_cc.final(2, "R1.R", [C("R2")]), t_cc.notes(3, 1),
             t_cc.final(4, "R2.Z", [C("Z1", status="uncontested")]), t_cc.final(5, "R2.R", [C("R2")])]
    assert "Z1" not in t_cc.tally_of(alone)["open_total"]
    # release gate: the same session fails on Z1; a claims_review verdict on Z1 (the arbiter named it) clears it
    import validate_debate as vd
    doc = t_cc.report_from_records(recs + [t_cc.notes(6, 2)])
    doc.update(schema_version="3.4", spawns={"R1.Z": {"view_seq": 0}, "R1.R": {"view_seq": 0},
                                             "R2.Z": {"view_seq": 3}, "R2.R": {"view_seq": 3}})
    fatal, _ = vd.validate(doc, None)
    assert any("open claim Z1" in f for f in fatal), fatal
    doc["claims_review"] = [{"claim": "Z1", "claim_rev": "R1.Z", "verdict": "adopted", "reason": "r"}]
    fatal, _ = vd.validate(doc, None)
    assert not any("open claim Z1" in f for f in fatal), fatal


def _live_self_settled():
    import tempfile
    import test_run_step as t_rs
    rs, dd, ds = t_rs._load("run_step"), t_rs._load("debate_driver"), t_rs._load("debate_store")
    tmp = Path(tempfile.mkdtemp())
    t_rs._ruler_root(dd, tmp)
    s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))
    s.open_receipt()
    beat = t_rs._ruler_rec(ds, 60, "Z1", status="need_more")
    beat["request_evidence"] = [{"type": "search", "target_claim": "R1", "question": "peers re-rated?",
                                 "would_change_my_mind": "x"}]
    job = s.route(1, 1, {"Zealot": beat, "Reaper": t_rs._ruler_rec(ds, 40, "R1")})["jobs"][0]
    ok = {"question": "q", "simple": "peers re-rated", "detailed": "d", "source": ["u"], "verdict": "refutes"}
    s.close_beat(1, 1, [{"job_id": job["job_id"], "status": "verified", "scope": "s", "result": ok}], role="Zealot")
    s.route(1, 2, {"Zealot": t_rs._ruler_rec(ds, 60, "Z1")})
    s.tally(1)
    s.close_round(1, t_rs.CloseRoundTest._notes(decision="CONTINUE"))
    z2 = t_rs._ruler_rec(ds, 60, "Z1")
    z2["claims"][0]["status"] = "uncontested"
    z2["claims"].append({"id": "Z2", "kind": "challenge", "target": "R1", "claim": "peers re-rated (Z.EV1)",
                         "evidence_tier": "T2", "status": "settled_for", "answers": ["Z.EV1"],
                         "effect": {"if_true": [], "if_false": []}})
    r2 = t_rs._ruler_rec(ds, 40, "R1")
    r2["claims"][0]["status"] = "settled_for"                               # R self-settles under Z's challenge
    # (Z's challenge Z2 is not open either: only R1's own status could close it — and it must not)
    s.route(2, 1, {"Zealot": z2, "Reaper": r2})
    s.tally(2)
    return t_rs, tmp, s


def test_g9_live_self_settled_claim_blocks_terminate_until_the_arbiter_rules():
    import shutil
    t_rs, tmp, s = _live_self_settled()
    try:
        assert "R1" in s.state["last_tally"]["open_total"]
        rc = s.close_round(2, t_rs.CloseRoundTest._notes(decision="TERMINATE"))
        assert rc["decision"] == "CONTINUE" and "R1" in rc["override_reason"], rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    t_rs, tmp, s = _live_self_settled()
    try:
        disp = [{"claim": "R1", "claim_rev": "R1.R", "evidence": ["Z.EV1"],
                 "ruling": "re-rating shown; R1 holds on the rest - settled for R"}]
        rc = s.close_round(2, t_rs.CloseRoundTest._notes(decision="TERMINATE", dispositions=disp))
        assert rc["decision"] == "TERMINATE", rc
        assert rc["closure"]["open"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================================ G9b (re-review: the withdrawn route)

def test_g9b_withdrawn_challenged_claim_and_self_settled_challenge_stay_open_until_ruled():
    import test_closure_check as t_cc
    from ledger_tally import tally
    import closure_check as cc
    C = t_cc.claim
    base = [t_cc.final(1, "R1.Z", [C("Z1")]),
            t_cc.final(2, "R1.R", [C("R1", kind="challenge", target="Z1")]),
            t_cc.notes(3, 1)]
    # the probe: Z withdraws Z1 while R1 is unanswered — R1 (open) stays open
    probe = base + [t_cc.final(4, "R2.Z", [C("Z1", status="withdrawn")]),
                    t_cc.final(5, "R2.R", [C("R1", kind="challenge", target="Z1")])]
    t = t_cc.tally_of(probe)
    assert "R1" in t["open_total"] and "Z1" not in t["open_total"]
    # the challenger self-settles R1 while Z1 is still open → R1 stays open
    self_won = base + [t_cc.final(4, "R2.Z", [C("Z1")]),
                       t_cc.final(5, "R2.R", [C("R1", kind="challenge", target="Z1", status="settled_for")])]
    t = t_cc.tally_of(self_won)
    assert "R1" in t["open_total"] and t["reaper"]["self_settled_pending"] == ["R1"]
    assert cc.check({}, t, self_won, {}, None, 20, 2)["ok"] is False
    # arbiter disposition naming the challenge (or its target) closes it
    for nm in ({"R1"}, {"Z1"}):
        t = tally(cc.entries_from_records(self_won), ruler=True, named=nm)
        assert "R1" not in t["open_total"], nm
    # the target's owner withdrew (a concession under the ruling) → the challenger's settled_for stands
    gave_up = base + [t_cc.final(4, "R2.Z", [C("Z1", status="withdrawn")]),
                      t_cc.final(5, "R2.R", [C("R1", kind="challenge", target="Z1", status="settled_for")])]
    assert t_cc.tally_of(gave_up)["open_total"] == []


Z_DOWN = [{"op": "p_move", "from": "A", "to": "C", "amount": 0.05}]
R1_FALSE = [{"op": "p_move", "from": "A", "to": "C", "amount": 0.10}]   # mirror of MOVE_Z1


def test_g9b_withdrawn_challenged_claim_settles_false_on_both_sides_once():
    z1 = t_flow.ruler_claim("Z1", "guidance raised", if_true=Z_UP, if_false=Z_DOWN)
    r1 = t_flow.ruler_claim("R1", "guidance raise is already priced", kind="challenge", target="Z1",
                        if_true=t_flow.MOVE_Z1, if_false=R1_FALSE)
    fins = [_fin(1, "R1.Z", [z1], t_flow.Z14), _fin(2, "R1.R", [r1], t_flow.R14),
            _fin(3, "R2.Z", [dict(z1, status="withdrawn")], t_flow.Z14), _fin(4, "R2.R", [r1], t_flow.R14)]
    assert sr.settlements(fins, []) == [("Z1", "false", 2, 3)]
    primes, applied, fatal = rlg.replay_primes(fins, [], [], None, sr.P_TOL)
    assert fatal == []
    assert [a["effect"] for a in applied["Z"]] == ["Z1#false"] and [a["effect"] for a in applied["R"]] == ["Z1#false"]
    assert sr.canonical(primes["Z"]) == sr.canonical(sr.apply_ops(t_flow.Z14, Z_DOWN))
    assert sr.canonical(primes["R"]) == sr.canonical(sr.apply_ops(t_flow.R14, R1_FALSE))
    # a never-challenged claim withdrawn: a plain withdrawal, nothing applies
    z2 = t_flow.ruler_claim("Z2", "margin expands", if_false=Z_DOWN)
    fins = [_fin(1, "R1.Z", [z2], t_flow.Z14), _fin(2, "R1.R", [t_flow.R_C2], t_flow.R14),
            _fin(3, "R2.Z", [dict(z2, status="withdrawn")], t_flow.Z14), _fin(4, "R2.R", [t_flow.R_C2], t_flow.R14)]
    assert sr.settlements(fins, []) == []
    primes, applied, fatal = rlg.replay_primes(fins, [], [], None, sr.P_TOL)
    assert fatal == [] and applied == {"Z": [], "R": []}
    assert sr.canonical(primes["Z"]) == sr.canonical(t_flow.Z14)
