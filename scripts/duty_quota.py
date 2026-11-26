"""Read account quota without starting a model turn or spending reset credits.

Hermes uses each registered executable with its own home. No auth files are read,
copied or logged. The stdio server is always reaped, including on timeout.
"""
from __future__ import annotations

import json
import math
import os
import selectors
import subprocess
import time
from datetime import datetime, timezone


def read_account_limits(exe: str, home: str, harness: str, timeout: float = 15) -> dict:
    env = dict(os.environ, CODEX_HOME=home, EQUIPOISE_HARNESS=harness)
    p = subprocess.Popen([exe, "app-server", "--stdio"], env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    pending = b""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)

            def send(msg):
                p.stdin.write((json.dumps(msg) + "\n").encode())
                p.stdin.flush()

            def receive(request_id):
                nonlocal pending
                while True:
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        rec = json.loads(line)
                        if rec.get("id") == request_id:
                            if "error" in rec:
                                # Do not expose backend error text or account information.
                                raise RuntimeError("quota RPC failed")
                            return rec["result"]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("quota read timed out")
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("quota server exited")
                    pending += chunk
                    if len(pending) > 2_000_000:
                        raise ValueError("quota response too large")

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "equipoise_duty_quota", "version": "1.0"},
                "capabilities": None}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read", "params": {
                "supportsLunaReserve": False, "excludeResetCreditDetails": True}})
            return receive(2)
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        p.stdin.close()
        p.stdout.close()


def normalize_limits(result: dict, now: datetime) -> dict:
    """Select the weekly window by duration, never by primary/secondary position."""
    if not isinstance(result.get("ordinaryUsageAllowed"), bool):
        raise ValueError("backend usage permission unavailable")
    buckets = result.get("rateLimitsByLimitId") or {}
    snapshot = buckets.get("codex") or result.get("rateLimits") or {}
    windows = []
    for key in ("primary", "secondary"):
        w = snapshot.get(key)
        if not w:
            continue
        used = float(w["usedPercent"])
        duration = int(w["windowDurationMins"])
        reset = datetime.fromtimestamp(w["resetsAt"], timezone.utc)
        if not math.isfinite(used) or not 0 <= used <= 100 or duration <= 0 or reset <= now:
            raise ValueError("invalid or expired quota window")
        windows.append({"used_percent": used, "window_minutes": duration,
                        "resets_at": reset.isoformat()})
    weekly = next((w for w in windows if w["window_minutes"] == 10080), None)
    if weekly is None:
        raise ValueError("weekly quota window missing")
    return {**weekly, "windows": windows, "sampled_at": now.isoformat(), "stale_minutes": 0,
            "source": "account/rateLimits/read", "plan_type": snapshot.get("planType"),
            "ordinary_usage_allowed": result.get("ordinaryUsageAllowed"),
            "backend_blocked": result.get("ordinaryUsageAllowed") is False
                or snapshot.get("spendControlReached") is True,
            "quota_state": "fresh"}


def live_quota(exe: str, home: str, harness: str) -> dict:
    try:
        result = read_account_limits(exe, home, harness)
        return normalize_limits(result, datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, TimeoutError) as exc:
        return {"available": False, "quota_state": "unknown",
                "unavailable_reason": "quota query failed: " + type(exc).__name__,
                "source": "account/rateLimits/read"}


def availability(u: dict, stop: float) -> tuple[bool, str | None]:
    if u.get("quota_state") == "unknown":
        return False, u.get("unavailable_reason", "quota unknown")
    if u.get("backend_blocked"):
        return False, "backend disallows ordinary usage"
    if any(w["used_percent"] >= 100 for w in u.get("windows", [])):
        return False, "quota exhausted"
    if u["used_percent"] >= stop:
        return False, "weekly dispatch threshold reached"
    return True, None


def unavailable_summary(usage: dict) -> str:
    details = "; ".join(f"{h}: {(u or {}).get('unavailable_reason') or 'quota unknown'}"
                        for h, u in usage.items() if h != "claude")
    return "no eligible quota: " + details + "; none launched"
