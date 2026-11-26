"""duty_store — the duty loop's memory in PG: one append-only table `duty_events` (2026-11-04).

Rows: `breath` (one per run of scripts/duty_status.py: the status snapshot, pruned after `BREATH_KEEP_DAYS`),
`decision` (one per duty-Codex decision, after the trusted layer verified it: accepted launches / defers, drops, the
updated skip counts), `launch` (one per background unit started). Only trusted scripts write: duty_status.py (breath),
duty_record.py via duty.sh (decision, launch). Hermes never writes. Readers: duty_status.py (skips, active defers) and
the `/api/duty` endpoint. The `data/status/*.json` files remain as debugging logs, not as the source of truth.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import connect

KINDS = ("breath", "decision", "launch")
BREATH_KEEP_DAYS = 14

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS duty_events (
    id       bigserial   PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    wake     text        NOT NULL,
    kind     text        NOT NULL CHECK (kind IN ('breath', 'decision', 'launch')),
    harness  text,
    symbol   text,
    market   text,
    payload  jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS duty_events_kind_at ON duty_events (kind, at DESC);
CREATE INDEX IF NOT EXISTS duty_events_wake ON duty_events (wake);
"""


def ensure_table() -> None:
    with connect() as con, con.cursor() as cur:
        cur.execute(_CREATE_SQL)


def record(kind: str, wake: str, payload: Dict[str, Any], harness: Optional[str] = None,
           symbol: Optional[str] = None, market: Optional[str] = None) -> int:
    """Append one event; returns its id. `wake` is the breath's UTC stamp (yyyymmddTHHMMSSZ) so a breath, its decision
    and its launches can be joined."""
    if kind not in KINDS:
        raise ValueError(f"unknown duty event kind {kind!r}")
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO duty_events (wake, kind, harness, symbol, market, payload) VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
            (wake, kind, harness, symbol, market, json.dumps(payload, ensure_ascii=False, default=str)))
        return int(cur.fetchone()[0])


def prune_breaths(days: int = BREATH_KEEP_DAYS) -> int:
    with connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM duty_events WHERE kind = 'breath' AND at < now() - make_interval(days => %s)", (int(days),))
        return cur.rowcount


def _rows(cur) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def recent(kind: str, limit: int = 20, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Newest first."""
    q = "SELECT id, at, wake, kind, harness, symbol, market, payload FROM duty_events WHERE kind = %s"
    args: list = [kind]
    if since is not None:
        q += " AND at >= %s"; args.append(since)
    q += " ORDER BY at DESC, id DESC LIMIT %s"; args.append(int(limit))
    with connect() as con, con.cursor() as cur:
        cur.execute(q, args)
        return _rows(cur)


def latest_breath() -> Optional[Dict[str, Any]]:
    r = recent("breath", 1)
    return r[0] if r else None


def latest_wake() -> Optional[Dict[str, Any]]:
    """The last breath that woke the duty Codex (exit 0): {at, wake, due: [symbols], free} — the wake-cooldown input
    (duty_status.py: no new due name and no more free slots within wake_cooldown_minutes → stay quiet)."""
    q = ("SELECT at, wake, payload FROM duty_events WHERE kind = 'breath' AND payload->>'exit' = '0' "
         "ORDER BY at DESC, id DESC LIMIT 1")
    with connect() as con, con.cursor() as cur:
        cur.execute(q)
        row = cur.fetchone()
    if not row:
        return None
    at, wake, p = row
    due = sorted({d.get("symbol") for m in (p.get("markets") or {}).values() for d in (m.get("due") or []) if d.get("symbol")})
    return {"at": at, "wake": wake, "due": due, "free": ((p.get("capacity") or {}).get("free"))}


def active_dismissals(now: Optional[datetime] = None, lookback_days: int = 30) -> Dict[str, Dict[str, Any]]:
    """symbol → the newest accepted dismissal (a duty Codex struck out the report's call point) with no launch of that
    symbol since: {last_run, why, harness, wake, at}. The status script voids it itself when the report changed."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    decisions = recent("decision", 5000, since)
    out: Dict[str, Dict[str, Any]] = {}
    for d in decisions:                                   # newest first → first seen per symbol wins
        for e in ((d.get("payload") or {}).get("accepted") or {}).get("dismiss") or []:
            s = e.get("symbol")
            if not s or s in out:
                continue
            # a launch does not void it (the launch may have failed before a report); the status script voids it
            # when the report's last_run differs from the one recorded here
            out[s] = {"last_run": e.get("last_run"), "why": e.get("why"), "reasons": e.get("reasons") or [],
                      "harness": d.get("harness"), "wake": d.get("wake"),
                      "at": d["at"].isoformat() if hasattr(d["at"], "isoformat") else str(d["at"])}
    return out


def latest_skips() -> Dict[str, int]:
    """The skip counts as of the last verified decision (the trusted layer writes them there)."""
    r = recent("decision", 1)
    if not r:
        return {}
    return {k: int(v) for k, v in ((r[0].get("payload") or {}).get("skips") or {}).items()}


def active_defers(now: Optional[datetime] = None, lookback_hours: int = 48) -> Dict[str, Dict[str, Any]]:
    """symbol → the newest accepted defer still in force: `until` in the future and no launch of that symbol since."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)
    decisions = recent("decision", 500, since)
    launches = recent("launch", 500, since)
    last_launch: Dict[str, datetime] = {}
    for l in launches:
        for s in (l.get("payload") or {}).get("symbols") or ([l["symbol"]] if l.get("symbol") else []):
            if s not in last_launch or l["at"] > last_launch[s]:
                last_launch[s] = l["at"]
    out: Dict[str, Dict[str, Any]] = {}
    for d in decisions:                                   # newest first → first seen per symbol wins
        for e in ((d.get("payload") or {}).get("accepted") or {}).get("defer") or []:
            s = e.get("symbol")
            if not s or s in out:
                continue
            try:
                until = datetime.fromisoformat(str(e.get("until")).replace("Z", "+00:00"))
            except Exception:
                continue
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                continue
            if s in last_launch and last_launch[s] > d["at"]:
                continue
            out[s] = {"until": until.isoformat(), "why": e.get("why"), "harness": d.get("harness"), "wake": d.get("wake"),
                      "at": d["at"].isoformat()}
    return out
