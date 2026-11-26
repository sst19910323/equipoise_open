#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Divergence of a report's own scenario tree from the realised price — the duty loop's routing measure (2026-11-06).

Replaces the "line hit" logic for v3.4 reports (owner, 11-06: the structured trigger lines were 4-8 unlabelled levels per
report; in the post-midterm shake nearly the whole US universe sat on a sticky "line hit" for two days and woke the duty
Codex every cooldown). The report already states what it expects: a mixture of paths (`scenario.nodes[]`, each with a
probability `p` and dated events). So the question the machine asks each breath is not "did the price cross a line" but
"how much of the report's own distribution is still compatible with where the price is now".

Measure (pure functions, no I/O):
- Each node's path is piecewise linear through (0, P0) and its events (t in trading days, price); past the last event it
  holds the terminal price. At trading day t each path is a Gaussian with that mean and sigma = P0 · ATR%14/100 · sqrt(t)
  (the same scale the ruler uses, config/odds.yaml: s_s = ATR%14/100 · sqrt(T_days)); the tree is the p-weighted mixture.
- `tail` = min(F, 1-F) with F the mixture CDF at the realised price (the probability-integral-transform tail: ~0 means the
  price sits outside everything the tree foresaw, 0.5 means dead centre).
- `kl_bits` = KL(posterior ‖ prior) over the nodes after conditioning on the realised price: how much the realised path
  has redrawn the picture (0 = nothing learned); `dominant` = the node with the largest posterior.
- `alive` = prior mass of nodes whose path lies within one sigma of the price (the coarse version, kept for reading).
- `level`: dead (tail < dead_tail) / drift (tail < drift_tail) / inside. Thresholds are routing parameters
  (config/duty.yaml `divergence`), to be calibrated; nothing here touches the postcheck ruler.
- `severity` = 1 - 2·tail ∈ [0, 1], for ranking the due list.

Lens-wise reports (GENERAL: by_lens[L].scenario) are judged per lens with that lens's own bars; the report's divergence is
the worst lens. ATR comes from the report (`atr_pct_14`) or, when absent (lenses), from the daily bars up to the base day.
"""
from __future__ import annotations

import math

DEAD_TAIL = 0.05
DRIFT_TAIL = 0.25
MIN_T = 0.5          # a partial current day counts half a trading day; never below this (sigma would be 0)
SIGMA_FLOOR = 0.25   # within-node sigma never below this fraction of the reference sigma (a tree wider than the
                     # reference keeps a kernel; otherwise the mixture collapses to spikes and prices between
                     # nodes read as dead) — council 2026-11-09 round 7
CC_DAYS = 60         # close-to-close daily SD window for the reference sigma


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def path_at(node: dict, P0: float, t: float, T: float | None = None) -> float:
    """Piecewise-linear path price of one node at trading day t: through (0, P0) and the node's events sorted by t,
    then (T, P_T) when the node states a terminal price beyond its last event; held flat past the last point."""
    pts = [(0.0, float(P0))]
    for e in node.get("events") or []:
        try:
            pts.append((float(e["t"]), float(e["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort()
    if T and node.get("P_T") is not None:
        try:
            pt = float(node["P_T"])
            if pts[-1][0] < float(T):
                pts.append((float(T), pt))
        except (TypeError, ValueError):
            pass
    if t <= 0:
        return float(P0)
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p1
    return pts[-1][1]


def tree_nodes(scenario: dict) -> list:
    """[(id, p, node)] with probabilities normalised; [] when the scenario is not a usable v3.4 tree. A node with
    `sub` is a group: its subs are the leaves (p = p_node × p_cond, the sub's events else the parent's) — the same
    leaves the ruler scores (odds_model.expand_leaves), so the watch and the score read one distribution."""
    if not isinstance(scenario, dict) or not scenario.get("nodes") or scenario.get("P0") is None:
        return []
    raw = []
    for n in scenario["nodes"]:
        if not isinstance(n, dict):
            continue
        try:
            p = float(n.get("p") or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        if p <= 0:
            continue
        subs = [x for x in (n.get("sub") or []) if isinstance(x, dict)]
        if subs:
            for k, x in enumerate(subs):
                try:
                    pc = float(x.get("p_cond") if x.get("p_cond") is not None else x.get("p") or 0.0)
                except (TypeError, ValueError):
                    pc = 0.0
                if pc <= 0:
                    continue
                leaf = dict(n, **{kk: vv for kk, vv in x.items() if kk not in ("p", "p_cond")})
                leaf.pop("sub", None)
                raw.append((str(x.get("id") or f"{n.get('id') or len(raw)}.{k}"), p * pc, leaf))
        else:
            raw.append((str(n.get("id") or len(raw)), p, n))
    W = sum(p for _, p, _ in raw)
    return [(i, p / W, n) for i, p, n in raw] if W > 0 else []


def atr_pct_from_bars(bars: list, base_day: str | None, n: int = 14) -> float | None:
    """ATR%14 from daily bars (dicts with date/open/high/low/close, ascending) ending on or before base_day: mean true range
    over the last n bars divided by the last close, in percent. None with fewer than 2 bars."""
    rows = [b for b in bars if not base_day or str(b.get("date")) <= str(base_day)]
    if len(rows) < 2:
        return None
    rows = rows[-(n + 1):]
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        try:
            h, l, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        except (KeyError, TypeError, ValueError):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    try:
        last = float(rows[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if not trs or last <= 0:
        return None
    return 100.0 * (sum(trs) / len(trs)) / last


def cc_sd_from_bars(bars: list, base_day: str | None, n: int = CC_DAYS) -> float | None:
    """Close-to-close daily return SD in percent over the last n bars up to the base day (the reference sigma's unit;
    ATR carries intraday range and runs ~1.3–1.5× wider than the close-to-close SD, which made the watch too lenient)."""
    xs = [b for b in bars or [] if not base_day or str(b.get("date")) <= str(base_day)]
    closes = []
    for b in xs[-(n + 1):]:
        try:
            closes.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < 20:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * 100.0 if var > 0 else None


def atr_of(entry: dict) -> float | None:
    """ATR%14 of a report or a lens entry: its own `atr_pct_14`, else the ruler's `scenario.derived.unit_basis.atr_pct_14`
    (present on every v3.4 report and lens; lens entries carry no top-level ATR)."""
    if not isinstance(entry, dict):
        return None
    for v in (entry.get("atr_pct_14"),
              (((entry.get("scenario") or {}).get("derived") or {}).get("unit_basis") or {}).get("atr_pct_14")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def expires_on(as_of_date, T_days) -> str | None:
    """ISO date of the horizon end: the base date plus T_days weekdays (the same rule as next_review.expires_at)."""
    from datetime import date, timedelta
    try:
        d = as_of_date if isinstance(as_of_date, date) else date.fromisoformat(str(as_of_date)[:10])
        T = max(1, int(float(T_days))) if T_days is not None else 15
    except (TypeError, ValueError):
        return None
    while T > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            T -= 1
    return d.isoformat()


def trading_days_between(as_of, now) -> float:
    """Trading days from the report's as_of to now without bars (the rebalance brief has prices, not bars): weekdays
    strictly after as_of's date up to and including today, today counted as half; never below MIN_T. Exchange
    holidays are not known here, so this can overstate t by a day around them — a routing quantity, not a ruler."""
    from datetime import timedelta
    d0, d1 = as_of.date(), now.date()
    if d1 <= d0:
        return MIN_T
    n, d = 0.0, d0 + timedelta(days=1)
    while d < d1:
        if d.weekday() < 5:
            n += 1.0
        d += timedelta(days=1)
    if d1.weekday() < 5:
        n += 0.5
    return max(MIN_T, n)


def report_divergence_at(report: dict, as_of, now, price_of, dead_tail: float = DEAD_TAIL,
                         drift_tail: float = DRIFT_TAIL) -> dict | None:
    """The same judgement as report_divergence, from a current price instead of bars: price_of(symbol) → float | None
    (the rebalance brief's live quote or holding price). t from trading_days_between; ATR from the report / lens
    entry (atr_of). Lens-wise reports: worst lens, each in `by_lens`. None without a tree, a price or an ATR."""
    by = report.get("by_lens")
    trees = []
    if isinstance(by, dict) and any(isinstance(e, dict) and tree_nodes(e.get("scenario") or {}) for e in by.values()):
        for L in [str(x) for x in (report.get("lenses") or [])] or list(by):
            e = by.get(L)
            if isinstance(e, dict) and tree_nodes(e.get("scenario") or {}):
                trees.append((L, e["scenario"], atr_of(e)))
    elif tree_nodes(report.get("scenario") or {}):
        trees.append((None, report["scenario"], atr_of(report)))
    if not trees or as_of is None or now is None:
        return None
    t = trading_days_between(as_of, now)
    results = {}
    for lens, sc, atr in trees:
        sym = lens or str(report.get("symbol") or "")
        try:
            price = price_of(sym)
        except Exception:
            price = None
        d = divergence(sc, atr, t, price, dead_tail, drift_tail)
        if d:
            d["lens"] = lens
            results[lens or sym] = d
    if not results:
        return None
    out = dict(max(results.values(), key=lambda d: d["severity"]))
    if trees[0][0] is not None:
        out["by_lens"] = {k: {kk: vv for kk, vv in v.items() if kk != "lens"} for k, v in results.items()}
    return out


def days_since(bars_after: list, today: str | None) -> float | None:
    """Trading days elapsed since the base day, from the bars dated after it (plus the base day's own partial bar when
    it is the only one). The latest bar is taken as partial (half a day) when it is dated today."""
    if not bars_after:
        return None
    last = str(bars_after[-1].get("date"))
    partial = bool(today) and last >= str(today)
    t = len(bars_after) - (0.5 if partial else 0.0)
    return max(MIN_T, float(t))


def divergence(scenario: dict, atr_pct: float | None, t: float | None, price: float | None,
               dead_tail: float = DEAD_TAIL, drift_tail: float = DRIFT_TAIL, cc_sd_pct: float | None = None) -> dict | None:
    """The measure for one tree at trading day t and realised price. None when any input is unusable.
    Reference sigma at t = P0 · sd_daily · √t, sd_daily the close-to-close SD (`cc_sd_pct`) when given, else ATR%.
    Within-node sigma = √max(σ_ref² − var_between, (SIGMA_FLOOR·σ_ref)²): the node spread already carries part of the
    forecast uncertainty, so the mixture's total variance is max(σ_ref², var_between) instead of their sum (the old
    sum made every tree read ~1.4× too wide: 0% of 233 duty readings ever fell below tail 0.05)."""
    nodes = tree_nodes(scenario)
    if not nodes or atr_pct is None or t is None or price is None:
        return None
    try:
        P0, atr_pct, price, t = float(scenario["P0"]), float(atr_pct), float(price), max(MIN_T, float(t))
    except (TypeError, ValueError):
        return None
    if P0 <= 0 or atr_pct <= 0 or price <= 0:
        return None
    sd_daily = float(cc_sd_pct) if cc_sd_pct and float(cc_sd_pct) > 0 else atr_pct
    sigma_ref = P0 * sd_daily / 100.0 * math.sqrt(t)
    T = scenario.get("T_days")
    mus = [(i, p, path_at(n, P0, t, T)) for i, p, n in nodes]
    m_bar = sum(p * mu for _, p, mu in mus)
    var_between = sum(p * (mu - m_bar) ** 2 for _, p, mu in mus)
    sigma = math.sqrt(max(sigma_ref ** 2 - var_between, (SIGMA_FLOOR * sigma_ref) ** 2))
    F = sum(p * _phi((price - mu) / sigma) for _, p, mu in mus)
    tail = max(0.0, min(F, 1.0 - F))
    lik = [(i, p, p * _pdf((price - mu) / sigma)) for i, p, mu in mus]
    Z = sum(w for _, _, w in lik)
    if Z > 0:
        post = [(i, p, w / Z) for i, p, w in lik]
        kl = sum(q * math.log2(q / p) for _, p, q in post if q > 0)
        dominant = max(post, key=lambda x: x[2])[0]
    else:   # every path is many sigmas away: the posterior is undefined numerically; the tree is dead either way
        kl = float("inf")
        dominant = min(mus, key=lambda x: abs(x[2] - price))[0]
    alive = sum(p for _, p, mu in mus if abs(mu - price) <= sigma)
    level = "dead" if tail < dead_tail else ("drift" if tail < drift_tail else "inside")
    expired = False
    try:
        expired = T is not None and t >= float(T)
    except (TypeError, ValueError):
        expired = False
    if expired:       # past the horizon the tree says nothing about today: a session is due whatever the price did
        level = "expired"
    return {"t": round(t, 1), "T_days": T, "price": price, "P0": P0, "move_atr": round((price - P0) / (P0 * atr_pct / 100.0), 2),
            "sigma_pct": round(sigma / P0 * 100.0, 3), "sigma_ref_pct": round(sigma_ref / P0 * 100.0, 3),
            "sigma_source": "close_to_close" if cc_sd_pct and float(cc_sd_pct) > 0 else "atr", "tail": round(tail, 4),
            "kl_bits": (round(kl, 2) if math.isfinite(kl) else None), "alive": round(alive, 3), "dominant": dominant,
            "level": level, "severity": (1.0 if expired else round(1.0 - 2.0 * tail, 3)), "n_nodes": len(nodes)}


def reason_text(d: dict, lens: str | None = None) -> str | None:
    """The due-reason sentence for a dead / drifting tree (None when inside). Prefix is the routing class:
    `tree dead:` is the only hard reason (overrides a defer); `drift:` is soft."""
    if not d or d.get("level") == "inside":
        return None
    who = f"{lens} " if lens else ""
    if d["level"] == "expired":
        return f"tree expired: {who}horizon {d.get('T_days')}d passed (t={d['t']:g}d), {d['move_atr']:+.1f} ATR since the report"
    head = "tree dead" if d["level"] == "dead" else "drift"
    kl = f"{d['kl_bits']:.1f}" if d.get("kl_bits") is not None else "inf"
    return f"{head}: {who}{d['move_atr']:+.1f} ATR in {d['t']:g}d, tail {d['tail']:.2f}, KL {kl} bits"


def report_divergence(report: dict, bars_for, base_day: str | None, today: str | None,
                      dead_tail: float = DEAD_TAIL, drift_tail: float = DRIFT_TAIL) -> dict | None:
    """Judge a whole report. bars_for(symbol) → (bars_after_base_day incl. the base day's partial bar, all_bars) or None.
    Lens-wise reports (by_lens[L].scenario) are judged per lens and the worst lens is returned (with `lens` set and
    `by_lens` holding each); single-tree reports use the report's own symbol. None when the report has no v3.4 tree."""
    by = report.get("by_lens")
    trees = []
    if isinstance(by, dict) and any(isinstance(e, dict) and tree_nodes(e.get("scenario") or {}) for e in by.values()):
        for L in [str(x) for x in (report.get("lenses") or [])] or list(by):
            e = by.get(L)
            if isinstance(e, dict) and tree_nodes(e.get("scenario") or {}):
                trees.append((L, e["scenario"], atr_of(e)))
    elif tree_nodes(report.get("scenario") or {}):
        trees.append((None, report["scenario"], atr_of(report)))
    if not trees:
        return None
    results = {}
    for lens, sc, atr in trees:
        sym = lens or str(report.get("symbol") or "")
        got = bars_for(sym) if bars_for else None
        if not got:
            continue
        after, allbars = got
        if not after:
            continue
        atr_pct = atr if atr else atr_pct_from_bars(allbars or [], base_day)
        cc = cc_sd_from_bars(allbars or [], base_day)
        t = days_since(after, today)
        try:
            price = float(after[-1]["close"])
        except (KeyError, TypeError, ValueError):
            continue
        d = divergence(sc, atr_pct, t, price, dead_tail, drift_tail, cc_sd_pct=cc)
        if d:
            d["lens"] = lens
            results[lens or sym] = d
    if not results:
        return None
    worst = max(results.values(), key=lambda d: d["severity"])
    out = dict(worst)
    if len(trees) > 1 or trees[0][0] is not None:
        out["by_lens"] = {k: {kk: vv for kk, vv in v.items() if kk != "lens"} for k, v in results.items()}
    return out
