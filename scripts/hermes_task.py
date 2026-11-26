#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hermes_task.py — Hermes 喊干活 AI 的唯一入口(低频任务: refresh-lowfreq / refresh-fundamentals / refresh-news / refresh-sentiment)。

Hermes 只说"刷什么", 引擎怎么选在这里: 按 config/ai_engine.yaml 的 order 依次试(claude → codex1 → codex3 → codex2 → codex4),
refresh-fundamentals ranks remaining Codex quota using the account capacities in config/duty.yaml.
codex 身份先看额度线(config/duty.yaml usage_stop_used_pct)超线就跳过; 一个成了就停, 都失败退出码 1。
codex 回执里的 pending_loads(沙箱挡住 PG 时留下的入库命令)在这里善后: 只执行 `./venv/bin/python datalayer/pg/db.py ` 开头的行。

用法(服务器 /opt/equipoise):
  ./venv/bin/python scripts/hermes_task.py refresh-lowfreq REGION=US INDICATORS=cpi_yoy,core_cpi_yoy
  ./venv/bin/python scripts/hermes_task.py refresh-fundamentals SYMBOL=AAPL
  ./venv/bin/python scripts/hermes_task.py refresh-news REGION=EU        # 或 SYMBOL=SIE
  ./venv/bin/python scripts/hermes_task.py refresh-sentiment FAILED_REGIONS=EU,JP
  加 --dry-run 只打印会跑的命令; --engine codex1 只试这一个。
参数 KEY=VALUE: claude 侧按给出顺序拼成 slash 参数("/refresh-lowfreq US cpi_yoy"); codex 侧逐行附到 Runtime Input("REGION: US")。
最后一行 stdout 是一行 JSON 回执 {task, engine, ok, receipt, pending_loads_run}; 日志 logs/hermes_task.log。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

from scripts.duty_quota import availability, live_quota  # noqa: E402
from workflows.harness_resolver import _CODEX_VARIANTS, resolve_codex  # noqa: E402

LOG = ROOT / "logs" / "hermes_task.log"
STATUS = ROOT / "data" / "status"
LOAD_PREFIX = "./venv/bin/python datalayer/pg/db.py "


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _yaml(p: Path) -> dict:
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def eligible_used_percent(name: str, duty: dict) -> float:
    account = resolve_codex(name)
    quota = live_quota(account.executable, account.codex_home, name)
    stop = float((duty.get("usage_stop_used_pct") or {}).get(name, 70))
    ok, reason = availability(quota, stop)
    if not ok:
        raise ValueError(reason)
    return quota["used_percent"]


def rank_codex_remaining(order: list[str], duty: dict):
    """Rank eligible accounts by remaining percentage scaled by weekly capacity."""
    capacities = duty.get("weekly_units_at_85") or {}
    ranked, skipped = [], []
    for name in order:
        try:
            used = eligible_used_percent(name, duty)
            capacity = float(capacities[name])
            if not math.isfinite(capacity) or capacity <= 0:
                raise ValueError("invalid weekly capacity")
            score = (100 - used) * capacity / 85
            ranked.append((name, score))
            log(f"quota rank: {name} remaining={100-used:.1f}% weekly_units_at_85={capacity:g} score={score:.2f}")
        except (KeyError, TypeError, ValueError, OSError) as e:
            skipped.append({"engine": name, "skipped": str(e)})
            log(f"quota rank: {name} skip ({e})")
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [name for name, _score in ranked], skipped


def _extract_json(text: str):
    """回执文本里找 JSON 对象(裸的或 ``` 围栏里的); 找不到返回 None。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = [m.group(1)] if m else []
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        cands.append(text[s:e + 1])
    for c in cands:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def run_pending_loads(receipt) -> list[dict]:
    done = []
    for line in (receipt or {}).get("pending_loads") or []:
        line = str(line).strip()
        if not line.startswith(LOAD_PREFIX):
            done.append({"cmd": line, "ok": False, "note": "refused: not a db.py load"})
            continue
        r = subprocess.run(line, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=300)
        done.append({"cmd": line, "ok": r.returncode == 0, "tail": (r.stdout + r.stderr)[-300:]})
        log(f"pending_load {'OK' if r.returncode == 0 else 'FAIL'}: {line}")
    return done


def build(engine: str, task: str, kv: list[tuple[str, str]], cfg: dict, tcfg: dict, ts: str):
    models = {**(cfg.get("models") or {}), **(tcfg.get("models") or {})}
    model = models.get(engine)
    if engine == "claude":
        slash = f"/{task} " + " ".join(v for _k, v in kv)
        tools = tcfg.get("allowed_tools") or cfg.get("allowed_tools") or ""
        cmd = ["claude", "-p", slash.strip(), "--permission-mode", "dontAsk", "--allowedTools", tools, "--output-format", "json"]
        if model:
            cmd[3:3] = ["--model", model]
        return cmd, dict(os.environ), None
    harness = resolve_codex(engine)
    exe = harness.executable
    prompt_file = ROOT / ".codex" / f"{task}.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"no codex shell for {task}: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8").rstrip() + "\n\n## Runtime Input\n" + "".join(f"{k}: {v}\n" for k, v in kv)
    out = STATUS / f"hermes_{task}_{ts}.{engine}.json"
    cmd = [exe, "--ask-for-approval", "never", "--search", "exec", "--skip-git-repo-check", "-C", str(ROOT),
           "--sandbox", "workspace-write", "-o", str(out)]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    env = harness.child_env()
    return cmd, env, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("kv", nargs="*", help="KEY=VALUE")
    ap.add_argument("--engine", help="只试这一个引擎")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    kv = []
    for item in a.kv:
        if "=" not in item:
            print(f"bad arg {item!r}, expect KEY=VALUE", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        kv.append((k.strip().upper(), v.strip()))

    cfg = _yaml(ROOT / "config" / "ai_engine.yaml")
    duty = _yaml(ROOT / "config" / "duty.yaml")
    tcfg = ((cfg.get("tasks") or {}).get(a.task) or {}) if isinstance((cfg.get("tasks") or {}).get(a.task), dict) else {}
    order = [a.engine] if a.engine else (tcfg.get("order") or cfg.get("order") or [cfg.get("default", "claude")])
    tried = []
    if not a.engine and tcfg.get("selection") == "weighted_remaining":
        order, tried = rank_codex_remaining(order, duty)
    timeout = int(tcfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 1200)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    STATUS.mkdir(parents=True, exist_ok=True)

    for engine in order:
        if engine not in ("claude", *_CODEX_VARIANTS):
            log(f"{a.task}: unknown engine {engine}, skip")
            continue
        if engine != "claude":
            try:
                eligible_used_percent(engine, duty)
            except ValueError as exc:
                log(f"{a.task}: {engine} quota unavailable ({exc}), skip")
                tried.append({"engine": engine, "skipped": str(exc)})
                continue
        try:
            cmd, env, out = build(engine, a.task, kv, cfg, tcfg, ts)
        except FileNotFoundError as e:
            log(f"{a.task}: {engine}: {e}")
            tried.append({"engine": engine, "skipped": str(e)})
            continue
        shown = " ".join(c if len(c) < 120 else c[:60] + "…" for c in cmd)
        if a.dry_run:
            print(f"[{engine}] {shown}")
            continue
        log(f"{a.task}: try {engine}: {shown}")
        try:
            r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"{a.task}: {engine} timeout {timeout}s")
            tried.append({"engine": engine, "error": "timeout"})
            continue
        if engine == "claude":
            receipt_path = STATUS / f"hermes_{a.task}_{ts}.claude.json"
            receipt_path.write_text(r.stdout, encoding="utf-8")
            try:
                top = json.loads(r.stdout)
                ok = r.returncode == 0 and not top.get("is_error")
                receipt = _extract_json(str(top.get("result", "")))
            except json.JSONDecodeError:
                ok, receipt = False, None
        else:
            receipt_path = out
            text = out.read_text(encoding="utf-8") if out.exists() else ""
            receipt = _extract_json(text)
            ok = r.returncode == 0 and bool(text.strip())
        if not ok:
            log(f"{a.task}: {engine} failed (exit {r.returncode}); stderr tail: {r.stderr[-300:]!r}")
            tried.append({"engine": engine, "error": f"exit {r.returncode}"})
            continue
        loads = run_pending_loads(receipt) if engine != "claude" else []
        if any(not x["ok"] for x in loads):
            log(f"{a.task}: {engine} ok but a pending load failed")
        result = {"task": a.task, "engine": engine, "ok": True, "receipt": str(receipt_path),
                  "pending_loads_run": loads, "tried": tried}
        # 带上参数: duty_status.py 靠 "refresh-fundamentals: done by … SYMBOL=X" 判"财报已落地"的时刻
        log(f"{a.task}: done by {engine} " + " ".join(f"{k}={v}" for k, v in kv))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if a.dry_run:
        return 0
    print(json.dumps({"task": a.task, "ok": False, "tried": tried}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
