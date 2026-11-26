#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""path_fit — how the realised daily path sat inside the report's scenario tree (postcheck reference block, 2026-11-06).

Owner (11-06): "a report that never hit a line but whose path looked like what it said also did well"; GPT council
(council/20261106-2030-path-fit-postcheck): take it into the postcheck as a *versioned diagnostic block*, never as points.
This module is the postcheck's own path measure — it does not reuse the duty loop's scripts/divergence.py (a routing
approximation): leaves come from the ruler's expand_leaves, event kinds are told apart (close events shape the close
path; open / gap_open / touch events are checked against the realised open / high / low), the mixture CRPS is the
continuous closed form, and the baseline is a zero-drift Gaussian on the same scale, named as such.

Model (PF1): leaf i with probability p_i has a close path m_i(t) piecewise-linear through (0, P0), its `close` events
and (T, P_T). On trading day t after T the forecast of the close is the mixture Σ p_i N(m_i(t), σ_t²) with
σ_t = P0 · ATR%14/100 · √t (the ruler's s_s scale, frozen on the report). Per day: directional PIT u_t = F_t(Y_t),
tail τ_t = min(u, 1−u), CRPS c_t normalised by P0·a·√t, the same for the baseline b_t, and the central-80% width.
Summaries follow the council's minimal set: directional PIT series, two-sided tail counts (α = 0.05) → central-90%
coverage, central-50% coverage (= the duty loop's `inside`), persistence (first breach / longest run / recovery),
path CRPS = equal-weight mean of normalised c_t over the valid days t < T, skill = 1 − Σc/Σb, mean W80. Mean tail,
mean KL, dominant-node stability are deliberately not produced (no "bigger is better" reading).

Pure functions on plain lists; the postcheck digest (workflows/postcheck_quant.py) loads the bars and calls `path_fit`.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

METHOD_VERSION = "PF1"
TAIL_ALPHA = 0.05          # two-sided tail count threshold → central-90% coverage
INSIDE_TAIL = 0.25         # central-50% band, the duty loop's `inside`
RECOVER_DAYS = 2           # recovery = first day after a breach with this many consecutive closes back inside central 90%
MIN_T = 0.5

CLOSE_KINDS = ("close",)
OPEN_KINDS = ("open", "gap_open")
TOUCH_KINDS = ("touch",)

try:
    from odds_model import expand_leaves  # noqa: F401
except Exception:  # pragma: no cover — the ruler lives beside this file; a bare fallback keeps the module importable
    def expand_leaves(sc: dict) -> List[dict]:
        leaves = []
        for n in sc.get("nodes") or []:
            subs = n.get("sub") or []
            if subs:
                for s in subs:
                    leaves.append({**s, "p": float(n.get("p") or 0) * float(s.get("p_cond") or s.get("p") or 0)})
            else:
                leaves.append(n)
        return leaves


# ---------------------------------------------------------------- Gaussian helpers
def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _Phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _e_abs_normal(mu: float, s: float) -> float:
    """E|Z| for Z ~ N(mu, s²)."""
    if s <= 0:
        return abs(mu)
    z = mu / s
    return s * math.sqrt(2.0 / math.pi) * math.exp(-0.5 * z * z) + mu * (1.0 - 2.0 * _Phi(-z))


def crps_gaussian_mixture(comps: Sequence[Tuple[float, float, float]], y: float) -> float:
    """CRPS of Σ p_i N(m_i, s_i²) at y, closed form: Σ p_i E|X_i − y| − ½ ΣΣ p_i p_j E|X_i − X_j|."""
    a = sum(p * _e_abs_normal(m - y, s) for p, m, s in comps)
    b = 0.0
    for p1, m1, s1 in comps:
        for p2, m2, s2 in comps:
            b += p1 * p2 * _e_abs_normal(m1 - m2, math.sqrt(s1 * s1 + s2 * s2))
    return a - 0.5 * b


def mixture_cdf(comps: Sequence[Tuple[float, float, float]], y: float) -> float:
    return sum(p * (_Phi((y - m) / s) if s > 0 else (1.0 if y >= m else 0.0)) for p, m, s in comps)


def mixture_quantile(comps: Sequence[Tuple[float, float, float]], q: float) -> float:
    lo = min(m - 8 * s for _p, m, s in comps)
    hi = max(m + 8 * s for _p, m, s in comps)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if mixture_cdf(comps, mid) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- tree → paths
def leaf_paths(scenario: dict) -> List[Dict[str, Any]]:
    """[{id, p, points[(t, price)], events[{t, kind, price}]}] with probabilities normalised. The close path runs through
    (0, P0), the leaf's `close` events sorted by t, and (T, P_T) when the terminal lies beyond the last close event."""
    if not isinstance(scenario, dict) or not scenario.get("nodes"):
        return []
    try:
        P0 = float(scenario.get("P0"))
        T = float(scenario.get("T_days") or 15)
    except (TypeError, ValueError):
        return []
    if P0 <= 0:
        return []
    leaves = []
    for n in expand_leaves(scenario):
        try:
            p = float(n.get("p") or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        if p <= 0:
            continue
        pts = [(0.0, P0)]
        evs = []
        for e in n.get("events") or []:
            try:
                t, price = float(e["t"]), float(e["price"])
            except (KeyError, TypeError, ValueError):
                continue
            kind = str(e.get("kind") or "close")
            evs.append({"t": t, "kind": kind, "price": price})
            if kind in CLOSE_KINDS:
                pts.append((t, price))
        pts.sort()
        try:
            PT = float(n.get("P_T"))
            if pts[-1][0] < T:
                pts.append((T, PT))
        except (TypeError, ValueError):
            pass
        leaves.append({"id": str(n.get("id") or ""), "p": p, "points": pts, "events": evs})
    tot = sum(l["p"] for l in leaves)
    if tot <= 0:
        return []
    for l in leaves:
        l["p"] = l["p"] / tot
    return leaves


def path_price(points: Sequence[Tuple[float, float]], t: float) -> float:
    if t <= 0:
        return points[0][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p1
    return points[-1][1]


def _authored_days(leaves: Sequence[Dict[str, Any]]) -> set:
    return {int(round(pt[0])) for l in leaves for pt in l["points"][1:-1]} | {int(round(l["points"][-1][0])) for l in leaves}


# ---------------------------------------------------------------- the block
def path_fit(scenario: dict, atr_pct: Optional[float], bars: Sequence[Dict[str, Any]], dt: int,
             event_day: Optional[int] = None) -> Dict[str, Any]:
    """bars: daily bars after T in order, dicts with day / open / high / low / close (the base day excluded). dt: the
    postcheck tier. event_day: 1-based index (in bars) of the first event day inside the window, for pre / post segments.
    Never raises on a bad tree — returns {"status": ...}."""
    leaves = leaf_paths(scenario)
    if not leaves:
        return {"status": "no_tree", "method_version": METHOD_VERSION, "diagnostic_only": True}
    try:
        P0 = float(scenario["P0"])
        T = int(round(float(scenario.get("T_days") or 15)))
        a = float(atr_pct) / 100.0 if atr_pct is not None else None
    except (TypeError, ValueError):
        return {"status": "bad_inputs", "method_version": METHOD_VERSION, "diagnostic_only": True}
    if not a or a <= 0:
        return {"status": "no_atr", "method_version": METHOD_VERSION, "diagnostic_only": True,
                "reason": "atr_pct_14 missing on the report: no frozen scale"}
    n_bars = len(bars)
    n_eff = min(n_bars, int(dt), T)   # the tree's forecast horizon ends at T; later days are not its samples
    if n_eff <= 0:
        return {"status": "no_bars", "method_version": METHOD_VERSION, "diagnostic_only": True, "bars_after_T": n_bars}
    authored = _authored_days(leaves)
    days: List[Dict[str, Any]] = []
    for t in range(1, n_eff + 1):
        b = bars[t - 1]
        try:
            y = float(b["close"])
        except (KeyError, TypeError, ValueError):
            days.append({"t": t, "day": b.get("day"), "status": "no_close"})
            continue
        s = P0 * a * math.sqrt(max(MIN_T, float(t)))
        comps = [(l["p"], path_price(l["points"], float(t)), s) for l in leaves]
        u = mixture_cdf(comps, y)
        c = crps_gaussian_mixture(comps, y)
        bl = _e_abs_normal(P0 - y, s) - 0.5 * _e_abs_normal(0.0, s * math.sqrt(2.0))   # zero-drift Gaussian on the same scale
        q10, q90 = mixture_quantile(comps, 0.1), mixture_quantile(comps, 0.9)
        days.append({"t": t, "day": b.get("day"), "close": round(y, 4), "u": round(u, 4), "tail": round(min(u, 1 - u), 4),
                     "crps_norm": round(c / s, 5), "baseline_crps_norm": round(bl / s, 5), "w80_norm": round((q90 - q10) / s, 4),
                     "point_source": "authored" if t in authored else ("terminal" if t == T else "interpolated")})
    valid = [d for d in days if "u" in d]
    out: Dict[str, Any] = {"status": "scored" if valid else "no_close", "method_version": METHOD_VERSION, "diagnostic_only": True,
                           "P0": P0, "atr_pct_14": round(a * 100.0, 4), "T_days": T, "dt": int(dt), "bars_after_T": n_bars,
                           "n_valid": len(valid), "horizon_truncated": bool(n_bars > T and int(dt) > T),
                           "sigma_model": "P0*ATR%14/100*sqrt(t); baseline = zero-drift Gaussian, same scale",
                           "days": days}
    if not valid:
        return out
    out["summary"] = _summary(valid, T)
    if event_day is not None and 1 <= int(event_day) <= n_eff:
        pre = [d for d in valid if d["t"] < int(event_day)]
        post = [d for d in valid if d["t"] >= int(event_day)]
        out["segments"] = {"event_day": int(event_day), "pre": _summary(pre, T) if pre else None, "post": _summary(post, T) if post else None,
                           "note": "first event day inside the window only; P0 / probabilities / ATR / t are never reset at the event"}
    term = next((d for d in valid if d["t"] == T), None)
    out["terminal"] = ({"t": T, "u": term["u"], "tail": term["tail"], "crps_norm": term["crps_norm"]} if term
                       else {"t": T, "status": "not reached in this window"})
    out["event_checks"] = event_checks(leaves, bars[:n_eff], P0)
    return out


def _summary(valid: List[Dict[str, Any]], T: int) -> Dict[str, Any]:
    """The council's minimal set over the given days (terminal day excluded from the mid-path CRPS aggregate)."""
    n = len(valid)
    lo = sum(1 for d in valid if d["u"] < TAIL_ALPHA)
    hi = sum(1 for d in valid if d["u"] > 1 - TAIL_ALPHA)
    inside = sum(1 for d in valid if d["tail"] >= INSIDE_TAIL)
    mid = [d for d in valid if d["t"] < T] or valid
    c_sum = sum(d["crps_norm"] for d in mid)
    b_sum = sum(d["baseline_crps_norm"] for d in mid)
    out = {"n": n, "tail_days_low": lo, "tail_days_high": hi,
           "coverage_90": round(1.0 - (lo + hi) / n, 4), "coverage_50": round(inside / n, 4),
           "path_crps_norm": round(c_sum / len(mid), 5), "baseline_crps_norm": round(b_sum / len(mid), 5),
           "skill_vs_zero_drift": (round(1.0 - c_sum / b_sum, 4) if b_sum > 0 else None),
           "w80_norm_mean": round(sum(d["w80_norm"] for d in valid) / n, 4),
           "bias": ("low" if lo > hi else "high" if hi > lo else "none") if (lo or hi) else "none"}
    out.update(_persistence(valid))
    return out


def _persistence(valid: List[Dict[str, Any]]) -> Dict[str, Any]:
    breached = [(d["t"], d["u"] < TAIL_ALPHA or d["u"] > 1 - TAIL_ALPHA) for d in valid]
    first = next((t for t, b in breached if b), None)
    longest, run = 0, 0
    for _t, b in breached:
        run = run + 1 if b else 0
        longest = max(longest, run)
    recover = None
    if first is not None:
        streak = 0
        for t, b in breached:
            if t <= first:
                continue
            streak = 0 if b else streak + 1
            if streak >= RECOVER_DAYS:
                recover = t - RECOVER_DAYS + 1
                break
    return {"first_breach_day": first, "longest_breach_run": longest,
            "recovery_day": recover, "recovered": (recover is not None) if first is not None else None,
            "recovery_rule": f"{RECOVER_DAYS} consecutive closes back inside central 90% after the first breach"}


def event_checks(leaves: Sequence[Dict[str, Any]], bars: Sequence[Dict[str, Any]], P0: float) -> List[Dict[str, Any]]:
    """Authored non-close events against the realised bars: an open / gap_open event at day t is checked on day t's open,
    a touch event on the high / low reached by day t (cumulative). `reached` = the realised value went at least as far from
    P0 as the event price, on the event's side; None when the day is outside the window or the bar lacks the field."""
    out = []
    n = len(bars)
    for l in leaves:
        for e in l["events"]:
            kind = e["kind"]
            if kind in CLOSE_KINDS:
                continue
            t = int(round(e["t"]))
            price = e["price"]
            up = price >= P0
            rec: Dict[str, Any] = {"node": l["id"], "t": t, "kind": kind, "price": price, "side": "up" if up else "down"}
            if t < 1 or t > n:
                rec["reached"] = None
                rec["note"] = "outside the window"
                out.append(rec)
                continue
            try:
                if kind in OPEN_KINDS:
                    v = float(bars[t - 1]["open"])
                    rec["realized_open"] = v
                    rec["reached"] = bool(v >= price) if up else bool(v <= price)
                elif kind in TOUCH_KINDS:
                    hs = [float(b["high"]) for b in bars[:t]]
                    ls = [float(b["low"]) for b in bars[:t]]
                    rec["realized_extreme"] = max(hs) if up else min(ls)
                    rec["reached"] = bool(max(hs) >= price) if up else bool(min(ls) <= price)
                else:
                    rec["reached"] = None
                    rec["note"] = f"unknown kind {kind}"
            except (KeyError, TypeError, ValueError):
                rec["reached"] = None
                rec["note"] = "bar lacks the field"
            out.append(rec)
    return out
