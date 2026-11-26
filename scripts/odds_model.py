#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""odds_model.py — the one ruler for composite score and size (v3.4, released 2026-10-31).

One discrete scenario distribution per side, a mixture A at the arbiter's weight, two projections:

    S_ref = 100 * G / (G + L)             G = Σ p·max(r,0), L = Σ p·max(−r,0), r = P_T/P0 − 1
    d*    = argmax CE(d) per strategy     CE = −(1/γ)·ln Σ p·exp(−γ·W*),  W* = (σ_ref/s_s)·(d·r_path − n_tx·|d|·c)

The scenario object carries nodes with probability, end price and an ordered list of execution events; every strategy's
return is simulated on those events so the same end price can give different strategy returns (filled / stopped / unfilled).
Single neutral reference q_ref = 0.5, deviation d in [-0.5, 0.5], five symmetric strategies on normalised net path payoffs,
candidate union of both sides' contracts, observable cost, session snapshot (contracts
docs/design/reorg/v35-contracts.md C4 / C6.4 / C7).

Pure functions, no I/O except `load_config`. Called by assemble_debate_artifacts, ruling, run_step and postcheck_quant.
"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG: Dict[str, Any] = {
    "schema": "3.4", "T_days": 15, "T_days_overrides": {},
    "gamma": 6.25,     # anchor: 15-day μ 2%, σ 8%, no fees → the full unit; γ = 0.02 / (0.5·0.0064)
    "gamma_sensitivity": [5, 20], "sigma_ref": 0.08,
    "p_tolerance": 0.01, "z_flag": 3.0, "tie_threshold": 0.0005,
    "size_display_bounds": [37.5, 75.0],
    "size_enum": {"light": "PILOT_SIZE", "standard": "STANDARD_SIZE", "heavy": "AGGRESSIVE_SIZE"},
    "baseline": {"nodes_sigma": [-1.0, 0.0, 1.0], "probs": [0.25, 0.5, 0.25], "zero_scale": "skip"},
    "coverage_nominal": 0.80,
    # a close stop whose path has no later open: "trigger_price" (≤ 2026-11 sessions: exit at the triggering close, i.e. no
    # overnight gap) or "next_price" (exit at the next later-day event's price, else the leaf's P_T); config/odds.yaml sets it
    "stop_fill_without_open": "trigger_price",
    "cost": {"base_bps": {"US": 2, "EU": 4, "JP": 4, "KR": 5}, "adr_surcharge_bps": 3, "k": 10,
             "fx_to_usd": {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0066, "KRW": 0.00072},
             "volume_lot": {"US": 100, "EU": 1, "JP": 1, "KR": 1}},   # IBKR daily volume units per venue (US = lots of 100)
}

EVENT_KINDS = ("gap_open", "open", "touch", "close")
_KIND_ORDER = {k: i for i, k in enumerate(EVENT_KINDS)}


def load_config(root: Path = ROOT) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        import yaml  # type: ignore
        p = Path(root) / "config" / "odds.yaml"
        if p.exists():
            got = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for k, v in got.items():
                cfg[k] = v
    except Exception:
        pass
    return cfg


# ----------------------------------------------------------------------------------------------------------------
# 1. Validation and leaf expansion
# ----------------------------------------------------------------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _sorted_events(events: List[dict]) -> List[dict]:
    return sorted(events, key=lambda e: (int(e.get("t", 0)), _KIND_ORDER.get(str(e.get("kind")), 99)))


def validate_scenario(sc: dict, cfg: Optional[dict] = None, atr_pct: Optional[float] = None,
                      claim_ids: Optional[set] = None) -> Tuple[List[str], List[str], dict]:
    """→ (fatal, warn, normalized copy). Data validity is fatal; persuasiveness is judgment and never checked here.
    Probabilities within `p_tolerance` of 1 are normalized and the raw / normalized / diff recorded under derived.p_normalization."""
    cfg = cfg or DEFAULT_CONFIG
    fatal: List[str] = []
    warn: List[str] = []
    if not isinstance(sc, dict):
        return ["scenario is not an object"], [], {}
    out = {k: v for k, v in sc.items()}
    P0 = sc.get("P0")
    if not _is_num(P0) or float(P0) <= 0:
        fatal.append("scenario.P0 missing or not a positive number")
    T = sc.get("T_days")
    if not isinstance(T, int) or T <= 0:
        fatal.append("scenario.T_days missing or not a positive integer")
    nodes = sc.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        fatal.append("scenario.nodes missing or empty")
        return fatal, warn, out
    ids: set = set()
    leaves_p: List[float] = []
    norm_nodes: List[dict] = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            fatal.append(f"nodes[{i}] is not an object"); continue
        nid = n.get("id")
        if not nid or not isinstance(nid, str):
            fatal.append(f"nodes[{i}] has no id"); continue
        if nid in ids:
            fatal.append(f"node id {nid} duplicated")
        ids.add(nid)
        p = n.get("p")
        if not _is_num(p) or float(p) < 0:
            fatal.append(f"node {nid}: p is not a non-negative finite number")
        PT = n.get("P_T")
        if not _is_num(PT) or float(PT) <= 0:
            fatal.append(f"node {nid}: P_T is not a positive number")
        refs = n.get("refs") or []
        if not refs:
            warn.append(f"node {nid}: no refs — a node nobody argued for")
        elif claim_ids is not None:
            bad = [r for r in refs if r not in claim_ids]
            if bad:
                fatal.append(f"node {nid}: refs {bad} point to unknown claims or outcalls")
        ev = n.get("events") or []
        ev_fatal = _validate_events(nid, ev, PT if _is_num(PT) else None)
        fatal.extend(ev_fatal)
        subs = n.get("sub") or []
        if subs:
            sp = 0.0
            sids = set()
            for j, s in enumerate(subs):
                if not isinstance(s, dict) or not s.get("id"):
                    fatal.append(f"node {nid}: sub[{j}] has no id"); continue
                if s["id"] in sids or s["id"] in ids:
                    fatal.append(f"node {nid}: sub id {s['id']} duplicated")
                sids.add(s["id"])
                pc = s.get("p_cond")
                if not _is_num(pc) or float(pc) < 0:
                    fatal.append(f"node {nid}: sub {s['id']} p_cond invalid"); continue
                sp += float(pc)
                sPT = s.get("P_T")
                if not _is_num(sPT) or float(sPT) <= 0:
                    fatal.append(f"node {nid}: sub {s['id']} P_T invalid")
                sev = s.get("events")
                if sev is None:
                    fatal.append(f"node {nid}: sub {s['id']} has no events — each leaf needs its own complete path")
                else:
                    fatal.extend(_validate_events(f"{nid}/{s['id']}", sev, sPT if _is_num(sPT) else None))
            if abs(sp - 1.0) > float(cfg.get("p_tolerance", 0.01)):
                fatal.append(f"node {nid}: Σ p_cond = {sp:.4f}, outside tolerance of 1")
        norm_nodes.append(dict(n))
        if _is_num(p):
            leaves_p.append(float(p))
    if fatal:
        return fatal, warn, out
    total = sum(leaves_p)
    tol = float(cfg.get("p_tolerance", 0.01))
    if abs(total - 1.0) > tol:
        fatal.append(f"Σ p = {total:.4f}, outside tolerance {tol} of 1 — invalid probabilities, not a rounding issue")
        return fatal, warn, out
    if abs(total - 1.0) > 1e-12:
        for n in norm_nodes:
            n["p_raw"] = n["p"]
            n["p"] = float(n["p"]) / total
        out.setdefault("derived", {})["p_normalization"] = {"raw_sum": total, "diff": total - 1.0, "normalized": True}
    out["nodes"] = norm_nodes
    # one-sidedness (warn) and z flags (warn)
    P0f = float(P0)
    ups = any(float(l["P_T"]) > P0f for l in expand_leaves(out))
    downs = any(float(l["P_T"]) < P0f for l in expand_leaves(out))
    if not (ups and downs):
        warn.append("one-sided distribution: no node on one side of P0 — does the other side really have zero probability?")
    if _is_num(atr_pct) and float(atr_pct) > 0 and isinstance(T, int):
        zf = float(cfg.get("z_flag", 3.0))
        for l in expand_leaves(out):
            z = (float(l["P_T"]) / P0f - 1.0) / (float(atr_pct) / 100.0 * math.sqrt(T))
            l["z"] = round(z, 2)
            if abs(z) > zf:
                warn.append(f"leaf {l['id']}: |z| = {abs(z):.1f} > {zf} — a move this large relative to the symbol's own range needs a transmission path")
    return fatal, warn, out


def _validate_events(label: str, events: List[dict], P_T: Optional[float]) -> List[str]:
    fatal: List[str] = []
    if not isinstance(events, list):
        return [f"{label}: events is not a list"]
    if not events:
        return fatal   # a node without events is allowed: strategies see only P_T (act_now fills at P0, stop never checked)
    seen_close: set = set()
    last_close = None
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            fatal.append(f"{label}: events[{i}] not an object"); continue
        if e.get("kind") not in EVENT_KINDS:
            fatal.append(f"{label}: events[{i}] kind {e.get('kind')!r} not in {EVENT_KINDS}")
        if not isinstance(e.get("t"), int) or e["t"] < 0:
            fatal.append(f"{label}: events[{i}] t must be a non-negative integer trading day")
        if not _is_num(e.get("price")) or float(e["price"]) <= 0:
            fatal.append(f"{label}: events[{i}] price invalid")
        if e.get("kind") == "close" and isinstance(e.get("t"), int):
            if e["t"] in seen_close:
                fatal.append(f"{label}: two close events on day {e['t']}")
            seen_close.add(e["t"])
    if fatal:
        return fatal
    ordered = _sorted_events(events)
    closes = [e for e in ordered if e["kind"] == "close"]
    if closes:
        last_close = float(closes[-1]["price"])
        if P_T is not None and abs(last_close - float(P_T)) / float(P_T) > 1e-6:
            fatal.append(f"{label}: last close {last_close:g} ≠ P_T {float(P_T):g} — path and end price contradict")
    return fatal


def expand_leaves(sc: dict) -> List[dict]:
    """Leaves = nodes without sub, plus each sub of a node with sub (p = p_node × p_cond). Parent with sub is a group only."""
    leaves: List[dict] = []
    for n in sc.get("nodes") or []:
        subs = n.get("sub") or []
        if subs:
            for s in subs:
                leaves.append({"id": f"{n['id']}/{s['id']}", "p": float(n["p"]) * float(s["p_cond"]), "P_T": float(s["P_T"]),
                               "events": _sorted_events(s.get("events") or []), "refs": n.get("refs") or [], "node": n["id"]})
        else:
            leaves.append({"id": n["id"], "p": float(n["p"]), "P_T": float(n["P_T"]),
                           "events": _sorted_events(n.get("events") or []), "refs": n.get("refs") or [], "node": n["id"]})
    return leaves


# ----------------------------------------------------------------------------------------------------------------
# 2. Score
# ----------------------------------------------------------------------------------------------------------------

def drift_mu(cfg: Optional[dict]) -> float:
    """The session's common drift over the valuation window (snapshot key `drift_mu`; absent = 0 = the ruler before the
    switch). EXPERIMENTAL, off in production: docs/design/drill/DRIFT-ANCHOR-2026-11-10.md."""
    v = (cfg or {}).get("drift_mu")
    return float(v) if _is_num(v) and float(v) > -1.0 else 0.0


def score(sc: dict, mu: float = 0.0) -> dict:
    """G, L, mu, sigma, S_ref from the leaves (end prices only; execution does not enter the score). `mu` = the common
    drift: returns are read against P0·(1+mu), so a tree centred on the drift scores 50."""
    P0 = float(sc["P0"]) * (1.0 + float(mu))
    leaves = expand_leaves(sc)
    rs = [(l["p"], l["P_T"] / P0 - 1.0) for l in leaves]
    G = sum(p * max(r, 0.0) for p, r in rs)
    L = sum(p * max(-r, 0.0) for p, r in rs)
    mu = sum(p * r for p, r in rs)
    var = sum(p * (r - mu) ** 2 for p, r in rs)
    S = 50.0 if (G + L) <= 0 else 100.0 * G / (G + L)
    p_up = sum(p for p, r in rs if r > 0)
    return {"G": G, "L": L, "mu": mu, "sigma": math.sqrt(max(var, 0.0)), "omega": (G / L if L > 0 else math.inf),
            "S_ref": round(S, 1), "p_up": p_up, "leaves": len(leaves)}


# ----------------------------------------------------------------------------------------------------------------
# 3. Strategy simulation on execution events
# ----------------------------------------------------------------------------------------------------------------

_COND_RE = re.compile(r"^\s*(close|touch|open|gap_open)\s*(>=|<=|>|<)\s*((?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\s*$")


def _parse_observe(text: str) -> Optional[Tuple[str, str, float]]:
    m = _COND_RE.match(str(text or ""))
    if not m:
        return None
    level = float(m.group(3))
    return (m.group(1), m.group(2), level) if math.isfinite(level) else None


def _cmp(a: float, op: str, b: float) -> bool:
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _next_open_price(events: List[dict], after_idx: int) -> Optional[Tuple[int, float]]:
    """Legacy next WRITTEN later-day open; gaps in recorded sessions are preserved for historical replay."""
    t0 = int(events[after_idx]["t"])
    for j in range(after_idx + 1, len(events)):
        e = events[j]
        if int(e["t"]) > t0 and e["kind"] in ("gap_open", "open"):
            return j, float(e["price"])
    return None


STOP_FILL_MODES = ("trigger_price", "next_price")


def stop_fill_mode(cfg: Optional[dict]) -> str:
    """cfg's stop_fill_without_open, validated (an unknown value is an error, never a silent fallback)."""
    m = (cfg or DEFAULT_CONFIG).get("stop_fill_without_open", DEFAULT_CONFIG["stop_fill_without_open"])
    if m not in STOP_FILL_MODES:
        raise ValueError(f"stop_fill_without_open must be one of {STOP_FILL_MODES}, got {m!r}")
    return m


def _next_day_price(events: List[dict], after_idx: int) -> Optional[Tuple[int, float]]:
    """The first event of any kind on a later day than the event at after_idx; → (index, price) or None."""
    t0 = int(events[after_idx]["t"])
    for j in range(after_idx + 1, len(events)):
        if int(events[j]["t"]) > t0:
            return j, float(events[j]["price"])
    return None


# ----------------------------------------------------------------------------------------------------------------
# 4. Utility, optimal participation, comparison
# ----------------------------------------------------------------------------------------------------------------

def certainty_equivalent(rows: List[Tuple[float, float]], gamma: float) -> float:
    """rows = [(p, W)] → CE = −(1/γ) ln Σ p·exp(−γW) (γ>0); γ→0 limit = Σ pW."""
    if gamma <= 0:
        return sum(p * w for p, w in rows)
    m = max(-gamma * w for _, w in rows)
    s = sum(p * math.exp(-gamma * w - m) for p, w in rows)
    return -(1.0 / gamma) * (m + math.log(s))


def optimal_participation(leaf_rows: List[Tuple[float, float, float]], gamma: float, lo: float = 0.0, hi: float = 1.0) -> Tuple[float, float]:
    """leaf_rows = [(p, B, Δ)] → (x*, CE(x*)) on the closed interval [lo, hi] (CE is concave in x: ternary search + endpoints)."""
    def ce(x: float) -> float:
        return certainty_equivalent([(p, b + x * d) for p, b, d in leaf_rows], gamma)
    a, b = lo, hi
    for _ in range(80):
        m1, m2 = a + (b - a) / 3, b - (b - a) / 3
        if ce(m1) < ce(m2):
            a = m1
        else:
            b = m2
    cands = [(lo, ce(lo)), (hi, ce(hi)), ((a + b) / 2, ce((a + b) / 2))]
    x, v = max(cands, key=lambda t: t[1])
    return (round(x, 4), v)


def size_from_x(x: float, cfg: Optional[dict] = None) -> Tuple[str, str]:
    """→ (tier word, storage enum). x = 0 → ('none', 'N/A'): the strategy should not be done; not light."""
    cfg = cfg or DEFAULT_CONFIG
    lo, hi = [float(v) for v in (cfg.get("size_display_bounds") or [37.5, 75.0])]
    enum = cfg.get("size_enum") or DEFAULT_CONFIG["size_enum"]
    I = 100.0 * x
    if I <= 1e-9:
        return "none", "N/A"
    if I < lo:
        return "light", enum["light"]
    if I < hi:
        return "standard", enum["standard"]
    return "heavy", enum["heavy"]


def mixture_moments(branches: List[Tuple[float, float, float]]) -> Tuple[float, float]:
    """branches = [(q_e, mu_e, sigma_e)] → (mu, sigma) with σ² = Σq σ_e² + Σq (μ_e − μ)² (within + between)."""
    mu = sum(q * m for q, m, _ in branches)
    var = sum(q * s * s for q, _, s in branches) + sum(q * (m - mu) ** 2 for q, m, _ in branches)
    return mu, math.sqrt(max(var, 0.0))


def crps_discrete(leaves: List[Tuple[float, float]], y: float) -> float:
    """CRPS of a discrete forecast [(p, r)] against realized y: Σp|r−y| − ½ΣΣ p_s p_t |r_s − r_t|."""
    a = sum(p * abs(r - y) for p, r in leaves)
    b = 0.5 * sum(p1 * p2 * abs(r1 - r2) for p1, r1 in leaves for p2, r2 in leaves)
    return a - b


def quantile_interval(leaves: List[Tuple[float, float]], nominal: float = 0.8) -> Tuple[float, float, float]:
    """(lo, hi, actual coverage) of the central interval with at least `nominal` mass on a discrete distribution."""
    pts = sorted(leaves, key=lambda t: t[1])
    tail = (1.0 - nominal) / 2.0
    cum, lo = 0.0, pts[0][1]
    for p, r in pts:
        cum += p
        if cum >= tail:
            lo = r; break
    cum, hi = 0.0, pts[-1][1]
    for p, r in reversed(pts):
        cum += p
        if cum >= tail:
            hi = r; break
    cov = sum(p for p, r in pts if lo <= r <= hi)
    return lo, hi, cov


# ================================================================================================================
# 5. Single neutral reference, five symmetric strategies, candidate union, observable cost, session snapshot
# (contracts docs/design/reorg/v35-contracts.md C4 / C6.4 / C7)
# ================================================================================================================

DIRECTIONS = ("overweight", "underweight")
DOCUMENTED_INVALIDITY = ("unparseable", "stop_wrong_side", "level_unreachable", "unknown_event", "stop_too_near")
STOP_NEAR_ATR = 1.0   # P06: at least one ATR between the entry anchor and its stop; a nearer stop is flagged, never dropped
                      # by the script — the arbiter may void it (voided.candidates, reason stop_too_near)
NEUTRAL_DISPLAY = "neutral; reference target exposure 50% of the standard unit"
_SNAPSHOT_KEYS = ("conditional_fill_policy", "mean_shrink", "schema", "T_days", "T_days_overrides", "gamma", "gamma_sensitivity", "sigma_ref", "p_tolerance", "z_flag",
                  "tie_threshold", "size_display_bounds", "size_enum", "baseline", "coverage_nominal", "cost",
                  "stop_fill_without_open")
_D_STEP = 0.005
_D_MAX = 0.5
_D_UNCONSTRAINED = 50.0
_CE_EPS = 1e-12


def cost_bps_for(atr_pct_14: Optional[float], adv_usd_m: Optional[float], venue: str, adr: bool,
                 cfg: Optional[dict] = None) -> Tuple[float, dict]:
    """c_bps = base[venue] + (adr surcharge) + k·ATR%14/√ADV_usd_m; missing ATR or ADV → base[venue] + 5 with the reason."""
    cfg = cfg or DEFAULT_CONFIG
    cc = cfg.get("cost") or DEFAULT_CONFIG["cost"]
    base_tbl = cc.get("base_bps") or {}
    venue = str(venue or "US")
    base = float(base_tbl.get(venue, base_tbl.get("US", 2)))
    T = int(cfg.get("T_days", 15))
    atr_ok = _is_num(atr_pct_14) and float(atr_pct_14) > 0
    adv_ok = _is_num(adv_usd_m) and float(adv_usd_m) > 0
    basis = {"atr_pct_14": float(atr_pct_14) if atr_ok else None, "adv_usd_m": float(adv_usd_m) if adv_ok else None,
             "s_s": round(float(atr_pct_14) / 100.0 * math.sqrt(T), 6) if atr_ok else None, "venue": venue, "adr": bool(adr),
             "reference_size": "cost assumes the reference order size; see proposal §3.7"}
    if not (atr_ok and adv_ok):
        missing = [n for n, ok in (("atr_pct_14", atr_ok), ("adv_usd_m", adv_ok)) if not ok]
        basis["fallback"] = f"{' and '.join(missing)} missing or not positive → base_bps[{venue}] + 5"
        return round(base + 5.0, 4), basis
    c = (base + (float(cc.get("adr_surcharge_bps", 0)) if adr else 0.0)
         + float(cc.get("k", 0)) * float(atr_pct_14) / math.sqrt(float(adv_usd_m)))
    return round(c, 4), basis


def snapshot_from_config(cfg: dict, cost_bps: Optional[float] = None, unit_basis: Optional[dict] = None,
                         commit: Optional[str] = None) -> dict:
    """The `_session.json.odds` snapshot (C4): the config's ruler parameters + computed cost and unit basis + provenance."""
    snap = {k: copy.deepcopy(cfg[k]) for k in _SNAPSHOT_KEYS if k in cfg}
    snap["cost_bps"] = cost_bps
    snap["unit_basis"] = copy.deepcopy(unit_basis) if unit_basis is not None else None
    snap["provenance"] = {"commit": commit}
    return snap


def config_from_snapshot(snapshot: dict) -> dict:
    """A cfg every odds_model function accepts: DEFAULT_CONFIG overlaid with the snapshot."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in (snapshot or {}).items():
        cfg[k] = copy.deepcopy(v)
    return cfg


def _parse_stop(stop: Any) -> Tuple[Optional[dict], bool]:
    """→ (normalized stop or None, parse ok)."""
    if stop is None or stop == {}:
        return None, True
    if not isinstance(stop, dict) or not _is_num(stop.get("level")) or float(stop["level"]) <= 0:
        return None, False
    sem = str(stop.get("semantics") or "close")
    if sem not in ("close", "touch"):
        return None, False
    return {"level": float(stop["level"]), "semantics": sem}, True


def _stop_protective(stop: dict, ref: float, long: bool) -> bool:
    return float(stop["level"]) < ref if long else float(stop["level"]) > ref


def _level_reachable(sc: dict, op: str, level: float) -> bool:
    """Some leaf's event path or P_T crosses the level inside T."""
    for l in expand_leaves(sc):
        if _cmp(float(l["P_T"]), op, level) or any(_cmp(float(e["price"]), op, level) for e in l["events"]):
            return True
    return False


def _stop_near(stop: Optional[dict], ref: Optional[float], atr_abs: Optional[float]) -> Optional[float]:
    """Distance of the stop from its entry reference in ATR when it is inside STOP_NEAR_ATR, else None."""
    if not stop or ref is None or not _is_num(atr_abs) or float(atr_abs) <= 0:
        return None
    k = abs(float(ref) - float(stop["level"])) / float(atr_abs)
    return k if k < STOP_NEAR_ATR else None


def atr_abs_of(cfg: Optional[dict], P0: Any) -> Optional[float]:
    """One ATR in price from the session snapshot (unit_basis.atr_pct_14) — None when either is missing."""
    a = ((cfg or {}).get("unit_basis") or {}).get("atr_pct_14")
    return float(P0) * float(a) / 100.0 if _is_num(a) and _is_num(P0) and float(a) > 0 else None


def validate_execution(ex: Any, sc: Optional[dict] = None, event_sc: Optional[dict] = None,
                       atr_abs: Optional[float] = None) -> Tuple[List[str], List[dict]]:
    """Parse a side's 3.4 `execution` (C3) → (fatal, raw contracts). Each raw contract carries `invalid`: the documented
    invalidities it has (unparseable / stop_wrong_side / level_unreachable / unknown_event). `cost_bps` present → FATAL.
    Levels and stops are checked against `sc`; a contract's `event` against the FILING side's scenario `event_sc`
    (default `sc`) — the mixture A only carries an event both sides agree on. `atr_abs` (one ATR in price) given → a stop
    nearer than STOP_NEAR_ATR to its entry reference carries `near` (the distance in ATR): a flag the arbiter may void
    on, not an invalidity the script acts on."""
    fatal: List[str] = []
    raw: List[dict] = []
    if ex is None or ex == {}:
        return fatal, raw
    if not isinstance(ex, dict):
        return ["execution is not an object"], raw
    if "cost_bps" in ex:
        fatal.append("execution.cost_bps is not allowed under schema 3.4 (cost comes from the session snapshot)")
    for k in ("stop", "conditional"):
        if k in ex:
            fatal.append(f"execution.{k} at the top level is not allowed; use execution.overweight / underweight")
    P0 = float(sc["P0"]) if sc and _is_num(sc.get("P0")) else None
    ev = (event_sc if event_sc is not None else (sc or {})).get("event")
    ev_name = ev.get("name") if isinstance(ev, dict) else None
    for direction in DIRECTIONS:
        d = ex.get(direction)
        if d is None or d == {}:
            continue
        if not isinstance(d, dict):
            fatal.append(f"execution.{direction} is not an object"); continue
        long = direction == "overweight"
        stop, stop_ok = _parse_stop(d.get("stop"))
        if d.get("stop") not in (None, {}):
            inv = [] if stop_ok else ["unparseable"]
            if stop_ok and P0 is not None and not _stop_protective(stop, P0, long):
                inv.append("stop_wrong_side")
            raw.append({"strategy": f"{direction}_now", "direction": direction, "name": None, "stop": stop, "invalid": inv,
                        "near": None if inv else _stop_near(stop, P0, atr_abs)})
        conds = d.get("conditional") or []
        if not isinstance(conds, list):
            fatal.append(f"execution.{direction}.conditional is not a list"); continue
        for c in conds:
            c = c if isinstance(c, dict) else {}
            item = {"strategy": f"{direction}_on_condition", "direction": direction, "name": c.get("name"), "stop": stop,
                    "observe": c.get("observe"), "fill": None, "kind": None, "op": None, "level": None, "invalid": []}
            obs = _parse_observe(c.get("observe"))
            fill = str(c.get("fill") or "next_open")
            if not obs or fill not in ("next_open", "at_level") or not stop_ok:
                item["invalid"].append("unparseable"); raw.append(item); continue
            kind, op, level = obs
            item.update({"kind": kind, "op": op, "level": level, "fill": fill, "observe": f"{kind} {op} {level:g}"})
            if c.get("event") is not None and c.get("event") != ev_name:
                item["invalid"].append("unknown_event")
            if stop is not None and not _stop_protective(stop, level, long):
                item["invalid"].append("stop_wrong_side")
            if sc is not None and not _level_reachable(sc, op, level):
                item["invalid"].append("level_unreachable")
            item["near"] = None if item["invalid"] else _stop_near(stop, level, atr_abs)
            raw.append(item)
    return fatal, raw


def _candidate_id(item: dict) -> str:
    st = item.get("stop")
    stop_s = f"{st['level']:g}{st['semantics']}" if st else "none"
    if item.get("kind"):
        return f"{item['strategy']}[{item['kind']}{item['op']}{item['level']:g}|{item['fill']}|stop:{stop_s}]"
    return f"{item['strategy']}[{item.get('observe')!s}|stop:{stop_s}]"


def build_candidates(z_exec: Any, r_exec: Any, sc: dict, voided: Optional[list] = None, rounds: Optional[dict] = None,
                     warn: Optional[list] = None, include_invalid: bool = False,
                     filers: Optional[Dict[str, dict]] = None, atr_abs: Optional[float] = None) -> List[dict]:
    """C6.4 candidate set: `_now` per direction (stop rule recorded in `stop_source`) + the union of both sides' valid
    conditionals, identical contracts merged with both provenances, voids honoured only for a confirmed documented invalidity.
    `filers` = {"Z": scenario, "R": scenario}: the scenario each side filed its execution in; a contract's `event` is
    validated against its filer's scenario (default: `sc`). `atr_abs` given → a stop nearer than one ATR to its entry
    reference is reported in `warn`; a void with reason `stop_too_near` then drops that stop from a `_now` candidate (the
    other side's stop, if it filed one that is not near, takes its place; else no stop) or excludes that conditional."""
    warn = warn if warn is not None else []
    rounds = rounds or {}
    stops: Dict[str, List[Tuple[str, dict]]] = {d: [] for d in DIRECTIONS}
    near_now: Dict[Tuple[str, str], float] = {}
    void_near = {str(v.get("id")) for v in voided or []
                 if isinstance(v, dict) and "stop_too_near" in str(v.get("reason") or "")}
    conds: Dict[str, dict] = {}
    for side, ex in (("Z", z_exec), ("R", r_exec)):
        fatal, raw = validate_execution(ex, sc, (filers or {}).get(side), atr_abs)
        warn.extend(f"{side}: {f}" for f in fatal)
        prov = {"side": side, "round": rounds.get(side)}
        for item in raw:
            if item["strategy"].endswith("_now"):
                if item["invalid"]:
                    warn.append(f"{side} {item['direction']} stop not used ({', '.join(item['invalid'])})")
                elif item.get("near") is not None and f"{item['direction']}_now" in void_near:
                    warn.append(f"{side} {item['direction']} stop {item['stop']['level']:g} not used: voided, "
                                f"{item['near']:.2f} ATR from the reference (stop_too_near)")
                    near_now[(item["direction"], "voided")] = item["near"]
                else:
                    stops[item["direction"]].append((side, item["stop"]))
                    if item.get("near") is not None:
                        near_now[(item["direction"], side)] = item["near"]
                continue
            cid = _candidate_id(item)
            if cid in conds:
                conds[cid]["provenance"].append(dict(prov, name=item.get("name")))
                continue
            conds[cid] = {"id": cid, "strategy": item["strategy"], "direction": item["direction"], "observe": item["observe"],
                          "kind": item["kind"], "op": item["op"], "level": item["level"], "fill": item["fill"],
                          "stop": item["stop"], "provenance": [dict(prov, name=item.get("name"))],
                          "invalid": list(item["invalid"]), "near": item.get("near")}
    nows: List[dict] = []
    for direction in DIRECTIONS:
        filed = stops[direction]
        stop, source = None, None
        if len(filed) == 1:
            source, stop = filed[0]
        elif len(filed) == 2:
            (_, a), (_, b) = filed
            if a == b:
                stop, source = a, "Z+R"
            else:
                long = direction == "overweight"
                # more conservative = closer to P0; equal level → touch (the earlier trigger)
                source, stop = max(filed, key=lambda t: ((t[1]["level"] if long else -t[1]["level"]), t[1]["semantics"] == "touch"))
        nows.append({"id": f"{direction}_now", "strategy": f"{direction}_now", "direction": direction, "level": None,
                     "stop": stop, "stop_source": source,
                     "provenance": [{"side": s, "round": rounds.get(s)} for s, st in filed if st == stop]})
        k = next((near_now[(direction, s)] for s, st in filed if st == stop and (direction, s) in near_now), None)
        if k is not None:
            warn.append(f"{direction}_now stop {stop['level']:g} is {k:.2f} ATR from the reference (P06 reference: one ATR or "
                        f"more); it stands unless voided: voided.candidates {{id: {direction}_now, reason: stop_too_near}}")
    for v in voided or []:
        v = v if isinstance(v, dict) else {}
        vid, reason = v.get("id"), str(v.get("reason") or "")
        token = next((t for t in DOCUMENTED_INVALIDITY if t in reason), None)
        if vid in (f"{d}_now" for d in DIRECTIONS):
            if token == "stop_too_near":
                if (vid[:-4], "voided") not in near_now:
                    warn.append(f"void of {vid} stop ignored: stop_too_near is not confirmed by the contract check")
                continue
            warn.append(f"void of {vid} ignored: the _now candidate of each direction always exists; only its stop can be "
                        f"voided (reason stop_too_near)"); continue
        if vid not in conds:
            warn.append(f"void of {vid} ignored: no such candidate"); continue
        if token is None:
            warn.append(f"void of {vid} ignored: reason {reason!r} is not a documented invalidity ({', '.join(DOCUMENTED_INVALIDITY)})")
            continue
        if token == "stop_too_near" and conds[vid].get("near") is not None:
            conds[vid]["invalid"].append(token)
        if token not in conds[vid]["invalid"]:
            warn.append(f"void of {vid} ignored: {token} is not confirmed by the contract check"); continue
        conds[vid]["voided"] = reason
    out = list(nows)
    # C7 (G1): candidate order never depends on who filed first — conditionals sorted by direction, then id
    for cid, c in sorted(conds.items(), key=lambda kv: (DIRECTIONS.index(kv[1]["direction"]), kv[0])):
        if not include_invalid:
            if c["invalid"]:
                if not c.get("voided"):
                    warn.append(f"candidate {cid} excluded: {', '.join(c['invalid'])}")
                continue
            if c.get("near") is not None:
                warn.append(f"candidate {cid}: stop is {c['near']:.2f} ATR from its entry level (P06 reference: one ATR or "
                            f"more); it stands unless voided with reason stop_too_near")
            c = {k: v for k, v in c.items() if k not in ("invalid", "near")}
        out.append(c)
    return out


def _stop_hit(e: dict, stop: Optional[dict], long: bool = True) -> bool:
    """Stop trigger. Close semantics: a close at or beyond the level. Touch semantics: ANY event at or beyond
    the level — touch, gap_open, open or close (a path that only records closes still passes through the level, and a
    touch stop is the earlier trigger of the two) — exiting at that event's price (the gap price for gap_open / open)."""
    if not stop or not _is_num(stop.get("level")):
        return False
    lvl, sem, price = float(stop["level"]), str(stop.get("semantics") or "close"), float(e["price"])
    beyond = price <= lvl if long else price >= lvl
    if sem == "close":
        return e["kind"] == "close" and beyond
    return beyond


def _observe_hit(e: dict, kind: str, op: str, level: float) -> bool:
    """Conditional trigger. `close` / `open` / `gap_open` observe exactly that event kind at/beyond the level. `touch`
    observes ANY event at or beyond the level — touch, close, open or gap_open — exactly like a touch stop and like the
    postcheck (daily high / low): a resting order is filled by a path that passes through its level even when the
    author recorded only the close or the gap of that day."""
    if kind != "touch" and e["kind"] != kind:
        return False
    return _cmp(float(e["price"]), op, float(level))


def conditional_fill_policy(cfg: Optional[dict]) -> str:
    """Opt-in versioned contract; absent in old snapshots and default configuration = legacy replay."""
    policy = (cfg or {}).get("conditional_fill_policy", "legacy_next_written_open")
    if policy not in ("legacy_next_written_open", "strict_next_open_v1"):
        raise ValueError(f"unknown conditional_fill_policy: {policy!r}")
    return policy


def simulate_leaf(leaf: dict, strategy: str, candidate: Optional[dict], sc: dict, cfg: Optional[dict] = None) -> dict:
    """One leaf under one strategy: r_path = (P_exit − P_fill)/P0 per unit of d (0 unfilled), n_tx, filled / stopped /
    exposure_end / missed / avoided. Stops exit (sell back or buy back) at the executable post-gap price. A conditional
    triggers per `_observe_hit` and fills per `fill`: `at_level` at the triggering event's price, `next_open` at the
    next day's first open / gap_open."""
    P0 = float(sc["P0"])
    events = leaf["events"]
    PT = float(leaf["P_T"])
    r_end = PT / P0 - 1.0
    out = {"strategy": strategy, "leaf": leaf["id"], "filled": False, "stopped": False, "fill_price": None, "exit_price": None,
           "r_path": 0.0, "n_tx": 0, "exposure_end": 0.0, "missed": 0.0, "avoided": 0.0}
    strict_entry = (conditional_fill_policy(cfg) == "strict_next_open_v1"
                    and strategy.endswith("_on_condition") and (candidate or {}).get("fill") == "next_open")
    if strict_entry:
        out["entry_status"] = "not_triggered"
    if strategy == "neutral":
        return out
    long = strategy.startswith("overweight")
    stop = (candidate or {}).get("stop")

    mode = stop_fill_mode(cfg)
    T = int(sc.get("T_days") or (cfg or DEFAULT_CONFIG).get("T_days", 15))
    mu = drift_mu(cfg)

    def bench(t: float) -> float:
        """The drift benchmark at day t, B_t = (1+mu)^(t/T); fills and stops are decided on real prices, only the
        payoff is read against it. mu = 0 → exactly 1."""
        return (1.0 + mu) ** (min(max(float(t), 0.0), float(T)) / T) if mu else 1.0

    def close_stop_exit(j: int, e: dict) -> Tuple[Optional[float], str, float]:
        """Exit of a close stop triggered at events[j] → (price, source, day); (None, "valuation", T) = not executed in the window."""
        if mode == "trigger_price":   # sessions before 2026-11: the next written open, else the triggering close
            nxt = _next_open_price(events, j)
            return (nxt[1], "open", float(events[nxt[0]]["t"])) if nxt else (float(e["price"]), "trigger", float(e["t"]))
        if int(e["t"]) >= T:          # the valuation-day close: valued at P_T, no exit inside the window
            return None, "valuation", float(T)
        later = _next_day_price(events, j)
        if later is None:             # nothing written after the trigger: the valuation-day price, never better than the trigger
            price, when = PT, float(T)
        elif events[later[0]]["kind"] in ("gap_open", "open") and int(events[later[0]]["t"]) == int(e["t"]) + 1:
            return later[1], "open", float(events[later[0]]["t"])   # the written next-day open, its gap included
        else:
            price, when = later[1], float(events[later[0]]["t"])   # a later event stands in for the missing open
        trig = float(e["price"])
        return (min(trig, price) if long else max(trig, price)), "proxy", when

    def filled(fill: float, start_idx: int, fill_t: float = 0.0) -> dict:
        exit_price, stopped, source, exit_t = PT, False, None, float(T)
        for j in range(start_idx, len(events)):
            e = events[j]
            if _stop_hit(e, stop, long):
                if str(stop.get("semantics") or "close") != "close":
                    exit_price, stopped, source, exit_t = float(e["price"]), True, "touch", float(e["t"])
                else:
                    px, source, when = close_stop_exit(j, e)
                    if px is not None:
                        exit_price, stopped, exit_t = px, True, when
                break
        out.update({"filled": True, "stopped": stopped, "fill_price": fill, "exit_price": exit_price,
                    "r_path": (exit_price / bench(exit_t) - fill / bench(fill_t)) / P0,
                    "n_tx": 2 if stopped else 1, "exposure_end": 0.0 if stopped else 1.0})
        if source:
            out["stop_fill"] = source
        return out

    if strategy.endswith("_now"):
        return filled(P0, 0)
    if not strategy.endswith("_on_condition"):
        raise ValueError(f"unknown 3.4 strategy {strategy}")
    cand = candidate or {}
    kind, op, level = cand.get("kind"), cand.get("op"), cand.get("level")
    if kind and op and _is_num(level):
        for j, e in enumerate(events):
            if _observe_hit(e, kind, op, float(level)):
                if cand.get("fill") == "limit":   # simple card: a resting limit fills at its level, or at a better opening gap
                    better = e["kind"] in ("gap_open", "open")
                    return filled(float(e["price"]) if better else float(level), j + 1, float(e["t"]))
                if cand.get("fill") == "at_level":
                    return filled(float(e["price"]), j + 1, float(e["t"]))
                nxt = _next_open_price(events, j)
                if strict_entry:
                    trigger_day = int(e["t"])
                    out.update(trigger_day=trigger_day, expected_open_day=trigger_day + 1)
                    if trigger_day >= T:
                        out["entry_status"] = "pending_after_horizon"
                        break
                    if nxt is None or int(events[nxt[0]]["t"]) != trigger_day + 1:
                        out.update(entry_status="unknown_next_open", r_path=None, n_tx=None,
                                   exposure_end=None, missed=None, avoided=None,
                                   next_written_open_day=int(events[nxt[0]]["t"]) if nxt else None)
                        return out
                    out["entry_status"] = "filled"
                if nxt:
                    return filled(nxt[1], nxt[0] + 1, float(events[nxt[0]]["t"]))
                break
    up, down = max(r_end, 0.0), max(-r_end, 0.0)
    out.update({"missed": up if long else down, "avoided": down if long else up})
    return out


def leaf_payoff(d: float, sim: dict, cost_bps: float, s_s: float, sigma_ref: float) -> float:
    """W* = (σ_ref / s_s)·(d·r_path − n_tx·|d|·c), c = cost_bps / 1e4; 0 if unfilled."""
    if sim.get("entry_status") == "unknown_next_open":
        raise ValueError("cannot price a triggered conditional with an unknown next-session open")
    if not sim.get("filled"):
        return 0.0
    c = float(cost_bps or 0.0) / 1e4
    return (float(sigma_ref) / float(s_s)) * (d * float(sim["r_path"]) - int(sim["n_tx"]) * abs(d) * c)


def _argmax_concave(f, lo: float, hi: float, iters: int = 100) -> Tuple[float, float]:
    a, b = lo, hi
    for _ in range(iters):
        m1, m2 = a + (b - a) / 3, b - (b - a) / 3
        if f(m1) < f(m2):
            a = m1
        else:
            b = m2
    x = (a + b) / 2
    return x, f(x)


def mean_shrink(cfg: Optional[dict]) -> float:
    """Share of a strategy's mean payoff taken as information when sizing (snapshot key `mean_shrink`, 0 < k ≤ 1; absent =
    1 = sessions opened before 2026-11-11). A declared convention until the slope of realised on forecast returns is
    measured; it changes sizes, hardly directions (docs/design/drill/ACCURACY-BASELINE-2026-11-10.md §13)."""
    v = (cfg or {}).get("mean_shrink")
    return float(v) if _is_num(v) and 0.0 < float(v) <= 1.0 else 1.0


def _shrink_rows(rows: List[Tuple[float, dict]], k: float) -> List[Tuple[float, dict]]:
    """Each filled leaf's payoff moved toward zero by (1−k) × the strategy's mean payoff over its filled leaves: the mean
    is multiplied by k, the dispersion kept; unfilled leaves stay at zero."""
    if k >= 1.0:
        return rows
    pf = sum(p for p, sim in rows if sim.get("filled"))
    if pf <= 0:
        return rows
    m = sum(p * float(sim["r_path"]) for p, sim in rows if sim.get("filled")) / pf
    return [(p, dict(sim, r_path=float(sim["r_path"]) - (1.0 - k) * m) if sim.get("filled") else sim) for p, sim in rows]


def _optimise_d(rows: List[Tuple[float, dict]], sign: int, gamma: float, c_bps: float, s_s: float, sigma_ref: float) -> dict:
    """Best |d| in [0, 0.5] (grid of 0.005 + ternary refinement) and the unconstrained |d| (no 0.5 cap); CE is concave in |d|."""
    def ce(a: float) -> float:
        return certainty_equivalent([(p, leaf_payoff(sign * a, s, c_bps, s_s, sigma_ref)) for p, s in rows], gamma)
    n = int(round(_D_MAX / _D_STEP))
    grid = [(i * _D_STEP, ce(i * _D_STEP)) for i in range(n + 1)]
    a, v = max(grid, key=lambda t: t[1])
    a_t, v_t = _argmax_concave(ce, max(0.0, a - _D_STEP), min(_D_MAX, a + _D_STEP))
    if v_t > v:
        a, v = a_t, v_t
    if v <= _CE_EPS or a <= 0.0:          # no strictly positive CE (float noise near d = 0 is not an edge)
        a, v = 0.0, 0.0
    a_u, v_u = _argmax_concave(ce, 0.0, _D_UNCONSTRAINED, 200)
    return {"a": a, "CE": v, "a_opt": a_u if v_u > _CE_EPS else 0.0}


def compare_strategies(sc: dict, candidates: List[dict], cfg: Optional[dict] = None, s_s: Optional[float] = None,
                          cost_bps: Optional[float] = None) -> dict:
    """C7: neutral + every candidate, each at its CE-optimal d inside its sign; sign-aware ties; neutral only if the best
    action's CE ≤ 0. cost_bps / s_s default to the snapshot values in cfg (cost_bps, unit_basis.s_s); s_s missing → σ_ref."""
    cfg = cfg or DEFAULT_CONFIG
    gamma = float(cfg.get("gamma", DEFAULT_CONFIG["gamma"]))
    policy = conditional_fill_policy(cfg)
    strict_entry = policy == "strict_next_open_v1"
    sigma_ref = float(cfg.get("sigma_ref", DEFAULT_CONFIG["sigma_ref"]))
    warn: List[str] = []
    if cost_bps is None and not _is_num(cfg.get("cost_bps")):
        warn.append("cost_bps unavailable → payoffs computed without cost (c = 0)")
    c_bps = float(cost_bps if cost_bps is not None else (cfg.get("cost_bps") or 0.0))
    if s_s is None:
        s_s = (cfg.get("unit_basis") or {}).get("s_s")
    if not _is_num(s_s) or float(s_s) <= 0:
        warn.append("s_s unavailable → payoffs not normalised (σ_ref / s_s = 1)")
        s_s = sigma_ref
    s_s = float(s_s)
    P0 = float(sc["P0"])
    shrink = mean_shrink(cfg)
    leaves = expand_leaves(sc)
    cands = list(candidates or [])
    have = {c.get("id") for c in cands}
    for i, direction in enumerate(DIRECTIONS):
        if f"{direction}_now" not in have:
            cands.insert(i, {"id": f"{direction}_now", "strategy": f"{direction}_now", "direction": direction, "level": None,
                             "stop": None, "stop_source": None, "provenance": []})
    res: Dict[str, dict] = {"neutral": {"strategy": "neutral", "d": 0.0, "q": 0.5, "x": 0.0, "x_opt": 0.0, "CE": 0.0,
                                        "exposure_change": 0.0, "p_filled": 0.0, "p_stopped": 0.0, "missed": 0.0,
                                        "avoided": 0.0, "provenance": [], "level": None, "stop": None, "stop_source": None}}
    detail: Dict[str, list] = {}
    eligibility: Dict[str, dict] = {}
    # C7 (G1): compared in a deterministic order (direction, then id) so insertion order (Z first) never decides a tie
    for c in sorted(cands, key=lambda c: (DIRECTIONS.index(c["direction"]) if c.get("direction") in DIRECTIONS else 2,
                                          str(c.get("id")))):
        name, strat = c["id"], c["strategy"]
        sign = 1 if strat.startswith("overweight") else -1
        sims = [simulate_leaf(l, strat, c, sc, cfg) for l in leaves]
        pairs = [(l, s) for l, s in zip(leaves, sims) if not strict_entry or l["p"] > 0]
        if strict_entry and c.get("fill") == "next_open":
            mass = {status: sum(l["p"] for l, s in pairs if s.get("entry_status") == status)
                    for status in ("not_triggered", "filled", "pending_after_horizon", "unknown_next_open")}
            eligibility[name] = {"eligible": mass["unknown_next_open"] == 0, "mass": mass,
                                 "leaves": [{"leaf": l["id"], "p": l["p"], **{k: s[k] for k in
                                             ("entry_status", "trigger_day", "expected_open_day", "next_written_open_day") if k in s}}
                                            for l, s in zip(leaves, sims)]}
            if mass["unknown_next_open"] > 0:
                detail[name] = sims
                warn.append(f"candidate {name} excluded: unknown next-session open on probability mass {mass['unknown_next_open']:.6g}")
                continue
        opt = _optimise_d(_shrink_rows([(l["p"], s) for l, s in pairs], shrink), sign, gamma, c_bps, s_s, sigma_ref)
        d = sign * opt["a"]
        p_filled = sum(l["p"] for l, s in pairs if s["filled"])
        res[name] = {"strategy": strat, "d": round(d, 6), "q": round(0.5 + d, 6), "x": round(2 * abs(d), 6),
                     "x_opt": round(2 * opt["a_opt"], 4), "CE": opt["CE"], "exposure_change": round(abs(d) * p_filled, 6),
                     "p_filled": round(p_filled, 4),
                     "p_stopped": round(sum(l["p"] for l, s in pairs if s["stopped"]), 4),
                     "missed": round(sum(l["p"] * s["missed"] for l, s in pairs), 5),
                     "avoided": round(sum(l["p"] * s["avoided"] for l, s in pairs), 5),
                     "provenance": c.get("provenance") or [], "level": c.get("level"), "stop": c.get("stop"),
                     "stop_source": c.get("stop_source")}
        detail[name] = sims
    tie = float(cfg.get("tie_threshold", 0.0005))
    actions = [n for n in res if n != "neutral"]
    best_act = max(actions, key=lambda n: res[n]["CE"])
    ce_best = res[best_act]["CE"]
    tie_rule = None
    # C7 (G1): the best CE shared (within _CE_EPS) by strategies of opposite directions → neutral, never a side's pick
    cross = sorted(n for n in actions if res[n]["d"] != 0 and ce_best - res[n]["CE"] <= _CE_EPS) if ce_best > 0 else []
    if ce_best <= 0:
        best, tie_set, tie_selected = "neutral", ["neutral"], False
    elif len({res[n]["d"] > 0 for n in cross}) > 1:
        best, tie_set, tie_selected = "neutral", ["neutral"] + cross, True
        tie_rule = "cross_direction_tie: the best CE is shared by opposite directions → neutral"
    else:
        pos = res[best_act]["d"] > 0
        tie_set = [n for n in actions if res[n]["d"] != 0 and (res[n]["d"] > 0) == pos and ce_best - res[n]["CE"] <= tie]
        tie_set.sort(key=lambda n: (res[n]["exposure_change"],
                                    abs(float(res[n]["level"]) - P0) if _is_num(res[n]["level"]) else 0.0, n))
        best = tie_set[0]
        tie_selected = len(tie_set) > 1 or ce_best <= tie   # another action, or neutral, inside the threshold
    x_opt_of = best if best != "neutral" else best_act   # neutral: report the best action's unconstrained optimum, named
    sens = {}
    for g in cfg.get("gamma_sensitivity") or []:
        gcfg = dict(cfg); gcfg["gamma"] = float(g); gcfg["gamma_sensitivity"] = []
        sens[str(g)] = {n: v["x"] for n, v in compare_strategies(sc, candidates, gcfg, s_s, c_bps)["strategies"].items()}
    out = {"schema": "3.4", "gamma": gamma, "mean_shrink": shrink, "cost_bps": c_bps, "s_s": s_s, "sigma_ref": sigma_ref,
           "strategies": res, "leaf_detail": detail, "best": best, "best_action": best_act,
           "x_opt": float(res[x_opt_of]["x_opt"]), "x_opt_strategy": x_opt_of, "tie_selected": bool(tie_selected), "tie_set": tie_set,
           "candidates": cands, "gamma_sensitivity": sens, "warn": warn}
    if tie_rule:
        out["tie_rule"] = tie_rule
    if strict_entry:
        out["conditional_execution"] = {"policy": policy, "candidates": eligibility}
    return out


def _operation(entry: dict, candidate: Optional[dict], P0: float) -> Tuple[str, float, Optional[dict]]:
    """C7 mapping → (operation_type, operation_target, operation_execution or None)."""
    strat, x = entry["strategy"], float(entry["x"])
    cond = None
    if strat.endswith("_on_condition"):
        cond = {"conditional": {"observe": candidate.get("observe"), "fill": candidate.get("fill")}, "stop": candidate.get("stop")}
    elif strat.endswith("_now") and candidate and candidate.get("stop"):
        cond = {"stop": candidate["stop"]}   # the stop the chosen _now candidate carries (simulated, so it is part of the contract)
    if strat == "neutral":
        return "HOLD_NEUTRAL", P0, None
    if strat == "overweight_now":
        return "ENTRY", P0, cond
    if strat == "overweight_on_condition":
        return "ENTRY", float(candidate["level"]), cond
    if strat == "underweight_now":
        return ("LIQUIDATE_NOW" if x >= 1.0 - 1e-9 else "TRIM_POSITION"), P0, cond
    if strat == "underweight_on_condition":
        return "TRIM_POSITION", float(candidate["level"]), cond   # never LIQUIDATE_NOW
    raise ValueError(f"unknown 3.4 strategy {strat}")


_LIMIT_OPS = {"overweight": ("<=", "<"), "underweight": (">=", ">")}


def simple_candidates(candidates: List[dict], P0: Optional[float] = None) -> List[dict]:
    """The simple card's candidates — one order placed and left, no stop, no confirmation: act now, or a resting limit
    order (buy below / sell above the current price: a `touch` conditional on the limit side, re-filled as `fill: limit`).
    Breakout orders (buy above / sell below, a stop order at the broker) are not in the simple card. A subset of the
    full card's valid candidates; the stop is dropped from each, and candidates left identical are kept once."""
    out, seen = [], set()
    for c in candidates or []:
        s, direction = str(c.get("strategy") or ""), c.get("direction")
        limit = (s.endswith("_on_condition") and c.get("kind") == "touch" and c.get("fill") == "at_level"
                 and c.get("op") in _LIMIT_OPS.get(direction, ()) and _is_num(c.get("level"))
                 and (P0 is None or (float(c["level"]) < P0 if direction == "overweight" else float(c["level"]) > P0)))
        if not (s.endswith("_now") or limit):
            continue
        key = (s, c.get("op"), c.get("level")) if limit else (s,)
        if key in seen:
            continue
        seen.add(key)
        c2 = copy.deepcopy(c)
        c2["stop"], c2["stop_source"] = None, None
        if limit:   # a plain limit fills at its level: inclusive (no queue or volume model)
            c2["fill"], c2["op"] = "limit", {"<": "<=", ">": ">="}.get(c2["op"], c2["op"])
        out.append(c2)
    return out


def derive_simple(norm: dict, candidates: List[dict], cfg: Optional[dict] = None, s_s: Optional[float] = None,
                  cost_bps: Optional[float] = None) -> dict:
    """The simple card on the same distribution: neutral / now / one resting order, no stop (simple_candidates).
    → chosen / q_star / x_star / operation_type (ENTRY / TRIM_POSITION / LIQUIDATE_NOW / HOLD_NEUTRAL) / operation_target /
    operation_size / display. Derived only; it never changes the full card."""
    cfg = cfg or DEFAULT_CONFIG
    cmp_ = compare_strategies(norm, simple_candidates(candidates, float(norm["P0"])), cfg, s_s, cost_bps)
    chosen = cmp_["best"]
    entry = cmp_["strategies"][chosen]
    cand = next((c for c in cmp_["candidates"] if c.get("id") == chosen), None)
    op_type, target, _ = _operation(entry, cand, float(norm["P0"]))
    q, x = float(entry["q"]), float(entry["x"])
    order = "none" if chosen == "neutral" else ("limit" if "_on_condition" in chosen else "market")
    if chosen == "neutral":
        enum, display = "N/A", NEUTRAL_DISPLAY
    else:
        enum = size_from_x(x, cfg)[1]
        how = f"limit at {target:g}" if order == "limit" else f"now at {target:g} (reference price)"
        display = (f"{entry['strategy'].split('_')[0]} {how}, no stop; size {enum}; "
                   f"target exposure {100 * q:.0f}% of the standard unit (x* {100 * x:.0f}%)")
    return {"chosen": chosen, "order": order, "q_star": q, "x_star": x, "operation_type": op_type, "operation_target": target,
            "operation_size": enum, "display": display, "tie_set": cmp_["tie_set"], "tie_rule": cmp_.get("tie_rule")}


def derive_for_report(sc: dict, candidates: List[dict], cfg: Optional[dict] = None, atr_pct: Optional[float] = None,
                         claim_ids: Optional[set] = None, s_s: Optional[float] = None, cost_bps: Optional[float] = None) -> dict:
    """Everything stored under scenario.derived for A (C6.5 / C7): the strategy and its enum are derived, never authored."""
    cfg = cfg or DEFAULT_CONFIG
    fatal, warn, norm = validate_scenario(sc, cfg, atr_pct, claim_ids)
    if fatal:
        return {"ok": False, "fatal": fatal, "warn": warn}
    sco = score(norm, drift_mu(cfg))
    cmp_ = compare_strategies(norm, candidates, cfg, s_s, cost_bps)
    warn.extend(cmp_["warn"])
    chosen = cmp_["best"]
    entry = cmp_["strategies"][chosen]
    cand = next((c for c in cmp_["candidates"] if c.get("id") == chosen), None)
    op_type, target, op_exec = _operation(entry, cand, float(norm["P0"]))
    q, x = float(entry["q"]), float(entry["x"])
    if chosen == "neutral":
        tier, enum, display = "none", "N/A", NEUTRAL_DISPLAY
    else:
        tier, enum = size_from_x(x, cfg)
        display = f"{entry['strategy']}; target exposure {100 * q:.0f}% of the standard unit (x* {100 * x:.0f}%)"
    simple = derive_simple(norm, candidates, cfg, s_s, cost_bps)
    return {"ok": True, "schema": "3.4", "score": sco, "simple": simple, "comparison": {k: v for k, v in cmp_.items() if k != "leaf_detail"},
            "chosen": chosen, "q_star": q, "x_star": x, "x_opt": cmp_["x_opt"], "x_opt_strategy": cmp_["x_opt_strategy"], "size_tier": tier, "operation_size": enum,
            "operation_type": op_type, "operation_target": target, "operation_execution": op_exec, "display": display,
            "candidates": cmp_["candidates"], "tie_selected": cmp_["tie_selected"], "cost_bps": cmp_["cost_bps"],
            "warn": warn, "p_normalization": (norm.get("derived") or {}).get("p_normalization"),
            "leaves": [{"id": l["id"], "p": round(l["p"], 4), "P_T": l["P_T"], "z": l.get("z")} for l in expand_leaves(norm)]}
