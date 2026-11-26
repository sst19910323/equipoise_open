#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline old/new comparison of the technical snapshot (V15 payloads from PG vs V16 built from frozen bar files).

Never touches PG (write_pg=False). Usage:
  ./venv/bin/python scripts/snapshot_v16_offline.py --fixtures <dir with <SYM>/<SYM>_<seg>_*.csv> \
      [--old <json dump {SYM: {as_of, payload}}>] [--out docs/design/reorg/v16-offline-<date>] [--symbols NVDA,TM,...]
Writes <out>/<SYM>.new.json (full payload), <SYM>.thin.json (debater view) and <out>/DIAGNOSTICS.md.
--as-of <ISO UTC>: truncate all four bar files to bars starting before that moment (the latest file of each segment is a
history, so a report from the last ~7 trading days can be re-seen at its own T: daily / 2 months of hourly / 7 days of
5-minute / 2 days of 1-minute reach back that far). Used by the "would V16 have changed this low-score debate" review.
Diagnostics (design §6 / §2.2): L1 coverage to T-504 or listing, points per layer, strict H/L alternation, provisional
distance to the last bar, nulls with their reasons, sizes; old-side counts when a dump is given."""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis import technical_snapshot_builder as tsb  # noqa: E402
from analysis import exchange_clock as ec  # noqa: E402
from analysis import snapshot_v16 as sv  # noqa: E402

SEGS = ("2d_1m", "7d_5m", "2m_1h", "5y_1d")


def _latest(fx: Path, sym: str, seg: str, as_of=None):
    """Latest file of a segment; with as_of, the earliest whose file tag (its last bar time) is ≥ as_of − 1 min so the
    file is the one that was on disk around that moment (intraday files only reach back 2 / 7 days)."""
    fs = sorted(glob.glob(str(fx / sym / f"{sym}_{seg}_*.csv")))
    if not fs:
        return None
    if as_of is not None:
        ok = []
        for f in fs:
            tag = Path(f).stem.split("_")[-1]
            try:
                ts = pd.Timestamp(tag[:8] + "T" + tag[9:15], tz="UTC")
            except Exception:
                continue
            if ts >= as_of - pd.Timedelta(minutes=1):   # file written at/after T (it contains the bars before T); days=1 picked the previous night's file for morning T (server Fable 10-24)
                ok.append((ts, f))
        if ok:
            return Path(min(ok)[1])
        return Path(fs[-1])
    return Path(fs[-1])


def _alternating(rows):
    kinds = [r[2] for r in rows]
    return all(a != b for a, b in zip(kinds, kinds[1:]))


def _old_counts(old_payload: dict, sym: str):
    """V15 payloads were flattened one level deeper: <SYM>_price_structure_<layer>_pivots / _flow_sequence."""
    def n(key, *list_keys):
        for lk in list_keys:
            pts = old_payload.get(f"{sym}_price_structure_{key}_{lk}")
            if isinstance(pts, list):
                last = pts[-1] if pts else {}
                d = last.get("date") or last.get("t") or last.get("time") or "" if isinstance(last, dict) else ""
                return len(pts), str(d)[:10]
        return None, ""
    return {"strategic": n("strategic", "pivots"), "tactical": n("tactical", "pivots"),
            "micro_flow": n("micro_flow_engine", "flow_sequence", "points")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--old")
    ap.add_argument("--out", default=str(ROOT / "docs" / "design" / "reorg" / "v16-offline"))
    ap.add_argument("--symbols")
    ap.add_argument("--as-of", help="UTC moment to re-see the symbols at (bars after it are dropped)")
    a = ap.parse_args()
    as_of = pd.Timestamp(a.as_of, tz="UTC") if a.as_of else None
    fx = Path(a.fixtures)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    old = json.loads(Path(a.old).read_text(encoding="utf-8")) if a.old else {}
    syms = a.symbols.split(",") if a.symbols else sorted(p.name for p in fx.iterdir() if p.is_dir())
    rows = []
    for sym in syms:
        mp = {seg: _latest(fx, sym, seg, as_of) for seg in SEGS}
        if any(v is None for v in mp.values()):
            rows.append(f"| {sym} | missing bar files {[k for k, v in mp.items() if v is None]} | | | | | | | |")
            continue
        if as_of is None:
            p = tsb.process_symbol_technical_analysis(sym, manual_paths=mp, config_path=ROOT / "config/data_sources.example.yaml", write_pg=False)
        else:
            frames = {k: tsb.load_ohlcv(v) for k, v in zip(("1m", "5m", "1h", "1d"), (mp["2d_1m"], mp["7d_5m"], mp["2m_1h"], mp["5y_1d"]))}
            frames = {k: (df[df.index < as_of] if df is not None else None) for k, df in frames.items()}
            if any(df is None or len(df) == 0 for df in frames.values()):
                rows.append(f"| {sym} | no bars before {a.as_of} in {[k for k, df in frames.items() if df is None or len(df) == 0]} | | | | | | | |")
                continue
            clock = ec.resolve_exchange_clock(sym)
            tag = as_of.strftime("%Y%m%dT%H%M%SZ")
            p = sv.build_payload(sym, frames, clock, tag, benchmark_symbol=tsb.resolve_benchmark_symbol(sym),
                                 instrument=tsb.load_instrument_metadata(sym, clock=clock), generated_at=as_of)
        if not p:
            rows.append(f"| {sym} | BUILD FAILED | | | | | | | |")
            continue
        thin = tsb.debater_view(p)
        (out / f"{sym}.new.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / f"{sym}.thin.json").write_text(json.dumps(thin, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        g = lambda k: p.get(f"{sym}_{k}")
        layers = {}
        for lay in ("strategic_2y", "tactical_2m", "intraday_7d"):
            c = g(f"price_structure_{lay}_confirmed") or []
            prov = g(f"price_structure_{lay}_provisional")
            layers[lay] = {"n": len(c), "first": c[0][0] if c else "", "last": c[-1][0] if c else "",
                           "alt": _alternating(c), "prov": (prov[2] + "@" + prov[0]) if prov else "-",
                           "from": g(f"price_structure_{lay}_coverage_from"), "bars": g(f"price_structure_{lay}_coverage_bars")}
        nulls = [k.split(f"{sym}_", 1)[1] for k, v in thin.items() if v is None]
        flags = g("session_context_quality_flags") or []
        o = old.get(sym, {})
        oc = _old_counts(o.get("payload") or {}, sym) if o else {}
        old_size = len(json.dumps(o.get("payload") or {}, ensure_ascii=False, separators=(",", ":"))) if o else None
        new_size = len(json.dumps(thin, ensure_ascii=False, separators=(",", ":")))
        l1 = layers["strategic_2y"]
        rows.append(
            f"| {sym} | {g('session_context_current_session')} / {g('session_context_attribution_day')} | "
            f"{l1['n']} ({l1['first']}..{l1['last']}; prov {l1['prov']}; alt {l1['alt']}; bars {l1['bars']}) | "
            f"{layers['tactical_2m']['n']} (prov {layers['tactical_2m']['prov']}; alt {layers['tactical_2m']['alt']}) | "
            f"{layers['intraday_7d']['n']} (prov {layers['intraday_7d']['prov']}; alt {layers['intraday_7d']['alt']}) | "
            f"{oc.get('strategic', ('-', ''))[0]}/{oc.get('tactical', ('-', ''))[0]}/{oc.get('micro_flow', ('-', ''))[0]} (old L1 last {oc.get('strategic', ('', ''))[1]}) | "
            f"{old_size if old_size else '-'} → {new_size} | {len(nulls)}: {', '.join(nulls[:6])}{'…' if len(nulls) > 6 else ''} | {len(flags)} |")
    md = ["# V16 offline comparison — frozen bars " + fx.name, "",
          "Built with write_pg=False from the frozen four-segment CSVs; old side = V15 payload dumped from PG (same day).",
          "",
          "| symbol | session / T_cur | L1 confirmed (first..last; provisional; alternation; bars) | L2 | L3 | old L1/L2/L3 points | size old → new thin (bytes) | nulls | flags |",
          "|---|---|---|---|---|---|---|---|---|"] + rows
    (out / "DIAGNOSTICS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
