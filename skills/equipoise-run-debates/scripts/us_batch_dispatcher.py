#!/usr/bin/env python3
"""Run US symbols through a prepare-on-demand bounded rolling pool.

The dispatcher is itself intended to run as a detached systemd service.  All
IBKR preparation is always serial.  The first pool is prepared up front; after
that, each freed slot prepares and launches one next symbol.  GENERAL is not a
synchronization barrier.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


EQUIPOISE = Path("/opt/equipoise")
PY = EQUIPOISE / "venv" / "bin" / "python"
RUNNER = EQUIPOISE / "skills" / "equipoise-run-debates" / "scripts" / "codex_loop.py"
PATH_ENV = "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if str(RUNNER.parent) not in sys.path:
    sys.path.insert(0, str(RUNNER.parent))
if str(EQUIPOISE) not in sys.path:
    sys.path.insert(0, str(EQUIPOISE))

from codex_loop import IbkrDown, prepare  # noqa: E402
from workflows.harness_resolver import CHOICES as HARNESS_CHOICES, resolve_codex  # noqa: E402
from workflows.codex_models import resolve_model
from workflows.symbol_universe import load_symbol_config, symbols_for  # noqa: E402

# codex1/codex2/codex3 identity: explicit --harness > EQUIPOISE_HARNESS env > codex1.
# Never guessed from CODEX_HOME (interactive shells may carry either value).
HARNESS = resolve_codex()


# Membership comes from config/symbols.yaml via symbol_universe — no local
# symbol lists here.  ETFs and stocks are deliberately interleaved to spread
# the IBKR preparation load; GENERAL joins the first concurrency group but
# does not block the other members from starting.
def _build_batches(groups: int = 3) -> list[list[str]]:
    cfg = load_symbol_config()
    etfs = symbols_for("etfs", "US", cfg)
    stocks = symbols_for("stocks", "US", cfg)
    order = list(symbols_for("generals", "US", cfg))
    for i in range(max(len(etfs), len(stocks))):
        if i < len(etfs):
            order.append(etfs[i])
        if i < len(stocks):
            order.append(stocks[i])
    size = -(-len(order) // groups)
    return [order[k:k + size] for k in range(0, len(order), size)]


BATCHES = _build_batches()


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def gateway_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def unit_name(symbol: str, run_stamp: str) -> str:
    slug = symbol.lower().replace(".", "-").replace("_", "-")
    return f"equipoise-codex-{slug}-{run_stamp}"


def active_symbol(symbol: str) -> bool:
    proc = subprocess.run(
        ["pgrep", "-af", f"codex_loop.py --symbols {symbol}"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def prepare_batch(symbols: list[str], timeout: int, force: bool,
                  stagger_seconds: float) -> tuple[list[str], bool]:
    ready: list[str] = []
    for index, symbol in enumerate(symbols):
        if active_symbol(symbol):
            print(f"SKIP {symbol}: already active", flush=True)
            continue
        state, receipt = prepare(symbol, timeout=timeout, force=force)
        print(f"PREPARE {symbol}: {state} {receipt}", flush=True)
        if state == "ibkr-down":
            raise IbkrDown(symbol)
        if state == "ready":
            ready.append(symbol)
        elif state not in {"fresh-exists"}:
            print(f"ABORT batch: {symbol} preparation is {state}", flush=True)
            return [], False
        if index + 1 < len(symbols):
            time.sleep(stagger_seconds)
    return ready, True


def launch(symbol: str, run_stamp: str, timeout: int, attempt: int = 1, model: str | None = None) -> str:
    unit = unit_name(symbol, run_stamp)
    if attempt > 1:
        unit = f"{unit}-retry{attempt - 1}"
    cmd = [
        "systemd-run", f"--unit={unit}",
        "--property=WorkingDirectory=/opt/equipoise",
        "--setenv=EQUIPOISE_IBKR_PORT=4001", f"--setenv=PATH={PATH_ENV}",
        f"--setenv=EQUIPOISE_HARNESS={HARNESS.name}",
        f"--setenv=CODEX_HOME={HARNESS.codex_home}",
        str(PY), str(RUNNER), "--symbols", symbol, "--skip-prepare", "--timeout", str(timeout),
        "--harness", HARNESS.name,
    ]
    if model:
        cmd.extend(["--model", model])
    proc = subprocess.run(cmd, cwd=str(EQUIPOISE), text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"launch {symbol} failed: {(proc.stderr or proc.stdout).strip()}")
    print(f"LAUNCH {symbol} attempt={attempt}: {unit}.service", flush=True)
    return f"{unit}.service"


def unit_finished(unit: str) -> tuple[bool, bool, dict[str, str]]:
    proc = subprocess.run(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"],
        capture_output=True, text=True,
    )
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    finished = values.get("ActiveState") in {"inactive", "failed"}
    success = values.get("Result") == "success" and values.get("ExecMainStatus") == "0"
    return finished, success, values


def run_queue(symbols: list[str], args: argparse.Namespace, run_stamp: str,
              adopted_units: list[str] | None = None) -> bool:
    pending = list(symbols)
    active = {unit: ("<adopted>", args.max_retries + 1)
              for unit in (adopted_units or [])}
    ready: list[tuple[str, int]] = []
    while len(ready) + len(active) < args.max_parallel and pending:
        batch, prepared = prepare_batch(pending[:1], args.prepare_timeout,
                                        args.force_prepare, 0)
        pending.pop(0)
        if not prepared:
            return False
        ready.extend((symbol, 1) for symbol in batch)
        if pending and len(ready) + len(active) < args.max_parallel:
            time.sleep(args.prepare_stagger_seconds)
    retries: list[tuple[str, int]] = []
    ok = True
    while ready or pending or retries or active:
        while len(active) < args.max_parallel:
            if ready:
                symbol, attempt = ready.pop(0)
            elif pending:
                candidate = pending.pop(0)
                batch, prepared = prepare_batch([candidate], args.prepare_timeout,
                                                args.force_prepare, 0)
                if not prepared:
                    return False
                if not batch:
                    continue
                symbol, attempt = batch[0], 1
            elif retries:
                symbol, attempt = retries.pop(0)
            else:
                break
            unit = launch(symbol, run_stamp, args.timeout, attempt, args.model)
            active[unit] = (symbol, attempt)
            if len(active) < args.max_parallel and (ready or pending or retries):
                time.sleep(args.stagger_seconds)

        completed_any = False
        for unit, (symbol, attempt) in list(active.items()):
            finished, success, values = unit_finished(unit)
            if finished:
                print(f"COMPLETE {unit}: {'ok' if success else values}", flush=True)
                del active[unit]
                if not success and symbol != "<adopted>" and attempt <= args.max_retries:
                    print(f"RETRY {symbol}: scheduling final attempt={attempt + 1} at queue tail", flush=True)
                    retries.append((symbol, attempt + 1))
                else:
                    ok &= success
                completed_any = True
        if active and not completed_any:
            time.sleep(args.poll_seconds)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stagger-seconds", type=int, default=15)
    ap.add_argument("--poll-seconds", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=9000)
    ap.add_argument("--prepare-timeout", type=int, default=900)
    ap.add_argument("--prepare-stagger-seconds", type=float, default=5.0,
                    help="Natural delay between serial IBKR preparations (default: 5).")
    ap.add_argument("--max-parallel", type=int, default=8,
                    help="Maximum active debate services in the rolling queue (default: 5).")
    ap.add_argument("--max-retries", type=int, default=1,
                    help="Retries after a failed debate service (default: 1).")
    ap.add_argument("--harness", choices=HARNESS_CHOICES, default=None,
                    help="Codex identity (executable + CODEX_HOME + quota). "
                         "Default: EQUIPOISE_HARNESS env, else codex1.")
    ap.add_argument("--force-prepare", action="store_true")
    ap.add_argument("--adopt-first-unit", default="",
                    help="Existing service counted in and awaited with batch 1 (resume only).")
    ap.add_argument("--start-batch", type=int, choices=range(1, len(BATCHES) + 1), default=1,
                    help="Start directly from this 1-based batch (resume without rerunning earlier batches).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=None, help="Judgment model; defaults to harness model policy")
    args = ap.parse_args()
    if args.max_parallel < 1:
        ap.error("--max-parallel must be at least 1")
    if args.max_retries < 0:
        ap.error("--max-retries cannot be negative")

    global HARNESS
    HARNESS = resolve_codex(args.harness)
    args.model = resolve_model(HARNESS, "debate", args.model, "gpt-6.1-sol")

    print(f"# US plan: bounded rolling queue max_parallel={args.max_parallel} "
          f"max_retries={args.max_retries} harness={HARNESS.describe()} model={args.model}; "
          "GENERAL is not a completion barrier", flush=True)
    for index, batch in enumerate(BATCHES, 1):
        print(f"  batch-{index}: {' '.join(batch)}", flush=True)
    if args.dry_run:
        return 0

    port = int(os.getenv("EQUIPOISE_IBKR_PORT", "4001"))
    if not gateway_alive(port):
        print(f"STOP: IBKR gateway is not accepting connections on {port}", flush=True)
        return 10

    run_stamp = stamp()
    try:
        symbols = [symbol for batch in BATCHES[args.start_batch - 1:] for symbol in batch]
        adopted = [args.adopt_first_unit] if args.adopt_first_unit else []
        queue_ok = run_queue(symbols, args, run_stamp, adopted)
        if not queue_ok:
            return 1
    except IbkrDown as exc:
        print(f"STOP: IBKR went down while preparing {exc}", flush=True)
        return 10
    except Exception as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print("# US batch run complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
