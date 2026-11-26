#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Price structure for the technical snapshot (V16, 2026-10-24).

One causal algorithm shared by the three structure layers (daily / hourly / 5-minute):
``atr_zigzag`` — ATR-adaptive reversal confirmation with strictly alternating highs and lows,
stage-switchable thresholds (L1 uses a coarse far stage and a fine near stage), an explicit
provisional (unconfirmed) right end and an anchor at the last completed bar.

Plus ``price_digest`` — the "telescope" OHLCV digest of the last two years (quarterly → monthly →
weekly → daily), parameter-free, so the snapshot is self-contained: blurrier the further back,
sharper the closer to now.

Design: docs/design/reorg/technical-snapshot-v16-2026-10-24.md §2.
Causality contract (tested): with fixed parameters, appending future bars never changes an
already confirmed pivot (price, time, confirmed_at, threshold); only ``provisional`` / ``anchor``
move. The centered-window swing finder and the RDP polyline of the old versions are gone; the
old ``calculate_swing_points`` name stays as a thin wrapper for the postcheck caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


# ───────────────────────────── ATR helpers (causal) ───────────────────────────── #

def wilder_atr_pct(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Wilder ATR divided by close, in percent, per bar; NaN until ``length`` true ranges exist.
    Causal: value at t uses bars ≤ t only."""
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0])
    atr = tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    return (atr / close * 100.0).astype(float)


def causal_median(values: pd.Series, window: int = 60, min_valid: int = 14) -> pd.Series:
    """Rolling median over the last ``window`` valid values up to and including t; NaN until
    ``min_valid`` valid values exist. Never looks forward."""
    out = values.rolling(window=window, min_periods=min_valid).median()
    return out


# ───────────────────────────── the zigzag state machine ───────────────────────────── #

@dataclass
class Stage:
    start_idx: int           # bar index (0-based, in the df handed in) where this stage's thresholds take over
    k: float
    floor_pct: float
    cap_pct: float

    def threshold(self, atr_pct_median: float) -> Optional[float]:
        if atr_pct_median is None or not np.isfinite(atr_pct_median):
            return None
        return float(min(max(self.k * atr_pct_median, self.floor_pct), self.cap_pct))


def _bar_end(index: pd.DatetimeIndex, i: int, bar_ends: Optional[Sequence] = None):
    """End time of bar i: caller-supplied (session-aware) ends if given, else the bar start itself."""
    if bar_ends is not None:
        return bar_ends[i]
    return index[i]


def _ts(x) -> Optional[str]:
    """Compact UTC stamp: 'YYYY-MM-DD' for midnight (daily bars), else 'YYYY-MM-DDTHH:MMZ' (keeps the thin view small)."""
    if x is None:
        return None
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo is not None else x
        if t.hour == 0 and t.minute == 0 and t.second == 0:
            return t.strftime("%Y-%m-%d")
        return t.strftime("%Y-%m-%dT%H:%MZ")
    return str(x)


def atr_zigzag(df: pd.DataFrame, stages: Sequence[Stage] | Sequence[Tuple[int, float, float, float]],
               atr_len: int = 14, atr_median_len: int = 60, bar_ends: Optional[Sequence] = None,
               layer: str = "", symbol: str = "") -> Optional[Dict[str, Any]]:
    """Causal, strictly alternating, ATR-adaptive reversal pivots.

    df: completed bars only (open/high/low/close), DatetimeIndex = bar start, RTH-filtered by the caller.
    stages: ordered list of (start_idx, k, floor_pct, cap_pct); the first should start at 0.
    bar_ends: optional per-bar end timestamps (session aware) used for confirmed_at / anchor_t.

    Rules (design §2.1):
      - threshold(t) = clip(k × median(ATR%_14 over bars ≤ t, last ``atr_median_len`` valid), floor, cap);
        the threshold recorded on a candidate is the one in force when the candidate was set/updated.
      - a candidate high (low) is confirmed when a completed bar CLOSES ≥ threshold below (above) it;
        confirmed_at = that bar's end; the new candidate of the opposite kind is that bar's low (high).
      - candidates update only on strictly more extreme prices (ties keep the earlier time).
      - the first bar seeds both a high and a low candidate; whichever is confirmed first wins,
        the other is dropped (same-bar tie → the side consistent with the bar's open→close direction).
      - no confirmation is allowed before the ATR median has ``min_valid`` values (candidates still track).
    """
    if df is None or len(df) == 0:
        return None
    n = len(df)
    st = [s if isinstance(s, Stage) else Stage(*s) for s in stages]
    st.sort(key=lambda s: s.start_idx)
    if not st or st[0].start_idx != 0:
        raise ValueError("first stage must start at index 0")

    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    opens = df["open"].astype(float).to_numpy() if "open" in df.columns else closes
    atr_pct = wilder_atr_pct(df, atr_len)
    med = causal_median(atr_pct, atr_median_len, min_valid=atr_len).to_numpy()
    idx = df.index

    def stage_at(i: int) -> Stage:
        cur = st[0]
        for s in st:
            if s.start_idx <= i:
                cur = s
        return cur

    def thr_at(i: int) -> Optional[float]:
        return stage_at(i).threshold(med[i])

    confirmed: List[list] = []
    # candidate state: direction None at seed; 'up' means we are tracking a candidate HIGH (leg rising)
    direction: Optional[str] = None
    cand_hi = {"i": 0, "price": highs[0], "thr": thr_at(0)}
    cand_lo = {"i": 0, "price": lows[0], "thr": thr_at(0)}

    def confirm(cand: dict, kind: str, i: int, prev_price: Optional[float]):
        leg = None if prev_price in (None, 0) else round((float(cand["price"]) / float(prev_price) - 1.0) * 100.0, 2)
        confirmed.append([_ts(idx[cand["i"]]), round(float(cand["price"]), 4), kind,
                          _ts(_bar_end(idx, i, bar_ends)), leg,
                          None if cand["thr"] is None else round(float(cand["thr"]), 3)])

    for i in range(1, n):
        thr_now = thr_at(i)
        c = closes[i]
        # a candidate set during ATR warm-up has no threshold yet: it takes the first one that becomes
        # available (still causal — nothing after bar i is used); later updates re-record as usual
        if thr_now is not None:
            if cand_hi["thr"] is None:
                cand_hi["thr"] = thr_now
            if cand_lo["thr"] is None:
                cand_lo["thr"] = thr_now
        if direction is None:
            # seed phase: track both candidates, confirm whichever reverses first
            if highs[i] > cand_hi["price"]:
                cand_hi = {"i": i, "price": highs[i], "thr": thr_now}
            if lows[i] < cand_lo["price"]:
                cand_lo = {"i": i, "price": lows[i], "thr": thr_now}
            hi_ok = cand_hi["thr"] is not None and c <= cand_hi["price"] * (1 - cand_hi["thr"] / 100.0)
            lo_ok = cand_lo["thr"] is not None and c >= cand_lo["price"] * (1 + cand_lo["thr"] / 100.0)
            if hi_ok and lo_ok:
                # same-bar tie: follow the bar's own open→close direction
                if c < opens[i]:
                    lo_ok = False
                else:
                    hi_ok = False
            if hi_ok:
                confirm(cand_hi, "H", i, None)
                direction = "down"
                cand_lo = {"i": i, "price": lows[i], "thr": thr_now}
            elif lo_ok:
                confirm(cand_lo, "L", i, None)
                direction = "up"
                cand_hi = {"i": i, "price": highs[i], "thr": thr_now}
            continue

        if direction == "up":
            if highs[i] > cand_hi["price"]:
                cand_hi = {"i": i, "price": highs[i], "thr": thr_now}
            elif cand_hi["thr"] is not None and c <= cand_hi["price"] * (1 - cand_hi["thr"] / 100.0):
                confirm(cand_hi, "H", i, confirmed[-1][1] if confirmed else None)
                direction = "down"
                cand_lo = {"i": i, "price": lows[i], "thr": thr_now}
        else:  # down: tracking a candidate LOW
            if lows[i] < cand_lo["price"]:
                cand_lo = {"i": i, "price": lows[i], "thr": thr_now}
            elif cand_lo["thr"] is not None and c >= cand_lo["price"] * (1 + cand_lo["thr"] / 100.0):
                confirm(cand_lo, "L", i, confirmed[-1][1] if confirmed else None)
                direction = "up"
                cand_hi = {"i": i, "price": highs[i], "thr": thr_now}

    last_close = float(closes[-1])
    provisional = None
    if direction == "up":
        cnd = cand_hi
        retrace = round((last_close / cnd["price"] - 1.0) * 100.0, 2)
        provisional = [_ts(idx[cnd["i"]]), round(float(cnd["price"]), 4), "H", retrace,
                       None if cnd["thr"] is None else round(float(cnd["thr"]), 3)]
    elif direction == "down":
        cnd = cand_lo
        retrace = round((last_close / cnd["price"] - 1.0) * 100.0, 2)
        provisional = [_ts(idx[cnd["i"]]), round(float(cnd["price"]), 4), "L", retrace,
                       None if cnd["thr"] is None else round(float(cnd["thr"]), 3)]

    tag = f"[{symbol} {layer}]".strip("[] ") if (symbol or layer) else ""
    schema = ((f"[{tag}] " if tag else "") +
              "confirmed cols: t,price,type,confirmed_at,leg_pct,threshold_pct; "
              "provisional cols: t,price,type,retrace_so_far_pct,threshold_pct")
    return {
        "schema": schema,
        "confirmed": confirmed,
        "provisional": provisional,
        "anchor_t": _ts(_bar_end(idx, n - 1, bar_ends)),
        "anchor_last": round(last_close, 4),
        "stages": "; ".join(f"k={s.k:g}/floor={s.floor_pct:g}/cap={s.cap_pct:g} from bar {s.start_idx}" for s in st),
        "coverage_from": _ts(idx[0]),
        "coverage_to": _ts(_bar_end(idx, n - 1, bar_ends)),
        "coverage_bars": int(n),
    }


# Layer parameter sets (design §2.2). L1 stages are expressed relative to the window: far stage
# from bar 0, near stage from the bar 126 trading days before the end.
L1_FAR = (3.5, 4.0, 22.0)
L1_NEAR = (1.8, 1.5, 12.0)
L2_PARAMS = (2.2, 0.5, 6.0)
L3_PARAMS = (3.0, 0.20, 3.0)


def l1_stages(n_bars: int, near_bars: int = 126) -> List[Stage]:
    split = max(0, n_bars - near_bars)
    if split == 0:
        return [Stage(0, *L1_NEAR)]
    return [Stage(0, *L1_FAR), Stage(split, *L1_NEAR)]


# ───────────────────────────── telescope digest ───────────────────────────── #

def price_digest(df_1d: pd.DataFrame, long_run_vol_mean: Optional[float] = None,
                 has_volume: bool = True) -> Dict[str, Any]:
    """Four exclusive, contiguous segments by trading-day index over the last ≤504 completed daily bars
    (design §2.3): daily = D[-10:], weekly = D[-73:-10] by ISO week, monthly = D[-252:-73] by calendar
    month, quarterly = D[:-252] by calendar quarter. Rows: [period, open, high, low, close, vol_rel,
    trading_days]; vol_rel = segment-row mean daily volume ÷ two-year mean daily volume (None if no volume).
    Boundary buckets may be partial calendar periods; ``period`` shows the actual first..last trading day."""
    d = df_1d.tail(504)
    n = len(d)
    if n == 0:
        return {}
    vol = d["volume"].astype(float) if ("volume" in d.columns and has_volume) else None
    base = long_run_vol_mean
    if vol is not None and (base is None or not np.isfinite(base) or base <= 0):
        base = float(vol.mean()) if len(vol) else None

    def rows(seg: pd.DataFrame, grouper) -> List[list]:
        out: List[list] = []
        if seg is None or len(seg) == 0:
            return out
        for _, g in seg.groupby(grouper(seg.index), sort=True):
            if len(g) == 0:
                continue
            vr = None
            if vol is not None and base:
                vr = round(float(g["volume"].astype(float).mean()) / base, 2)
            out.append([f"{g.index[0].date()}..{g.index[-1].date()}", round(float(g['open'].iloc[0]), 4),
                        round(float(g['high'].max()), 4), round(float(g['low'].min()), 4),
                        round(float(g['close'].iloc[-1]), 4), vr, int(len(g))])
        return out

    daily = d.iloc[-10:]
    weekly = d.iloc[-73:-10] if n > 10 else d.iloc[0:0]
    monthly = d.iloc[-252:-73] if n > 73 else d.iloc[0:0]
    quarterly = d.iloc[:-252] if n > 252 else d.iloc[0:0]
    iso_week = lambda ix: [f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}" for t in ix]
    month = lambda ix: [f"{t.year}-{t.month:02d}" for t in ix]
    quarter = lambda ix: [f"{t.year}-Q{(t.month - 1) // 3 + 1}" for t in ix]
    schema = "cols: period(first..last trading day),open,high,low,close,vol_rel(row mean daily vol ÷ 2y mean daily vol),trading_days"
    return {
        "schema": schema,
        "quarterly": rows(quarterly, quarter),
        "monthly": rows(monthly, month),
        "weekly": rows(weekly, iso_week),
        "daily": [[f"{t.date()}..{t.date()}", round(float(r['open']), 4), round(float(r['high']), 4),
                   round(float(r['low']), 4), round(float(r['close']), 4),
                   (round(float(r['volume']) / base, 2) if (vol is not None and base) else None), 1]
                  for t, r in daily.iterrows()],
    }


# ───────────────────────────── compatibility wrapper ───────────────────────────── #

def calculate_swing_points(df: pd.DataFrame, lookback_window: int = 20, min_swing_pct: float = 0.015,
                           max_points: int = 20) -> Optional[Dict[str, Any]]:
    """Kept for workflows/postcheck_quant.py (HOLD alternatives read swing levels registered at T).
    The old centered-window / fixed-percent parameters are accepted and ignored; the result comes from
    the same causal ATR zigzag the debaters see (single near-stage daily parameters), so the reckoner
    and the debaters measure with one ruler. Returns {"pivots": [{date, price, type}], "provisional": {...}|None}."""
    if df is None or len(df) < 20:
        return None
    try:
        z = atr_zigzag(df, [Stage(0, *L1_NEAR)])
    except Exception:
        return None
    if not z:
        return None
    pivots = [{"date": c[0], "price": c[1], "type": "high" if c[2] == "H" else "low", "confirmed_at": c[3]}
              for c in z["confirmed"]]
    prov = None
    if z["provisional"]:
        p = z["provisional"]
        prov = {"date": p[0], "price": p[1], "type": "high" if p[2] == "H" else "low", "retrace_so_far_pct": p[3]}
    return {"description": "causal ATR zigzag (V16); pivots are confirmed highs/lows, provisional is the unconfirmed right end",
            "pivots": pivots, "provisional": prov}


def calculate_simplified_trend(df: pd.DataFrame, target: int = 30) -> Optional[Dict[str, Any]]:
    """Retired (V16): the RDP polyline had no financial semantics and duplicated the hourly layer.
    Kept only so old imports do not break during the builder rewrite; always returns None."""
    return None
