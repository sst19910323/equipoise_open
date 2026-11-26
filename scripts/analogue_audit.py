"""Historical analogue audit: extension band plus five-session relative return."""
import math
import re

import numpy as np
import pandas as pd


def feature_rows(symbol, bars, benchmark):
    rows = []
    c = bars.close
    extension = c.pct_change(20) / (c.pct_change().rolling(20).std() * np.sqrt(20))
    tr = pd.concat([bars.high - bars.low, (bars.high - c.shift()).abs(),
                    (bars.low - c.shift()).abs()], axis=1).max(axis=1)
    tr.iloc[13] = tr.iloc[:14].mean()
    atr = tr.iloc[13:].ewm(alpha=1 / 14, adjust=False).mean()
    for h in [3, 15]:
        width = 0.707 * atr / c * np.sqrt(h)
        raw = c.shift(-h) / c - 1
        rows.append(pd.DataFrame({
            "symbol": symbol, "h": h, "band": np.searchsorted([-1, 0, 1], extension),
            "rel": (c.pct_change(5) > benchmark.pct_change(5)).astype(int),
            "block": np.arange(len(c)) // 63, "width": width, "y": raw / width,
            "end": c.index.to_series().shift(-h), "raw": raw,
            "breakout": (c > c.shift().rolling(20).max()).astype(int)
                        - (c < c.shift().rolling(20).min()).astype(int),
        }).loc[np.isfinite(extension)])
    return pd.concat(rows).rename_axis("date").reset_index()


def fit(train):
    parents = {}
    effects = {}
    for key, parent in train.groupby(["symbol", "band"]):
        parents[key] = parent.y.mean()
        if len(parent.groupby("rel")) != 2 or parent.groupby("rel").block.nunique().min() < 3:
            continue
        # Sufficient statistics make the paired delete-block estimate inexpensive.
        blocks = sorted(parent.block.unique())
        for rel, child in parent.groupby("rel"):
            difference = child.y.mean() - parents[key]
            omitted = []
            for sample in (child, parent):
                by_block = sample.groupby("block").y.agg(["sum", "count"]).reindex(blocks, fill_value=0)
                omitted.append((sample.y.sum() - by_block["sum"]) / (len(sample) - by_block["count"]))
            jack = omitted[0] - omitted[1]
            variance = (len(jack) - 1) / len(jack) * ((jack - jack.mean()) ** 2).sum()
            effects[(*key, rel)] = difference, variance
    tau2 = max(0., np.mean([d*d - v for d, v in effects.values()])) if effects else 0.
    return parents, effects, tau2


def daily_history(root, symbol, ts, day):
    directory = root / "data" / "market_data" / symbol
    vintages = []
    for path in directory.glob(f"{symbol}_*_1d_*.csv"):
        stamp = path.stem.rsplit("_", 1)[-1]
        if re.fullmatch(r"\d{8}T\d{6}Z", stamp) and stamp <= ts:
            vintages.append((stamp, path))
    if not vintages:
        raise ValueError(f"No daily archive for {symbol} as of {ts}")
    _, path = max(vintages)
    bars = pd.read_csv(path, usecols=["date", "high", "low", "close"])
    bars.index = pd.to_datetime(bars.pop("date").astype(str).str[:10], errors="raise")
    bars = bars.loc[bars.index < day].sort_index().astype(float)
    if bars.empty or bars.index.has_duplicates or not np.isfinite(bars.values).all():
        raise ValueError(f"Missing, duplicate or nonfinite daily bars for {symbol}")
    if (bars.low <= 0).any() or (bars.low > bars.close).any() or (bars.high < bars.close).any():
        raise ValueError(f"Invalid daily price range for {symbol}")
    return bars, path.relative_to(root).as_posix()


def loss(mu, observed):
    """Normal CRPS at unit width, in the pilot's standardized return units."""
    e = observed - mu
    return e * math.erf(e / math.sqrt(2)) + math.sqrt(2 / math.pi) * math.exp(-e * e / 2) - 1 / math.sqrt(math.pi)


def horizon_result(history):
    current = history.iloc[-1]
    quarter = current.date.to_period("Q")
    loss_differences = []
    for period in pd.period_range(quarter - 4, quarter, freq="Q"):
        train = history[history.end < period.start_time]
        parents, effects, tau2 = fit(train)
        if period == quarter:
            break
        held_out = history[history.date.dt.to_period("Q") == period].dropna(subset=["y"])
        for row in held_out.itertuples():
            key = (row.symbol, row.band)
            if key not in parents:
                continue
            base = parents[key]
            shift, variance = effects.get((*key, row.rel), (0., np.inf))
            weight = tau2 / (tau2 + variance) if tau2 else 0.
            loss_differences.append(loss(base + weight * shift, row.y) - loss(base, row.y))
    key = (current.symbol, current.band)
    subgroup = train[(train.band == current.band) & (train.rel == current.rel)]
    result = {"sessions": int(current.h), "matched_dates": len(subgroup),
              "occupied_63_session_blocks": int(subgroup.block.nunique()),
              "validation_dates": len(loss_differences)}
    gap = float(np.mean(loss_differences)) if loss_differences else None
    result["validation_crps_minus_parent"] = gap
    if (*key, current.rel) not in effects:
        return dict(result, status="insufficient_support")
    shift, variance = effects[(*key, current.rel)]
    weight = tau2 / (tau2 + variance) if tau2 else 0.
    if gap is None:
        status = "no_validation"
    elif not weight or gap >= 0:
        status = "no_incremental_support"
    else:
        status = "lower_historical_loss"
    scale = 100 * current.width
    return dict(result, status=status, parent_return_pct=scale * parents[key],
                raw_increment_pp=scale * shift, standard_error_pp=scale * math.sqrt(variance),
                weight=weight, shrunk_increment_pp=scale * weight * shift)


def receipt(root, state, ts, params):
    """Calculate only on filing, using completed prior-day bars and mature labels."""
    if not isinstance(params, dict) or params.get("condition") != "relative_strength_5d" or set(params) - {"symbol", "condition", "claim"}:
        return {"status": "not_covered", "reason": "Supported condition: relative_strength_5d versus SPY"}
    target = params.get("symbol")
    allowed = state.get("lenses") or [state["symbol"]]
    if state.get("region") != "US" or target not in allowed or target == "SPY":
        return {"status": "not_covered", "reason": "Requires a US session instrument distinct from SPY"}
    try:
        day = pd.to_datetime(ts, format="%Y%m%dT%H%M%SZ", utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
        bars, source = daily_history(root, target, ts, day)
        spy, benchmark_source = daily_history(root, "SPY", ts, day)
        start = max(bars.index.min(), spy.index.min())
        bars, spy = bars.loc[start:], spy.loc[start:]
        if not bars.index.equals(spy.index) or len(bars) < 63 or (day - bars.index[-1]).days > 7:
            raise ValueError("Daily histories are misaligned, stale or too short")
        frame = feature_rows(target, bars, spy.close)
        if frame.empty or frame.date.max() != bars.index[-1] or not (frame.width > 0).all():
            raise ValueError("Undefined volatility scale or current extension")
        frame = frame.replace([np.inf, -np.inf], np.nan)
        out = [horizon_result(part) for _, part in frame.groupby("h")]
        current = frame.iloc[-1]
        bands = ["below -1", "-1 to 0", "0 to 1", "above 1"]
        return {"status": "measured", "symbol": target, "benchmark": "SPY", "as_of": ts,
                "extension_band_sd": bands[current.band],
                "five_session_relative_return": "above SPY" if current.rel else "at or below SPY",
                "last_close_date": str(bars.index[-1].date()), "sources": [source, benchmark_source],
                "horizons": out}
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "data_unavailable", "reason": str(exc)}
