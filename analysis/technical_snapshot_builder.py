#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Technical snapshot builder — I/O shell (V16, 2026-10-25).

Reads the four IBKR bar files of one symbol (2d_1m / 7d_5m / 2m_1h / 5y_1d), hands them to
analysis/snapshot_v16.build_payload (all feature computation lives there; structure algorithms in
analysis/trend_analyzer.py; exchange clocks / calendars in analysis/exchange_clock.py), and writes the flat
<SYM>_-prefixed payload to PG `technical` (symbol, as_of = market_data_timestamp_utc, payload).

Two projections of one computation: PG keeps the full payload (chart series, all confirmed pivots);
`debater_view` (re-exported from snapshot_v16) is what hydrate_pg_to_files writes for the debaters.

Entry point: process_symbol_technical_analysis(symbol, manual_paths=None, force_update=False, config_path=None,
write_pg=True) → the flat payload dict (or None). Offline comparisons pass write_pg=False and never touch PG.
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

# ───────────── Session classification lives in analysis/exchange_clock.py (V16); re-exported here ───────────── #
from analysis.exchange_clock import (  # noqa: E402,F401
    PREMARKET_START_ET_HOUR, AFTERHOURS_END_ET_HOUR, EXCHANGE_CLOCKS, get_calendar, get_nyse_calendar,
    _symbols_cfg, _contract, _SYMBOLS_CFG_CACHE, resolve_exchange_clock, _as_utc, session_break, classify_session,
    get_prior_regular_close, last_completed_daily_idx, resolve_benchmark_symbol, benchmark_is_fresh,
    load_instrument_metadata,
)
from analysis.snapshot_v16 import build_payload, debater_view, FEATURE_VERSION  # noqa: E402,F401

# ──────────────────────────── Logging Setup ──────────────────────────── #
log = logging.getLogger("technical_snapshot_builder")
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

REQUIRED_SCHEDULES = {"2d_1m": "1m", "7d_5m": "5m", "2m_1h": "1h", "5y_1d": "1d"}


# ──────────────────────────── Config & File Loading ──────────────────────────── #

def load_config(config_path: Optional[Path] = None) -> dict:
    if config_path:
        path_to_load = Path(config_path)
    else:
        path_to_load = Path(__file__).resolve().parents[1] / "config/data_sources.yaml"
    if not path_to_load.exists():
        path_to_load = Path("config/data_sources.yaml")
    if not path_to_load.exists():
        log.error(f"Config file not found: {path_to_load}")
        raise FileNotFoundError(f"Config file not found: {path_to_load}")
    with open(path_to_load, encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# ──────────────────────────── Filename Parsing ──────────────────────────── #

def extract_time_tag_from_filename(csv_path: Path) -> Optional[str]:
    """ISO 8601 basic (YYYYMMDDTHHMMSSZ) tag at the end of the file stem; legacy 'LATEST' is rejected."""
    try:
        candidate = Path(csv_path).stem.split("_")[-1]
        if candidate == "LATEST":
            log.warning(f"[SKIP] Ignored legacy 'LATEST' file: {Path(csv_path).name}")
            return None
        if ("T" in candidate and candidate.endswith("Z")) or candidate.isdigit():
            return candidate
        log.warning(f"[SKIP] Invalid time format '{candidate}' in {Path(csv_path).name}. Ignoring.")
        return None
    except (ValueError, IndexError):
        return None


def get_latest_csv_for_resolution(symbol: str, cache_dir: Path, filename_duration: str, filename_resolution: str) -> Optional[Path]:
    symbol_dir = Path(cache_dir) / symbol
    if not symbol_dir.exists():
        return None
    matching = list(symbol_dir.glob(f"{symbol}_{filename_duration}_{filename_resolution}_*.csv"))
    if not matching:
        return None
    valid = [f for f in matching if extract_time_tag_from_filename(f) is not None]
    if not valid:
        log.warning(f"[WARN] No valid timestamped files found for {symbol} (found {len(matching)} invalid/legacy).")
        return None
    valid.sort(key=lambda p: p.stat().st_mtime)
    return valid[-1]


def extract_market_data_timestamp_iso(manual_paths: Optional[Dict[str, Path]] = None, symbol: str = None,
                                      config: dict = None) -> Optional[str]:
    """The snapshot's base timestamp comes from the 2d_1m file name only (no fallback: without 1m bars nothing works)."""
    if manual_paths:
        target = manual_paths.get("2d_1m")
    else:
        cache_dir = Path(config["data_sources"]["market_data"]["cache_dir"])
        target = get_latest_csv_for_resolution(symbol, cache_dir, "2d", "1m")
    if target is None:
        log.error(f"Failed to find a VALID '2d_1m' market data file for {symbol}. Analysis cannot proceed.")
        return None
    return extract_time_tag_from_filename(Path(target))


# ──────────────────────────── Data Loading ──────────────────────────── #

def load_ohlcv(csv_path: Path, min_rows: int = 1) -> Optional[pd.DataFrame]:
    """UTC-indexed OHLCV. Only open/high/low/close must be present and non-NaN; volume / wap / barcount may be
    missing or NaN (index contracts such as DAX / N225 carry no volume — V15 dropped those rows wholesale)."""
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        log.warning(f"Unable to read {csv_path}: {exc}")
        return None
    df.columns = [c.lower() for c in df.columns]
    date_col = "datetime" if "datetime" in df.columns else "date"
    if date_col not in df.columns:
        return None
    df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    df = df.dropna(subset=[date_col]).sort_values(date_col).set_index(date_col)
    cols = [c for c in ("open", "high", "low", "close", "volume", "wap", "barcount") if c in df.columns]
    df = df[cols]
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return None
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")]
    if len(df) < min_rows:
        return None
    return df


def load_all_dataframes(symbol: str, config: dict, manual_paths: Optional[Dict[str, Path]] = None) -> Optional[Dict[str, pd.DataFrame]]:
    """All four resolutions are required (1m, 5m, 1h, 1d); any missing → None."""
    frames: Dict[str, pd.DataFrame] = {}
    if manual_paths:
        for label, key in REQUIRED_SCHEDULES.items():
            if label not in manual_paths:
                log.error(f"Missing required manual path: {label}")
                return None
            df = load_ohlcv(manual_paths[label])
            if df is None:
                log.error(f"Failed to load dataframe from manual path: {label}")
                return None
            frames[key] = df
        return frames
    cache_dir = Path(config["data_sources"]["market_data"]["cache_dir"])
    for schedule in config["data_sources"]["market_data"]["schedules"]:
        label = schedule.get("label")
        if label in REQUIRED_SCHEDULES:
            p = get_latest_csv_for_resolution(symbol, cache_dir, schedule["filename_duration"], schedule["filename_resolution"])
            if p:
                df = load_ohlcv(p)
                if df is not None:
                    frames[REQUIRED_SCHEDULES[label]] = df
    if len(frames) != len(REQUIRED_SCHEDULES):
        log.error(f"Failed to find all required CSV schedules for {symbol}. Found {len(frames)}/{len(REQUIRED_SCHEDULES)}.")
        return None
    return frames


def safe_float(value: Any, precision: int = 4) -> Optional[float]:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    try:
        return round(float(value), precision)
    except (ValueError, TypeError):
        return None


def safe_int(value: Any) -> Optional[int]:
    val = safe_float(value, 0)
    return int(val) if val is not None else None


BENCHMARK_DATA: Dict[str, pd.DataFrame] = {}


def get_benchmark_data(symbol: str, config: dict) -> Optional[pd.DataFrame]:
    """The benchmark's 5y_1d cache (freshness is judged by the caller with benchmark_is_fresh)."""
    if symbol in BENCHMARK_DATA:
        return BENCHMARK_DATA[symbol]
    cache_dir = Path(config["data_sources"]["market_data"]["cache_dir"])
    bench_file = get_latest_csv_for_resolution(symbol, cache_dir, "5y", "1d")
    if not bench_file:
        return None
    df = load_ohlcv(bench_file, min_rows=252)
    if df is not None:
        BENCHMARK_DATA[symbol] = df
    return df


def read_feed_meta(symbol: str, config: dict, clock: Dict[str, Any]) -> Dict[str, Any]:
    """Feed mode of the bars: the fetcher's sidecar data/market_data/<SYM>/_feed_meta.json when present, else the
    configured default (symbols.yaml market_data_mode; USD → realtime, others → delayed 15). US historical 1m bars lag
    about fifteen minutes even on a realtime feed, so session_context.bar_age_minutes is the honest staleness measure."""
    meta: Dict[str, Any] = {}
    try:
        p = Path(config["data_sources"]["market_data"]["cache_dir"]) / symbol / "_feed_meta.json"
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning(f"[feed_meta] unreadable for {symbol}: {exc}")
    if "feed_mode" not in meta:
        contract = _contract(symbol)
        mode = str(contract.get("market_data_mode") or "").lower()
        if mode not in ("realtime", "delayed"):
            mode = "realtime" if str(contract.get("currency") or "USD").upper() == "USD" else "delayed"
        meta["feed_mode"] = mode
    if "feed_delay_minutes" not in meta:
        meta["feed_delay_minutes"] = 0 if meta["feed_mode"] == "realtime" else 15
    return meta


# ─────────────────────── Save (PG) ─────────────────────── #

def save_json(json_data: Dict[str, Any], output_path: Path = None) -> bool:
    """Write the technical snapshot to PG equipoise.technical (best-effort); no JSON file is written any more.
    output_path is accepted for old callers and ignored."""
    try:
        from datalayer.pg.marketdata_pg_store import save_technical
        ok = save_technical(json_data)
        log.info(f"Technical snapshot → PG technical ({'ok' if ok else 'write-failed'})")
    except Exception as exc:
        log.warning(f"[save_json] PG write of technical failed (ignored): {exc}")
    return True  # best-effort: let the caller continue (data is in memory / PG); downstream checks skip if PG is down


# ─────────────────────── Main Processing Function ─────────────────────── #

def process_symbol_technical_analysis(symbol: str, manual_paths: Optional[Dict[str, Path]] = None,
                                      force_update: bool = False, config_path: Optional[Path] = None,
                                      write_pg: bool = True) -> Optional[Dict[str, Any]]:
    """Build the V16 snapshot for one symbol. Returns the flat payload (None on failure). write_pg=False → compute only
    (offline comparisons). force_update is accepted for old callers and has no effect: there is no file cache to skip
    (V15's tech_path.exists() short-circuit silently skipped the PG write after hydrate had written a file)."""
    symbol = str(symbol).upper()
    log.info(f"--- Technical snapshot {FEATURE_VERSION} for {symbol} ---")
    try:
        config = load_config(config_path)
    except Exception as exc:
        log.error(f"Failed to load config: {exc}")
        return None
    ts_tag = extract_market_data_timestamp_iso(manual_paths=manual_paths, symbol=symbol, config=config)
    if not ts_tag:
        log.error(f"[ABORT] {symbol}: missing or invalid 1m data timestamp.")
        return None
    frames = load_all_dataframes(symbol, config, manual_paths)
    if not frames:
        log.error(f"Could not load all required dataframes for {symbol}. Aborting.")
        return None
    clock = resolve_exchange_clock(symbol)
    instrument = load_instrument_metadata(symbol, clock=clock)
    bench = resolve_benchmark_symbol(symbol)
    bench_df = get_benchmark_data(bench, config) if bench else None
    bench_fresh = bool(bench_df is not None and benchmark_is_fresh(bench_df, frames["1d"]))
    feed_meta = read_feed_meta(symbol, config, clock)
    try:
        payload = build_payload(symbol, frames, clock, ts_tag, benchmark_symbol=bench, bench_frames=bench_df,
                                bench_fresh=bench_fresh, instrument=instrument, feed_meta=feed_meta,
                                generated_at=datetime.now(timezone.utc))
    except Exception as exc:
        log.error(f"[ABORT] {symbol}: feature computation failed: {exc}", exc_info=True)
        return None
    if write_pg:
        save_json(payload)
    log.info(f"--- Finished {symbol}: {len(payload)} keys ---")
    return payload


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    out = process_symbol_technical_analysis(sym, write_pg="--no-pg" not in sys.argv)
    if out:
        print(json.dumps(debater_view(out), ensure_ascii=False)[:2000])
