"""divergence_log — the duty loop's divergence readings kept for calibration (2026-11-06, owner: "record the divergence
across periods"; thresholds dead_tail / drift_tail in config/duty.yaml are provisional until two weeks of samples exist).

One table `divergence_log`, one row per (symbol, report as_of, lens, trading day): the latest reading of the day plus
the day's extremes (`tail_min`, `level_worst`), upserted by every breath of scripts/duty_status.py from the rows
pre_round_brief.pull produced. Breaths come every ten minutes, so the day row is rewritten ~40 times and the table
grows by one row per report per lens per day. Never pruned: the calibration joins it with postcheck scores by
(symbol, as_of) to see whether "tree dead on day t" went with wrong-footed reports. Only duty_status.py writes;
readers: scripts/divergence_history.py (owner's view) and the future calibration script.

The row builder `rows_from` is pure (tested); `record` needs PG and never raises past its caller's guard.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .db import connect

LEVEL_RANK = ("inside", "drift", "dead", "expired")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS divergence_log (
    symbol      text        NOT NULL,
    as_of       timestamptz NOT NULL,
    lens        text        NOT NULL DEFAULT '',
    day         date        NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    t           double precision,
    t_days      integer,
    price       double precision,
    p0          double precision,
    move_atr    double precision,
    sigma_pct   double precision,
    tail        double precision,
    kl_bits     double precision,
    alive       double precision,
    dominant    text,
    level       text,
    severity    double precision,
    n_nodes     integer,
    tail_min    double precision,
    level_worst text,
    source      text        NOT NULL DEFAULT 'breath',
    PRIMARY KEY (symbol, as_of, lens, day)
);
CREATE INDEX IF NOT EXISTS divergence_log_symbol_day ON divergence_log (symbol, day DESC);
"""

_UPSERT_SQL = """
INSERT INTO divergence_log (symbol, as_of, lens, day, at, t, t_days, price, p0, move_atr, sigma_pct, tail, kl_bits, alive,
                            dominant, level, severity, n_nodes, tail_min, level_worst, source)
VALUES (%(symbol)s, %(as_of)s, %(lens)s, %(day)s, %(at)s, %(t)s, %(t_days)s, %(price)s, %(p0)s, %(move_atr)s, %(sigma_pct)s,
        %(tail)s, %(kl_bits)s, %(alive)s, %(dominant)s, %(level)s, %(severity)s, %(n_nodes)s, %(tail)s, %(level)s, %(source)s)
ON CONFLICT (symbol, as_of, lens, day) DO UPDATE SET
    at = EXCLUDED.at, t = EXCLUDED.t, t_days = EXCLUDED.t_days, price = EXCLUDED.price, p0 = EXCLUDED.p0,
    move_atr = EXCLUDED.move_atr, sigma_pct = EXCLUDED.sigma_pct, tail = EXCLUDED.tail, kl_bits = EXCLUDED.kl_bits,
    alive = EXCLUDED.alive, dominant = EXCLUDED.dominant, level = EXCLUDED.level, severity = EXCLUDED.severity,
    n_nodes = EXCLUDED.n_nodes, source = EXCLUDED.source,
    tail_min = LEAST(divergence_log.tail_min, EXCLUDED.tail_min),
    level_worst = CASE WHEN COALESCE(array_position(ARRAY['inside','drift','dead','expired'], EXCLUDED.level), 0)
                          > COALESCE(array_position(ARRAY['inside','drift','dead','expired'], divergence_log.level_worst), 0)
                       THEN EXCLUDED.level ELSE divergence_log.level_worst END
"""


def ensure_table() -> None:
    with connect() as con, con.cursor() as cur:
        cur.execute(_CREATE_SQL)


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _one(symbol: str, as_of, lens: str, d: Dict[str, Any], now: datetime, day: date, source: str) -> Dict[str, Any]:
    t = _num(d.get("t"))
    return {"symbol": symbol, "as_of": as_of, "lens": lens or "", "day": day, "at": now, "t": t,
            "t_days": int(d["T_days"]) if _num(d.get("T_days")) is not None else None,
            "price": _num(d.get("price")), "p0": _num(d.get("P0")), "move_atr": _num(d.get("move_atr")),
            "sigma_pct": _num(d.get("sigma_pct")), "tail": _num(d.get("tail")), "kl_bits": _num(d.get("kl_bits")),
            "alive": _num(d.get("alive")), "dominant": d.get("dominant"), "level": d.get("level"),
            "severity": _num(d.get("severity")),
            "n_nodes": int(d["n_nodes"]) if _num(d.get("n_nodes")) is not None else None, "source": source}


def rows_from(brief_rows: Iterable[Dict[str, Any]], now: datetime, source: str = "breath") -> List[Dict[str, Any]]:
    """Pure: pre_round_brief.pull rows → one log row per lens (single-tree reports → lens ''). Rows without a
    divergence block or without `as_of` are skipped. `day` = the day the bars reached (`bars_through`), else today."""
    out: List[Dict[str, Any]] = []
    for r in brief_rows or []:
        d = r.get("divergence")
        as_of = r.get("as_of")
        if not isinstance(d, dict) or not as_of or not r.get("symbol"):
            continue
        try:
            day = date.fromisoformat(str(r.get("bars_through") or now.date().isoformat())[:10])
        except ValueError:
            day = now.date()
        sym = str(r["symbol"]).upper()
        by = d.get("by_lens")
        if isinstance(by, dict) and by:
            for lens, dd in by.items():
                if isinstance(dd, dict):
                    out.append(_one(sym, as_of, str(lens), dd, now, day, source))
        else:
            out.append(_one(sym, as_of, "", d, now, day, source))
    return out


def record(brief_rows: Iterable[Dict[str, Any]], now: Optional[datetime] = None, source: str = "breath") -> int:
    """Upsert the day rows; returns how many were written. Raises on PG trouble (the caller guards)."""
    now = now or datetime.now(timezone.utc)
    rows = rows_from(brief_rows, now, source)
    if not rows:
        return 0
    with connect() as con, con.cursor() as cur:
        cur.execute(_CREATE_SQL)
        cur.executemany(_UPSERT_SQL, rows)
    return len(rows)


def series(symbol: str, as_of=None, days: int = 30) -> List[Dict[str, Any]]:
    """Day rows for one symbol (newest report first, then day), for the owner's view and the calibration."""
    q = ("SELECT symbol, as_of, lens, day, at, t, t_days, price, p0, move_atr, tail, kl_bits, alive, dominant, level, severity, "
         "tail_min, level_worst FROM divergence_log WHERE symbol = %s AND day >= current_date - %s")
    args: list = [symbol.upper(), int(days)]
    if as_of is not None:
        q += " AND as_of = %s"
        args.append(as_of)
    q += " ORDER BY as_of DESC, lens, day"
    with connect() as con, con.cursor() as cur:
        cur.execute(q, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
