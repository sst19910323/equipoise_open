#!/usr/bin/env python3
"""divergence_history.py — the owner's view of PG divergence_log: how a report's divergence moved day by day.

  ./venv/bin/python scripts/divergence_history.py NVDA                 # last 30 days, every report of the symbol
  ./venv/bin/python scripts/divergence_history.py GENERAL --days 60
  ./venv/bin/python scripts/divergence_history.py NVDA --json

The log is written by every duty breath (scripts/duty_status.py → datalayer/pg/divergence_log.py); one row per
symbol / report as_of / lens / trading day with the day's latest reading and extremes (tail_min, level_worst).
Read-only; the calibration of config/duty.yaml `divergence` thresholds reads the same table.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    from datalayer.pg import divergence_log
    rows = divergence_log.series(a.symbol, days=a.days)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, default=str, indent=1))
        return 0
    if not rows:
        print(f"no divergence rows for {a.symbol.upper()} in the last {a.days} days")
        return 0
    print(f"{'as_of':17} {'lens':6} {'day':10} {'t':>4} {'move':>6} {'tail':>6} {'min':>6} {'kl':>5} {'dom':5} {'level':8} {'worst':8} {'at':6}")
    for r in rows:
        kl = "inf" if r.get("kl_bits") is None else f"{r['kl_bits']:.1f}"
        print(f"{str(r['as_of'])[:16]:17} {r['lens'] or '-':6} {str(r['day']):10} {r['t'] if r['t'] is not None else '-':>4} "
              f"{(r['move_atr'] if r['move_atr'] is not None else 0):>+6.1f} {(r['tail'] if r['tail'] is not None else 0):>6.2f} "
              f"{(r['tail_min'] if r['tail_min'] is not None else 0):>6.2f} {kl:>5} {str(r.get('dominant') or '-'):5} "
              f"{str(r.get('level') or '-'):8} {str(r.get('level_worst') or '-'):8} {str(r['at'])[11:16]:6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
