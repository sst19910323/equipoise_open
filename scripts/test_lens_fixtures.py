# -*- coding: utf-8 -*-
"""Shared three-lens fixture (C11, v3.4.1): a GENERAL mock root with SPY / QQQ / DIA and a scripted mock-engine session
whose finals carry `scenario_by_lens` and lens-tagged ops. Imported by the lens suites (Task 4 ruling, Task 5 gate,
Task 9 end to end); this module holds no tests.

Root (`lens_root`): `debate_driver._mock_root` + the live `config/odds.yaml` + a symbols.yaml with GENERAL's lenses
SPY 0.5 / QQQ 0.3 / DIA 0.2 (normalised default λ = the same) + one opening technical snapshot per lens (price REF,
ATR%14 ATR). No daily CSVs: ADV is missing on every lens (warning only; cost = base + 5), and `drive_lens_session(...,
zero_cost=True)` zeroes every lens's cost like `run_ruler_session` does for the single-lens session.

Session (`lens_script`), the single-lens §1.4 story on three lenses, w = 0.5:
- Claims are the single-lens ones: Z1 (guidance raised; adopted), R1 (R's challenge on Z1; its if_true is the p_move
  C→A .10 on QQQ only), R2 (multiple compression; deadlocked with CLOSING_R2). R's round-2 update `applies: "Z1#true"`
  carries that one QQQ op; SPY and DIA are not moved by any claim.
- Per-lens scenarios (T = 15, events = one close at P_T; Z files overweight_now with a stop at 95 % of P0, R
  underweight_now with a stop at 106 % of P0):
    SPY P0 500 (ATR 1.2):  Z A .40→505 / B .30→500 / C .30→495;  R A .30→505 / B .30→500 / C .40→495
    QQQ P0 400 (ATR 8/√15, s_s = σ_ref): the single-lens fixture ×4 — Z A .55→440 / B .30→400 / C .15→360;
                            R A .25→432 / B .35→392 / C .40→352 (round 2 after Z1: A .35 / C .30)
    DIA P0 300 (ATR 1.0):  Z A .35→306 / B .30→300 / C .35→294;  R A .30→306 / B .30→300 / C .40→294
- Tuning (no cost, γ 6.25, σ_ref 0.08): SPY's mixture is symmetric around P0 (.35 / .30 / .35 on ±1 %) → every action's
  CE ≤ 0 → HOLD_NEUTRAL, S_ref 50.0; QQQ is the single-lens fixture's A → ENTRY (overweight_now, STANDARD, x* 0.58),
  S_ref 58.9; DIA's mixture leans −0.1 % with a small s_s (ATR 1.0 → k = σ_ref/s_s ≈ 2.07) → TRIM_POSITION
  (underweight_now, STANDARD, x* 0.55; ATR 1.5 would be AGGRESSIVE, 2.0 LIQUIDATE_NOW), S_ref 46.4.
  Per-lens composite 50 / 59 / 46 → default-weighted composite round(25 + 17.7 + 9.2) = 52.

Helpers: `lens_root(tmp)`, `lens_script()`, `drive_lens_session(tmp, script, until=...)` (the debate rounds through
`debate_driver.Driver.run_round`, then the final stages called directly with the scripted inputs, through signoff
when `until="signoff"`: the lens-wise report passes the release gate (Task 5); a stage that fails is recorded in
`errors`, not raised), `lens_final(...)`, `tag(ops, *lenses)`.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("debate_driver", ROOT / "scripts" / "debate_driver.py")
dd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(dd)
import scenario_replay as sr   # noqa: E402
from run_step import Session, MockTools, StepError   # noqa: E402

SYMBOL = "GENERAL"
LENSES = ["SPY", "QQQ", "DIA"]
WEIGHTS = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
REF = {"SPY": 500.0, "QQQ": 400.0, "DIA": 300.0}
ATR = {"SPY": 1.2, "QQQ": 8 / 15 ** 0.5, "DIA": 1.0}
EXPECTED_OPS = {"SPY": "HOLD_NEUTRAL", "QQQ": "ENTRY", "DIA": "TRIM_POSITION"}
EXPECTED_S_REF = {"SPY": 50.0, "QQQ": 58.9, "DIA": 46.4}   # rounded to 1 dp
MOVE = {"op": "p_move", "from": "C", "to": "A", "amount": 0.10}


def tag(ops, *lenses):
    """Lens-tagged copies of `ops`, one set per lens."""
    return [dict(o, lens=L) for L in lenses for o in ops]


def _node(nid, p, P_T, label, refs):
    return dd._node(nid, p, P_T, label, refs)


def _sc(L, nodes, execution):
    s = dd.ruler_sc(nodes, execution)
    s["P0"] = REF[L]
    return s


def _z_exec(L):
    return {"overweight": {"stop": {"level": round(REF[L] * 0.95, 6), "semantics": "close"}, "conditional": []}}


def _r_exec(L):
    return {"underweight": {"stop": {"level": round(REF[L] * 1.06, 6), "semantics": "close"}, "conditional": []}}


Z_BY = {
    "SPY": _sc("SPY", [_node("A", 0.40, 505, "broad breadth", ["Z1"]), _node("B", 0.30, 500, "flat", ["Z1"]),
                       _node("C", 0.30, 495, "dip", ["R2"])], _z_exec("SPY")),
    "QQQ": _sc("QQQ", [_node("A", 0.55, 440, "beat and raise", ["Z1"]), _node("B", 0.30, 400, "flat", ["Z1"]),
                       _node("C", 0.15, 360, "miss", ["R2"])], _z_exec("QQQ")),
    "DIA": _sc("DIA", [_node("A", 0.35, 306, "cyclicals bid", ["Z1"]), _node("B", 0.30, 300, "flat", ["Z1"]),
                       _node("C", 0.35, 294, "rates bite", ["R2"])], _z_exec("DIA")),
}
R_BY = {
    "SPY": _sc("SPY", [_node("A", 0.30, 505, "broad breadth", ["Z1"]), _node("B", 0.30, 500, "flat", ["R2"]),
                       _node("C", 0.40, 495, "dip", ["R2"])], _r_exec("SPY")),
    "QQQ": _sc("QQQ", [_node("A", 0.25, 432, "raise", ["Z1"]), _node("B", 0.35, 392, "drift lower", ["R2"]),
                       _node("C", 0.40, 352, "multiple compression", ["R2"])], _r_exec("QQQ")),
    "DIA": _sc("DIA", [_node("A", 0.30, 306, "cyclicals bid", ["Z1"]), _node("B", 0.30, 300, "flat", ["R2"]),
                       _node("C", 0.40, 294, "rates bite", ["R2"])], _r_exec("DIA")),
}
R_BY_AFTER_Z1 = dict(R_BY, QQQ=sr.apply_ops(R_BY["QQQ"], [MOVE]))

Z_C1 = dd.ruler_claim("Z1", "guidance raised")
R_C1 = dd.ruler_claim("R1", "guidance raise is already priced", kind="challenge", target="Z1", if_true=tag([MOVE], "QQQ"))
R_C2 = dd.ruler_claim("R2", "multiple compression")
Z_FIX_SETTLED = dict(Z_C1, status="settled_for")
R_C1_DONE = dict(R_C1, status="withdrawn")
R_C2_DEAD = dict(R_C2, status="deadlocked")
UPD_Z1 = {"claim": "Z1", "applies": "Z1#true", "ops": tag([MOVE], "QQQ"), "why": "Z1 verified: guidance raised"}
UPD_R2 = dict(dd.UPD_R2)
CLOSING_R2 = copy.deepcopy(dd.CLOSING_R2)
LENS_ARBITER = copy.deepcopy(dd.ARBITER)
LENS_ARBITER["next_review"]["structured_triggers"] = [{"type": "price_below", "symbol": "SPY", "value": 475}]
LENS_ARBITER["risk_management"] = {"stop_loss": "per lens: the derived stops", "take_profit": "per lens",
                                   "max_drawdown_tolerance": "5%", "review_triggers": ["SPY below 475"]}
R_OBJ_REV = {"predecessor": "R2.R", "ops": tag([{"op": "p_move", "from": "C", "to": "B", "amount": 0.05}], "QQQ"),
             "why": "after reading the draft: QQQ compression is milder than I filed"}
Z_SIGN_REV = {"predecessor": "R2.Z", "ops": tag([{"op": "p_move", "from": "A", "to": "B", "amount": 0.05}], "QQQ"),
              "why": "the redraft's R2 reading persuades me a little on QQQ"}


def lens_final(score, claims, scenario_by_lens, updates=(), status="final", req=None):
    """A multi-lens debater round JSON: `dd.ruler_final` with `scenario` replaced by `scenario_by_lens`."""
    r = dd.ruler_final(score, claims, Z_BY["SPY"], updates, status=status, req=req)
    del r["scenario"]
    r["scenario_by_lens"] = copy.deepcopy(scenario_by_lens)
    return r


def lens_script() -> dict:
    """The scripted three-lens session (fresh copy per call: the mock engine pops the answers)."""
    return {
        "beat_zealot_r1_b1": [lens_final(99, [Z_C1], Z_BY, status="need_more",
                                         req=[{"type": "search", "target_claim": "R2", "question": "Is the multiple compressing?",
                                               "would_change_my_mind": "w"}])],
        "beat_reaper_r1_b1": [lens_final(1, [R_C1, R_C2], R_BY)],
        "scout_R1B1-1": [{"question": "Is the multiple compressing?", "simple": "Forward P/E flat at 24x over 3 months",
                          "detailed": "[conclusion] ...", "source": ["src"], "verdict": "refutes"}],
        "review_r1_b1": [{"stamps": [{"job_id": "R1B1-1", "status": "verified", "scope": "P/E level verified"}]}],
        "beat_zealot_r1_b2": [lens_final(99, [Z_C1], Z_BY)],
        "notes_r1": [{"arbiter_notes": "no ruling this round", "private_note": "Z1 open; R2 open",
                      "focus": "Z1", "proactive_requests": [], "decision": "CONTINUE"}],
        "beat_zealot_r2_b1": [lens_final(98, [Z_FIX_SETTLED], Z_BY)],
        "beat_reaper_r2_b1": [lens_final(2, [R_C1_DONE, R_C2_DEAD], R_BY_AFTER_Z1, [UPD_Z1, UPD_R2])],
        "notes_r2": [{"arbiter_notes": "Z1 settled; R2 deadlocked", "private_note": "A p contested on C",
                      "focus": "(close)", "decision": "TERMINATE", "terminal_open": {"R2": CLOSING_R2},
                      "dispositions": []}],
        "provisional": [{"claims_review": LENS_ARBITER["claims_review"], "mixture": {"w": 0.5, "reason": None},
                         "rationale": "Z1 adopted; R2 deadlocked", "mentality": "with_trend", "composite_score": 97}],
        "proposed": [dict(LENS_ARBITER, composite_score=96)],
        "objection_zealot_1": [{"objections": [], "replies": [], "needs_new_evidence": []}],
        "objection_reaper_1": [{"objections": [], "replies": [], "needs_new_evidence": [], "scenario_revision": R_OBJ_REV}],
        "ruling": [dict(LENS_ARBITER, composite_score=95, objection_dispositions=[])],
        "signoff_zealot": [{"reservation": "z", "dissent_refs": ["Z1"], "endorsed_composite_score": 11,
                            "scenario_revision": Z_SIGN_REV}],
        "signoff_reaper": [{"reservation": "r", "dissent_refs": [], "endorsed_composite_score": 12}],
    }


def lens_root(tmp: Path, lenses: list | None = None, ref: dict | None = None, adv: dict | None = None) -> str:
    """The GENERAL mock root (see the module docstring); `lenses` reorders GENERAL's lens list in symbols.yaml (same
    weights), `ref` overrides opening snapshot prices per lens; `adv` {lens: USD millions} writes a flat 25-session daily
    CSV per lens whose close × volume × the US volume lot (100) is that ADV (cost then follows ATR and ADV per lens);
    → the data timestamp."""
    lenses = list(lenses or LENSES)
    ref = dict(REF, **(ref or {}))
    ts = dd._mock_root(tmp)
    (tmp / "config" / "odds.yaml").write_text((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    contracts = {s: {"symbol": s, "region": "US", "currency": "USD"} for s in LENSES}
    contracts[SYMBOL] = {"symbol": SYMBOL, "region": "US", "currency": "USD",
                         "lenses": [{"symbol": L, "weight": WEIGHTS[L]} for L in lenses]}
    (tmp / "config" / "symbols.yaml").write_text(json.dumps({
        "analysis_targets": {"stocks": [], "etfs": [], "generals": [SYMBOL], "benchmarks": []},
        "symbol_contracts": contracts}), encoding="utf-8")
    for L in LENSES:
        d = tmp / "data" / "technical" / L
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{L}_technical_{ts}.json").write_text(json.dumps({
            f"{L}_current_snapshot_last_price": ref[L], f"{L}_daily_atr_pct_14": ATR[L],
            f"{L}_latest_data_timestamp": "2026-09-30T00:00:00+00:00",
            f"{L}_session_context_current_session": "closed"}), encoding="utf-8")
    for L, v in (adv or {}).items():
        d = tmp / "data" / "market_data" / L
        d.mkdir(parents=True, exist_ok=True)
        vol = v * 1e6 / (ref[L] * 100)
        rows = ["date,open,high,low,close,volume"] + [f"2026-09-{i + 1:02d},{ref[L]},{ref[L]},{ref[L]},{ref[L]},{vol}"
                                                     for i in range(25)]
        (d / f"{L}_5y_1d_{ts}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return ts


STAGES = ("rounds", "provisional", "proposed", "objection", "ruling", "signoff")


def drive_lens_session(tmp: Path, script: dict | None = None, until: str = "ruling", zero_cost: bool = True,
                       lenses: list | None = None, ref: dict | None = None, adv: dict | None = None) -> dict:
    """Open the GENERAL session in `tmp` (cwd must be `tmp`: data_files are checked relative), run the debate rounds with
    the mock engine, then the final stages up to `until` with the scripted inputs. → {"session", "ts", "results":
    {stage: receipt}, "errors": {stage: message}, "tracebacks": {stage: text}}; a failing stage stops the drive and is
    recorded in `errors` (a StepError or a gate refusal as its message; any other exception — a bug, not a refusal —
    also with its traceback in `tracebacks`, so callers asserting `errors == {}` see it in full). An exception during the
    debate rounds is raised. `lenses` reorders the session's lens list (config and `GENERAL_LENSES` patched for the
    drive); `ref` overrides opening prices per lens, `adv` writes per-lens daily CSVs (see lens_root)."""
    from unittest import mock
    from workflows import data_manifest
    order = list(lenses or LENSES)
    with mock.patch.dict(data_manifest.GENERAL_LENSES, {SYMBOL: order}):
        return _drive(tmp, script if script is not None else lens_script(), until, zero_cost, order, ref, adv)


def _drive(tmp: Path, script: dict, until: str, zero_cost: bool, order: list, ref: dict | None,
           adv: dict | None = None) -> dict:
    import traceback
    ts = lens_root(tmp, order, ref, adv)
    s = Session.open(SYMBOL, tmp, engine="mock", model="mock-judge", tools=MockTools(tmp))
    if zero_cost:
        s.state["odds"]["cost_bps"] = 0.0
        for L in s.state["odds"].get("by_lens") or {}:
            s.state["odds"]["by_lens"][L]["cost_bps"] = 0.0
        s.save()
    engine = dd.MockEngine(script)
    drv = dd.Driver(SYMBOL, tmp, engine, "mock-judge", "mock-worker", tools=MockTools(tmp))
    out = {"session": s, "ts": ts, "results": {}, "errors": {}, "tracebacks": {}, "engine": engine}
    s.open_receipt()
    while s.state.get("status") == "debating":
        drv.run_round(s)
    out["results"]["rounds"] = {"ok": True, "round": s.state.get("round")}
    pop = lambda k: script[k].pop(0)   # noqa: E731
    steps = [
        ("provisional", lambda: s.finalize("provisional", pop("provisional"))),
        ("proposed", lambda: (s.finalize("audit"), s.finalize("proposed", pop("proposed")))[1]),
        ("objection", lambda: s.finalize("objection", round_=1, sides={"Zealot": pop("objection_zealot_1"),
                                                                       "Reaper": pop("objection_reaper_1")})),
        ("ruling", lambda: s.finalize("ruling", pop("ruling"))),
        ("signoff", lambda: s.finalize("signoff", sides={"Zealot": pop("signoff_zealot"), "Reaper": pop("signoff_reaper")})),
    ]
    for name, step in steps:
        if STAGES.index(name) > STAGES.index(until):
            break
        try:
            rc = step()
        except StepError as exc:   # a refusal: recorded for the caller to assert on
            out["errors"][name] = f"{type(exc).__name__}: {exc}"
            break
        except Exception as exc:   # noqa: BLE001 — not a refusal: recorded with its traceback, never silently
            out["errors"][name] = f"unexpected {type(exc).__name__}: {exc}"
            out["tracebacks"][name] = traceback.format_exc()
            break
        out["results"][name] = rc
        if isinstance(rc, dict) and rc.get("ok") is False:
            out["errors"][name] = json.dumps(rc.get("gate") or rc, ensure_ascii=False)[:2000]
            break
    return out


def s_s_of(L: str) -> float:
    return round(ATR[L] / 100 * math.sqrt(15), 6)
