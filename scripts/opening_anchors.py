"""Opening anchors: machine facts printed into the debaters' opening material (drill r1-rational-3..5; council
20261109-1500-zealot-structural-edge rounds 3-4). Computed only from files already in the session's data directory
(daily bars, the sentiment file, the odds snapshot), so nothing after as_of can enter. References, not limits.

build_anchor_lines(state, root) -> list[str]; never raises (a failed section prints why it is missing).
decompose(nodes, P0, s_s) -> str: the tally's score decomposition (r1-rational-5), display only.
"""
from __future__ import annotations

import csv
import glob
import math
import statistics as st
import re
from datetime import date, timedelta
from pathlib import Path

H = 15                      # trading days to the valuation day (the odds snapshot's T_days)
CALENDARS = {"US": "XNYS", "EU": "XETR", "JP": "XTKS", "KR": "XKRX"}
RATES_PROXY = "IEF"         # 7-10y Treasury ETF: daily price changes stand in for 10y yield changes (sign inverted)
REF_SD = 1 / math.sqrt(2)   # the scoring baseline (0.25/0.5/0.25 at -1/0/+1 x ATR·√15) has SD 0.707 x ATR·√15
CADENCE, CADENCE_WIN = 91, 7  # next_review_enrich: next report = last release + 91 days, ±7


def _daily(root: Path, sym: str, ts: str) -> list[tuple[str, float, float, float]] | None:
    """(date, high, low, close) of completed sessions before the opening day, oldest first."""
    files = sorted(p for p in glob.glob(str(root / "data" / "market_data" / sym / f"{sym}_*1d_*.csv")))
    if not files:
        return None
    day = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    with open(files[-1], newline="", encoding="utf-8") as fh:
        rows = [(r["date"][:10], float(r["high"]), float(r["low"]), float(r["close"])) for r in csv.DictReader(fh)]
    rows = [r for r in rows if r[0] < day]
    return rows if len(rows) > 70 else None


def _ret(c: list[float], i: int, n: int) -> float:
    return c[i] / c[i - n] - 1


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _valuation_date(ts: str, venue: str, adr: bool) -> date | None:
    try:
        import exchange_calendars as xc
        cal = xc.get_calendar(CALENDARS.get("US" if adr else venue, "XNYS"))
        d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        sess = [x.date() for x in cal.sessions_in_range(d, date(d.year + 1, d.month, 1))]
        start = sess.index(d) if d in sess else next(i for i, x in enumerate(sess) if x > d) - 1
        return sess[start + H]
    except Exception:  # noqa: BLE001
        return None


def _valuation_day(ts: str, venue: str, adr: bool) -> str:
    try:
        import exchange_calendars as xc
        cal = xc.get_calendar(CALENDARS.get("US" if adr else venue, "XNYS"))
        d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        sessions = cal.sessions_in_range(d, date(d.year + 1, d.month, 1))
        sess = [s.date() for s in sessions]
        start = sess.index(d) if d in sess else next(i for i, s in enumerate(sess) if s > d) - 1
        return f"{sess[start + H].isoformat()} ({cal.name}; the opening day counts as day 0)"
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({type(exc).__name__})"


def _width(rows, s_s: float, vix: float | None, is_spy: bool, releases: list | None = None) -> str:
    c = [r[3] for r in rows]
    days = [date.fromisoformat(r[0]) for r in rows]
    idx = [i for i in range(len(c) - 250, len(c)) if i - H >= 0]
    r15 = [c[i] / c[i - H] - 1 for i in idx]     # simple returns, the tree's own unit
    sd = st.pstdev(r15) if len(r15) > 30 else float("nan")
    qs = sorted(r15)
    q = lambda p: qs[int(p * (len(qs) - 1))]  # noqa: E731
    s = (f"15-day width (simple returns): ATR·√15 = {s_s * 100:.2f}%, so the accounting baseline's SD is {REF_SD * s_s * 100:.2f}%; "
         f"realized SD of 15-day returns over the last year = {sd * 100:.2f}% "
         f"(5/25/50/75/95th pct: {_pct(q(.05))} / {_pct(q(.25))} / {_pct(q(.5))} / {_pct(q(.75))} / {_pct(q(.95))})")
    if releases:
        def spans(i):
            a, b = days[i - H], days[i]
            return any(a - timedelta(days=CADENCE_WIN) <= d <= b + timedelta(days=CADENCE_WIN) for d in releases)
        clean = [c[i] / c[i - H] - 1 for i in idx if not spans(i)]
        if len(clean) >= 20:
            s += (f"; windows not spanning an estimated report date: SD {st.pstdev(clean) * 100:.2f}% "
                  f"(n {len(clean)} overlapping, ≈{max(1, len(clean) // H)} independent; report dates estimated by the 91-day cadence, approximate)")
    if is_spy and vix:
        s += (f"; VIX {vix:g} scaled to 15 sessions: {vix / 100 * math.sqrt(H / 252) * 100:.2f}% "
              f"(a proxy from the 30-day index implied vol, not a window-matched implied move)")
    else:
        s += "; window-matched implied move: not in the frozen material (an outcall can fetch one: name the expiry and whether it spans an event)"
    return s + (". Your tree's SD is set against these, in the same unit; wider or narrower both need a reason "
                "that the trailing sample does not already contain.")


def _extension(rows, s_s: float) -> str:
    c = [r[3] for r in rows]
    ma = lambda i, n: sum(c[i - n + 1:i + 1]) / n  # noqa: E731
    i = len(c) - 1
    parts = [f"close vs MA{n} {_pct(c[i] / ma(i, n) - 1)}" for n in (20, 50, 200) if i >= n]
    if i < 200 + H + 60:
        return "Extension: " + "; ".join(parts) + " (history too short for a base rate)."
    dist = {j: c[j] / ma(j, 200) - 1 for j in range(199, i + 1)}
    cur = dist[i]
    hist = sorted(dist.values())
    pctile = sum(v <= cur for v in hist) / len(hist)
    lo, hi = max(0.0, pctile - .10), min(1.0, pctile + .10)
    band = (hist[int(lo * (len(hist) - 1))], hist[int(hi * (len(hist) - 1))])
    fwd_all = [c[j + H] / c[j] - 1 for j in dist if j + H <= i]
    fwd = [c[j + H] / c[j] - 1 for j, v in dist.items() if j + H <= i and band[0] <= v <= band[1]]

    def stats(xs):
        return (f"n {len(xs)} (≈{max(1, len(xs) // H)} independent), mean {_pct(st.mean(xs))}, P(<0) {sum(x < 0 for x in xs) / len(xs):.2f}, "
                f"P(<−s_s) {sum(x < -s_s for x in xs) / len(xs):.2f}, P(>+s_s) {sum(x > s_s for x in xs) / len(xs):.2f}")
    return ("Extension: " + "; ".join(parts) + f". MA200 distance sits at percentile {pctile * 100:.0f} of its own "
            f"{len(hist)}-day history. Forward 15-day returns from days in the same band ({_pct(band[0])} to {_pct(band[1])}): "
            f"{stats(fwd)}; all days: {stats(fwd_all)}. A base rate with overlapping windows, not a forecast; a state argument "
            f"(stretched or washed-out) is sized against the gap between these two lines.")


def _trend_facts(rows, s_s: float) -> str:
    """Breakout state and path efficiency from completed closes (council round 6: facts tonight, base rate after the drill)."""
    c = [r[3] for r in rows]
    if len(c) < 62:
        return "Trend facts: history too short."
    last = c[-1]
    atr = last * s_s / (15 ** 0.5)          # ATR in price, from the same ATR% the ruler uses

    def pos(n):
        hi, lo = max(c[-n - 1:-1]), min(c[-n - 1:-1])   # the prior n closes, the latest one excluded
        return f"prior {n}-day closing high {hi:g} ({(last - hi) / atr:+.2f} ATR) / low {lo:g} ({(last - lo) / atr:+.2f} ATR)"
    new_hi = [n for n in (20, 60) if last > max(c[-n - 1:-1])]
    new_lo = [n for n in (20, 60) if last < min(c[-n - 1:-1])]
    path = sum(abs(c[i] - c[i - 1]) for i in range(len(c) - 20, len(c)))
    er = (last - c[-21]) / path if path else 0.0
    flags = ([f"new {n}-day closing high" for n in new_hi] + [f"new {n}-day closing low" for n in new_lo]) or ["no new 20/60-day closing extreme"]
    return (f"Trend facts (machine, no forecast): last close {last:g}; {pos(10)}; {pos(20)}; {', '.join(flags)}; "
            f"20-day efficiency ratio {er:+.2f} (net move ÷ path length: ±1 = one-way, 0 = all chop). A trend-state reading "
            f"(continuation, breakout, stall, reversal) is argued from these, on either side.")


def _short_state(rows, root: Path) -> str:
    """Short-horizon state (v3.7): where the last 5-day and 1-day moves sit in this symbol's own daily sigma, and what the
    next 5 sessions did after such a state across the universe (config/short_base.yaml, built by build_short_base.py with
    fixed thresholds). The line prints the gap to the all-days rate, so an ordinary state reads as no lean."""
    import sys
    import yaml
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from build_short_base import BUCKETS_1D, BUCKETS_5D, bucket, moves
    table = yaml.safe_load((Path(root) / "config" / "short_base.yaml").read_text(encoding="utf-8"))
    c = [r[3] for r in rows]
    m = moves(c, len(c) - 1)
    if not m:
        return "Short-term state: history too short."
    p5, p1, sd = m
    states, base = table["states"], table["states"]["all"]
    scale = sd * math.sqrt(table["forward_sessions"])

    def cell(key: str) -> str:
        s = states[key]
        return (f"after a {s['label']} the next {table['forward_sessions']} sessions closed higher {s['up'] * 100:.0f}% of the time "
                f"(first / second half of the sample {s['up_first_half'] * 100:.0f}% / {s['up_second_half'] * 100:.0f}%; n {s['n']}), "
                f"mean {_pct(s['mean_sigma'] * scale)} at this symbol's volatility — against all days {base['up'] * 100:.0f}% and "
                f"{_pct(base['mean_sigma'] * scale)}: a gap of {(s['up'] - base['up']) * 100:+.0f} points")
    parts = [cell(bucket(p5, BUCKETS_5D)[0])]
    b1 = bucket(p1, BUCKETS_1D)
    if b1:
        parts.append(cell(b1[0]))
    return (f"Short-term state (machine; base rates pooled over {table['symbols']} symbols' daily bars through {table['cutoff']}, "
            f"thresholds fixed in advance, overlapping samples): 5-day move {p5:+.1f}σ, 1-day move {p1:+.1f}σ (σ = this symbol's daily "
            f"SD over the last year, a k-day move divided by √k). " + (lambda t: t[0].upper() + t[1:])("; ".join(parts)) + ". The gap to the all-days line is "
            "the whole statistical lean of the state, in either direction — where it is a point or two there is none; a short-term "
            "path that leans further names a dated cause. At the 15-day horizon the trend states tested on the same bars (20- and "
            "60-day efficiency, MA200 distance, place in the 20-day range) showed no stable out-of-sample gain over the unconditional "
            "rate: a past rise or fall supports neither continuation nor reversal by itself.")


def _rs(rows, bench_rows, label: str) -> str:
    c = {r[0]: r[3] for r in rows}
    b = {r[0]: r[3] for r in bench_rows}
    d = [x for x in sorted(c) if x in b]
    if len(d) < 61:
        return f"Relative strength vs {label}: insufficient overlapping history."
    cc, bb = [c[x] for x in d], [b[x] for x in d]
    i = len(d) - 1
    cells = [f"{n}d {(_ret(cc, i, n) - _ret(bb, i, n)) * 100:+.2f}pp" for n in (5, 10, 20, 60)]
    return f"Relative strength vs {label} (return difference): " + " · ".join(cells) + "."


def _rates(rows, ief_rows, s_s: float) -> str:
    c = {r[0]: r[3] for r in rows}
    f = {r[0]: r[3] for r in ief_rows}
    d = [x for x in sorted(c) if x in f]
    if len(d) < 260:
        return "Rate sensitivity: insufficient history."
    a = [c[d[k]] / c[d[k - 1]] - 1 for k in range(len(d) - 60, len(d))]
    g = [f[d[k]] / f[d[k - 1]] - 1 for k in range(len(d) - 60, len(d))]
    beta = st.covariance(a, g) / st.variance(g)
    corr = st.correlation(a, g)
    ff = [f[x] for x in d]
    sd15 = st.pstdev([ff[k] / ff[k - H] - 1 for k in range(len(ff) - 250, len(ff))])
    return (f"Rates co-movement: 60-day beta to {RATES_PROXY} daily returns {beta:+.2f} (corr {corr:+.2f}); {RATES_PROXY} 15-day SD over the last year "
            f"{sd15 * 100:.2f}% → a one-SD in-window rates move has gone with about {abs(beta) * sd15 * 100:.2f}% on this symbol's return "
            f"({abs(beta) * sd15 / s_s:.2f} of ATR·√15). A scale for return magnitude (not probability points), from co-movement "
            f"(not a causal effect), for a change in rates (not a discount for the current level); an argument that a rates path moves "
            f"an end price by more says which in-window rates move and why this symbol's response exceeds the co-movement.")


def _releases(root: Path, sym: str, ts: str) -> tuple:
    """Estimated report dates: the latest release in the frozen fundamentals file, stepped back by the 91-day cadence."""
    files = sorted(glob.glob(str(root / "data" / "fundamentals" / sym / f"{sym}_fundamentals_*")))
    for f in reversed(files):
        m = re.search(r'"latest_release"\s*:\s*"(\d{4}-\d{2}-\d{2})"', Path(f).read_text(encoding="utf-8", errors="ignore"))
        if m:
            last = date.fromisoformat(m.group(1))
            return [last - timedelta(days=CADENCE * k) for k in range(0, 6)], Path(f).name
    return [], None


def _earnings_line(releases: list, src: str, vday, ts: str) -> str:
    last = releases[0]
    exp = last + timedelta(days=CADENCE)
    lo, hi = exp - timedelta(days=CADENCE_WIN), exp + timedelta(days=CADENCE_WIN)
    open_d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
    if hi < open_d:
        return f"Earnings: last release {last.isoformat()} ({src}); the cadence estimate {exp.isoformat()} has passed — check whether it was reported."
    rel = ("unknown valuation day" if vday is None else
           "the whole window falls after the valuation day" if lo > vday else
           "the whole window falls before the valuation day" if hi < vday else
           f"the window straddles the valuation day {vday.isoformat()}")
    return (f"Earnings: last release {last.isoformat()} ({src}); next expected about {exp.isoformat()} (window {lo.isoformat()} to {hi.isoformat()}, "
            f"quarterly cadence, an estimate not a confirmed date); {rel}. Whether the reaction lands before the valuation-day close depends "
            f"on the confirmed date and before/after-market timing — an outcall can confirm; until then price the report as a branch with q "
            f"given as a range, not as zero and not as certain.")


def _lens_lines(root: Path, state: dict, sym: str, info: dict, rel_targets: list[str], vix: float | None) -> list[str]:
    ts = state["ts"]
    out = []
    P0, s_s = float(info["reference_price"]), float(info["s_s"])
    out.append(f"[{sym}] P0 {P0:g}. Valuation day: {_valuation_day(ts, str(info.get('venue') or 'US'), bool(info.get('adr')))}.")
    nb = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))  # noqa: E731
    k1, k2 = 1 / REF_SD, 0.1 / REF_SD      # bucket edges ±1 and ±0.1 ATR·√15 in baseline-SD units
    tail, mid = 1 - nb(k1), nb(k2) - nb(-k2)
    side = 0.5 - tail - mid / 2
    out.append(f"[{sym}] Accounting baseline (fixed in advance): no evidence = normal around P0 with SD {REF_SD * s_s * 100:.2f}% "
               f"(0.707·ATR·√15, the scoring baseline's SD; S_ref 50). Its mass by return bucket: below {_pct(-s_s)} {tail:.2f} · "
               f"{_pct(-s_s)} to {_pct(-.1 * s_s)} {side:.2f} · inside ±0.10·ATR·√15 {mid:.2f} · {_pct(.1 * s_s)} to {_pct(s_s)} {side:.2f} · "
               f"above {_pct(s_s)} {tail:.2f}. A ledger starts from these masses; they are a bookkeeping convention, not a forecast and not a template for nodes. "
               f"Consolidation band: {P0 * (1 - .1 * s_s):.2f}–{P0 * (1 + .1 * s_s):.2f} (±0.10·ATR·√15, a ceiling for range nodes).")
    releases, src = ([], None) if state.get("context_type") in ("general", "etf") else _releases(root, sym, ts)
    if releases:
        vday = _valuation_date(ts, str(info.get("venue") or "US"), bool(info.get("adr")))
        out.append(f"[{sym}] " + _earnings_line(releases, src, vday, ts))
    rows = _daily(root, str(info.get("data_symbol") or sym), ts)
    if not rows:
        return out + [f"[{sym}] daily history missing: width, extension, relative strength and rate lines unavailable."]
    for fn in (lambda: _width(rows, s_s, vix, sym == "SPY", releases), lambda: _extension(rows, s_s), lambda: _trend_facts(rows, s_s),
               lambda: _short_state(rows, root)):
        try:
            out.append(f"[{sym}] " + fn())
        except Exception as exc:  # noqa: BLE001
            out.append(f"[{sym}] (section unavailable: {type(exc).__name__})")
    for t in rel_targets:
        br = _daily(root, t, ts)
        out.append(f"[{sym}] " + (_rs(rows, br, t) if br else f"Relative strength vs {t}: no daily file."))
    ief = _daily(root, RATES_PROXY, ts)
    if ief:
        try:
            out.append(f"[{sym}] " + _rates(rows, ief, s_s))
        except Exception as exc:  # noqa: BLE001
            out.append(f"[{sym}] (rate line unavailable: {type(exc).__name__})")
    return out


def build_anchor_lines(state: dict, root: Path) -> list[str]:
    try:
        root = Path(root)
        odds = state.get("odds") or {}
        vix = None
        for p in sorted(glob.glob(str(root / "data" / "fear_greed" / "fear_greed_latest_*.json"))):
            import json
            vix = ((json.loads(Path(p).read_text(encoding="utf-8")).get("vix") or {}).get("value"))
        lines: list[str] = []
        if odds.get("by_lens"):
            lenses = list(odds["by_lens"])
            for sym in lenses:
                rel = [x for x in lenses if x != sym] if sym == "SPY" else ["SPY"]
                lines += _lens_lines(root, state, sym, odds["by_lens"][sym], rel, vix)
        else:
            sym = state["symbol"]
            info = dict(odds.get("unit_basis") or {}, reference_price=odds.get("reference_price"))
            rel = [x for x in (state.get("benchmark"), state.get("sector")) if x and x != sym]
            lines += _lens_lines(root, state, sym, info, rel, vix)
        return lines
    except Exception as exc:  # noqa: BLE001
        return [f"(opening anchors unavailable: {type(exc).__name__}: {exc})"]


def decompose(nodes: list, P0: float, s_s: float) -> str:
    """S_ref/(100−S_ref) = (p₊/p₋) × (μ₊/μ₋) on the nodes' actual returns (council synthesis-4); display only, never changes a score."""
    try:
        rs = [(n.get("id", "?"), float(n["p"]), float(n["P_T"]) / float(P0) - 1) for n in nodes]
        up = [(p, r) for _, p, r in rs if r > 0]       # exact: G/L = direction ratio × magnitude ratio (council round 6)
        dn = [(p, r) for _, p, r in rs if r < 0]
        pu, pd = sum(p for p, _ in up), sum(p for p, _ in dn)
        band = sum(p for _, p, r in rs if abs(r) <= 0.1 * s_s)
        mean = sum(p * r for _, p, r in rs)
        sd = math.sqrt(max(0.0, sum(p * (r - mean) ** 2 for _, p, r in rs)))
        head = f"decomposition: P(up) {pu:.2f} · P(down) {pd:.2f} · band mass {band:.2f} (shown apart; band nodes still count by sign)"
        if pu > 0 and pd > 0:
            mu, md = sum(p * r for p, r in up) / pu, -sum(p * r for p, r in dn) / pd
            head += (f" · odds = direction ratio {pu / pd:.2f} × magnitude ratio {mu / md:.2f} "
                     f"(mean gain {_pct(mu)} / mean loss {_pct(-md)}) · equal-magnitude score {100 * pu / (pu + pd):.1f}")
        else:
            head += " · one side empty: ratios undefined"
        head += f" · tree SD {sd * 100:.2f}% = {sd / (REF_SD * s_s):.2f}× baseline SD"
        nodes_txt = "; ".join(f"{i} {_pct(r)} ({r / s_s:+.2f} ATR·√15) → {'G' if r > 0 else 'L' if r < 0 else '0'} {abs(p * r) * 100:.2f}pp"
                              for i, p, r in rs)
        return head + " · nodes: " + nodes_txt
    except Exception as exc:  # noqa: BLE001
        return f"decomposition unavailable ({type(exc).__name__})"


def _close_path_at(node: dict, P0: float, t: float) -> float | None:
    """Node price at trading day t read from `close` events only (touch / gap_open are not closing positions);
    None when the node states no close at or before t and no earlier close to hold."""
    pts = sorted((float(e["t"]), float(e["price"])) for e in (node.get("events") or [])
                 if isinstance(e, dict) and e.get("kind") == "close" and e.get("t") is not None and e.get("price") is not None)
    pts = [(0.0, float(P0))] + pts
    at = [pr for tt, pr in pts if tt <= t]
    return at[-1] if at else None


def short_scores(nodes: list, P0: float, days=(3, 5)) -> dict:
    """S_ref of the tree read at day t from close events (the forecast's short-term reading, council round 7).
    {t: score or None}; None when fewer than half the mass states a close by t."""
    out = {}
    for t in days:
        g = l = m = 0.0
        for n in nodes:
            pr = _close_path_at(n, P0, float(t))
            if pr is None:
                continue
            r = pr / P0 - 1
            m += float(n["p"]); g += float(n["p"]) * max(r, 0); l += float(n["p"]) * max(-r, 0)
        out[t] = (round(100 * g / (g + l), 1) if (g + l) > 0 and m >= 0.5 else (50.0 if m >= 0.5 else None))
    return out


def contributions(nodes: list, P0: float) -> str:
    """Each node's share of G+L (c_j = p·|r| / (G+L)) — what the tree's score rests on (council round 6/7)."""
    rs = [(n.get("id", "?"), float(n["p"]) * abs(float(n["P_T"]) / float(P0) - 1), float(n["P_T"]) / float(P0) - 1) for n in nodes]
    tot = sum(c for _, c, _ in rs) or 1.0
    rs.sort(key=lambda x: -x[1])
    return " · ".join(f"{i} {c / tot * 100:.0f}% ({'G' if r > 0 else 'L' if r < 0 else '0'})" for i, c, r in rs)


def equal_magnitude(nodes: list, P0: float) -> float:
    """100·p₊/(p₊+p₋): the score the tree would have at equal up/down magnitudes (sign-based, band nodes included)."""
    pu = sum(float(n["p"]) for n in nodes if float(n["P_T"]) / float(P0) - 1 > 0)
    pd = sum(float(n["p"]) for n in nodes if float(n["P_T"]) / float(P0) - 1 < 0)
    return round(100 * pu / (pu + pd), 1) if pu + pd > 0 else 50.0
