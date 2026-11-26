#!/usr/bin/env python3
"""Verify that an evidence officer can temporarily spawn one search subagent."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / ".codex" / "search_agent_smoke.md"
STATUS_DIR = ROOT / "data" / "status"
SCHEMA = STATUS_DIR / "codex_search_agent_smoke_schema.json"

SMOKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["spawn_available", "parent_observations", "search_agent"],
    "properties": {
        "spawn_available": {"type": "boolean"},
        "parent_observations": {"type": "string"},
        "search_agent": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": [
                "role", "topology_independent", "can_use_web",
                "can_spawn_agents", "search_completed", "sources",
                "production_boundary", "sample_query",
            ],
            "properties": {
                "role": {"type": "string"},
                "topology_independent": {"type": "boolean"},
                "can_use_web": {"type": "boolean"},
                "can_spawn_agents": {"type": "boolean"},
                "search_completed": {"type": "boolean"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "production_boundary": {"type": "string"},
                "sample_query": {"type": "string"},
            },
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA.write_text(json.dumps(SMOKE_SCHEMA, ensure_ascii=False, indent=2) + "\n")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = STATUS_DIR / f"codex_search_agent_smoke_{stamp}.json"
    cmd = [
        "codex1", "--ask-for-approval", "never", "--search", "exec", "-C", str(ROOT),
        "--sandbox", "workspace-write", "--output-schema", str(SCHEMA),
        "-o", str(output),
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.append(PROMPT.read_text(encoding="utf-8"))
    print(f"# output={output.relative_to(ROOT)}")
    if args.dry_run:
        print(" ".join(json.dumps(part) for part in cmd))
        return 0

    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                          timeout=args.timeout)
    if proc.returncode:
        print(proc.stderr[-4000:])
        return proc.returncode
    payload = json.loads(output.read_text(encoding="utf-8"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    agent = payload.get("search_agent") or {}
    passed = (payload["spawn_available"]
              and agent.get("topology_independent")
              and agent.get("search_completed")
              and any("openai.com" in item for item in agent.get("sources", [])))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
