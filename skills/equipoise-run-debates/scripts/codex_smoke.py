#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex debate smoke test.

This verifies the first hard dependency for the Codex debate engine:
whether a non-interactive `codex exec` child session can coordinate three
topologically independent role agents.

It deliberately does not:
- fetch IBKR market data
- read or write PostgreSQL
- run a real debate
- write reports
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / ".codex" / "smoke_run.md"
STATUS_DIR = ROOT / "data" / "status"
SCHEMA = STATUS_DIR / "codex_smoke_schema.json"


SMOKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "spawn_available",
        "parent_observations",
        "agents",
        "production_recommendation",
    ],
    "properties": {
        "spawn_available": {"type": "boolean"},
        "parent_observations": {"type": "string"},
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "role",
                    "topology_independent",
                    "can_use_web",
                    "can_spawn_agents",
                    "can_message_siblings",
                    "data_boundary",
                    "sample_search_request",
                ],
                "properties": {
                    "role": {"type": "string"},
                    "topology_independent": {"type": "boolean"},
                    "can_use_web": {"type": "boolean"},
                    "can_spawn_agents": {"type": "boolean"},
                    "can_message_siblings": {"type": "boolean"},
                    "data_boundary": {"type": "string"},
                    "sample_search_request": {"type": "string"},
                },
            },
        },
        "production_recommendation": {"type": "string"},
    },
}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_schema() -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA.write_text(json.dumps(SMOKE_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")


def _cmd(model: str | None, output_path: Path, sandbox: str) -> list[str]:
    cmd = [
        "codex1",
        "--ask-for-approval",
        "never",
        "exec",
        "-C",
        str(ROOT),
        "--sandbox",
        sandbox,
        "--output-schema",
        str(SCHEMA),
        "-o",
        str(output_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(PROMPT.read_text(encoding="utf-8"))
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test headless Codex subagent orchestration.")
    ap.add_argument("--model", default="", help="Optional Codex model override.")
    ap.add_argument("--timeout", type=int, default=600, help="codex exec timeout in seconds.")
    ap.add_argument("--sandbox", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"],
                    help="Sandbox passed to codex exec.")
    ap.add_argument("--dry-run", action="store_true", help="Print command and output path only.")
    args = ap.parse_args()

    if not PROMPT.exists():
        print(f"missing prompt: {PROMPT}", file=sys.stderr)
        return 2

    out = STATUS_DIR / f"codex_smoke_{_ts()}.json"
    cmd = _cmd(args.model or None, out, args.sandbox)

    print(f"# codex smoke cwd={ROOT}")
    print(f"# output={out.relative_to(ROOT)}")
    print("# command:")
    print(" ".join(json.dumps(x) for x in cmd))
    if args.dry_run:
        return 0

    _write_schema()

    t0 = time.monotonic()
    env = dict(os.environ)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=args.timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {args.timeout}s", file=sys.stderr)
        return 124

    dur = time.monotonic() - t0
    print(f"# exit={proc.returncode} dur={dur:.1f}s")
    if proc.stdout.strip():
        print("# stdout tail:")
        print("\n".join(proc.stdout.strip().splitlines()[-20:]))
    if proc.stderr.strip():
        print("# stderr tail:", file=sys.stderr)
        print("\n".join(proc.stderr.strip().splitlines()[-20:]), file=sys.stderr)

    if proc.returncode != 0:
        return proc.returncode

    if not out.exists():
        print(f"codex finished but output file missing: {out}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(out.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"output is not valid JSON: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("# parsed result:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
