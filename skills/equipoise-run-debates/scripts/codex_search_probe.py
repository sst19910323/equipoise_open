#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe Codex web-search answer length.

This intentionally avoids Equipoise data preparation, IBKR, and PostgreSQL. It asks
headless Codex to do a small search task, saves the raw answer, and prints basic
length/source metrics so we can compare default search behavior against the
debate prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/equipoise")
OUT_DIR = ROOT / "data" / "status" / "search_probe"

DEFAULT_QUERY = (
    "Search the web for the latest available information relevant to a Japan "
    "macro market note: USDJPY level and Japan intervention risk, Nikkei 225 "
    "drivers, BOJ policy context, foreign investor flows, and ETF dividend "
    "rebalancing pressure. Summarize what you found with sources."
)

PROMPTS = {
    "default": """You are testing default web-search behavior.

Task:
{query}

Do not use JSON. Do not follow any Equipoise debate schema. Answer naturally.""",
    "evidence": """You are testing evidence collection behavior.

Task:
{query}

Return evidence notes. For each useful source, include:
- claim
- why it matters
- source name
- short quote or close paraphrase if available

Do not compress unrelated facts into one bullet.""",
    "strict_json": """You are testing structured evidence behavior.

Task:
{query}

Return compact JSON only:
{{
  "items": [
    {{
      "claim": "...",
      "why_it_matters": "...",
      "source": "...",
      "quote_or_paraphrase": "..."
    }}
  ]
}}

Keep separate facts as separate items. Do not merge BOJ, FX, flows, sectors,
and rebalancing into one item.""",
}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _cmd(prompt: str, out_path: Path, sandbox: str, model: str | None) -> list[str]:
    cmd = [
        "codex1",
        "--ask-for-approval",
        "never",
        "--search",
        "exec",
        "-C",
        str(ROOT),
        "--sandbox",
        sandbox,
        "-o",
        str(out_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def _metrics(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    lower = text.lower()
    sourceish = [
        ln for ln in lines
        if re.search(r"\b(source|sources|reuters|bloomberg|marketwatch|ft|financial times|wsj|nikkei|boj|mof|tse)\b", ln, re.I)
    ]
    urls = re.findall(r"https?://\S+", text)
    return {
        "chars": len(text),
        "words_ascii_split": len(text.split()),
        "nonempty_lines": len(lines),
        "sourceish_lines": len(sourceish),
        "url_count": len(urls),
        "has_json_shape": text.lstrip().startswith("{") or text.lstrip().startswith("["),
        "mentions_quote": "quote" in lower or "原文" in text or "quoted" in lower,
    }


def run_one(mode: str, query: str, timeout: int, sandbox: str, model: str | None) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    out = OUT_DIR / f"codex_search_probe_{mode}_{stamp}.txt"
    prompt = PROMPTS[mode].format(query=query)
    cmd = _cmd(prompt, out, sandbox, model)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        return {
            "mode": mode,
            "ok": False,
            "error": f"timeout after {timeout}s",
            "output_path": str(out.relative_to(ROOT)),
        }
    dur = time.monotonic() - t0
    text = out.read_text(encoding="utf-8") if out.exists() else (proc.stdout or "")
    return {
        "mode": mode,
        "ok": proc.returncode == 0 and bool(text.strip()),
        "exit": proc.returncode,
        "duration_sec": round(dur, 1),
        "output_path": str(out.relative_to(ROOT)),
        "metrics": _metrics(text),
        "stderr_tail": (proc.stderr or "")[-1200:],
        "preview": text.strip()[:1200],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Codex --search answer length.")
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--mode", choices=["default", "evidence", "strict_json", "all"], default="all")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    ap.add_argument("--model", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    modes = list(PROMPTS) if args.mode == "all" else [args.mode]
    if args.dry_run:
        for mode in modes:
            print(f"## {mode}\n{PROMPTS[mode].format(query=args.query)}\n")
        return 0

    results = [
        run_one(mode, args.query, args.timeout, args.sandbox, args.model or None)
        for mode in modes
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
