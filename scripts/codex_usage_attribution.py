#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Attribute codex token usage to debate roles, mechanically, from rollout files.

消耗归因(2026-09-14 立案, 先量后切): every codex thread — the debate parent and
each spawned zealot/reaper/scout/runner/signoff — writes its own rollout JSONL
under ``$CODEX_HOME/sessions/YYYY/MM/DD/`` with per-API-call
``token_usage_record`` rows. This script scans a day (or days), classifies each
thread by the role markers in its opening user messages, and prints a per-group
and per-symbol breakdown: threads / API calls / input / cached / fresh
(=input−cached) / output tokens.

Numbers are RELATIVE attribution material: cached and fresh input do not weigh
equally in the weekly quota, and this script never claims to convert to quota.
Fresh input + output are the closest proxy for real cost; cached input is the
reload volume the architecture causes.

Usage (on the server):
    python3 scripts/codex_usage_attribution.py 2026-09-14            # one day
    python3 scripts/codex_usage_attribution.py 2026-09-14 --symbol TSLA
    python3 scripts/codex_usage_attribution.py 2026-09-14 2026-09-15 --json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_CODEX_HOME = Path("~/.codex").expanduser()
HEAD_CHARS = 12000      # opening user text scanned for role markers
HEAD_MESSAGES = 3

# (marker, group) — first hit wins; parent checked before roles because the
# parent prompt embeds every role filename in its instructions.
PARENT_MARKER = "Debate Runner"   # 2026-10-22 改名: 新标题 Equipoise Debate Runner, 老日志 Apex Debate Runner, 取共同子串
ROLE_MARKERS = [
    ("agents/zealot.md", "zealot"),
    ("agents/reaper.md", "reaper"),
    ("agents/scout.md", "scout"),
    ("agents/runner.md", "runner"),
]
SYMBOL_RE = re.compile(r"\bSYMBOL:\s*([A-Z][A-Z0-9_.]{0,15})")
# spawned children: the task payload is encrypted in the rollout, but the spawn
# names the thread (agent_message.recipient, e.g. "/root/zealot_init") and the
# child's own shell calls touch symbol-named data paths.
RECIPIENT_RE = re.compile(r"(zealot|reaper|scout|runner|signoff)", re.IGNORECASE)
PATH_SYM_RE = re.compile(r"data/(?:debate|evidence|technical|news|fundamentals|evidence_prep|store)/([A-Z][A-Z0-9_.]{0,15})/")
GROUP_ORDER = ["parent", "zealot", "reaper", "signoff", "scout", "runner", "other"]


def _scan_rollout(path: Path) -> dict[str, Any] | None:
    model = None
    started = None
    calls = 0
    usage = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    head_parts: list[str] = []
    head_msgs = 0
    head_len = 0
    recipient = None          # spawn thread name, e.g. "/root/zealot_init"
    assistant_head = ""       # child's first words name its role in plaintext
    path_symbol = None        # symbol from data paths the thread's shell touches
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            p = d.get("payload") or {}
            if t == "session_meta":
                started = p.get("timestamp")
            elif t == "turn_context" and p.get("model"):
                model = p["model"]
            elif t == "token_usage_record":
                u = p.get("usage") or {}
                calls += 1
                usage["input"] += u.get("input_tokens", 0)
                usage["cached"] += u.get("cached_input_tokens", 0)
                usage["output"] += u.get("output_tokens", 0)
                usage["reasoning"] += u.get("reasoning_output_tokens", 0)
            elif t == "response_item":
                pt = p.get("type")
                if (pt == "message" and p.get("role") == "user"
                        and head_msgs < HEAD_MESSAGES and head_len < HEAD_CHARS):
                    head_msgs += 1
                    for seg in p.get("content") or []:
                        if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                            head_parts.append(seg["text"][: HEAD_CHARS - head_len])
                            head_len += len(head_parts[-1])
                            if head_len >= HEAD_CHARS:
                                break
                elif pt == "agent_message" and recipient is None:
                    recipient = p.get("recipient") or ""
                    for seg in p.get("content") or []:  # plaintext part carries the task name
                        if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                            head_parts.append(seg["text"][:2000])
                            head_len += len(head_parts[-1])
                elif pt == "message" and p.get("role") == "assistant" and len(assistant_head) < 600:
                    for seg in p.get("content") or []:
                        if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                            assistant_head += seg["text"][:300]
                elif pt in ("custom_tool_call", "local_shell_call", "function_call") and path_symbol is None:
                    m = PATH_SYM_RE.search(json.dumps(p, ensure_ascii=False)[:4000])
                    if m:
                        path_symbol = m.group(1)
    if calls == 0:
        return None  # empty/aborted thread: nothing to attribute
    head = "\n".join(head_parts)
    # Spawn direction is authoritative: a spawned child's FIRST agent_message is
    # the NEW_TASK addressed TO it (recipient = "/root/<task name>"), while a
    # parent's first agent_message is a child's reply addressed to "/root".
    # Debater spawns are full-history forks, so their head contains the parent
    # prompt (incl. the Debate Runner marker) — head markers alone lie.
    task_name = None
    if recipient and recipient.strip("/").lower() != "root":
        task_name = recipient.rsplit("/", 1)[-1]
    group = "other"
    if task_name:
        m = RECIPIENT_RE.search(task_name) or RECIPIENT_RE.search(assistant_head)
        if m:
            group = m.group(1).lower()
        else:
            for marker, name in ROLE_MARKERS:
                if marker in head:
                    group = name
                    break
        if group in ("zealot", "reaper") and "signoff" in task_name.lower():
            group = "signoff"
    elif PARENT_MARKER in head:
        group = "parent"
    sym = SYMBOL_RE.search(head)
    return {
        "file": path.name,
        "started": started,
        "model": model,
        "group": group,
        "task_name": task_name,
        "symbol": sym.group(1) if sym else path_symbol,
        "calls": calls,
        **usage,
        "fresh": usage["input"] - usage["cached"],
    }


def _fmt(n: int) -> str:
    if n >= 10_000_000:
        return f"{n/1e6:,.0f}M"
    if n >= 100_000:
        return f"{n/1e6:,.2f}M"
    return f"{n:,}"


def _agg(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        g = agg[r[key] or "?"]
        g["threads"] += 1
        for k in ("calls", "input", "cached", "fresh", "output", "reasoning"):
            g[k] += r[k]
    return agg


def _print_table(title: str, agg: dict[str, dict[str, int]], order: list[str] | None = None) -> None:
    keys = [k for k in (order or []) if k in agg] + sorted(k for k in agg if not order or k not in order)
    print(f"\n== {title} ==")
    hdr = f"{'group':<10} {'thr':>4} {'calls':>6} {'input':>10} {'cached':>10} {'fresh':>10} {'output':>9} {'cache%':>7}"
    print(hdr)
    print("-" * len(hdr))
    tot: dict[str, int] = defaultdict(int)
    for k in keys:
        g = agg[k]
        rate = (g["cached"] / g["input"] * 100) if g["input"] else 0.0
        print(f"{k:<10} {g['threads']:>4} {g['calls']:>6} {_fmt(g['input']):>10} "
              f"{_fmt(g['cached']):>10} {_fmt(g['fresh']):>10} {_fmt(g['output']):>9} {rate:>6.1f}%")
        for f in ("threads", "calls", "input", "cached", "fresh", "output"):
            tot[f] += g[f]
    rate = (tot["cached"] / tot["input"] * 100) if tot["input"] else 0.0
    print("-" * len(hdr))
    print(f"{'TOTAL':<10} {tot['threads']:>4} {tot['calls']:>6} {_fmt(tot['input']):>10} "
          f"{_fmt(tot['cached']):>10} {_fmt(tot['fresh']):>10} {_fmt(tot['output']):>9} {rate:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dates", nargs="+", help="YYYY-MM-DD (UTC), one or more session days")
    parser.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    parser.add_argument("--symbol", help="restrict to one debate symbol")
    parser.add_argument("--per-symbol", action="store_true",
                        help="also print one table per symbol (debate threads only)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for day in args.dates:
        y, m, d = day.split("-")
        folder = args.codex_home / "sessions" / y / m / d
        for path in sorted(folder.glob("rollout-*.jsonl")):
            row = _scan_rollout(path)
            if row:
                rows.append(row)
    debate_rows = [r for r in rows if r["group"] != "other"]
    if args.symbol:
        debate_rows = [r for r in debate_rows if r["symbol"] == args.symbol.upper()]
    other_rows = [r for r in rows if r["group"] == "other"]

    if args.json:
        print(json.dumps({
            "days": args.dates,
            "by_group": _agg(debate_rows, "group"),
            "by_symbol": {s: dict(g) for s, g in _agg(debate_rows, "symbol").items()},
            "by_model": _agg(debate_rows, "model"),
            "non_debate": _agg(other_rows, "group").get("other", {}),
            "threads": debate_rows,
        }, ensure_ascii=False, default=dict))
        return

    scope = f"{'+'.join(args.dates)}" + (f" · {args.symbol.upper()}" if args.symbol else "")
    print(f"codex usage attribution — {scope}")
    print(f"debate threads: {len(debate_rows)} | non-debate threads (excluded): {len(other_rows)}")
    if not debate_rows:
        print("no debate threads found for this scope.")
        return
    _print_table("by role group", _agg(debate_rows, "group"), GROUP_ORDER)
    _print_table("by model", _agg(debate_rows, "model"))
    sym_agg = _agg(debate_rows, "symbol")
    n_syms = len([s for s in sym_agg if s != "?"])
    if n_syms:
        tot_fresh = sum(g["fresh"] for g in sym_agg.values())
        tot_input = sum(g["input"] for g in sym_agg.values())
        print(f"\nper-debate average over {n_syms} symbols: "
              f"input {_fmt(tot_input // n_syms)} / fresh {_fmt(tot_fresh // n_syms)}")
    if args.per_symbol:
        for sym in sorted(sym_agg):
            _print_table(f"symbol {sym}", _agg([r for r in debate_rows if r["symbol"] == sym], "group"), GROUP_ORDER)


if __name__ == "__main__":
    main()
