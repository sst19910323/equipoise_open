"""Read terminal events already recorded by the host service manager."""
import json
import re
import subprocess
from datetime import datetime, timezone


UNIT_INACTIVE = "7ad2d189f7e94e70a38c781354912448"
UNIT_FAILED = "d9b373ed55a64feb8242e02dbe79a49c"


def recent_completions():
    """Newest 20 duty service exits, or None when the journal cannot be read."""
    cmd = ["journalctl", "--no-pager", "--reverse", "--lines=20", "--unit=equipoise-codex-duty-*",
           "--output=json", "--output-fields=MESSAGE_ID,UNIT,INVOCATION_ID,UNIT_RESULT",
           "_PID=1", f"MESSAGE_ID={UNIT_INACTIVE}", f"MESSAGE_ID={UNIT_FAILED}"]
    try:
        output = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3)
        if output.stderr.strip():
            # journalctl can exit zero with a partial-view permissions warning.
            return None
        events = []
        for line in output.stdout.splitlines():
            if line == "-- No entries --":
                continue
            row = json.loads(line)
            unit = row.get("UNIT", "")
            job = re.fullmatch(r"equipoise-codex-duty-([a-z]+)-(.+)\.service", unit)
            if not job or row.get("MESSAGE_ID") not in (UNIT_FAILED, UNIT_INACTIVE):
                continue
            at = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc)
            events.append({"at": at.isoformat(), "invocation_id": row["INVOCATION_ID"], "unit": unit,
                           "market": job[1], "key": job[2],
                           "state": "failed" if row["MESSAGE_ID"] == UNIT_FAILED else "ended",
                           "reason": row.get("UNIT_RESULT")})
        return events
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, OverflowError):
        return None
