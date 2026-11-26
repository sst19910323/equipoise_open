#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Technical snapshot V16 — feature computation and payload assembly (2026-10-25).

Design: docs/design/reorg/technical-snapshot-v16-2026-10-24.md. Invariants:
  * four bar resolutions (1d / 1h / 5m / 1m); three structure layers (daily 2y / hourly 2m / 5-minute 7d) share the
    causal ATR zigzag in analysis/trend_analyzer.py; the 1-minute bars only anchor "now";
  * rolling indicators and confirmed structure use COMPLETED bars up to as_of only; regular-session (RTH) bars only for
    the hourly / 5-minute layers (the 1h file is fetched RTH; 5m is filtered here so the extended-hours 5m stays
    available to the postcheck and to the debaters' bars calls);
  * no judgment in the data layer: numbers and percentiles, no Bullish / High / Consolidating labels;
  * every scalar leaf key carries the <SYM>_ prefix (flatten_with_symbol_prefix); every short list has a sibling
    *_schema key naming the symbol and the columns, so attention cannot drift across symbols;
  * one computation, two projections: the PG payload keeps everything (incl. chart series and all confirmed pivots);
    debater_view() strips chart_* and truncates the three confirmed lists (16/12/10) with earlier_count.

Public entry points: build_payload(...) and debater_view(payload).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from analysis import exchange_clock as ec
from analysis import trend_analyzer as ta
from analysis.symbol_prefix import flatten_with_symbol_prefix

log = logging.getLogger(__name__)

FEATURE_VERSION = "v16"
PRICE_BASIS = "IBKR TRADES bars: split-adjusted, not dividend-adjusted"
DEBATER_KEEP = {"strategic_2y": 16, "tactical_2m": 12, "intraday_7d": 10}
GAP_MIN_ATR = 0.5
EVENT_VOL_MULT = 2.0
EVENT_RET_ATR_MULT = 2.0
RVOL_LOOKBACK_DAYS = 6
RVOL_MIN_VALID_DAYS = 3
TSE_PATCH_DAYS = 7
TSE_PATCH_MIN_COVERAGE = 0.9
TSE_WICK_ATR_MULT = 4.0


# ───────────────────────────── small numeric helpers ───────────────────────────── #

def _f(x, nd: int = 4) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except (TypeError, ValueError):
        return None


def _pct(a, b, nd: int = 2) -> Optional[float]:
    """100 × (a / b − 1)."""
    try:
        if a is None or b is None or float(b) == 0:
            return None
        return _f((float(a) / float(b) - 1.0) * 100.0, nd)
    except (TypeError, ValueError):
        return None


def _pctile(series: pd.Series, value: Optional[float], window: int, min_samples: int = 20) -> Optional[float]:
    """Percentile (0-100, strict less-than) of value among the last ``window`` valid points of series."""
    if value is None:
        return None
    recent = series.dropna().iloc[-window:]
    if len(recent) < min_samples:
        return None
    return _f((recent < value).sum() / len(recent) * 100.0, 1)


def wilder_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    ad = dn.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = au / ad.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(ad != 0, 100.0)
    return rsi


def wilder_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = float(h.iloc[0] - l.iloc[0])
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def log_fit(closes: np.ndarray) -> Tuple[Optional[float], Optional[float]]:
    """Linear fit on log price → (annualized slope %, R²). Needs ≥ 5 points."""
    y = np.log(np.asarray(closes, dtype=float))
    n = len(y)
    if n < 5 or not np.all(np.isfinite(y)):
        return None, None
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    ann = (math.exp(slope * 252.0) - 1.0) * 100.0
    return _f(ann, 2), (None if r2 is None else _f(r2, 3))


# ───────────────────────────── calendar / session plumbing ───────────────────────────── #

class Sessions:
    """Regular-session windows (UTC) per local trading date for one exchange clock, built once per build."""

    def __init__(self, clock: Dict[str, Any], start_date, end_date):
        self.clock = clock
        self.tz = clock["tz"]
        self.cal = ec.get_calendar(clock["calendar"])
        self.sched = self.cal.schedule(start_date=start_date, end_date=end_date)
        self.rows: Dict[Any, Dict[str, Any]] = {}
        for i in range(len(self.sched)):
            day = pd.Timestamp(self.sched.index[i]).date()
            o = ec._as_utc(self.sched.iloc[i]["market_open"])
            c = ec._as_utc(self.sched.iloc[i]["market_close"])
            brk = ec.session_break(self.cal, self.sched, i)
            self.rows[day] = {"open": o, "close": c, "break": brk}

    def trading_days(self) -> List[Any]:
        return sorted(self.rows.keys())

    def local_date(self, ts_utc: pd.Timestamp):
        return ec._as_utc(ts_utc).tz_convert(self.tz).date()

    def row_for(self, ts_utc: pd.Timestamp) -> Optional[Dict[str, Any]]:
        return self.rows.get(self.local_date(ts_utc))

    def in_rth(self, ts_utc: pd.Timestamp) -> bool:
        r = self.row_for(ts_utc)
        if r is None:
            return False
        t = ec._as_utc(ts_utc)
        if not (r["open"] <= t < r["close"]):
            return False
        if r["break"] and r["break"][0] <= t < r["break"][1]:
            return False
        return True

    def segment_end(self, ts_utc: pd.Timestamp) -> Optional[pd.Timestamp]:
        """End of the RTH segment the bar starting at ts belongs to (break start or close)."""
        r = self.row_for(ts_utc)
        if r is None:
            return None
        t = ec._as_utc(ts_utc)
        if r["break"] and t < r["break"][0]:
            return r["break"][0]
        return r["close"]

    def bar_end(self, ts_utc: pd.Timestamp, minutes: int) -> pd.Timestamp:
        t = ec._as_utc(ts_utc)
        nominal = t + pd.Timedelta(minutes=minutes)
        seg = self.segment_end(t)
        if seg is not None and self.in_rth(t):
            return min(nominal, seg)
        return nominal

    def prev_trading_day(self, day):
        days = [d for d in self.trading_days() if d < day]
        return days[-1] if days else None


def drop_bad_prices(df: pd.DataFrame, flags: List[str], label: str) -> pd.DataFrame:
    """Rows with a non-positive open/high/low/close are feed artefacts (the Korean daily files carry a few 0.0 bars on
    holidays, 2026-10-25); they would make the 2y / 52w low 0.0. Drop them and say so."""
    if df is None or len(df) == 0:
        return df
    bad = (df[["open", "high", "low", "close"]].astype(float) <= 0).any(axis=1)
    n = int(bad.sum())
    if n:
        dates = [str(pd.Timestamp(t).date()) for t in df.index[bad][:4]]
        flags.append(f"{label}: dropped {n} bar(s) with non-positive prices ({', '.join(dates)}{'…' if n > 4 else ''})")
        return df[~bad]
    return df


def rth_only(df: pd.DataFrame, s: Sessions) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    mask = [s.in_rth(t) for t in df.index]
    return df[mask]


def completed(df: pd.DataFrame, s: Sessions, minutes: Optional[int], as_of: pd.Timestamp) -> pd.DataFrame:
    """Bars whose end ≤ as_of. minutes=None → daily bars (end = that trading day's regular close; a date the
    calendar does not know counts as completed when it is before as_of's local date)."""
    if df is None or len(df) == 0:
        return df
    as_of = ec._as_utc(as_of)
    keep = []
    as_of_date = s.local_date(as_of)
    for t in df.index:
        if minutes is None:
            r = s.rows.get(pd.Timestamp(t).date())
            if r is None:
                keep.append(pd.Timestamp(t).date() < as_of_date)
            else:
                keep.append(r["close"] <= as_of)
        else:
            keep.append(s.bar_end(t, minutes) <= as_of)
    return df[keep]


def attribution_day(as_of: pd.Timestamp, s: Sessions):
    """T_cur: the trading day the moment belongs to. Extended-hours markets: from pre-market start (04:00 local);
    others: from the regular open. Earlier than that → the previous trading day."""
    t = ec._as_utc(as_of)
    local = t.tz_convert(s.tz)
    day = local.date()
    r = s.rows.get(day)
    if r is not None:
        if s.clock.get("has_extended_hours"):
            start = pd.Timestamp(datetime_combine(day, int(s.clock["premarket_start_hour"]))).tz_localize(s.tz).tz_convert("UTC")
        else:
            start = r["open"]
        if t >= start:
            return day
    return s.prev_trading_day(day)


def datetime_combine(day, hour: int):
    from datetime import datetime
    return datetime(day.year, day.month, day.day, hour)


# ───────────────────────────── TSE daily repair (causal) ───────────────────────────── #

def patch_tse_daily(df_1d: pd.DataFrame, df_5m_rth: pd.DataFrame, s: Sessions, flags: List[str]) -> pd.DataFrame:
    """Tokyo names routed through the generic venue carry off-exchange prints in the daily O/H/L. Last TSE_PATCH_DAYS
    trading days: rebuild open/high/low from the clean RTH 5m bars when ≥ 90% of the expected bars exist (keep the
    official close). Older days: a lone wick beyond TSE_WICK_ATR_MULT × the median ATR(14) of the prior 20 days is
    clamped to the body edge (only information up to that day is used; original values kept in the flag)."""
    d = df_1d.copy()
    patched: List[str] = []
    clamped: List[str] = []
    if df_5m_rth is not None and len(df_5m_rth):
        by_day = df_5m_rth.groupby([pd.Timestamp(t).tz_convert(s.tz).date() for t in df_5m_rth.index])
        recent_days = sorted(by_day.groups.keys())[-TSE_PATCH_DAYS:]
        for day in recent_days:
            r = s.rows.get(day)
            if r is None:
                continue
            rth_minutes = (r["close"] - r["open"]).total_seconds() / 60.0
            if r["break"]:
                rth_minutes -= (r["break"][1] - r["break"][0]).total_seconds() / 60.0
            expected = max(1, int(round(rth_minutes / 5.0)))
            g = by_day.get_group(day)
            if len(g) < TSE_PATCH_MIN_COVERAGE * expected:
                continue
            key = [t for t in d.index if pd.Timestamp(t).date() == day]
            if not key:
                continue
            k = key[0]
            o, h, l = float(g["open"].iloc[0]), float(g["high"].max()), float(g["low"].min())
            if (abs(d.at[k, "high"] - h) > 1e-9) or (abs(d.at[k, "low"] - l) > 1e-9) or (abs(d.at[k, "open"] - o) > 1e-9):
                patched.append(f"{day}(h {d.at[k,'high']}→{h}, l {d.at[k,'low']}→{l})")
            d.at[k, "open"], d.at[k, "high"], d.at[k, "low"] = o, h, l
    # older lone wicks
    atr = wilder_atr(d, 14)
    med20 = atr.rolling(20, min_periods=10).median().shift(1)
    for i in range(len(d)):
        m = med20.iloc[i]
        if m is None or not np.isfinite(m) or m <= 0:
            continue
        o, h, l, c = (float(d["open"].iloc[i]), float(d["high"].iloc[i]), float(d["low"].iloc[i]), float(d["close"].iloc[i]))
        body_hi, body_lo = max(o, c), min(o, c)
        if h - body_hi > TSE_WICK_ATR_MULT * m:
            clamped.append(f"{pd.Timestamp(d.index[i]).date()} h {h}→{body_hi}")
            d.iat[i, d.columns.get_loc("high")] = body_hi
        if body_lo - l > TSE_WICK_ATR_MULT * m:
            clamped.append(f"{pd.Timestamp(d.index[i]).date()} l {l}→{body_lo}")
            d.iat[i, d.columns.get_loc("low")] = body_lo
    if patched:
        flags.append(f"tse_recent_1d_ohl_from_rth_5m: {len(patched)} day(s) rebuilt from clean RTH 5m: " + "; ".join(patched[-3:]))
    if clamped:
        flags.append(f"tse_wick_anomaly: {len(clamped)} lone wick(s) beyond {TSE_WICK_ATR_MULT:g}× median ATR clamped to the body "
                     f"(off-exchange prints; ATR / structure / ranges use the clamped values): " + "; ".join(clamped[-3:]))
    return d


# ───────────────────────────── daily features ───────────────────────────── #

def _episode_ladder(closes: List[float], depths=(2.0, 3.0, 5.0, 8.0), up: bool = False) -> Dict[str, int]:
    """Verbatim port of workflows/trend_context._episode_ladder (close-defined pullback / bounce rounds)."""
    out = {}
    for dpt in depths:
        ref = ext = None
        in_ep = False
        n = 0
        for c in closes:
            if not in_ep:
                ref = c if ref is None else (min(ref, c) if up else max(ref, c))
                hit = c >= ref * (1 + dpt / 100) if up else c <= ref * (1 - dpt / 100)
                if hit:
                    in_ep, ext, n = True, c, n + 1
            else:
                ext = max(ext, c) if up else min(ext, c)
                done = c <= ext * (1 - dpt / 200) if up else c >= ext * (1 + dpt / 200)
                if done:
                    in_ep, ref = False, c
        out[f">={dpt:g}%"] = n
    return out


def _consec(closes: List[float], ref_fn) -> int:
    cnt = 0
    for i in range(len(closes) - 1, -1, -1):
        ref = ref_fn(i)
        if ref is None or closes[i] <= ref:
            break
        cnt += 1
    return cnt


def _ma(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return float(sum(vals[-n:]) / n)


def daily_features(dc: pd.DataFrame, last_price: Optional[float], has_volume: bool, flags: List[str]) -> Dict[str, Any]:
    """dc = completed daily bars (already TSE-repaired). Returns {daily, positioning_part, trend_signal_inputs}."""
    out: Dict[str, Any] = {}
    n = len(dc)
    if n < 2:
        flags.append("daily: fewer than 2 completed bars")
        return {"daily": {}, "trend_signal_inputs": {}, "range_anchors": {}, "ladders": {}}
    C = dc["close"].astype(float)
    H = dc["high"].astype(float)
    L = dc["low"].astype(float)
    V = dc["volume"].astype(float) if ("volume" in dc.columns and has_volume) else None
    c = C.to_numpy()
    cl = c.tolist()
    lp = last_price if last_price is not None else float(c[-1])

    # returns vs last_price
    rets = {}
    for lbl, k in (("5d", 5), ("20d", 20), ("60d", 60), ("126d", 126), ("252d", 252), ("504d", 504)):
        rets[lbl] = _pct(lp, c[-k]) if n >= k + 0 and k <= n else None
    # moving averages
    ma = {}
    for k in (20, 50, 200):
        v = _ma(cl, k)
        ma[f"ma{k}"] = _f(v)
        ma[f"dist_ma{k}_pct"] = _pct(lp, v) if v else None
    ma50_series = C.rolling(50).mean()
    ma["ma50_slope_20d_pct"] = _pct(ma50_series.iloc[-1], ma50_series.iloc[-21]) if n >= 71 else None
    # trend fit
    s20, r20 = log_fit(c[-20:]) if n >= 20 else (None, None)
    s252, r252 = log_fit(c[-252:]) if n >= 60 else (None, None)
    # RSI / ATR / BB
    rsi = wilder_rsi(C, 14)
    atr = wilder_atr(dc, 14)
    atr_pct = atr / C * 100.0
    bb_mid = C.rolling(20).mean()
    bb_sd = C.rolling(20).std(ddof=0)
    bb_width = (4.0 * bb_sd) / bb_mid * 100.0
    atr_now = _f(atr.iloc[-1])
    atr_pct_now = _f(atr_pct.iloc[-1], 3)
    # volume structure
    vol_ratio_last = up_down = None
    if V is not None and n >= 22:
        prev20 = V.iloc[-21:-1].mean()
        vol_ratio_last = _f(V.iloc[-1] / prev20, 2) if prev20 and prev20 > 0 else None
        dc20 = C.diff().iloc[-20:]
        v20 = V.iloc[-20:]
        up_sum = float(v20[dc20 > 0].sum())
        dn_sum = float(v20[dc20 < 0].sum())
        if dn_sum > 0:
            up_down = _f(up_sum / dn_sum, 2)
        else:
            flags.append("daily.up_down_vol_ratio_20d: no down-day volume in the last 20 days")
    elif V is None:
        flags.append("daily volume fields: has_volume=false")
    # unfilled gaps (last 60 completed days)
    gaps: List[list] = []
    if n >= 3:
        start = max(1, n - 60)
        for t in range(start, n):
            ph, pl, pc = float(H.iloc[t - 1]), float(L.iloc[t - 1]), float(C.iloc[t - 1])
            ap = atr_pct.iloc[t - 1]
            if ap is None or not np.isfinite(ap) or pc <= 0:
                continue
            lo_t, hi_t = float(L.iloc[t]), float(H.iloc[t])
            if lo_t > ph:
                size = (lo_t - ph) / pc * 100.0
                if size < GAP_MIN_ATR * ap:
                    continue
                later_low = float(L.iloc[t + 1:].min()) if t + 1 < n else float("inf")
                top = min(lo_t, later_low, lp)
                if top > ph:
                    gaps.append([str(dc.index[t].date()), "up", _f(ph), _f(top), _f((top - ph) / pc * 100.0, 2)])
            elif hi_t < pl:
                size = (pl - hi_t) / pc * 100.0
                if size < GAP_MIN_ATR * ap:
                    continue
                later_high = float(H.iloc[t + 1:].max()) if t + 1 < n else float("-inf")
                bottom = max(hi_t, later_high, lp)
                if bottom < pl:
                    gaps.append([str(dc.index[t].date()), "down", _f(bottom), _f(pl), _f((pl - bottom) / pc * 100.0, 2)])
    gaps = gaps[-3:]
    # high-volume / large-move event bars (last 60)
    events: List[list] = []
    if n >= 22:
        for t in range(max(21, n - 60), n):
            ret = _pct(C.iloc[t], C.iloc[t - 1])
            ap = atr_pct.iloc[t - 1]
            vr = None
            if V is not None:
                base = V.iloc[t - 20:t].mean()
                vr = _f(V.iloc[t] / base, 2) if base and base > 0 else None
            big_vol = vr is not None and vr >= EVENT_VOL_MULT
            big_ret = ret is not None and ap is not None and np.isfinite(ap) and abs(ret) >= EVENT_RET_ATR_MULT * ap
            if big_vol or big_ret:
                events.append([str(dc.index[t].date()), ret, vr, _f(H.iloc[t]), _f(L.iloc[t]), _f(C.iloc[t])])
    events = events[-3:]

    daily = {
        "returns_pct": rets,
        "ma": ma,
        "trend_fit": {"fit_20d_ann_pct": s20, "fit_20d_r2": r20, "fit_252d_ann_pct": s252, "fit_252d_r2": r252},
        "rsi_14": _f(rsi.iloc[-1], 2),
        "atr_14": atr_now,
        "atr_pct_14": atr_pct_now,
        "atr_pctile_252d": _pctile(atr_pct, atr_pct_now, 252),
        "bb_width_pct": _f(bb_width.iloc[-1], 3),
        "bb_width_pctile_252d": _pctile(bb_width, _f(bb_width.iloc[-1], 3), 252),
        "vol_ratio_last_full_day_vs_20d": vol_ratio_last,
        "up_down_vol_ratio_20d": up_down,
        "unfilled_gaps_60d_schema": "cols: date,dir,gap_low,gap_high,size_pct (full gaps ≥0.5 ATR, not yet filled by later bars or the current price)",
        "unfilled_gaps_60d": gaps,
        "high_vol_event_bars_60d_schema": "cols: date,ret_pct,vol_ratio_vs_prior20d,high,low,close (vol ≥2× prior-20d mean or |ret| ≥2× ATR%)",
        "high_vol_event_bars_60d": events,
    }

    # trend signal inputs (verbatim trend_context semantics, completed closes only)
    tsi: Dict[str, Any] = {}
    if n >= 25:
        W = cl[-20:]
        last = cl[-1]
        ret20 = (last / W[0] - 1) * 100
        peak = trough = W[0]
        max_dd = max_rally = 0.0
        for v in W:
            peak = max(peak, v)
            trough = min(trough, v)
            max_dd = min(max_dd, (v / peak - 1) * 100)
            max_rally = max(max_rally, (v / trough - 1) * 100)
        adverse = abs(max_dd) if ret20 >= 0 else max_rally
        hold_above = _consec(cl, lambda i: max(cl[max(0, i - 20):i]) if i >= 5 else None)
        hold_below = 0
        if hold_above == 0:
            cnt = 0
            for i in range(len(cl) - 1, 4, -1):
                lo = min(cl[max(0, i - 20):i])
                if cl[i] >= lo:
                    break
                cnt += 1
            hold_below = cnt
        hi60, lo60 = max(cl[-60:]), min(cl[-60:])
        tsi = {
            "ret_20d_close_pct": _f(ret20, 2),
            "ret_first10d_pct": _f((W[9] / W[0] - 1) * 100, 2),
            "ret_last10d_pct": _f((last / W[9] - 1) * 100, 2),
            "adverse_20d_pct": _f(adverse, 2),
            "fit_20d_r2": r20,
            "consec_closes_above_ma20": _consec(cl, lambda i: _ma(cl[:i + 1], 20)),
            "hold_days_above_prior20d_high": hold_above,
            "hold_days_below_prior20d_low": hold_below,
            "dist_60d_close_high_pct": _f((last / hi60 - 1) * 100, 2),
            "dist_60d_close_low_pct": _f((last / lo60 - 1) * 100, 2),
            "lower_highs_5v5": bool(max(cl[-5:]) < max(cl[-10:-5])),
            "higher_lows_5v5": bool(min(cl[-5:]) > min(cl[-10:-5])),
            "ladder_bars": int(min(n, 90)),
        }
    else:
        flags.append("trend_signal_inputs: fewer than 25 completed daily bars")

    # range anchors + drawdown + ladders
    anchors: Dict[str, Any] = {}
    for lbl, k in (("2y", 504), ("52w", 252), ("60d", 60), ("30d", 30)):
        w = dc.iloc[-k:]
        if len(w) < min(k, 5):
            continue
        hi, lo = float(w["high"].max()), float(w["low"].min())
        hi_d, lo_d = str(w["high"].idxmax().date()), str(w["low"].idxmin().date())
        a = {"high": _f(hi), "high_date": hi_d, "low": _f(lo), "low_date": lo_d,
             "pctile": _f((lp - lo) / (hi - lo) * 100.0, 1) if hi > lo else None,
             "dist_to_high_pct": _pct(lp, hi), "dist_to_low_pct": _pct(lp, lo)}
        if len(w) < k:
            a["bars"] = int(len(w))          # only said when the window is shorter than nominal (young listing)
        if lbl == "30d":
            up_room, dn_room = hi - lp, lp - lo
            a["upside_over_downside"] = _f(up_room / dn_room, 2) if dn_room > 0 else None
        anchors[lbl] = a
    dd = None
    if n >= 5:
        w = C.iloc[-30:]
        dd = _f(((w - w.cummax()) / w.cummax()).min() * 100.0, 2)
    ladders = {"max_dd_30d_pct": dd,
               "pullback_ladder_90d": _episode_ladder(cl[-90:], up=False),
               "bounce_ladder_90d": _episode_ladder(cl[-90:], up=True)}
    return {"daily": daily, "trend_signal_inputs": tsi, "range_anchors": anchors, "ladders": ladders}


def benchmark_rel(dc: pd.DataFrame, bench_dc: Optional[pd.DataFrame], bench_label: Optional[str], flags: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"benchmark_symbol": bench_label, "rs_20d_pct": None, "rs_60d_pct": None, "beta_60d": None}
    if bench_dc is None or dc is None or len(dc) == 0:
        return out
    a = dc["close"].astype(float)
    b = bench_dc["close"].astype(float)
    a.index = [pd.Timestamp(t).date() for t in a.index]
    b.index = [pd.Timestamp(t).date() for t in b.index]
    common = sorted(set(a.index) & set(b.index))
    if len(common) < 25:
        flags.append("benchmark_rel: fewer than 25 common completed days")
        return out
    ca, cb = a.loc[common], b.loc[common]
    for lbl, w in (("rs_20d_pct", 20), ("rs_60d_pct", 60)):
        if len(common) > w:
            out[lbl] = _f(((ca.iloc[-1] / ca.iloc[-1 - w]) / (cb.iloc[-1] / cb.iloc[-1 - w]) - 1.0) * 100.0, 2)
    ra, rb = ca.pct_change().dropna(), cb.pct_change().dropna()
    if len(ra) > 60:
        ra, rb = ra.iloc[-60:], rb.iloc[-60:]
        var = float(rb.var())
        if var > 0:
            out["beta_60d"] = _f(float(np.cov(ra.to_numpy(), rb.to_numpy())[0, 1]) / var, 3)
    return out


# ───────────────────────────── hourly features ───────────────────────────── #

def hourly_features(hc: pd.DataFrame, last_price: Optional[float], flags: List[str]) -> Dict[str, Any]:
    if hc is None or len(hc) < 21:
        flags.append("hourly: fewer than 21 completed RTH hourly bars")
        return {"ma_20_rth": None, "dist_ma20_pct": None, "ma20_slope_10bar_pct": None, "atr_14_1h": None, "atr_pct_14_1h": None}
    C = hc["close"].astype(float)
    ma20 = C.rolling(20).mean()
    atr = wilder_atr(hc, 14)
    lp = last_price if last_price is not None else float(C.iloc[-1])
    return {
        "ma_20_rth": _f(ma20.iloc[-1]),
        "dist_ma20_pct": _pct(lp, ma20.iloc[-1]),
        "ma20_slope_10bar_pct": _pct(ma20.iloc[-1], ma20.iloc[-11], 3) if len(hc) >= 31 else None,
        "atr_14_1h": _f(atr.iloc[-1]),
        "atr_pct_14_1h": _f(atr.iloc[-1] / C.iloc[-1] * 100.0, 3),
    }


# ───────────────────────────── current snapshot (1m / 5m) ───────────────────────────── #

def current_snapshot(df_1m: pd.DataFrame, df_5m_rth_c: pd.DataFrame, dc: pd.DataFrame, s: Sessions, as_of: pd.Timestamp,
                     session: str, t_cur, has_volume: bool, flags: List[str]) -> Dict[str, Any]:
    last_bar = df_1m.iloc[-1]
    lp = _f(last_bar["close"])
    out: Dict[str, Any] = {"last_price": lp}
    row = s.rows.get(t_cur) if t_cur else None
    prev_day = s.prev_trading_day(t_cur) if t_cur else None
    # prior RTH close = close of the completed daily bar dated the previous trading day (fallback: last completed < t_cur)
    prior_close = None
    if dc is not None and len(dc):
        dates = [pd.Timestamp(t).date() for t in dc.index]
        cand = [i for i, d in enumerate(dates) if (prev_day is not None and d == prev_day)]
        if not cand and t_cur is not None:
            cand = [i for i, d in enumerate(dates) if d < t_cur]
        if cand:
            prior_close = _f(dc["close"].iloc[cand[-1]])
    out["prior_rth_close"] = prior_close
    out["change_vs_prior_close_pct"] = _pct(lp, prior_close)
    # today's RTH 1m bars
    today_rth = df_1m.iloc[0:0]
    if row is not None:
        m1 = df_1m[(df_1m.index >= row["open"]) & (df_1m.index < row["close"])]
        if row["break"]:
            m1 = m1[~((m1.index >= row["break"][0]) & (m1.index < row["break"][1]))]
        today_rth = m1
    rth_open = _f(today_rth["open"].iloc[0]) if len(today_rth) else None
    out["open_gap_pct"] = _pct(rth_open, prior_close) if rth_open else None
    out["change_since_rth_open_pct"] = _pct(lp, rth_open) if (rth_open and session in ("regular", "after-hours")) else None
    # extended session
    ext_change = ext_share = None
    if s.clock.get("has_extended_hours") and row is not None:
        if session == "pre-market":
            ext_change = _pct(lp, prior_close)
            seg = df_1m[(df_1m.index < row["open"]) & (df_1m.index >= row["open"] - pd.Timedelta(hours=5, minutes=30))]
        elif session == "after-hours":
            today_close = _f(today_rth["close"].iloc[-1]) if len(today_rth) else None
            ext_change = _pct(lp, today_close) if today_close else None
            seg = df_1m[(df_1m.index >= row["close"])]
        else:
            seg = None
        if seg is not None and has_volume and dc is not None and len(dc) >= 21 and "volume" in dc.columns:
            base = float(dc["volume"].iloc[-20:].mean())
            if base > 0:
                ext_share = _f(float(seg["volume"].sum()) / base, 3)
    out["ext_session_change_pct"] = ext_change
    out["ext_session_volume_share"] = ext_share
    if not s.clock.get("has_extended_hours"):
        flags.append("ext_session_*: exchange has no extended hours")
    # VWAP (today's completed RTH 1m)
    vwap = None
    if has_volume and len(today_rth) and "volume" in today_rth.columns:
        tr = today_rth[[s.bar_end(t, 1) <= ec._as_utc(as_of) for t in today_rth.index]]
        vol_sum = float(tr["volume"].sum()) if len(tr) else 0.0
        if vol_sum > 0:
            wcol = "wap" if "wap" in tr.columns else "close"
            vwap = _f(float((tr[wcol].astype(float) * tr["volume"].astype(float)).sum()) / vol_sum)
    if session == "pre-market" or session == "closed":
        vwap = None
    out["rth_vwap"] = vwap
    out["vwap_dist_pct"] = _pct(lp, vwap) if vwap else None
    if vwap is None:
        flags.append("rth_vwap: null (pre-market / closed / no volume / no RTH bars yet)")
    # rvol_tod
    rvol = None
    if session == "regular" and has_volume and df_5m_rth_c is not None and len(df_5m_rth_c) and row is not None and "volume" in df_5m_rth_c.columns:
        today = df_5m_rth_c[[s.local_date(t) == t_cur for t in df_5m_rth_c.index]]
        if len(today):
            cutoff_local = s.bar_end(today.index[-1], 5).tz_convert(s.tz).time()
            num = float(today["volume"].sum())
            n_today = len(today)
            vals = []
            days = sorted({s.local_date(t) for t in df_5m_rth_c.index if s.local_date(t) != t_cur})[-RVOL_LOOKBACK_DAYS:]
            for d in days:
                g = df_5m_rth_c[[s.local_date(t) == d for t in df_5m_rth_c.index]]
                g = g[[s.bar_end(t, 5).tz_convert(s.tz).time() <= cutoff_local for t in g.index]]
                if len(g) == n_today:                     # same cutoff, complete coverage
                    vals.append(float(g["volume"].sum()))
            if len(vals) >= RVOL_MIN_VALID_DAYS and sum(vals) > 0:
                rvol = _f(num / (sum(vals) / len(vals)), 2)
            else:
                flags.append(f"rvol_tod: only {len(vals)} comparable prior days (<{RVOL_MIN_VALID_DAYS})")
    else:
        flags.append("rvol_tod: null (not regular session, no volume, or no completed RTH 5m today)")
    out["rvol_tod"] = rvol
    return out


# ───────────────────────────── payload assembly ───────────────────────────── #

def _chart_series(df: pd.DataFrame, n: int) -> List[list]:
    if df is None or len(df) == 0:
        return []
    w = df.iloc[-n:]
    return [[pd.Timestamp(t).isoformat(), _f(c)] for t, c in zip(w.index, w["close"].astype(float))]


def leg_z(layers: Dict[str, Any], daily_index, last_price, atr_pct) -> Dict[str, Any]:
    """v3.4 (2026-10-29): how stretched the current leg is, in units of the symbol's own range.
    Start = the last CONFIRMED extreme of the tactical layer (L2, `tactical_2m`), counted from the day the extreme occurred
    (only points confirmed before the opening are in `confirmed`); n = completed daily bars from that day to the last one
    (the day itself = 0); ATR = ATR%14 of the last completed daily bar. leg_z = (P0 / P_pivot − 1) / (ATR% · √n).
    It describes extension only — not a direction probability. null (with a reason) when n = 0, the ATR is missing or 0, or
    the layer has no confirmed point."""
    out = {"leg_z": None, "leg_z_schema": "(P0/P_pivot − 1) ÷ (ATR%14 · √n); pivot = last confirmed L2 extreme (its own day), n = completed daily bars since; extension only, not a direction"}
    try:
        conf = ((layers or {}).get("tactical_2m") or {}).get("confirmed") or []
        if not conf:
            out["leg_z_null_reason"] = "no confirmed tactical extreme"; return out
        piv = conf[-1]
        t, price, kind = str(piv[0]), float(piv[1]), str(piv[2])
        day = pd.Timestamp(t[:10])
        idx = pd.DatetimeIndex([pd.Timestamp(str(d)[:10]) for d in daily_index])
        n = int((idx > day).sum())
        if n <= 0:
            out["leg_z_null_reason"] = "pivot is the last completed day (n = 0)"; return out
        if not isinstance(atr_pct, (int, float)) or atr_pct <= 0 or not isinstance(last_price, (int, float)) or last_price <= 0:
            out["leg_z_null_reason"] = "ATR%14 or last price missing"; return out
        z = (float(last_price) / price - 1.0) / (float(atr_pct) / 100.0 * (n ** 0.5))
        out.update({"leg_z": round(z, 2), "leg_pivot_t": t, "leg_pivot_price": price, "leg_pivot_type": kind, "leg_bars": n})
        return out
    except Exception as exc:   # never break the snapshot over the diagnostic
        out["leg_z_null_reason"] = f"error: {type(exc).__name__}"
        return out


def build_payload(symbol: str, frames: Dict[str, pd.DataFrame], clock: Dict[str, Any], market_data_timestamp: str,
                  benchmark_symbol: Optional[str] = None, bench_frames: Optional[pd.DataFrame] = None,
                  bench_fresh: bool = True, instrument: Optional[Dict[str, Any]] = None,
                  feed_meta: Optional[Dict[str, Any]] = None, generated_at: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    """frames: {"1m","5m","1h","1d"} raw DataFrames (UTC index = bar start). Returns the FLAT prefixed payload."""
    symbol = symbol.upper()
    flags: List[str] = []
    df_1m = drop_bad_prices(frames["1m"], flags, "1m")
    df_5m = drop_bad_prices(frames["5m"], flags, "5m")
    df_1h = drop_bad_prices(frames["1h"], flags, "1h")
    df_1d = drop_bad_prices(frames["1d"], flags, "daily")
    if bench_frames is not None and len(bench_frames):
        bench_frames = drop_bad_prices(bench_frames, [], "benchmark")
    latest_bar = ec._as_utc(df_1m.index[-1])
    as_of = latest_bar + pd.Timedelta(minutes=1)              # end of the last 1m bar
    first = min(ec._as_utc(df_1d.index[0]), ec._as_utc(df_1h.index[0])).tz_convert(clock["tz"]).date()
    s = Sessions(clock, first - pd.Timedelta(days=7), latest_bar.tz_convert(clock["tz"]).date() + pd.Timedelta(days=7))
    sess = ec.classify_session(latest_bar, clock=clock)
    session = sess["current_session"]
    t_cur = attribution_day(as_of, s)
    contract = ec._contract(symbol)
    sec_type = str(contract.get("sec_type") or "").upper()
    has_volume = sec_type != "IND"
    if has_volume and "volume" in df_1d.columns and len(df_1d) >= 20 and float(df_1d["volume"].iloc[-20:].fillna(0).sum()) == 0:
        has_volume = False
    if not has_volume:
        flags.append("has_volume=false (index contract or zero volume in the last 20 daily bars): volume-derived fields null")

    # RTH / completed frames
    h_rth = rth_only(df_1h, s)
    m5_rth = rth_only(df_5m, s)
    hc = completed(h_rth, s, 60, as_of)
    m5c = completed(m5_rth, s, 5, as_of)
    d1 = df_1d
    if str(contract.get("primary_exchange") or "").upper() == "TSEJ":
        d1 = patch_tse_daily(df_1d, m5_rth, s, flags)
    dc = completed(d1, s, None, as_of)
    if len(hc) < len(h_rth):
        flags.append(f"hourly: {len(h_rth) - len(hc)} unfinished RTH bar(s) excluded")
    if len(dc) < len(d1):
        flags.append("daily: today's unfinished bar excluded")

    cs = current_snapshot(df_1m, m5c, dc, s, as_of, session, t_cur, has_volume, flags)
    lp = cs["last_price"]
    dfeat = daily_features(dc, lp, has_volume, flags)
    hfeat = hourly_features(hc, lp, flags)
    bench_label = None
    bench_dc = None
    if benchmark_symbol:
        if bench_frames is None:
            bench_label = f"{benchmark_symbol} (no cached daily data, skipped)"
        elif not bench_fresh:
            bench_label = f"{benchmark_symbol} (stale cache, skipped)"
        else:
            bench_label = benchmark_symbol
            bench_dc = completed(bench_frames, s, None, as_of) if len(bench_frames) else None
    brel = benchmark_rel(dc, bench_dc, bench_label, flags)

    # structure layers
    d504 = dc.iloc[-504:]
    layers: Dict[str, Any] = {}
    z1 = ta.atr_zigzag(d504, ta.l1_stages(len(d504)), layer="strategic_2y", symbol=symbol) if len(d504) >= 2 else None
    z2 = ta.atr_zigzag(hc, [ta.Stage(0, *ta.L2_PARAMS)], layer="tactical_2m", symbol=symbol,
                       bar_ends=[s.bar_end(t, 60) for t in hc.index]) if len(hc) >= 2 else None
    z3 = ta.atr_zigzag(m5c, [ta.Stage(0, *ta.L3_PARAMS)], layer="intraday_7d", symbol=symbol,
                       bar_ends=[s.bar_end(t, 5) for t in m5c.index]) if len(m5c) >= 2 else None
    for name, z in (("strategic_2y", z1), ("tactical_2m", z2), ("intraday_7d", z3)):
        if z is None:
            flags.append(f"price_structure.{name}: not enough completed bars")
            layers[name] = None
        else:
            layers[name] = z
    digest = ta.price_digest(dc, has_volume=has_volume)
    if digest:
        digest["schema"] = f"[{symbol}] " + digest["schema"]
    for name in ("daily", "trend_signal_inputs"):
        pass

    nested: Dict[str, Any] = {
        "symbol": symbol,
        "market_data_timestamp_utc": market_data_timestamp,
        "latest_data_timestamp": latest_bar.isoformat(),
        "feature_version": FEATURE_VERSION,
        "price_basis": PRICE_BASIS,
        "session_context": {
            "current_session": session,
            "elapsed_minutes": sess.get("elapsed_minutes"),
            "exchange": clock["exchange"],
            "exchange_tz": clock["tz"],
            "currency": str(contract.get("currency") or ("USD" if clock["exchange"] in ("NYSE", "NASDAQ", "ARCA", "SMART") else "")),
            "instrument_type": (instrument or {}).get("instrument_type"),
            "latest_bar_local": sess.get("latest_bar_local"),
            "as_of_utc": as_of.isoformat(),
            "attribution_day": str(t_cur) if t_cur else None,
            "prior_regular_close_local": ec.get_prior_regular_close(latest_bar, clock=clock),
            "feed_mode": (feed_meta or {}).get("feed_mode"),
            "feed_delay_minutes": (feed_meta or {}).get("feed_delay_minutes"),
            "bar_age_minutes": None if generated_at is None else int((ec._as_utc(generated_at) - latest_bar).total_seconds() // 60),
            "has_extended_hours": bool(clock.get("has_extended_hours")),
            "has_volume": has_volume,
            "generated_at_utc": None if generated_at is None else ec._as_utc(generated_at).isoformat(),
        },
        "current_snapshot": cs,
        "daily": dfeat["daily"],
        "trend_signal_inputs": dfeat["trend_signal_inputs"],
        "hourly": hfeat,
        "positioning": {
            "range_anchors": dfeat["range_anchors"],
            **dfeat["ladders"],
            **leg_z(layers, dc.index, lp, (dfeat.get("daily") or {}).get("atr_pct_14")),
            "benchmark_rel": brel,
        },
        "price_structure": layers,
        "price_digest": digest,
        "chart": {
            "daily_close_schema": "cols: t,close (completed daily, last 504)",
            "daily_close": _chart_series(dc, 504),
            "hourly_close_rth": _chart_series(hc, 400),
            "m5_close_rth": _chart_series(m5c, 600),
        },
    }
    nested["session_context"]["quality_flags"] = flags
    for k in ("unfilled_gaps_60d", "high_vol_event_bars_60d"):
        if nested["daily"].get(k + "_schema"):
            nested["daily"][k + "_schema"] = f"[{symbol}] " + nested["daily"][k + "_schema"]
    return flatten_with_symbol_prefix(symbol, nested)


def debater_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Projection for the debaters: drop chart series, truncate each structure layer's confirmed list to the most
    recent N (16/12/10) and add *_earlier_count. Everything else unchanged. Works on the flat prefixed payload."""
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if "_chart_" in k:
            continue
        out[k] = v
    for layer, keep in DEBATER_KEEP.items():
        ck = next((k for k in out if k.endswith(f"_price_structure_{layer}_confirmed")), None)
        if ck is None or not isinstance(out[ck], list):
            continue
        full = out[ck]
        out[ck] = full[-keep:]
        out[ck.replace("_confirmed", "_earlier_count")] = max(0, len(full) - keep)
    return out
