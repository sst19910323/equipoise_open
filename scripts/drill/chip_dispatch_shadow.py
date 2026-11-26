"""Replay duty snapshots for the five-stock pilot. Writes observations only."""
import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


MEMBERS = {"MU", "NVDA", "AMD", "AVGO", "TSM"}
UNITS = 2.0
NY = ZoneInfo("America/New_York")


def observation(snapshot):
    harness = snapshot.get("recommended_harness")
    budgets = {name: entry.get("debates_left") for name, entry in
               (snapshot.get("usage") or {}).items()
               if isinstance(entry, dict) and entry.get("available")}
    row = {"at": snapshot["at"], "state": "no_us_data", "dispatch_reason": snapshot.get("exit_reason"),
           "selected_harness": harness, "available_single_units": budgets, "estimated_group_units": UNITS}
    market = (snapshot.get("markets") or {}).get("us") or {}
    if not isinstance(market.get("due"), list):
        return row
    local = datetime.fromisoformat(snapshot["at"]).astimezone(NY)
    if local.weekday() >= 5 or not 4 <= local.hour < 20:
        row["state"] = "outside_window"
        return row
    signals = {}
    for item in market["due"]:
        symbol = item["symbol"]
        reasons = item.get("reasons") or []
        if symbol in MEMBERS and any(r.startswith(("tree dead", "drift")) for r in reasons):
            signals[symbol] = {
                "level": "dead" if any(r.startswith("tree dead") for r in reasons) else "drift",
                "severity": item.get("severity"), "report": item.get("last_run"),
                "events": [r for r in reasons if r.startswith("earnings landed")]}
    urgent = any(s["level"] == "dead" for s in signals.values()) or len(signals) >= 3
    state = "priority" if urgent else "watch" if len(signals) == 2 else "below_threshold"
    busy = sorted({s for unit in snapshot.get("running", []) for s in unit.get("symbols", [])} & MEMBERS)
    remaining = budgets.get(harness)
    affordable = remaining >= UNITS if isinstance(remaining, (int, float)) else None
    row.update(state=state, members=signals, running_members=busy, affordable=affordable,
               slot_open=(snapshot.get("capacity") or {}).get("launch_cap", 0) > 0)
    return row


def replay(status, start, end):
    seen = set()
    previous = None
    for path in sorted(status.glob("duty_status_*.json")):
        stamp = path.stem.removeprefix("duty_status_")
        if not start <= stamp < end:
            continue
        try:
            row = observation(json.loads(path.read_text(encoding="utf-8")))
            now = datetime.fromisoformat(row["at"]).astimezone(NY)
        except PermissionError:
            yield {"file": path.name, "state": "restricted_snapshot"}
            continue
        except (ValueError, KeyError, TypeError) as exc:
            yield {"file": path.name, "state": "invalid_snapshot", "error": str(exc)}
            continue
        if previous is None or now.date() != previous.date() or now - previous > timedelta(hours=1):
            seen.clear()
        if row["state"] in ("priority", "watch", "below_threshold"):
            previous = now
            if row["state"] == "below_threshold":
                seen.clear()
        row["would_queue"] = False
        if row["state"] == "priority":
            active = {(sym, s["report"], s["level"], tuple(s["events"])) for sym, s in row["members"].items()}
            new = active - seen
            row["new_signals"] = sorted({k[0] for k in new})
            if new and row["affordable"] and row["slot_open"] and not row["running_members"]:
                row["would_queue"] = True
                seen.update(active)
                seen.update((sym, report, "drift", events) for sym, report, level, events in active
                            if level == "dead")
        yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", type=Path)
    parser.add_argument("start", help="Inclusive UTC timestamp YYYYMMDDTHHMMSSZ")
    parser.add_argument("end", help="Exclusive UTC timestamp YYYYMMDDTHHMMSSZ")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    start, end = [datetime.strptime(t, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                  for t in (args.start, args.end)]
    if end <= start or not args.status.is_dir():
        parser.error("Expected an existing snapshot directory and increasing UTC bounds.")
    rows = list(replay(args.status, args.start, args.end))
    count = sum(r.get("would_queue", False) for r in rows)
    summary = {"start": args.start, "end": args.end, "evaluated_at": datetime.now(timezone.utc).isoformat(),
               "snapshots": len(rows), "states": dict(Counter(r["state"] for r in rows)),
               "would_queue": count, "estimated_units": UNITS * count}
    args.output.mkdir(exist_ok=True, parents=True)
    for name, content in (("observations.jsonl", "".join(json.dumps(r) + "\n" for r in rows)),
                          ("summary.json", json.dumps(summary, indent=2) + "\n")):
        tmp = args.output / (name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(args.output / name)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
