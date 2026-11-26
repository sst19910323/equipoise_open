#!/usr/bin/env python3
"""slow_pressure_fetch.py — pull the slow-pressure series from FRED into PG `slow_pressure` (stored only; not read by
data preparation — see datalayer/pg/slow_pressure.py).

    ./venv/bin/python scripts/slow_pressure_fetch.py              # daily: the last 45 days of every series
    ./venv/bin/python scripts/slow_pressure_fetch.py --backfill   # first load: history since --since (default 2021-01-01)
    ./venv/bin/python scripts/slow_pressure_fetch.py --show       # latest stored reading per indicator

Exit 0 = every series fetched; 1 = at least one failed (the others are still stored)."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from datalayer.pg import slow_pressure as sp  # noqa: E402

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch(series_id: str, since: date) -> str:
    r = requests.get(URL, params={"id": series_id, "cosd": since.isoformat()}, timeout=30,
                     headers={"User-Agent": "equipoise slow-pressure fetch"})
    r.raise_for_status()
    return r.text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since", default="2021-01-01")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    sp.ensure_table()
    if a.show:
        for ind in sp.SERIES:
            rows = sp.series(ind, since=date.today() - timedelta(days=30))
            print(f"{ind:10s} {rows[-1][0]} {rows[-1][1]}" if rows else f"{ind:10s} (empty)")
        return 0
    now = datetime.now(timezone.utc)
    since = date.fromisoformat(a.since) if a.backfill else date.today() - timedelta(days=45)
    bad = 0
    for ind, (sid, _unit, _cad, _what) in sp.SERIES.items():
        try:
            obs = sp.parse_fred_csv(fetch(sid, since))
            had = sp.latest(ind)
            n = sp.upsert(sp.rows_from(ind, obs, now, backfill=a.backfill))
            print(f"[slow_pressure] {ind}: {n} obs through {obs[-1][0] if obs else '-'} (had {had})")
        except Exception as exc:  # one series failing must not lose the others
            bad += 1
            print(f"[slow_pressure][WARN] {ind} ({sid}): {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
