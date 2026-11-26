#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""duty_record.py — the trusted layer's bookkeeping for one duty breath (called by scripts/duty.sh; zero LLM).

  verify STATUS DECISION HARNESS CAP      check the duty Codex's decision against the status it was given: only symbols
                                          on a due list, at most CAP in total, the same identity; defers only for due
                                          symbols, `until` in the future and within `max_defer_hours` (config/duty.yaml);
                                          update the skip counts (due and not launched → +1, launched → 0); record one
                                          `decision` event in PG; print one "MARKET SYM,SYM" line per market to launch
                                          (stdout — duty.sh reads them; refusals and drops go to stderr and the event).
  launch WAKE HARNESS MARKET SYMS UNIT    record one `launch` event (a background unit was started).

The rules live here and in config/duty.yaml — not in duty.sh and not in the Codex task text (.codex/duty.md only
describes the decision file). A decision that fails verification is still recorded (with `refused`), so the owner can
see what the Codex wanted. PG unreachable → the decision is applied but not recorded (stderr says so).
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _parse_until(text: Any) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


DISMISSABLE_PREFIXES = ("review time passed", "earnings landed", "order expired")


def dismiss_key(reason) -> str | None:
    """The identity of a dismissable reason: `review time passed` is one call point per report (the day count moves),
    an event or an order state is its own text; hard / measured reasons (tree dead / expired, drift, backstop) → None."""
    r = str(reason or "")
    if r.startswith("review time passed"):
        return "review time passed"
    if r.startswith(("earnings landed", "order expired")):
        return r.strip()
    return None


def verify_decision(status: Dict[str, Any], decision: Dict[str, Any], harness: str, cap: int, now: datetime,
                    max_defer_hours: float, prev_skips: Dict[str, int], universe: Dict[str, set]) -> Dict[str, Any]:
    """Pure: → {"refused": str|None, "launch": [{market, symbols}], "defer": [{symbol, until, why, clamped}],
    "dropped": [{what, symbol(s), why}], "skips": {symbol: n}}. `universe` = market → its symbols (symbols.yaml waves).
    A launch is allowed for any symbol of that market that is not running (the duty Codex may see something the due
    list missed), only while the market is inside its data window, at most `cap` in total. Defers only for due names.
    Skips are updated only when the decision is accepted."""
    out: Dict[str, Any] = {"refused": None, "launch": [], "defer": [], "dismiss": [], "dropped": [], "skips": dict(prev_skips or {})}
    if decision.get("harness") != harness:
        out["refused"] = f"decision harness {decision.get('harness')!r} != {harness!r}"
        return out
    markets = status.get("markets") or {}
    due = {m: {d["symbol"] for d in (v.get("due") or [])} for m, v in markets.items()}
    all_due = {s for ss in due.values() for s in ss}
    running = {s for u in (status.get("running") or []) for s in (u.get("symbols") or [])}
    n = 0
    entries = decision.get("launch") or []
    if "dispatch_jobs" in status:
        jobs = status["dispatch_jobs"]
        usage = status.get("usage", {}).get(harness) or {}
        balance = usage.get("units_left", 0) if usage.get("available") else 0
        if status.get("recommended_harness") != harness or not isinstance(balance, (int, float)) or not math.isfinite(balance):
            out["refused"] = "identity or unit budget unavailable"
            return out
        for entry in entries:
            market = entry.get("market")
            for key in entry.get("units", []):
                job = jobs.get(key)
                if (not job or job["market"] != market or not markets.get(market, {}).get("market", {}).get("in_window")
                        or not set(job["members"]) <= universe.get(market, set()) or running.intersection(job["members"])
                        or n >= cap or job["cost_units"] > balance):
                    out["dropped"].append(dict(what="launch", symbol=key, why="outside window, roster, free slots or unit budget, or already running"))
                    continue
                out["launch"].append(dict(market=market, key=key, flag=job["flag"], symbols=job["members"], cost_units=job["cost_units"]))
                running.update(job["members"])
                balance -= job["cost_units"]
                n += 1
            if entry.get("symbols"):
                out["dropped"].append(dict(what="launch", symbols=entry["symbols"], why="use launch[].units from dispatch_jobs"))
        entries = []
    for e in entries:
        m = e.get("market"); syms = [str(s) for s in (e.get("symbols") or [])]
        if not (((markets.get(m) or {}).get("market") or {}).get("in_window", False)):
            out["dropped"].append({"what": "launch", "market": m, "symbols": syms, "why": "market outside its data window, dropped"})
            continue
        allowed = set(universe.get(m) or set()) - running
        ok = [s for s in syms if s in allowed]
        bad = [s for s in syms if s not in allowed]
        if bad:
            out["dropped"].append({"what": "launch", "market": m, "symbols": bad, "why": "not in market or already running, dropped"})
        room = max(0, int(cap) - n)
        over, ok = ok[room:], ok[:room]
        if over:
            out["dropped"].append({"what": "launch", "market": m, "symbols": over, "why": f"over launch_cap {cap}"})
        n += len(ok)
        if ok:
            out["launch"].append({"market": m, "symbols": ok})
    launched = {s for e in out["launch"] for s in e["symbols"]}
    for d in decision.get("defer") or []:
        s = str(d.get("symbol") or "")
        if s not in all_due:
            out["dropped"].append({"what": "defer", "symbol": s, "why": "not on the due list"}); continue
        if s in launched:
            out["dropped"].append({"what": "defer", "symbol": s, "why": "also launched"}); continue
        until = _parse_until(d.get("until"))
        if until is None:
            out["dropped"].append({"what": "defer", "symbol": s, "why": f"until unparseable: {d.get('until')!r}"}); continue
        if until <= now:
            out["dropped"].append({"what": "defer", "symbol": s, "why": "until already passed"}); continue
        clamped = False
        cap_t = now + timedelta(hours=float(max_defer_hours))
        if until > cap_t:
            until, clamped = cap_t, True
        out["defer"].append({"symbol": s, "until": until.isoformat(), "why": d.get("why"), "clamped": clamped})
    # 11-06 (owner): the duty Codex may strike out a report's call point it judged wrong — only a due name, not one it
    # launched, with a reason; the status script stops counting that report's authored / event reasons (hard ones stay)
    due_items = {d["symbol"]: d for v in markets.values() for d in (v.get("due") or []) if isinstance(d, dict)}
    for d in decision.get("dismiss") or []:
        s = str(d.get("symbol") or "")
        if s not in all_due:
            out["dropped"].append({"what": "dismiss", "symbol": s, "why": "not on the due list"}); continue
        if s in launched:
            out["dropped"].append({"what": "dismiss", "symbol": s, "why": "also launched"}); continue
        if not str(d.get("why") or "").strip():
            out["dropped"].append({"what": "dismiss", "symbol": s, "why": "a dismissal needs a why"}); continue
        it = due_items.get(s) or {}
        struck = sorted({dismiss_key(r) for r in (it.get("reasons") or []) if dismiss_key(r)})
        if not struck:
            out["dropped"].append({"what": "dismiss", "symbol": s, "why": "nothing dismissable (only hard / measured reasons)"}); continue
        out["dismiss"].append({"symbol": s, "why": d.get("why"), "last_run": it.get("last_run"), "reasons": struck})
    dismissed = {d["symbol"] for d in out["dismiss"]}
    skips = {k: int(v) for k, v in (prev_skips or {}).items()}
    for s in all_due:
        skips[s] = 0 if (s in launched or s in dismissed) else skips.get(s, 0) + 1
    out["skips"] = {k: v for k, v in skips.items() if v > 0 and k in all_due}
    return out


def _cfg() -> Dict[str, Any]:
    import yaml
    p = ROOT / "config" / "duty.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}


def _record(kind: str, wake: str, payload: Dict[str, Any], **kw) -> None:
    try:
        from datalayer.pg import duty_store
        duty_store.ensure_table()
        duty_store.record(kind, wake, payload, **kw)
    except Exception as exc:                                  # the loop must not stall on bookkeeping
        print(f"# duty_record: PG write failed ({type(exc).__name__}: {exc}); event not recorded", file=sys.stderr)


def cmd_verify(argv: List[str]) -> int:
    status_path, decision_path, harness, cap = argv[0], argv[1], argv[2], int(argv[3])
    status = json.loads(Path(status_path).read_text(encoding="utf-8"))
    decision = json.loads(Path(decision_path).read_text(encoding="utf-8"))
    if Path(str(decision.get("status", ""))).resolve() != Path(status_path).resolve():
        print("# refused decision for a different wake", file=sys.stderr)
        return 1
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    try:
        from datalayer.pg import duty_store
        prev = duty_store.latest_skips()
    except Exception as exc:
        print(f"# duty_record: PG read failed ({type(exc).__name__}); skip counts start from zero", file=sys.stderr)
        prev = {}
    try:
        from workflows.symbol_universe import dispatch_jobs, load_symbol_config, market_waves
        config = load_symbol_config()
        jobs = dispatch_jobs(config)
        if "dispatch_jobs" not in status or any(
                {k:v for k,v in j.items() if k != "due_members"} != jobs.get(key)
                for key,j in status["dispatch_jobs"].items()) or set(status["dispatch_jobs"]) != set(jobs):
            raise ValueError("dispatch roster changed since wake")
        from scripts.duty_status import running_units, market_state
        from scripts.duty_quota import live_quota, availability
        from workflows.harness_resolver import resolve_codex
        if status.get("exit") != 0 or status.get("ibkr", {}).get("probe_exit") != 0 or harness not in cfg["weekly_units_at_85"]:
            raise ValueError("wake, broker or account not eligible")
        active = running_units(jobs)
        if any(u.get("harness") != harness for u in active):
            raise ValueError("another identity is active")
        status["running"] = active
        for market, data in status["markets"].items():
            current = market_state(market.upper(), now)
            if current["state"] == "closed" and (current.get("minutes_since_close") or 0) > cfg.get("after_close_grace_minutes", 90):
                data["market"]["in_window"] = False
        account = resolve_codex(harness)
        quota = live_quota(account.executable, account.codex_home, harness)
        u = status["usage"][harness]
        if not availability(quota, u["stop_used_pct"])[0]:
            raise ValueError("quota unavailable at launch")
        reserved = sum(x["cost_units"] for x in active if x.get("harness") == harness)
        fresh_left = (u["stop_used_pct"] - quota["used_percent"]) * cfg["weekly_units_at_85"][harness] / 85 - reserved
        u["units_left"] = min(u["units_left"], max(0, fresh_left))
        cap = min(cap, max(0, cfg["max_parallel"] - sum(max(1,len(u["symbols"])) if u["kind"] == "dispatcher" else 1 for u in active)))
        universe = {m: {s for _l, ss in g for s in ss} for m, g in market_waves(config).items()}
    except Exception as exc:
        print(f"# duty_record: no dispatch: {exc}", file=sys.stderr)
        return 1
    res = verify_decision(status, decision, harness, cap, now, float(cfg.get("max_defer_hours", 24)), prev, universe)
    wake = Path(status_path).stem.replace("duty_status_", "")
    payload = {"status": status_path, "decision_file": decision_path, "decision": decision, "launch_cap": cap,
               "refused": res["refused"], "accepted": {"launch": res["launch"], "defer": res["defer"], "dismiss": res["dismiss"]},
               "dropped": res["dropped"], "skips": res["skips"]}
    _record("decision", wake, payload, harness=harness)
    if res["refused"]:
        print(f"# {res['refused']}; refused", file=sys.stderr)
        return 0
    for d in res["dropped"]:
        print(f"# dropped {d}", file=sys.stderr)
    for d in res["defer"]:
        print(f"# defer {d['symbol']} until {d['until']}" + (" (clamped)" if d["clamped"] else ""), file=sys.stderr)
    for d in res["dismiss"]:
        print(f"# dismiss {d['symbol']}: {d['why']}", file=sys.stderr)
    for e in res["launch"]:
        print(e["market"], e["key"], e["flag"], ",".join(e["symbols"]), e["cost_units"])
    return 0


def cmd_launch(argv: List[str]) -> int:
    wake, harness, market, syms, unit = argv[0], argv[1], argv[2], argv[3], argv[4]
    symbols = [s for s in syms.split(",") if s]
    details = {"key": argv[5], "cost_units": float(argv[6])} if len(argv) >= 7 else {}
    _record("launch", wake, {"unit": unit, "symbols": symbols, "market": market, **details}, harness=harness, market=market,
            symbol=symbols[0] if len(symbols) == 1 else None)
    return 0


def main(argv: List[str]) -> int:
    if len(argv) >= 5 and argv[0] == "verify":
        return cmd_verify(argv[1:])
    if len(argv) >= 6 and argv[0] == "launch":
        return cmd_launch(argv[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
