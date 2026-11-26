#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""odds_snapshot.py — the `_session.json.odds` snapshot (contracts C4), built once at `open`.

Every stage after open reads the ruler from this snapshot (run_step.session_cfg), never from config/odds.yaml.
- build_odds_snapshot(state, symbol, root): fresh snapshot for a session being opened: the ruler of config/odds.yaml
  (its `schema`) plus cost_bps / unit_basis from ATR%14 (the opening technical snapshot) and ADV (20-day median
  close×volume of the daily CSV, × fx_to_usd). A session opened before v3.4 has no snapshot and cannot be resumed.
- adv_usd_m(df, fx): the pure ADV computation.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple

import odds_model as om

class OddsSnapshotError(ValueError):
    """The opening snapshot cannot be built (a lens lacks its reference price / ATR%14, or lens weights are unusable)."""


ADV_WINDOW = 20
DAILY_DIR = Path("data") / "market_data"      # <root>/data/market_data/<SYM>/<SYM>_5y_1d_<tag>.csv (same as postcheck_quant)


def adv_usd_m(df: Any, fx: float = 1.0, window: int = ADV_WINDOW) -> Tuple[Optional[float], Optional[str]]:
    """20-day median of close × volume (last `window` bars), × fx, in millions → (adv, None) or (None, reason)."""
    if df is None or len(df) == 0:
        return None, "no daily bars"
    cols = [str(c).lower() for c in df.columns]
    if "close" not in cols or "volume" not in cols:
        return None, "daily CSV has no volume column" if "volume" not in cols else "daily CSV has no close column"
    d = df.copy()
    d.columns = cols
    tail = d[["close", "volume"]].dropna().tail(window)
    if tail.empty or float(tail["volume"].abs().sum()) <= 0:
        return None, "daily volume missing or all zero"
    v = float((tail["close"].astype(float) * tail["volume"].astype(float)).median()) * float(fx) / 1e6
    if not v > 0:
        return None, "daily close×volume median not positive"
    return round(v, 4), None


def daily_csv(root: Path, symbol: str) -> Optional[Path]:
    """Latest `<SYM>_5y_1d_*.csv` for the symbol (by file name, as postcheck_quant reads it); None if absent."""
    d = Path(root) / DAILY_DIR / symbol
    files = sorted(d.glob(f"{symbol}_*_1d_*.csv")) if d.exists() else []
    return max(files, key=lambda f: f.name) if files else None


def _load_daily(path: Path):
    import pandas as pd
    df = pd.read_csv(path)
    df.columns = [str(c).lower() for c in df.columns]
    dcol = next((c for c in ("date", "datetime", "time") if c in df.columns), None)
    if dcol:
        df[dcol] = pd.to_datetime(df[dcol], utc=True, errors="coerce")
        df = df.dropna(subset=[dcol]).sort_values(dcol)
    return df


def _contracts(root: Path) -> dict:
    p = Path(root) / "config" / "symbols.yaml"
    try:
        import yaml
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("symbol_contracts") or {}
    except Exception:
        return {}


VOLUME_LOT_BASIS_US = "IBKR US stock bars report volume in 100-share lots (AAPL CSV: 304,982 vs 158,671 trades)"
ADV_SANITY_MIN_USD_M = 50.0          # a US name below $50 M/day with a normal ATR: suspect the volume units
ATR_NORMAL_PCT = (0.5, 10.0)         # "normal" ATR%14 band for the ADV sanity check


def is_adr(region: str, contract: dict) -> bool:
    """ADR / foreign issuer quoted in USD (ledger ruling, 2nd fix wave): explicit `adr: true` in symbols.yaml, OR a
    non-US region traded in USD (an ADR / OTC line of a foreign name), OR region US with a non-USD contract currency."""
    if contract.get("adr") is True:
        return True
    cur = str(contract.get("currency") or "").upper()
    reg = str(region or "US").upper()
    if not cur:
        return False
    return (reg != "US" and cur == "USD") or (reg == "US" and cur != "USD")


def commit_of(root: Path) -> Tuple[Optional[str], Optional[str]]:
    """→ (commit, source): the one-line file ROOT/DEPLOY_COMMIT that the git-archive deploy writes ("deploy_file") when it
    exists and is non-empty (it wins: the server checkout is a git repo whose HEAD is stale, since deploys overlay by
    `git archive`); else `git rev-parse HEAD` of the tree at root ("git"); else (None, None). Never raises."""
    try:
        out = (Path(root) / "DEPLOY_COMMIT").read_text(encoding="utf-8").strip()
        if out:
            return out.splitlines()[0].strip(), "deploy_file"
    except Exception:
        pass
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        out = (p.stdout or "").strip()
        if p.returncode == 0 and len(out) >= 7:
            return out, "git"
    except Exception:
        pass
    return None, None


def git_commit(root: Path) -> Optional[str]:
    """The deployed tree's commit (DEPLOY_COMMIT, else git); None when unavailable (never raises)."""
    return commit_of(root)[0]


def _atr_of(root: Path, tech_rel: Optional[str], sym: str) -> Optional[float]:
    try:
        if not tech_rel:
            return None
        v = json.loads((Path(root) / tech_rel).read_text(encoding="utf-8")).get(f"{sym}_daily_atr_pct_14")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    except Exception:
        return None


def _data_symbol(state: dict, symbol: str, lens: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """→ (symbol whose bars price the unit, its technical snapshot path). With `lens`: that lens. Otherwise GENERAL sessions
    use the primary (first) lens, the same lens whose price is a GENERAL scenario's P0 (Task 4 ruling), so cost and unit
    match the scenario's scale."""
    df = state.get("data_files") or {}
    if lens is not None:
        return str(lens), (df.get("lenses") or {}).get(str(lens))
    lenses = state.get("lenses") or []
    if lenses and not (df.get("symbol") or {}).get("technical"):
        lens = str(lenses[0])
        return lens, (df.get("lenses") or {}).get(lens)
    return symbol, (df.get("symbol") or {}).get("technical")


def reference_price(state: dict, symbol: str, root: Path, lens: Optional[str] = None) -> Optional[float]:
    """The session reference price a 3.4 scenario's P0 is pinned to: the opening technical snapshot's last price of the
    symbol (of the primary lens for GENERAL, as _data_symbol picks it; of `lens` when given); None when the session has no
    such snapshot."""
    sym, tech = _data_symbol(state or {}, symbol, lens)
    try:
        if not tech:
            return None
        v = json.loads((Path(root) / tech).read_text(encoding="utf-8")).get(f"{sym}_current_snapshot_last_price")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    except Exception:
        return None


def unit_inputs(state: dict, symbol: str, root: Path, cfg: dict, lens: Optional[str] = None) -> dict:
    """ATR%14, ADV (USD millions), venue, adr, plus the reason ADV is missing (if it is). With `lens`, that lens's inputs."""
    sym, tech = _data_symbol(state, symbol, lens)
    contracts = _contracts(root)
    contract = contracts.get(sym) or {}
    venue = str(state.get("region") or (contracts.get(symbol) or {}).get("region") or "US").upper()
    currency = str((contract.get("currency") if sym != symbol else (state.get("currency") or contract.get("currency")))
                   or "USD").upper()
    fx_tbl = ((cfg.get("cost") or {}).get("fx_to_usd") or {})
    lot = float(((cfg.get("cost") or {}).get("volume_lot") or {}).get(venue, 1) or 1)
    adv, why = None, None
    csv = daily_csv(root, sym)
    if currency not in fx_tbl:
        why = f"fx_to_usd has no {currency}"
    elif csv is None:
        why = f"no daily CSV for {sym}"
    else:
        try:
            bars = _load_daily(csv)
            if "volume" in bars.columns:
                bars["volume"] = bars["volume"].astype(float) * lot   # IBKR lot units → shares
            adv, why = adv_usd_m(bars, float(fx_tbl[currency]))
        except Exception as exc:   # an unreadable CSV is a missing input, not a failure of open
            why = f"daily CSV unreadable ({type(exc).__name__})"
    return {"data_symbol": sym, "atr_pct_14": _atr_of(root, tech, sym), "adv_usd_m": adv, "adv_missing": why, "volume_lot": lot,
            "venue": venue, "adr": is_adr(venue, contract), "currency": currency, "technical": tech,
            "csv": csv.relative_to(root).as_posix() if csv is not None else None}


def _cost_and_basis(state: dict, symbol: str, root: Path, cfg: dict, lens: Optional[str] = None) -> Tuple[float, dict, dict]:
    """→ (cost_bps, unit_basis, unit inputs) for the primary data symbol (lens None) or for one lens."""
    u = unit_inputs(state, symbol, root, cfg, lens)
    cost, basis = om.cost_bps_for(u["atr_pct_14"], u["adv_usd_m"], u["venue"], u["adr"], cfg)
    if "fallback" in basis:
        reasons = [r for r in (u["adv_missing"],) if r] + ([] if u["atr_pct_14"] else ["ATR%14 missing on the technical snapshot"])
        basis["fallback"] = "; ".join(reasons + [basis["fallback"]])
    if u["data_symbol"] != symbol:
        basis["data_symbol"] = u["data_symbol"]
    if u["csv"]:
        basis["adv_source"] = u["csv"]
    basis["volume_lot"] = u["volume_lot"]
    basis["volume_lot_basis"] = (VOLUME_LOT_BASIS_US if u["venue"] == "US"
                                 else f"provisional: {u['venue']} volume taken as shares (lot {u['volume_lot']:g}), "
                                      f"unverified — check the first snapshot against a known ADV")
    atr, adv = u["atr_pct_14"], u["adv_usd_m"]
    if (u["venue"] == "US" and om._is_num(adv) and float(adv) < ADV_SANITY_MIN_USD_M and om._is_num(atr)
            and ATR_NORMAL_PCT[0] <= float(atr) <= ATR_NORMAL_PCT[1]):
        basis["warning"] = "ADV implausibly low — check volume units"
    return cost, basis, u


def _lens_entry(state: dict, symbol: str, root: Path, cfg: dict, lens: str) -> dict:
    """One `by_lens` entry: the same values the top level / unit_basis carry, computed by the same functions."""
    ref = reference_price(state, symbol, root, lens)
    if ref is None:
        raise OddsSnapshotError(f"lens {lens}: no reference price")
    cost, basis, u = _cost_and_basis(state, symbol, root, cfg, lens)
    if basis.get("atr_pct_14") is None:
        raise OddsSnapshotError(f"lens {lens}: ATR%14 missing")
    e = {"reference_price": ref, "currency": u["currency"], "data_symbol": u["data_symbol"],
         "atr_pct_14": basis["atr_pct_14"], "adv_usd_m": basis["adv_usd_m"], "adv_missing": u["adv_missing"],
         "volume_lot": basis["volume_lot"], "volume_lot_basis": basis["volume_lot_basis"], "venue": basis["venue"],
         "adr": basis["adr"], "s_s": basis["s_s"], "cost_bps": cost, "adv_source": basis.get("adv_source"),
         "technical_source": u["technical"]}
    if "warning" in basis:
        e["warning"] = basis["warning"]
    if "fallback" in basis:
        e["fallback"] = basis["fallback"]
    # G6 (wave 2): an index lens (contract sec_type IND, e.g. DAX) has no shares — its levels are observation lines only;
    # frozen here, carried by the ruling into by_lens[L] and by the report to the rebalance brief
    if str((_contracts(root).get(lens) or {}).get("sec_type") or "").upper() == "IND":
        e["observe_only"] = True
    return e


def build_odds_snapshot(state: dict, symbol: str, root: Path) -> dict:
    """The C4 snapshot for a session being opened (schema = config/odds.yaml's `schema`). A multi-lens state adds
    `lens_weights_default` and `by_lens` (one frozen entry per lens); the top-level values stay those of the primary lens."""
    commit, source = commit_of(root)
    cfg = om.load_config(root)
    cost, basis, _u = _cost_and_basis(state, symbol, root, cfg)
    snap = om.snapshot_from_config(cfg, cost_bps=cost, unit_basis=basis, commit=commit)
    snap["provenance"]["source"] = source
    # the P0 pin (C3 / I4): resolved once here and frozen; ingestion refuses every final while it is None
    snap["reference_price"] = reference_price(state, symbol, root)
    from lens_views import LensError, default_weights, lenses_of
    lenses = lenses_of(state)
    if not lenses and (_contracts(root).get(symbol) or {}).get("sec_type") == "IND":
        snap["observe_only"] = True
    if lenses:
        snap["by_lens"] = {l: _lens_entry(state, symbol, root, cfg, l) for l in lenses}
        try:
            snap["lens_weights_default"] = default_weights(root, symbol)
        except (LensError, OSError) as exc:
            raise OddsSnapshotError(f"lens weights for {symbol}: {exc}") from exc
        cfg_lenses = list(snap["lens_weights_default"])
        if cfg_lenses != list(lenses):   # one lens set: the session's (data manifest) = the config weights' (symbols.yaml)
            missing = [l for l in lenses if l not in cfg_lenses]
            extra = [l for l in cfg_lenses if l not in lenses]
            raise OddsSnapshotError(
                f"lens set of {symbol}: session lenses {list(lenses)} differ from the symbols.yaml lens weights {cfg_lenses} "
                f"(no weight for {missing}; a weight for a non-session lens {extra}; same names in the same order required)")
    return snap
