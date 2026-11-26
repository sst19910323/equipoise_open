#!/usr/bin/env python3
"""oracle_daemon.py — 演习外呼的执行者(沙箱外跑, 有网): 接队列里的 Scout / Runner 任务, 交给更强的模型按 as_of 时间线作答。

    ./venv/bin/python scripts/drill/oracle_daemon.py --pack /opt/equipoise_drill/<NAME> [--model claude-opus-5-5] [--parallel 4]

每条请求: 起一个 `claude -p`(cwd = 沙箱根, 环境带 EQUIPOISE_AS_OF、不带盈透端口), 提示词 = prompts/drill/oracle.md + 任务材料全文;
写完跑 leak_scan, 有晚于 as_of 的日期就带着命中行重写一次; 审计写进封存目录 <sealed>/audit/<job>.json(演习方看不到);
最后落 <job>.done。心跳文件 drill/queue/.daemon_alive 每轮更新, 启动器靠它判断守护进程在不在。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import leak_scan as L  # noqa: E402
import oracle_request as R  # noqa: E402

MODEL = "claude-opus-5-5"
NEON_MODEL = "gemini-4-neon-medium"
TIMEOUT_S = 1200
TOOLS = ["Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch", "Bash(./venv/bin/python evidence_quant.py:*)"]


def claude_cmd(model: str = MODEL) -> list[str]:
    return ["claude", "-p", "--model", model, "--permission-mode", "dontAsk", "--output-format", "text",
            "--allowedTools", *TOOLS]


@dataclass
class Config:
    root: Path
    cmd: list[str] = field(default_factory=claude_cmd)
    model: str = MODEL
    timeout: int = TIMEOUT_S
    parallel: int = 4
    engine: str = "claude"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def child_env(as_of: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "EQUIPOISE_IBKR_PORT"}
    env.update({"EQUIPOISE_AS_OF": as_of, "EQUIPOISE_EVIDENCE_LIVE_FETCH": "0"})
    return env


def _human(as_of: str) -> str:
    return datetime.strptime(as_of, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M UTC")


def build_prompt(root: Path, as_of: str, material: str, notes: Path, feedback: list | None = None) -> str:
    tpl_p = root / "prompts" / "drill" / "oracle.md"
    if not tpl_p.exists():
        tpl_p = HERE.parents[1] / "prompts" / "drill" / "oracle.md"
    tpl = tpl_p.read_text(encoding="utf-8") if tpl_p.exists() else "AS_OF: {AS_OF}\n"
    text = (tpl.replace("{AS_OF}", as_of).replace("{AS_OF_HUMAN}", _human(as_of))
            .replace("{ARCHIVE}", "drill/archive.jsonl").replace("{NOTES}", str(notes)))
    text += "\n\n---\nJOB MATERIAL (verbatim):\n\n" + material
    if feedback:
        text += ("\n\n---\nLEAK FEEDBACK: your previous OUTPUT contains dates after the as-of moment "
                 f"({_human(as_of)}); the passages are listed below. Decide for each which it is. A schedule published before "
                 "the moment (a release date, a meeting, an expiry, an earnings date, the debate horizon) was known then: keep "
                 "the date and say it is scheduled. Information first published after the moment (an outcome, a print, a price, "
                 "a later article) was not known: remove it; if a point can only be supported by it, say it was not known yet. "
                 "Rewrite the same OUTPUT file. Passages flagged:\n"
                 + "\n".join(f"- {h['match']} → {h['line']}" for h in feedback))
    return text


def _run(cfg: Config, prompt: str, as_of: str) -> dict:
    try:
        p = subprocess.run(cfg.cmd, input=prompt, cwd=cfg.root, env=child_env(as_of), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=cfg.timeout)
        return {"rc": p.returncode, "stderr": (p.stderr or "")[-1500:]}
    except subprocess.TimeoutExpired:
        return {"rc": "timeout", "stderr": ""}


def _agy_job(cfg: Config, job: Path, material: str, as_of: str, notes: Path) -> tuple[dict, dict]:
    """Use stdout for delivery so partial CLI output never becomes evidence."""
    job.resolve().relative_to(cfg.root.resolve())
    prompt = (build_prompt(cfg.root, as_of, material, Path("host-managed-notes"))
              + "\n\nDelivery for this provider: return a single JSON object with output (the requested Scout or Runner "
                "result object) and notes (your source and exclusion audit as a string). The host writes both. "
                "Use the file-reading tool directly for named inputs. Do not use a shell to discover files. "
                "Do not edit files, read sealed outcomes, or inspect other sessions.")
    run = {"rc": None}
    stdout = None
    try:
        cmd = [*cfg.cmd, "--model", cfg.model, "--mode", "plan", "--output-format", "text",
               "--print-timeout", f"{cfg.timeout}s", "--print", prompt]
        proc = subprocess.run(cmd, cwd=cfg.root, env=child_env(as_of), capture_output=True,
                              text=True, encoding="utf8", errors="replace", timeout=cfg.timeout + 10)
        run.update(rc=proc.returncode, stderr=proc.stderr[-1500:])
        stdout = proc.stdout
        if proc.returncode:
            raise ValueError(f"agy exited with status {proc.returncode}")
        raw = proc.stdout.strip()
        if raw.startswith("```json\n") and raw.endswith("\n```"):
            raw = raw[8:-4]
        reply = json.loads(raw)
        if not isinstance(reply, dict) or not isinstance(reply.get("notes"), str) or not reply["notes"].strip():
            raise ValueError("missing source audit")
        notes.write_text(reply["notes"], encoding="utf8")
        answer = reply.get("output")
        if not isinstance(answer, dict):
            raise ValueError("missing result object")
        if answer.get("status") == "failed":
            raise ValueError(str(answer.get("reason") or "evidence unavailable"))
        runner = "TASK: model_call" in material
        fields = ("model_call_id", "model", "steps", "finding", "payload_text") if runner else ("question", "simple", "detailed", "source")
        if any(key not in answer for key in fields) or answer.get("verdict") not in (
                "supports", "refutes", "mixed", "not_found", "unrelated"):
            raise ValueError("incomplete evidence result")
        strings = ("finding", "issue", "target", "payload_text") if runner else ("question", "simple", "detailed")
        if any(not isinstance(answer.get(k), str) or not answer[k].strip() for k in strings):
            raise ValueError("missing evidence text")
        if runner:
            if (not isinstance(answer["steps"], list) or not answer["steps"]
                    or any(not isinstance(step, dict) or not all(isinstance(step.get(k), str) and step[k].strip()
                               for k in ("kind", "what", "result")) for step in answer["steps"])
                    or answer.get("suggested_evidence_tier") not in ("T1", "T2", "T3")):
                raise ValueError("invalid runner steps or evidence tier")
        elif not isinstance(answer["source"], list) or not all(isinstance(v, str) for v in answer["source"]):
            raise ValueError("invalid scout source list")
        if runner:
            expected = R.failed_shape(material, "")
            if any(answer[k] != expected[k] for k in ("model_call_id", "model")):
                raise ValueError("runner result identifies another call")
        hits = L.leaks(json.dumps(answer, ensure_ascii=False), datetime.strptime(as_of, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        if hits:
            run["quarantined_hits"] = hits
            raise ValueError("result contains unresolved post-cutoff information")
        return answer, run
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        run["error"] = str(exc)
        if stdout is not None:
            # Kept in the sealed audit only, for diagnosis of malformed or quarantined output.
            run["rejected_stdout"] = stdout
        return R.failed_shape(material, "Drill evidence retrieval failed"), run


def process(cfg: Config, req_path: Path) -> None:
    req = json.loads(req_path.read_text(encoding="utf-8"))
    pack = json.loads((cfg.root / "drill" / "pack.json").read_text(encoding="utf-8"))
    as_of = pack["as_of"]
    a = datetime.strptime(as_of, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    sealed = Path(pack["sealed"])
    (sealed / "audit").mkdir(parents=True, exist_ok=True)
    (sealed / "oracle_notes").mkdir(parents=True, exist_ok=True)
    jid, out = req["job_id"], Path(req["output"])
    material = Path(req["material"]).read_text(encoding="utf-8")
    notes = sealed / "oracle_notes" / f"{jid}.md"
    audit = {"job_id": jid, "task": "model_call" if "TASK: model_call" in material else "scout",
             "model": cfg.model, "started": _now(), "runs": [], "attempts": 0}
    if cfg.engine == "agy":
        answer, run = _agy_job(cfg, Path(req["material"]), material, as_of, notes)
        audit.update(engine="agy", runs=[run], attempts=1, ended=_now(), status=answer.get("status", "answered"))
        R._atomic_write(sealed / "audit" / f"{jid}.json", json.dumps(audit, ensure_ascii=False, indent=1))
        R._atomic_write(out, json.dumps(answer, ensure_ascii=False))
        R._atomic_write(req_path.parent / f"{jid}.done", _now())
        return
    feedback, first = None, None
    for attempt in (1, 2):
        audit["attempts"] = attempt
        audit["runs"].append(_run(cfg, build_prompt(cfg.root, as_of, material, notes, feedback), as_of))
        if not out.exists():
            break
        hits = L.leaks(out.read_text(encoding="utf-8"), a)
        if first is None:
            first = hits
        if not hits:
            break
        feedback = hits
    if not out.exists():
        out.write_text(json.dumps(R.failed_shape(material, "drill oracle produced no OUTPUT file"), ensure_ascii=False),
                       encoding="utf-8")
    audit["leaks_first"] = first or []
    audit["leaks_final"] = L.leaks(out.read_text(encoding="utf-8"), a)
    audit["leak"] = "unresolved" if audit["leaks_final"] else ("fixed" if audit["leaks_first"] else "none")
    audit["ended"] = _now()
    (sealed / "audit" / f"{jid}.json").write_text(json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")
    (req_path.parent / f"{jid}.done").write_text(_now(), encoding="utf-8")


def _claim(req: Path) -> bool:
    jid = req.name[: -len(".req.json")]
    if (req.parent / f"{jid}.done").exists():
        return False
    try:
        os.close(os.open(req.parent / f"{jid}.claimed", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def _safe(cfg: Config, req: Path) -> None:
    try:
        process(cfg, req)
    except Exception as e:  # 一条坏请求不能拖死守护进程; 请求方超时后自会写 failed
        print(f"[oracle] {req.name} crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def run_once(cfg: Config, ex: ThreadPoolExecutor | None = None) -> int:
    """认领队列里的新请求。给了 ex 就提交后立刻返回(守护进程: 后到的请求不用等前一批); 没给就同步做完(单测)。"""
    q = R.queue_dir(cfg.root)
    q.mkdir(parents=True, exist_ok=True)
    (q / ".daemon_alive").write_text(_now(), encoding="utf-8")
    todo = [p for p in sorted(q.glob("*.req.json")) if _claim(p)]
    for p in todo:
        if ex is None:
            process(cfg, p)
        else:
            ex.submit(_safe, cfg, p)
    return len(todo)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pack", required=True)
    ap.add_argument("--engine", choices=("claude", "agy"), default="claude")
    ap.add_argument("--model")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    ap.add_argument("--poll", type=float, default=3.0)
    a = ap.parse_args(argv)
    model = a.model or (NEON_MODEL if a.engine == "agy" else MODEL)
    cfg = Config(root=Path(a.pack), cmd=["agy"] if a.engine == "agy" else claude_cmd(model),
                 model=model, timeout=a.timeout, parallel=a.parallel, engine=a.engine)
    if not (cfg.root / "drill" / "pack.json").exists():
        print(f"not a drill pack: {cfg.root}", file=sys.stderr)
        return 2
    print(f"[oracle] watching {R.queue_dir(cfg.root)} with {model}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, cfg.parallel)) as ex:
        while True:
            n = run_once(cfg, ex)
            if n:
                print(f"[oracle] {_now()} picked up {n}", flush=True)
            time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main())
