"""slow_pressure — slow, price-independent pressure series kept in PG (2026-11-12; council
council/20261106-1609-october-grind-strategic-sell/: the October grind was argued on one-off Scout lookups of credit
spreads and Fed liquidity; a continuous, synchronised page replaces those repeated lookups).

**Stored only. Nothing here enters the debate material**: data preparation does not read this table. Whether a series
carries information for the following 15 trading days is measured first (drill session); wiring it into the material
is a behaviour change with its own cut line in docs/MAJOR-CHANGES.md.

One table `slow_pressure`, one row per (indicator, observation date). `first_seen_at` is when this store first held
the observation and is never rewritten: for rows written by the daily fetch it approximates the publication time a
point-in-time study needs; rows from the initial history load carry `backfill = true` (their first_seen_at is the load
time, not the publication time). A revised value updates `value` / `revised_at` and keeps `first_value`.
Raw series only: no composite "net liquidity" index, good and bad readings alike.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from .db import connect

# indicator -> (FRED series id, unit, cadence, what it is)
SERIES: Dict[str, tuple] = {
    "hy_oas":    ("BAMLH0A0HYM2", "pct",    "daily",  "ICE BofA US High Yield option-adjusted spread"),
    "ig_oas":    ("BAMLC0A0CM",   "pct",    "daily",  "ICE BofA US Corporate (investment grade) option-adjusted spread"),
    "sofr":      ("SOFR",         "pct",    "daily",  "Secured Overnight Financing Rate"),
    "iorb":      ("IORB",         "pct",    "daily",  "Interest rate on reserve balances"),
    "on_rrp":    ("RRPONTSYD",    "usd_bn", "daily",  "Overnight reverse repo, Treasury securities sold by the Fed"),
    "reserves":  ("WRESBAL",      "usd_mn", "weekly", "Reserve balances with Federal Reserve Banks (H.4.1, Wednesday level)"),
    "tga":       ("WTREGEN",      "usd_mn", "weekly", "Treasury General Account (H.4.1, Wednesday level)"),
    "usd_broad": ("DTWEXBGS",     "index",  "daily",  "Nominal broad US dollar index"),
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS slow_pressure (
    indicator     text             NOT NULL,
    obs_date      date             NOT NULL,
    value         double precision NOT NULL,
    first_value   double precision NOT NULL,
    unit          text,
    source        text             NOT NULL,
    first_seen_at timestamptz      NOT NULL DEFAULT now(),
    revised_at    timestamptz,
    backfill      boolean          NOT NULL DEFAULT false,
    PRIMARY KEY (indicator, obs_date)
);
"""

_UPSERT_SQL = """
INSERT INTO slow_pressure (indicator, obs_date, value, first_value, unit, source, first_seen_at, backfill)
VALUES (%(indicator)s, %(obs_date)s, %(value)s, %(value)s, %(unit)s, %(source)s, %(at)s, %(backfill)s)
ON CONFLICT (indicator, obs_date) DO UPDATE SET
    revised_at = CASE WHEN slow_pressure.value IS DISTINCT FROM EXCLUDED.value THEN EXCLUDED.first_seen_at
                      ELSE slow_pressure.revised_at END,
    value = EXCLUDED.value
"""


def parse_fred_csv(text: str) -> List[tuple]:
    """Pure: FRED graph CSV (header `observation_date,<ID>`; a missing value is a dot or empty) -> [(date, float)]."""
    out: List[tuple] = []
    rows = list(csv.reader(io.StringIO(text)))
    for r in rows[1:]:
        if len(r) < 2:
            continue
        v = r[1].strip()
        if v in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(r[0].strip()), float(v)))
        except ValueError:
            continue
    return out


def rows_from(indicator: str, obs: Iterable[tuple], at: datetime, backfill: bool) -> List[Dict[str, Any]]:
    """Pure: observations -> upsert rows for one indicator."""
    sid, unit, _, _ = SERIES[indicator]
    return [{"indicator": indicator, "obs_date": d, "value": v, "unit": unit, "source": f"FRED:{sid}", "at": at,
             "backfill": backfill} for d, v in obs]


def ensure_table() -> None:
    with connect() as con:
        con.cursor().execute(_CREATE_SQL)
        con.commit()


def upsert(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with connect() as con:
        cur = con.cursor()
        cur.executemany(_UPSERT_SQL, rows)
        con.commit()
    return len(rows)


def latest(indicator: str) -> Optional[date]:
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT max(obs_date) FROM slow_pressure WHERE indicator = %s", (indicator,))
        r = cur.fetchone()
    return r[0] if r else None


def series(indicator: str, since: Optional[date] = None, known_by: Optional[datetime] = None) -> List[tuple]:
    """[(obs_date, value, first_seen_at, backfill)], oldest first. `known_by` keeps only rows this store already held
    at that time (a point-in-time read; backfilled rows are filtered by obs_date instead, since their first_seen_at is
    the load time)."""
    q = "SELECT obs_date, value, first_seen_at, backfill FROM slow_pressure WHERE indicator = %s"
    a: list = [indicator]
    if since:
        q += " AND obs_date >= %s"
        a.append(since)
    if known_by:
        q += " AND ((backfill AND obs_date < %s::date) OR (NOT backfill AND first_seen_at <= %s))"
        a += [known_by, known_by]
    with connect() as con:
        cur = con.cursor()
        cur.execute(q + " ORDER BY obs_date", a)
        return cur.fetchall()


# ───────────────────────── the opening page (prompt-ready; behind config/slow_pressure.yaml) ─────────────────────────

PAGE_PURPOSE = ("Slow, price-independent pressure readings (credit, funding, reserves, dollar), given at the opening so they "
                "are not looked up again every session. They describe conditions, not direction: a level or a change supports "
                "a claim only through a stated transmission to this object in this window. Improvement and deterioration are "
                "reported alike; there is no composite index.")
PAGE_READING = ("Per series: latest value with its observation date, the change over the short and the longer step "
                "(daily series: 5 and 20 observations; weekly series: 1 and 4), and `pctile` = the share of this store's "
                "history up to the query time at or below the latest value (0-1; history length is in `n_hist`). "
                "`sofr_minus_iorb` is derived (percentage points; positive = secured funding trades above the floor rate).")


def summarise(obs: List[tuple], cadence: str) -> Optional[Dict[str, Any]]:
    """Pure: [(obs_date, value)] oldest first -> one page row (None when empty)."""
    if not obs:
        return None
    vals = [v for _, v in obs]
    last_d, last = obs[-1]
    s, l = (5, 20) if cadence == "daily" else (1, 4)
    chg = lambda n: round(last - vals[-1 - n], 4) if len(vals) > n else None
    return {"value": last, "obs_date": last_d.isoformat(), "chg_short": chg(s), "chg_long": chg(l), "steps": [s, l],
            "pctile": round(sum(1 for v in vals if v <= last) / len(vals), 3), "n_hist": len(vals)}


def page(as_of: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The opening page as of `as_of` (None = now): every series summarised on what this store knew by then."""
    out: Dict[str, Any] = {}
    raw: Dict[str, List[tuple]] = {}
    for ind, (_sid, unit, cadence, what) in SERIES.items():
        rows = [(d, v) for d, v, _seen, _bf in series(ind, known_by=as_of)]
        raw[ind] = rows
        row = summarise(rows, cadence)
        if row:
            out[ind] = {"what": what, "unit": unit, "cadence": cadence, **row}
    if not out:
        return None
    iorb = dict(raw.get("iorb") or [])
    spread = [(d, round(v - iorb[d], 4)) for d, v in raw.get("sofr") or [] if d in iorb]
    row = summarise(spread, "daily")
    if row:
        out["sofr_minus_iorb"] = {"what": "SOFR minus IORB", "unit": "pct_points", "cadence": "daily", **row}
    return {"source": "equipoise.pg.slow_pressure (FRED)", "purpose": PAGE_PURPOSE, "reading": PAGE_READING, "series": out}
