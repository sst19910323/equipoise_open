"""Offline QQQ breadth pilot on a named, frozen IBKR daily-bar export.

python scripts/breadth_audit.py --market-dir data/market_data --stamp 20261120T000000Z
Each sector has one vote. Forecasts use expanding, purged quarterly training.
The archive is a single historical vintage, not a reconstruction of past vintages.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as calendars


SECTORS = ("XLK", "XLE", "XLP", "XLV", "XLF", "XLU")
HORIZONS = (3, 20)


def read_panel(directory, stamp):
    cutoff = pd.to_datetime(stamp, format="%Y%m%dT%H%M%SZ", utc=True)
    columns, hashes = {}, {}
    for symbol in ("QQQ", *SECTORS):
        path = directory / symbol / f"{symbol}_5y_1d_{stamp}.csv"
        bars = pd.read_csv(path, usecols=["date", "close", "open"])
        bars.index = pd.DatetimeIndex(bars.pop("date").str[:10])
        if bars.index.hasnans or bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
            raise ValueError(f"Invalid session dates for {symbol}")
        if bars.empty or not np.isfinite(bars.to_numpy()).all() or (bars <= 0).any().any():
            raise ValueError(f"Invalid prices for {symbol}")
        if columns and not bars.index.equals(columns["QQQ"].index):
            raise ValueError(f"Misaligned daily archive for {symbol}")
        columns[symbol] = bars.close
        if symbol == "QQQ":
            columns["qqq_open"] = bars.open
        hashes[symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
    panel = pd.DataFrame(columns)
    schedule = calendars.get_calendar("NYSE").schedule(panel.index.min(), panel.index.max())
    if not panel.index.equals(schedule.index):
        raise ValueError("Archive has missing or extra US sessions")
    completed = panel.loc[schedule.market_close <= cutoff]
    if completed.empty:
        raise ValueError("No completed sessions at this cutoff")
    return completed, hashes


def features(panel):
    prices = np.log(panel[["QQQ", *SECTORS]])
    move = prices.diff(5)
    volatility = prices.QQQ.diff().rolling(20).std()
    sector_moves = move[list(SECTORS)]
    # Count strictly signed sectors so unchanged prices cannot imply participation.
    breadth = np.select([(sector_moves < 0).sum(axis=1) >= 5,
                         (sector_moves > 0).sum(axis=1) >= 5], [-1, 1], default=0)
    extension = move.QQQ / (volatility * np.sqrt(5))
    return pd.DataFrame({"move_band": np.searchsorted([-1., 0., 1.], extension),
                         "vol": volatility, "breadth": breadth,
                         "session": np.arange(len(panel)), "block": np.arange(len(panel)) // 63}, index=panel.index
                        ).loc[np.isfinite(extension) & sector_moves.notna().all(axis=1)]


def replay(panel, horizon):
    rows = features(panel)
    rows["end"] = panel.index.to_series().shift(-horizon)
    # Signal after close t, enter at open t+1, exit at close t+h.
    rows["y"] = 100 * (panel.QQQ.shift(-horizon) / panel.qqq_open.shift(-1) - 1)
    rows = rows.dropna(subset=["end", "y"]).copy()
    results = []
    for quarter, test in rows.groupby(rows.index.to_period("Q")):
        train = rows.loc[rows.end < quarter.start_time].copy()
        if len(train) < 504:
            continue
        threshold = train.vol.median()
        train["high_vol"] = train.vol > threshold
        test = test.loc[test.breadth != 0].assign(high_vol=test.vol > threshold)
        for (side, band, high_vol), subset in test.groupby(["breadth", "move_band", "high_vol"]):
            subset = subset.copy()
            parent = train.loc[(train.move_band == band) & (train.high_vol == high_vol)]
            child = parent.loc[parent.breadth == side]
            blocks = child.block.nunique()
            subset["parent_n"], subset["child_n"] = len(parent), len(child)
            spaced_count = len(disjoint(child, spacing=max(5, horizon)))
            subset["child_spaced_n"] = spaced_count
            subset["base_noncontinuation_rate"] = (side * parent.y <= 0).mean()
            # Empty parent cells stay visible as unscored. Require ten expected
            # tail observations even after spacing the training dates.
            for metric, tail in (("mean", None), ("lower", .1), ("upper", .9)):
                supported = len(child) >= 30 and blocks >= 4 and spaced_count >= (10 if tail is None else 100)
                subset[f"{metric}_supported"] = supported
                for label, sample in (("base", parent), ("split", child if supported else parent)):
                    forecast = sample.y.mean() if metric == "mean" else sample.y.quantile(tail)
                    subset[f"{label}_{metric}"] = forecast
                    error = subset.y - forecast
                    if metric == "mean":
                        subset[f"{label}_mse"] = error ** 2
                    else:
                        subset[f"{label}_{metric}_pinball"] = np.maximum(tail * error, (tail - 1) * error)
            results.append(subset)
    return pd.concat(results).sort_index() if results else pd.DataFrame()


def disjoint(sample, spacing=None):
    """Greedily retain windows in chronological order, entry always after signal."""
    keep = []
    previous_exit = -np.inf if spacing else pd.Timestamp.min
    for date, session, end in sample[["session", "end"]].itertuples(name=None):
        start = session if spacing else date
        if start >= previous_exit:
            keep.append(date)
            previous_exit = start + spacing if spacing else end
    return sample.loc[keep]


def score_summary(sample, name, loss):
    paired = (sample[f"split_{loss}"] - sample[f"base_{loss}"]).dropna()
    active = sample.loc[sample[f"{name}_supported"]]
    delta = paired.reindex(active.index)
    quarters = delta.groupby(delta.index.to_period("Q")).mean()
    spaced = disjoint(active)
    return {
        "scored": len(paired), "supported": len(active), "nonoverlapping_supported": len(spaced),
        "delta_with_fallback": float(paired.mean()) if len(paired) else None,
        "delta_supported": float(delta.mean()) if len(delta) else None,
        "delta_nonoverlapping_supported": float(delta.loc[spaced.index].mean()) if len(spaced) else None,
        "supported_quarters_better_tied_worse": [int((quarters < 0).sum()), int((quarters == 0).sum()), int((quarters > 0).sum())],
    }


def summary(rows, side):
    sample = rows.loc[rows.breadth == side] if not rows.empty else rows
    if sample.empty:
        return {"evaluated": 0}
    separated = disjoint(sample)
    return {
        "evaluated": len(sample), "nonoverlapping": len(separated),
        "first": str(sample.index.min().date()), "last": str(sample.index.max().date()),
        "lower_pinball_pp": score_summary(sample, "lower", "lower_pinball"),
        "upper_pinball_pp": score_summary(sample, "upper", "upper_pinball"),
        "mse_pp2": score_summary(sample, "mean", "mse"),
        "mean_return_pct": float(sample.y.mean()),
        "noncontinuation_count": int((side * sample.y <= 0).sum()),
        "parent_expected_noncontinuation_count": float(sample.base_noncontinuation_rate.sum(min_count=1))
        if sample.base_noncontinuation_rate.notna().any() else None,
        "nonoverlapping_mean_return_pct": float(separated.y.mean()),
        "nonoverlapping_noncontinuation_count": int((side * separated.y <= 0).sum()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-dir", type=Path, required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--rows", type=Path, help="Write dated forecasts and outcomes to CSV")
    args = parser.parse_args()
    panel, hashes = read_panel(args.market_dir, args.stamp)
    output = {"stamp": args.stamp, "sha256": hashes, "last_session": str(panel.index.max().date()),
              "six_sector_proxies": SECTORS, "horizons": {}}
    forecasts = []
    for horizon in HORIZONS:
        rows = replay(panel, horizon)
        forecasts.append(rows.assign(horizon=horizon))
        output["horizons"][horizon] = {key: summary(rows, side) for key, side in (("down", -1), ("up", 1))}
    if args.rows:
        pd.concat(forecasts).to_csv(args.rows, index_label="signal_date")
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
