#!/usr/bin/env python3
"""sol_proc.py — 演习里起 / 续一个独立的 sol 进程(仲裁者或辩手), 断网、带 as_of 时钟、留痕并扫越界读取。

    ./venv/bin/python scripts/drill/sol_proc.py --pack <root> --role zealot --prompt-file <f> [--thread <id>]

每次调用一个 `codex exec`(新会话)或 `codex exec resume <id>`(续会话); **不带 `--search`**; cwd / -C = 沙箱根;
env 带 EQUIPOISE_AS_OF、去掉 EQUIPOISE_IBKR_PORT。留痕在 drill/runs/<n>-<role>.*(提示词、事件流、回复、摘要);
摘要里的 out_of_root = 事件流里命令读过的沙箱外路径(生产目录、封存目录等), 导演据此判回合作废。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = "gpt-6.1-sol"
TIMEOUT_S = 3600
OUTSIDE = ("/opt/", "/root", "/home", "/var/lib/postgresql", "/etc/postgresql")
ALLOWED = ("/opt/equipoise/venv/bin/python",)          # 沙箱的 venv 是软链回生产的, 调解释器不算越界
_PATH = re.compile(r"(?<![\w.~-])(/[^\s'\"|;&<>(){}`,]+)")
_N_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_exe(harness: str | None) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from workflows.harness_resolver import resolve_codex
    return [resolve_codex(harness).executable]


@dataclass
class SolConfig:
    root: Path
    exe: list[str]
    model: str = MODEL
    timeout: int = TIMEOUT_S
    env_base: dict = field(default_factory=lambda: dict(os.environ))
    engine: str = "codex"          # codex = 生产 sol; claude = 天花板对照(`claude -p`, 生产 CC 辩手同款工具 + 写 OUTPUT / 跑校验)


def pack_of(root: Path) -> dict:
    return json.loads((root / "drill" / "pack.json").read_text(encoding="utf-8"))


def child_env(cfg: SolConfig) -> dict:
    env = {k: v for k, v in cfg.env_base.items() if k != "EQUIPOISE_IBKR_PORT"}
    env.update({"EQUIPOISE_AS_OF": pack_of(cfg.root)["as_of"], "EQUIPOISE_EVIDENCE_LIVE_FETCH": "0"})
    return env


def codex_argv(cfg: SolConfig, reply: Path, thread: str | None = None) -> list[str]:
    """新会话: exec -C <root> --sandbox workspace-write; 续会话: exec resume <id>(resume 不收 -C / --sandbox, 用 -c 钉沙箱,
    且必须再给 -m, 否则落回配置默认模型)。提示词一律走 stdin(`-`)。"""
    head = [*cfg.exe, "--ask-for-approval", "never", "exec"]
    if thread:
        return [*head, "resume", "--skip-git-repo-check", "--json", "-o", str(reply), "-m", cfg.model,
                "-c", 'sandbox_mode="workspace-write"', thread, "-"]
    return [*head, "--skip-git-repo-check", "-C", str(cfg.root), "--sandbox", "workspace-write", "--json",
            "-o", str(reply), "-m", cfg.model, "-"]


CLAUDE_TOOLS = ["Read", "Glob", "Grep", "Write", "Bash(python3 scripts/validate_role_record.py:*)",
                "Bash(./venv/bin/python scripts/validate_role_record.py:*)"]


def claude_argv(cfg: SolConfig, thread: str | None = None) -> list[str]:
    argv = [*cfg.exe, "-p", "--model", cfg.model, "--permission-mode", "dontAsk", "--output-format", "stream-json",
            "--verbose", "--allowedTools", *CLAUDE_TOOLS]
    return argv + (["--resume", thread] if thread else [])


def _claude_events(events: str):
    for line in events.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def claude_reply(events: str) -> tuple[str | None, str]:
    sid, text = None, ""
    for ev in _claude_events(events):
        sid = ev.get("session_id") or sid
        if ev.get("type") == "result":
            text = str(ev.get("result") or "")
    return sid, text


def claude_out_of_root(events: str, root: Path) -> list[str]:
    """claude 的工具调用(Read / Glob / Grep / Write 的路径, Bash 的命令)里出现沙箱外的绝对路径、`_sealed`、`..` 上溯 → 记一条。"""
    hits, r = [], root.as_posix().rstrip("/") + "/"
    for ev in _claude_events(events):
        if ev.get("type") != "assistant":
            continue
        for c in (ev.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            inp = c.get("input") or {}
            text = " ".join(str(inp.get(k) or "") for k in ("file_path", "path", "pattern", "command"))
            bad = [q for q in _PATH.findall(text)
                   if q.startswith(OUTSIDE) and not (q + "/").startswith(r) and not q.startswith(ALLOWED)]
            if bad or "_sealed" in text or re.search(r"(^|[\s/'\"])\.\./", text):
                hits.append(f"{c.get('name')}: {text[:380]}")
    return hits


def out_of_root_reads(events: str, root: Path) -> list[str]:
    """事件流里每条 command_execution 的命令: 出现沙箱外的绝对路径、`_sealed`、`..` 上溯或 psql → 记一条。"""
    hits, r = [], root.as_posix().rstrip("/") + "/"
    for line in events.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item") or {}
        if item.get("type") != "command_execution" or ev.get("type") != "item.completed":
            continue
        cmd = str(item.get("command") or "")
        bad = [p for p in _PATH.findall(cmd)
               if p.startswith(OUTSIDE) and not (p + "/").startswith(r) and not p.startswith(ALLOWED)]
        if bad or "_sealed" in cmd or re.search(r"(^|[\s/'\"])\.\./", cmd) or re.search(r"\bpsql\b", cmd):
            hits.append(cmd[:400])
    return hits


def thread_of(events: str) -> str | None:
    for line in events.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started":
            return ev.get("thread_id")
    return None


def _next_n(runs: Path) -> int:
    with _N_LOCK:
        runs.mkdir(parents=True, exist_ok=True)
        n = 1 + max([int(p.name.split("-", 1)[0]) for p in runs.glob("*-*.json") if p.name.split("-", 1)[0].isdigit()] or [0])
        (runs / f"{n:03d}-reserved.json").write_text("{}", encoding="utf-8")
        return n


def run(cfg: SolConfig, role: str, prompt: str, thread: str | None = None) -> dict:
    runs = cfg.root / "drill" / "runs"
    n = _next_n(runs)
    stem = runs / f"{n:03d}-{role}"
    (runs / f"{n:03d}-reserved.json").unlink(missing_ok=True)
    reply_p = stem.with_suffix(".reply.txt")
    stem.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
    argv = claude_argv(cfg, thread) if cfg.engine == "claude" else codex_argv(cfg, reply_p, thread)
    started = _now()
    try:
        p = subprocess.run(argv, input=prompt, cwd=cfg.root, env=child_env(cfg), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=cfg.timeout)
        rc, events, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc, events, err = "timeout", (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), ""
    stem.with_suffix(".events.jsonl").write_text(events, encoding="utf-8")
    if cfg.engine == "claude":
        sid, reply = claude_reply(events)
        reply_p.write_text(reply, encoding="utf-8")
        tid, oor, shown = sid or thread, claude_out_of_root(events, cfg.root), argv
    else:
        reply = reply_p.read_text(encoding="utf-8") if reply_p.exists() else ""
        tid, oor, shown = thread_of(events) or thread, out_of_root_reads(events, cfg.root), argv[:-1]
    summary = {"n": n, "role": role, "engine": cfg.engine, "model": cfg.model, "thread": tid, "resumed": bool(thread),
               "argv": shown, "rc": rc, "started": started, "ended": _now(), "reply": reply,
               "out_of_root": oor, "stderr": err[-1500:]}
    stem.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pack", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--thread")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--harness")
    a = ap.parse_args(argv)
    cfg = SolConfig(root=Path(a.pack), exe=default_exe(a.harness), model=a.model)
    s = run(cfg, a.role, Path(a.prompt_file).read_text(encoding="utf-8"), a.thread)
    print(json.dumps({k: s[k] for k in ("n", "thread", "rc", "out_of_root")}, ensure_ascii=False))
    print(s["reply"])
    return 0 if s["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
