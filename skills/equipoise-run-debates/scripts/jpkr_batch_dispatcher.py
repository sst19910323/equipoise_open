#!/usr/bin/env python3
"""Run JP/KR symbols through a prepare-on-demand rolling debate pool."""
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


def _region_symbols(regions: tuple[str, ...]) -> list[str]:
    """区域全集(区域大盘先行 + 个股), 唯一真源 config/symbols.yaml — 不在此写死清单。"""
    cfg = load_symbol_config()
    out: list[str] = []
    for region in regions:
        out.extend(symbols_for("generals", region, cfg))
        out.extend(symbols_for("stocks", region, cfg))
    return out


DEFAULT_MARKETS = ("JP", "KR")


def gateway_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def active_symbol(symbol: str) -> bool:
    proc = subprocess.run(
        ["pgrep", "-af", f"codex_loop.py --symbols {symbol}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def launch(symbol: str, run_stamp: str, timeout: int, attempt: int = 1, model: str | None = None) -> str:
    slug = symbol.lower().replace(".", "-").replace("_", "-")
    unit = f"equipoise-codex-{slug}-{run_stamp}"
    if attempt > 1:
        unit = f"{unit}-retry{attempt - 1}"
    cmd = [
        "systemd-run",
        f"--unit={unit}",
        "--property=WorkingDirectory=/opt/equipoise",
        "--setenv=EQUIPOISE_IBKR_PORT=4001",
        f"--setenv=PATH={PATH_ENV}",
        f"--setenv=EQUIPOISE_HARNESS={HARNESS.name}",
        f"--setenv=CODEX_HOME={HARNESS.codex_home}",
        str(PY),
        str(RUNNER),
        "--symbols",
        symbol,
        "--skip-prepare",
        "--timeout",
        str(timeout),
        "--harness",
        HARNESS.name,
    ]
    if model:
        cmd.extend(["--model", model])
    proc = subprocess.run(cmd, cwd=EQUIPOISE, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"launch {symbol} failed: {(proc.stderr or proc.stdout).strip()}")
    print(f"LAUNCH {symbol} attempt={attempt}: {unit}.service", flush=True)
    return f"{unit}.service"


def prepare_next(symbols: list[str], args: argparse.Namespace) -> str | None:
    while symbols:
        symbol = symbols.pop(0)
        if active_symbol(symbol):
            print(f"SKIP {symbol}: already active", flush=True)
            continue
        state, receipt = prepare(symbol, timeout=args.prepare_timeout, force=args.force_prepare)
        print(f"PREPARE {symbol}: {state} {receipt}", flush=True)
        if state == "ibkr-down":
            raise IbkrDown(symbol)
        if state == "ready":
            return symbol
        elif state != "fresh-exists":
            print(f"SKIP {symbol}: preparation is {state}", flush=True)
        if symbols:
            time.sleep(args.prepare_stagger_seconds)
    return None


def unit_finished(unit: str) -> tuple[bool, bool, dict[str, str]]:
    proc = subprocess.run(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"],
        capture_output=True, text=True,
    )
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    finished = values.get("ActiveState") in {"inactive", "failed"}
    success = values.get("Result") == "success" and values.get("ExecMainStatus") == "0"
    return finished, success, values


def run_pipeline(symbols: list[str], args: argparse.Namespace, run_stamp: str) -> bool:
    pending = list(symbols)
    ready: list[tuple[str, int]] = []
    while len(ready) < args.max_parallel and pending:
        symbol = prepare_next(pending, args)
        if symbol:
            ready.append((symbol, 1))
        if pending and len(ready) < args.max_parallel:
            time.sleep(args.prepare_stagger_seconds)
    print(f"# INITIAL PREPARE COMPLETE: {len(ready)} ready; starting rolling pool", flush=True)

    retries: list[tuple[str, int]] = []
    active: dict[str, tuple[str, int]] = {}
    ok = True
    while ready or pending or retries or active:
        while len(active) < args.max_parallel:
            if ready:
                symbol, attempt = ready.pop(0)
            elif pending:
                symbol = prepare_next(pending, args)
                if not symbol:
                    continue
                attempt = 1
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
            if not finished:
                continue
            print(f"COMPLETE {unit}: {'ok' if success else values}", flush=True)
            del active[unit]
            if not success and attempt <= args.max_retries:
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
    ap.add_argument("--symbols", default="",
                    help="Explicit comma-separated override; wins over --markets.")
    ap.add_argument("--markets", default="",
                    help="Comma-separated regions from config/symbols.yaml "
                         "(e.g. jp,kr or eu). Default: jp,kr.")
    ap.add_argument("--stagger-seconds", type=int, default=15)
    ap.add_argument("--poll-seconds", type=int, default=20)
    ap.add_argument("--prepare-timeout", type=int, default=900)
    ap.add_argument("--prepare-stagger-seconds", type=float, default=5.0)
    ap.add_argument("--timeout", type=int, default=9000)
    ap.add_argument("--max-parallel", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=1)
    ap.add_argument("--harness", choices=HARNESS_CHOICES, default=None,
                    help="Codex identity (executable + CODEX_HOME + quota). "
                         "Default: EQUIPOISE_HARNESS env, else codex1.")
    ap.add_argument("--force-prepare", action="store_true")
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

    if args.symbols:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    else:
        regions = tuple(item.strip().upper() for item in args.markets.split(",")
                        if item.strip()) or DEFAULT_MARKETS
        symbols = _region_symbols(regions)
        if not symbols:
            ap.error(f"no symbols for markets {','.join(regions)} in config/symbols.yaml")
    print(f"# JPKR plan: prepare first pool serially, then prepare-on-demand rolling queue "
          f"max_parallel={args.max_parallel} stagger={args.stagger_seconds}s "
          f"harness={HARNESS.describe()} model={args.model}", flush=True)
    print(f"  symbols: {' '.join(symbols)}", flush=True)
    if args.dry_run:
        return 0
    port = int(os.getenv("EQUIPOISE_IBKR_PORT", "4001"))
    if not gateway_alive(port):
        print(f"STOP: IBKR gateway is not accepting connections on {port}", flush=True)
        return 10

    run_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        queue_ok = run_pipeline(symbols, args, run_stamp)
    except IbkrDown as exc:
        print(f"STOP: IBKR went down while preparing {exc}", flush=True)
        return 10
    except Exception as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print("# JPKR batch run complete", flush=True)
    return 0 if queue_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
