#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""next_review enrichment by the assembler (2026-11-06, owner's ruling on the report's re-evaluation block).

The arbiter writes `next_review.recommend_after` (the call time: when the duty loop should wake someone — early when
unsure) and `next_review.events[]` (the reasons: which expected future facts test which premise, how to check, what
then). Price trigger lines are retired. The machine adds two things the arbiter cannot or should not write:

- `expires_at`: the scenario tree's horizon end — as_of + T_days weekdays (exchange holidays are not known here; a
  routing quantity, not a ruler). Past it the tree has expired and a session is due regardless of divergence.
- `known_events[]`: events the machine can see on its own, for the gate's *warning* (never a verdict — whether an event
  matters is the arbiter's judgement; the warning only says "this was knowable and is not mentioned"):
  `earnings_expected` = the issuer's last release date + one quarter (91 days, ±7), from PG fundamentals;
  `macro` = scheduled releases in PG economic for the region whose release_time lies in the window.

Pure parts take explicit inputs so they can be tested; `enrich_next_review` wraps them with PG access and never raises.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

EARNINGS_CADENCE_DAYS = 91
EARNINGS_WINDOW_DAYS = 7
MIN_T_DAYS = 1


def parse_ts(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def add_weekdays(d: date, n: int) -> date:
    """d plus n weekdays (Mon-Fri), n >= 0."""
    out, left = d, max(0, int(n))
    while left > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out


def expires_at(as_of, T_days) -> Optional[str]:
    """ISO date of the horizon end: as_of's date + T_days weekdays. None without a usable as_of."""
    dt = parse_ts(as_of)
    if dt is None:
        return None
    try:
        T = max(MIN_T_DAYS, int(float(T_days))) if T_days is not None else 15
    except (TypeError, ValueError):
        T = 15
    return add_weekdays(dt.date(), T).isoformat()


def earnings_expected(last_release: Optional[date], as_of_d: date, end_d: date) -> Optional[Dict[str, Any]]:
    """The issuer's next report expected one quarter after its last release; returned only when the ±7-day window
    overlaps (as_of, end]."""
    if not last_release:
        return None
    exp = last_release + timedelta(days=EARNINGS_CADENCE_DAYS)
    lo, hi = exp - timedelta(days=EARNINGS_WINDOW_DAYS), exp + timedelta(days=EARNINGS_WINDOW_DAYS)
    if hi < as_of_d or lo > end_d:
        return None
    return {"kind": "earnings_expected", "date": exp.isoformat(), "window": [lo.isoformat(), hi.isoformat()],
            "basis": f"quarterly cadence from the last release {last_release.isoformat()} (+{EARNINGS_CADENCE_DAYS}d, ±{EARNINGS_WINDOW_DAYS}d)"}


def macro_events(rows: List[Dict[str, Any]], as_of_d: date, end_d: date) -> List[Dict[str, Any]]:
    """rows: [{indicator, release_time(date|str)}] → distinct (indicator, date) inside (as_of, end], sorted."""
    seen, out = set(), []
    for r in rows or []:
        rt = r.get("release_time")
        try:
            d = rt if isinstance(rt, date) else datetime.fromisoformat(str(rt)[:10]).date()
        except ValueError:
            continue
        if d <= as_of_d or d > end_d:
            continue
        key = (str(r.get("indicator")), d.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": "macro", "date": d.isoformat(), "name": str(r.get("indicator")), "region": r.get("region"),
                    **({"basis": r["reason"]} if r.get("reason") else {})})
    return sorted(out, key=lambda e: (e["date"], e["name"]))


def build(next_review: Optional[Dict[str, Any]], as_of, T_days, last_release: Optional[date],
          macro_rows: List[Dict[str, Any]], note: Optional[str] = None) -> Dict[str, Any]:
    """Pure: the arbiter's next_review plus expires_at and known_events (and `known_events_note` when the sources were
    unavailable). Retired fields are left as the arbiter wrote them — the validator warns about them."""
    nr = dict(next_review or {})
    exp = expires_at(as_of, T_days)
    nr["expires_at"] = exp
    dt = parse_ts(as_of)
    known: List[Dict[str, Any]] = []
    if dt is not None and exp:
        as_of_d, end_d = dt.date(), date.fromisoformat(exp)
        e = earnings_expected(last_release, as_of_d, end_d)
        if e:
            known.append(e)
        known += macro_events(macro_rows, as_of_d, end_d)
    nr["known_events"] = known
    if note:
        nr["known_events_note"] = note
    return nr


def macro_rows_from_schedule(schedule: dict, region: str) -> List[Dict[str, Any]]:
    """config/update_schedule.yaml → rows for macro_events: each indicator's `next.time.at` (the next scheduled release;
    the refresh runs right after it) as release_time. PG economic holds only published values, so the schedule is the
    only forward calendar the machine has (GPT review, 11-06)."""
    econ = (schedule or {}).get("economic") or {}
    reg = str(region).upper()
    block = econ.get(reg) or econ.get({"EU": "EZ", "EZ": "EU"}.get(reg, reg)) or {}
    rows = []
    for ind, spec in (block.items() if isinstance(block, dict) else []):
        at = (((spec or {}).get("next") or {}).get("time") or {}).get("at") if isinstance(spec, dict) else None
        dt = parse_ts(at)
        if dt is None:
            continue
        rows.append({"region": reg, "indicator": str(ind), "release_time": dt.date(),
                     "reason": (((spec or {}).get("next") or {}).get("time") or {}).get("reason")})
    return rows


def _sources(symbol: str, region: str, as_of_d: date, end_d: date, root: Optional[Path] = None):
    """(last_release, macro_rows, note): the issuer's last release from PG fundamentals; the macro calendar from
    config/update_schedule.yaml. Each failure is noted, neither raises."""
    notes = []
    last = None
    try:
        from datalayer.pg.db import connect
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT max(release_date) FROM fundamentals WHERE symbol = %s AND release_date <= %s",
                        (symbol.upper(), as_of_d))
            row = cur.fetchone()
            last = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pg fundamentals unavailable ({type(exc).__name__}): expected earnings unknown")
    rows: List[Dict[str, Any]] = []
    try:
        import yaml
        p = (root or Path(__file__).resolve().parents[1]) / "config" / "update_schedule.yaml"
        rows = macro_rows_from_schedule(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, region)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"update_schedule.yaml unavailable ({type(exc).__name__}): macro calendar unknown")
    return last, rows, ("; ".join(notes) or None)


def enrich_next_review(next_review: Optional[Dict[str, Any]], as_of, T_days, symbol: str, region: str = "US") -> Dict[str, Any]:
    """The assembler's entry: never raises; without PG the block still gets expires_at and an empty known_events with a note."""
    try:
        dt = parse_ts(as_of)
        exp = expires_at(as_of, T_days)
        if dt is None or not exp:
            return build(next_review, as_of, T_days, None, [], "as_of unparseable: known events not computed")
        last, rows, note = _sources(symbol, region, dt.date(), date.fromisoformat(exp))
        return build(next_review, as_of, T_days, last, rows, note)
    except Exception as exc:  # noqa: BLE001
        return build(next_review, as_of, T_days, None, [], f"enrichment failed ({type(exc).__name__})")
