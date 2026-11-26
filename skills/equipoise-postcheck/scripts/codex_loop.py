#!/usr/bin/env python
"""Codex postcheck pool: one independent Codex reckoner per mature report."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/equipoise")

from datalayer.pg.db import connect  # noqa: E402
from datalayer.pg.postcheck_store import _maturity_cutoff  # noqa: E402
from workflows.harness_resolver import CHOICES as HARNESS_CHOICES, resolve_codex  # noqa: E402
from workflows.codex_models import resolve_model
from workflows.symbol_universe import postcheck_universes  # noqa: E402


EQUIPOISE = "/opt/equipoise"
# codex1/codex2/codex3 identity: explicit --harness > EQUIPOISE_HARNESS env > codex1.
HARNESS = resolve_codex()
MODEL = None
UNIVERSES = postcheck_universes()
AUTH_STOP = threading.Event()


def _is_agent(reckoner):
    return bool(reckoner) and reckoner.lower().startswith(("claude", "gpt", "codex"))


def pending(dt, start, universe, include_redo):
    syms = UNIVERSES[universe]
    cutoff = _maturity_cutoff(datetime.now(timezone.utc), dt)
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT symbol, as_of FROM debates "
            "WHERE symbol = ANY(%s) AND as_of >= %s AND as_of <= %s ORDER BY symbol, as_of",
            (syms, start_dt, cutoff),
        )
        mature = cur.fetchall()
        cur.execute(
            "SELECT symbol, as_of, reckoner FROM postcheck WHERE symbol = ANY(%s) AND dt = %s",
            (syms, dt),
        )
        done = {(symbol, as_of): reckoner for symbol, as_of, reckoner in cur.fetchall()}
    todo, redo = [], []
    for symbol, as_of in mature:
        reckoner = done.get((symbol, as_of))
        item = (symbol, as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), reckoner)
        if _is_agent(reckoner):
            continue
        if reckoner is None:
            todo.append(item)
        else:
            redo.append(item)
    return todo + (redo if include_redo else [])


def prompt_for(symbol, dt, as_of, staging_path=None):
    prep_instruction = (
        f"The parent dispatcher has already run prep for this item serially; read the fixed staging file: {staging_path}. "
        "Do not request IBKR again and do not run prep again; prices and path facts come only from that staging digest."
        if staging_path
        else "Run prep with EQUIPOISE_IBKR_PORT=4001; prices and path facts come only from the digest."
    )
    return f"""In {EQUIPOISE}, postcheck exactly one item: {symbol} --dt {dt} --as_of {as_of}.
You are the sole independent reckoner (main agent) for this postcheck. Read in full and follow strictly:
- {EQUIPOISE}/skills/equipoise-postcheck/SKILL.md
- {EQUIPOISE}/skills/equipoise-postcheck/references/codex.md
- {EQUIPOISE}/orchestration/postcheck-protocol.md
Do not start Claude, a debate, or any subagent. {prep_instruction}
If the symbol is GENERAL/GENERAL_EU/GENERAL_JP/GENERAL_KR, you must weigh every index lens in digest.members[]; do not treat it as a single name.
Except for the single exemption (a precise numeric forecast whose matching operation was realized), use review=true and read the full debate_history and evidence; search the web yourself when needed, with a hard cutoff at win_end. When writing the judgment JSON, fill reckoner with your real model slug, then save to PG and finish with the structured receipt.
Everything you write (judgment, verdict, attribution, receipt) is English; protocol enum values are copied verbatim.
The owner requires: reckoner must be the actual runtime model version of the current process; never copy an example, a fixed default, or the gpt-codex fallback. This overrides any older fallback rule in the skill. If the runtime identity cannot confirm the actual version, report plainly that the signature cannot be confirmed and do not write a guessed version to the database."""


def run_one(index, item, dt, timeout, stagger_seconds, staging_path=None):
    symbol, as_of, old_reckoner = item
    if index:
        time.sleep(index * stagger_seconds)
    if AUTH_STOP.is_set():
        return symbol, as_of, False, 0.0, "SKIPPED: another worker detected invalid Codex authentication"
    out_path = None
    runtime_path = None
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(prefix="equipoise-postcheck-codex-", suffix=".txt", delete=False) as tmp:
            out_path = tmp.name
        with tempfile.NamedTemporaryFile(prefix="equipoise-postcheck-runtime-", suffix=".log", delete=False) as tmp:
            runtime_path = tmp.name
        prompt = prompt_for(symbol, dt, as_of, staging_path) + (
            f"\nThe parent process saves the live stderr of this Codex CLI run to {runtime_path}. "
            "Read at most the first 20 lines of that file: the model: field in the startup header is the actual model version this CLI process reports. "
            "Fill reckoner with that exact value; do not use the CLI software version, a config default, or your own guess as the model version."
        )
        cmd = [
            HARNESS.executable,
            "--search",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "-C",
            EQUIPOISE,
            "-o",
            out_path,
            prompt,
        ]
        if MODEL:
            cmd[1:1] = ["--model", MODEL]
        env = HARNESS.child_env()
        env.setdefault("EQUIPOISE_IBKR_PORT", "4001")
        with open(runtime_path, "w") as runtime_log:
            proc = subprocess.run(cmd, cwd=EQUIPOISE, stdout=subprocess.PIPE, stderr=runtime_log,
                                  text=True, timeout=timeout, env=env)
        stderr = Path(runtime_path).read_text(errors="replace")
        receipt = Path(out_path).read_text(errors="replace").strip() if Path(out_path).exists() else ""
        if not receipt:
            receipt = (proc.stdout or stderr)[-3000:].strip()
        combined = f"{receipt}\n{proc.stdout}\n{stderr}".lower()
        if "token_invalidated" in combined or "refresh_token_invalidated" in combined:
            AUTH_STOP.set()
        ok = proc.returncode == 0
        if ok:
            model_match = re.search(r"^model:\s*(\S+)\s*$", stderr[:4000], re.MULTILINE)
            actual_model = model_match.group(1) if model_match else None
            with connect() as con, con.cursor() as cur:
                cur.execute("SELECT reckoner FROM postcheck WHERE symbol=%s AND as_of=%s AND dt=%s",
                            (symbol, as_of, dt))
                row = cur.fetchone()
            ok = bool(actual_model and row and row[0] == actual_model)
            receipt += f"\n# runtime_model={actual_model} pg_reckoner={row[0] if row else None} verified={ok}"
        return symbol, as_of, ok, time.monotonic() - started, receipt
    except subprocess.TimeoutExpired:
        return symbol, as_of, False, time.monotonic() - started, f"TIMEOUT after {timeout}s"
    finally:
        if out_path:
            Path(out_path).unlink(missing_ok=True)
        if runtime_path:
            Path(runtime_path).unlink(missing_ok=True)


def prep_one(item, dt):
    symbol, as_of, _old_reckoner = item
    cmd = [
        sys.executable,
        f"{EQUIPOISE}/datalayer/pg/postcheck_store.py",
        "prep",
        "--symbol",
        symbol,
        "--dt",
        str(dt),
        "--as_of",
        as_of,
    ]
    env = dict(os.environ)
    env["EQUIPOISE_IBKR_PORT"] = "4001"
    proc = subprocess.run(cmd, cwd=EQUIPOISE, capture_output=True, text=True, env=env)
    payload = None
    stdout = proc.stdout or ""
    decoder = json.JSONDecoder()
    for pos, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(stdout[pos:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and "status" in candidate:
            payload = candidate
    if proc.returncode != 0 or payload is None:
        detail = (proc.stderr or proc.stdout or "prep produced no receipt")[-2000:].strip()
        return None, f"error: {detail}"
    if payload.get("status") != "ok":
        return None, f"{payload.get('status')}: {payload.get('why', '')}"
    return payload.get("staging_path"), "ok"


def run_serial_prep_pool(batch, args):
    """Serialize IBKR prep while keeping a rolling Codex reckoner pool full."""
    ok_count = 0
    done_count = 0
    skipped = 0
    next_index = 0
    active = {}

    def prepare_and_submit(pool):
        nonlocal next_index, skipped
        while next_index < len(batch) and len(active) < max(1, args.jobs):
            item = batch[next_index]
            task_no = next_index + 1
            next_index += 1
            symbol, as_of, _ = item
            print(f"# prep {task_no}/{len(batch)} {symbol} {as_of}", flush=True)
            staging_path, status = prep_one(item, args.dt)
            if not staging_path:
                skipped += 1
                print(f"# prep-skip {symbol} {as_of}: {status}", flush=True)
                continue
            future = pool.submit(
                run_one, 0, item, args.dt, args.timeout, 0, staging_path
            )
            active[future] = item

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        prepare_and_submit(pool)
        while active:
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                active.pop(future)
                symbol, as_of, ok, elapsed, receipt = future.result()
                done_count += 1
                ok_count += int(ok)
                print(f"\n=== {symbol} {as_of} {'OK' if ok else 'FAIL'} {elapsed:.0f}s ===")
                print(receipt[-5000:])
                print(
                    f"# progress done={done_count} skipped={skipped} "
                    f"ok={ok_count} active={len(active)}",
                    flush=True,
                )
            prepare_and_submit(pool)
    return ok_count, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dt", type=int, default=3)
    parser.add_argument("--start", default="2026-06-18")
    parser.add_argument("--universe", default="all", choices=list(UNIVERSES))
    parser.add_argument("--limit", type=int, default=16, help="总任务数；0 表示当前全部待复盘")
    parser.add_argument("--jobs", "-j", type=int, default=16, help="滚动 pool 宽度，默认 16(库主 2026-10-08 定)")
    parser.add_argument("--stagger-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--no-redo", action="store_true")
    parser.add_argument("--harness", choices=HARNESS_CHOICES, default=None,
                        help="Codex identity (executable + CODEX_HOME + quota). "
                             "Default: EQUIPOISE_HARNESS env, else codex1.")
    parser.add_argument(
        "--serial-prep",
        action="store_true",
        help="父层串行 prep/IBKR 请求，再投入滚动 Codex pool",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default=None, help="Reckoner model; defaults to harness model policy")
    args = parser.parse_args()

    global HARNESS, MODEL
    HARNESS = resolve_codex(args.harness)
    MODEL = resolve_model(HARNESS, "postcheck", args.model)

    items = pending(args.dt, args.start, args.universe, include_redo=not args.no_redo)
    batch = items if args.limit == 0 else items[: args.limit]
    print(
        f"# codex postcheck pool dt={args.dt} universe={args.universe} "
        f"pending={len(items)} batch={len(batch)} jobs={args.jobs} "
        f"harness={HARNESS.describe()} model={MODEL or 'CLI-default'}",
        flush=True,
    )
    for i, (symbol, as_of, reckoner) in enumerate(batch, 1):
        tag = "new" if reckoner is None else f"replace:{reckoner}"
        print(f"[{i}] {symbol:12} {as_of} {tag}")
    if args.dry_run or not batch:
        return

    if args.serial_prep:
        ok_count, skipped = run_serial_prep_pool(batch, args)
        remain = pending(args.dt, args.start, args.universe, include_redo=not args.no_redo)
        print(
            f"# complete ok={ok_count}/{len(batch)} prep_skipped={skipped} "
            f"remaining={len(remain)}",
            flush=True,
        )
        return

    ok_count = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(run_one, index, item, args.dt, args.timeout, args.stagger_seconds): item
            for index, item in enumerate(batch)
        }
        for done_count, future in enumerate(as_completed(futures), 1):
            symbol, as_of, ok, elapsed, receipt = future.result()
            ok_count += int(ok)
            print(f"\n=== {symbol} {as_of} {'OK' if ok else 'FAIL'} {elapsed:.0f}s ===")
            print(receipt[-5000:])
            print(f"# progress {done_count}/{len(batch)} ok={ok_count}", flush=True)

    remain = pending(args.dt, args.start, args.universe, include_redo=not args.no_redo)
    print(f"# complete ok={ok_count}/{len(batch)} remaining={len(remain)}", flush=True)


if __name__ == "__main__":
    main()
