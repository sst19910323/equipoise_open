"""Resolve per-harness model policy once before launching a batch."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from workflows.harness_resolver import resolve_codex


def resolve_model(harness, role: str, explicit: str | None = None,
                  fallback: str | None = None) -> str | None:
    if explicit:
        return explicit
    # Each identity reads only its own policy and model cache.
    home = Path(harness.codex_home)
    policy = json.loads((home / "equipoise_models.json").read_text())
    selection = policy[role]
    if not selection.startswith("latest:"):
        return selection
    family = selection.split(":", 1)[1]
    cache = json.loads((home / "models_cache.json").read_text())
    fetched = datetime.fromisoformat(cache["fetched_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if age < -300 or age > policy.get("cache_max_age_hours", 24) * 3600:
        raise ValueError("Codex model cache is stale; refresh it with Codex before launching")
    candidates = []
    for item in cache.get("models", []):
        slug = item.get("slug", "")
        match = re.fullmatch(r"gpt-(\d+)(?:\.(\d+))?-" + re.escape(family), slug)
        if match and item.get("visibility") == "list":
            candidates.append(((int(match[1]), int(match[2] or 0)), slug))
    if not candidates:
        raise ValueError(f"No visible stable {family} model in Codex cache")
    return max(candidates)[1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True)
    parser.add_argument("--role", required=True, choices=["duty", "debate", "postcheck"])
    args = parser.parse_args()
    print(resolve_model(resolve_codex(args.harness), args.role) or "")
