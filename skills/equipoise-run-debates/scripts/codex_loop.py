#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPT/Codex production debate loop.

This is intentionally close to the Claude Code loop:
prepare_debate_data.py <SYM> -> one fresh `codex exec` child -> pg_store_debate.py.

The child prompt is a thin adapter over the current Claude Code production
protocol.  Both engines share the two-debater council and evidence contracts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EQUIPOISE = Path("/opt/equipoise")
PY = EQUIPOISE / "venv" / "bin" / "python"
PROMPT = EQUIPOISE / ".codex" / "run.md"
PG_STORE = EQUIPOISE / "scripts" / "pg_store_debate.py"
STATUS_DIR = EQUIPOISE / "data" / "status"

if str(EQUIPOISE) not in sys.path:
    sys.path.insert(0, str(EQUIPOISE))

from workflows.harness_resolver import CHOICES as HARNESS_CHOICES, resolve_codex
from workflows.codex_models import resolve_model
from workflows.symbol_universe import market_waves

# codex1/codex2/codex3 identity: explicit --harness > EQUIPOISE_HARNESS env > codex1.
HARNESS = resolve_codex()

# 2026-09-30 重建批 3: 父层验收 = 与主会话同一条出厂闸(scripts/run_step.py gate =
# validate_debate + 结构契约 + Evidence 侧文件一致), 不再在这里手写字段清单。
sys.path.insert(0, str(EQUIPOISE / "scripts"))

WAVES = market_waves()


class IbkrDown(Exception):
    pass


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prepare(symbol: str, timeout: int = 900, force: bool = False) -> tuple[str, str]:
    env = dict(os.environ)
    env.setdefault("EQUIPOISE_IBKR_PORT", "4001")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [str(PY), str(EQUIPOISE / "prepare_debate_data.py"), symbol, *(["--force"] if force else [])],
            cwd=str(EQUIPOISE),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "error", f"prepare TIMEOUT after {timeout}s"
    tail = [ln.strip() for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines()[-6:]]
    receipt = " | ".join([ln for ln in tail if ln]) + f" ({time.monotonic() - t0:.0f}s)"
    return {0: "ready", 3: "not-ready", 10: "ibkr-down",
            12: "fresh-exists"}.get(proc.returncode, "error"), receipt


ROLE_FILES = {
    "ZEALOT": EQUIPOISE / ".claude" / "agents" / "zealot.md",
    "REAPER": EQUIPOISE / ".claude" / "agents" / "reaper.md",
    "SCOUT": EQUIPOISE / ".claude" / "agents" / "scout.md",
    "RUNNER": EQUIPOISE / ".claude" / "agents" / "runner.md",
}


def _role_blocks() -> str:
    """Reference copies of the generated role files for the parent (it verifies role
    behaviour against them); subagents Read the same files themselves per the
    material files run_step writes (no model-side retelling)."""
    parts = ["## COMPLETE ROLE BLOCKS (runner-injected reference copies; subagents Read the files)\n"]
    for name, path in ROLE_FILES.items():
        text = path.read_text(encoding="utf-8")
        parts.append(
            f"<<<BEGIN {name}_ROLE_FILE {path}>>>\n{text}\n<<<END {name}_ROLE_FILE>>>\n"
        )
    return "\n".join(parts)


def _calc_check_line() -> str:
    """算术提示的档位: 只有用户点了非默认档才出现在 Runtime Input 里, 主会话照它给 `open` 带参数。"""
    v = os.environ.get("EQUIPOISE_CALC_CHECK", "")
    return f"CALC_CHECK: {v}\n" if v in ("off", "always") else ""


def _delta_line() -> str:
    """Follow-up (delta) session: the launcher only relays the dispatcher's choice (base TS + reason) through the
    environment; `run_step open` reads EQUIPOISE_DELTA_BASE / _WHY itself, so the arbiter's commands do not change."""
    base = os.environ.get("EQUIPOISE_DELTA_BASE", "").strip()
    if not base:
        return ""
    why = os.environ.get("EQUIPOISE_DELTA_WHY", "").strip()
    return (f"SESSION_MODE: follow-up (delta) on base {base}" + (f"; why: {why}" if why else "")
            + " — run_step open reads it from the environment; the protocol's §7 applies.\n")


def _child_prompt(symbol: str, prepare_receipt: str, relay_leg: int = 1, model: str | None = None) -> str:
    body = PROMPT.read_text(encoding="utf-8")
    relay = ""
    if relay_leg > 1:
        relay = (
            f"\n## RELAY CONTINUATION (leg {relay_leg})\n\n"
            "A previous child process for THIS SAME debate ended before final artifacts\n"
            "were produced. The object store on disk is the authoritative state of\n"
            f"this debate: locate the newest `data/store/{symbol}/<TS>/_session.json`\n"
            "and its `manifest.jsonl`, then run\n"
            "`./venv/bin/python scripts/run_step.py status --symbol <SYM> --ts <TS>` —\n"
            "its receipt names the step the debate stopped at, the next command and\n"
            "the material files to use.\n"
            "CONTINUE the protocol (orchestration/run-protocol.md §6) from the last\n"
            "archived object by running the next `run_step.py` command: do not restart\n"
            "the debate, never re-archive, never edit existing objects or buffer lines.\n"
            "If no store directory exists for this run, execute the debate from the\n"
            "start as normal.\n"
        )
    return (
        f"{body}\n\n"
        f"{_role_blocks()}\n"
        "## Runtime Input\n\n"
        f"SYMBOL: {symbol}\n"
        f"EQUIPOISE_ROOT: {EQUIPOISE}\n"
        f"JUDGE_MODEL: {model or '(runner default; omit model on debater spawns)'}\n"
        f"{_calc_check_line()}"
        f"PREPARE_RECEIPT: {prepare_receipt}\n"
        f"{_delta_line()}"
        f"{relay}"
    )


def _buffer_lines(symbol: str) -> int:
    """Sum of non-blank lines across this symbol's inflight audit buffers.

    Only used as a progress delta between relay legs (stale leftovers from old
    runs sit in the baseline and cancel out); never parsed for content here."""
    total = 0
    # 对象仓 manifest 是真进度(异议 / 再定稿 / 签章不写 buffer): 一并计入
    for mf in (EQUIPOISE / "data" / "store" / symbol).glob("*/manifest.jsonl"):
        try:
            with open(mf, encoding="utf-8", errors="replace") as fh:
                total += sum(1 for ln in fh if ln.strip())
        except OSError:
            pass
    for d in (EQUIPOISE / "data" / "debate" / symbol, EQUIPOISE / "data" / "evidence" / symbol):
        if not d.is_dir():
            continue
        for f in d.glob("_inflight_*.jsonl"):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    total += sum(1 for ln in fh if ln.strip())
            except OSError:
                pass
    return total


def _rederive_evidence_file(result: dict) -> str | None:
    """独立 Evidence 文件 = Analysis.evidence 的纯派生副本(同一列表落两处)。副本坏而源头好
    → 受信层直接机械重建, 不烧 repair agent、不让模型手抄格式(2026-08-17 GENERAL_JP 案:
    repair agent 曾把副本抄成摘要形状)。确定性派生不属于"父层不修补子进程产物"铁律
    (那条护的是辩论内容); 回执 evidence_rederived 留痕可审计。源头也坏 → 返回 None,
    走 repair agent。"""
    apath, epath = result.get("analysis_path"), result.get("evidence_path")
    if not (apath and epath):
        return None
    ap = EQUIPOISE / apath if not os.path.isabs(apath) else Path(apath)
    ep = EQUIPOISE / epath if not os.path.isabs(epath) else Path(epath)
    try:
        ev = json.loads(ap.read_text(encoding="utf-8")).get("evidence")
        if not isinstance(ev, list) or not ev:
            return None
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"rebuilt verbatim from Analysis.evidence ({len(ev)} entries)"
    except Exception:
        return None


def _repair_prompt(symbol: str, result: dict, validation_errors: list[str]) -> str:
    """Ask a fresh child to repair invalid artifacts without rewriting debate history."""
    return f"""You are the artifact-schema repair pass for an Equipoise debate.

SYMBOL: {symbol}
EQUIPOISE_ROOT: {EQUIPOISE}
ANALYSIS_PATH: {result.get('analysis_path')}
EVIDENCE_PATH: {result.get('evidence_path')}
VALIDATION_ERRORS: {json.dumps(validation_errors, ensure_ascii=False)}

Reopen the two existing JSON artifacts and fix only the reported schema/metadata
violations. Do not rerun the debate, do not search, and do not summarize, truncate,
or otherwise alter debate_history role content. Preserve evidence meaning and source
honesty.

There are TWO DIFFERENT evidence schemas in play. Never mix them:

1. Analysis `evidence` field AND the standalone EVIDENCE_PATH file = the COMPLETE
   evidence list (prompts/agent/formats/evidence.md). Every entry:
   {{"question":"...","simple":"...","detailed":"...","source":["..."]}}
   (data-leg entries must also keep their full `payload_text`). The standalone
   EVIDENCE_PATH file must be exactly this list — normally identical to the
   Analysis `evidence` array. If the standalone file is broken but Analysis
   `evidence` is valid, rewrite the standalone file as a verbatim copy of that
   array. NEVER write item/as_of summary objects into EVIDENCE_PATH.

2. Analysis `evidence_added` (and ONLY this field) = the mechanical index the
   assembler derives: a non-empty JSON array of objects, each with non-empty
   item, as_of, status, source. Prefer re-running
   `./venv/bin/python scripts/run_step.py finalize --symbol <SYM> --ts <TS> --stage signoff ...`
   over hand-editing; never write these objects into the EVIDENCE_PATH file.

After editing, run the REAL gate yourself (same validators the parent uses):

  ./venv/bin/python skills/equipoise-run-debates/scripts/codex_loop.py --validate-artifacts {symbol} "{result.get('analysis_path')}" "{result.get('evidence_path')}"

Only when it prints ok=true and exits 0, return this receipt JSON:
{{"ok":true,"symbol":"{symbol}","analysis_path":"{result.get('analysis_path')}","evidence_path":"{result.get('evidence_path')}","notes":"artifact schema repaired; gate validator exit 0"}}
If it still fails, keep fixing; if you cannot make it pass, return ok:false with
the remaining validator output in notes. Do not claim revalidation you did not run.
"""


def _cmd(prompt: str, out_path: Path, sandbox: str, model: str | None) -> list[str]:
    cmd = [
        HARNESS.executable,
        "--ask-for-approval",
        "never",
        "--search",
        "exec",
        "-C",
        str(EQUIPOISE),
        "--sandbox",
        sandbox,
        "--json",
        "-o",
        str(out_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def _parse_last_message(symbol: str, text: str, stderr: str, dur: float) -> dict:
    raw = (text or "").strip()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    payload = None
    if isinstance(payload, dict):
        return {
            "symbol": symbol,
            "ok": bool(payload.get("ok")),
            "receipt": payload,
            "dur": dur,
            "analysis_path": payload.get("analysis_path"),
            "evidence_path": payload.get("evidence_path"),
            "composite_score": payload.get("composite_score", payload.get("action")),
            "operation_type": payload.get("operation_type"),
        }
    return {
        "symbol": symbol,
        "ok": False,
        "receipt": (raw or stderr or "")[-2000:],
        "dur": dur,
        "analysis_path": None,
        "evidence_path": None,
        "composite_score": None,
        "operation_type": None,
    }


def _validate_analysis(symbol: str, analysis_path: str | None) -> list[str]:
    """同一条出厂闸(run_step.gate): validate_debate 全检 + 结构契约 + symbol / 时戳锚一致。"""
    if not analysis_path:
        return ["missing analysis_path"]
    path = EQUIPOISE / analysis_path if not os.path.isabs(analysis_path) else Path(analysis_path)
    if not path.exists():
        return [f"analysis file not found: {analysis_path}"]
    try:
        from run_step import gate
        verdict = gate(path, EQUIPOISE)
    except Exception as e:  # noqa: BLE001
        return [f"gate crashed: {type(e).__name__}: {e}"]
    errors = list(verdict.get("fatal") or [])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return errors + [f"analysis JSON unreadable: {type(e).__name__}: {e}"]
    if str(report.get("symbol", "")).upper() != symbol.upper():
        errors.append(f"symbol mismatch: {report.get('symbol')!r}")
    report_ts = _canonical_ts_token(report.get("market_data_timestamp_utc"))
    technical_tokens = _technical_timestamp_tokens(report)
    if report_ts and technical_tokens and report_ts not in technical_tokens:
        errors.append("market_data_timestamp_utc must match technical raw-data timestamp(s): "
                      + ",".join(sorted(technical_tokens)))
    return errors


def _validate_evidence(evidence_path: str | None) -> list[str]:
    if not evidence_path:
        return ["missing evidence_path"]
    path = EQUIPOISE / evidence_path if not os.path.isabs(evidence_path) else Path(evidence_path)
    if not path.exists():
        return [f"evidence file not found: {evidence_path}"]
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"evidence JSON unreadable: {type(e).__name__}: {e}"]
    if not isinstance(evidence, list) or not evidence:
        return ["evidence JSON must be a non-empty list"]
    bad = [str(i) for i, it in enumerate(evidence)
           if not isinstance(it, dict) or not str(it.get("question") or "").strip()
           or not str(it.get("simple") or "").strip() or not str(it.get("detailed") or "").strip()
           or not isinstance(it.get("source"), list)]
    if bad:
        return ["evidence entries must use question/simple/detailed/source[] shape; bad indexes: " + ",".join(bad)]
    missing_payload = [str(i) for i, it in enumerate(evidence)
                       if it.get("kind") == "data" and not str(it.get("payload_text") or "").strip()]
    if missing_payload:
        return ["data-leg evidence must contain full payload_text; bad indexes: " + ",".join(missing_payload)]
    return []


def _canonical_ts_token(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}T\d{6}Z", text):
        return text
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return None


def _analysis_token_from_path(path: Path) -> str | None:
    m = re.search(r"_Analysis_(\d{8}T\d{6}Z)\.json$", path.name)
    return m.group(1) if m else None


def _walk_paths(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_paths(item)


def _technical_timestamp_tokens(report: dict) -> set[str]:
    tokens: set[str] = set()
    for file_name in _walk_paths(report.get("data_files")):
        if "/technical/" not in file_name and not file_name.startswith("data/technical/"):
            continue
        path = EQUIPOISE / file_name if not os.path.isabs(file_name) else Path(file_name)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key, value in payload.items() if isinstance(payload, dict) else []:
            if key.endswith("market_data_timestamp_utc") or key == "market_data_timestamp_utc":
                token = _canonical_ts_token(value)
                if token:
                    tokens.add(token)
    return tokens


def _resolve_path(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    return path if path.is_absolute() else EQUIPOISE / path


def _load_json_file(path_text: str | None):
    path = _resolve_path(path_text)
    if not path or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _run_child(cmd: list[str], timeout: int, stall: int) -> tuple[int | None, str, str, str | None]:
    """Run the codex child under two fences: a hard wall-clock timeout (dumb outer
    backstop) and a per-child liveness watchdog — the `--json` event stream on
    stdout must keep growing; `stall` seconds with no new output means the agent
    is neither speaking nor summoning, so it is killed (stall=0 disables).
    stdin is /dev/null: with an attached stdin `codex exec` waits reading it.
    Returns (returncode, stdout_text, stderr_text, kill_reason)."""
    with tempfile.NamedTemporaryFile(prefix="codex_ev_", suffix=".jsonl", delete=False) as f:
        out_path = f.name
    with tempfile.NamedTemporaryFile(prefix="codex_err_", suffix=".txt", delete=False) as f:
        err_path = f.name
    kill_reason = None
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=str(EQUIPOISE), stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL, env=HARNESS.child_env(),
                                start_new_session=True)
        t0 = time.monotonic()
        last_size = -1
        last_activity = t0
        while proc.poll() is None:
            time.sleep(10)
            now = time.monotonic()
            try:
                size = os.path.getsize(out_path) + os.path.getsize(err_path)
            except OSError:
                size = last_size
            if size != last_size:
                last_size, last_activity = size, now
            reason = None
            if now - t0 > timeout:
                reason = f"codex TIMEOUT after {timeout}s"
            elif stall and now - last_activity > stall:
                reason = (f"codex LIVENESS-STALL: no event output for {int(now - last_activity)}s"
                          f" (limit {stall}s) — agent neither speaking nor summoning")
            if reason:
                kill_reason = reason
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, AttributeError):
                    proc.kill()
                proc.wait()
                break
    stdout_text = Path(out_path).read_text(encoding="utf-8", errors="replace")
    stderr_text = Path(err_path).read_text(encoding="utf-8", errors="replace")
    Path(out_path).unlink(missing_ok=True)
    Path(err_path).unlink(missing_ok=True)
    return proc.returncode, stdout_text, stderr_text, kill_reason


# 2026-10-26: OpenAI 两次短暂过载窗口(01:49-01:57Z / 07:18-07:44Z, `server_overloaded` "Selected model is at capacity")
# 把 HYMTF / RHM 的接力腿一条接一条烧光 —— 腿之间没有退避, 三条腿都撞在同一个窗口里就判失败了。
# 这类错是基础设施的瞬时故障, 不是辩论卡住: 退避后重试, 不消耗接力腿(最多 TRANSIENT_BACKOFF 这么多次)。
TRANSIENT_MARKERS = ("server_overloaded", "at capacity", "overloaded", "rate_limit", "rate limit exceeded",
                     "stream disconnected", "connection reset")
TRANSIENT_BACKOFF = (120, 240, 480, 900, 900)


def _transient_error(child_stdout: str, child_stderr: str) -> str | None:
    """Marker of a transient provider-side failure in the child's tail output (the `--json` event stream carries the
    `turn.failed` / `error` message; stderr may repeat it), else None."""
    # Only the engine's own error events count (`{"type":"error"|"turn.failed",...}` lines on stdout, verified 2026-10-26 with a
    # bogus model), plus stderr — not the debate text, which can legitimately contain words like "overloaded".
    probe = []
    for line in child_stdout[-200000:].splitlines():
        if line.startswith("{") and ('"type":"error"' in line or '"type":"turn.failed"' in line):
            probe.append(line)
    tail = ("\n".join(probe[-20:]) + "\n" + child_stderr[-5000:]).lower()
    for m in TRANSIENT_MARKERS:
        if m in tail:
            return m
    return None


def _harness_truth(child_stdout: str) -> dict | None:
    """Ground-truth engine lineage, mechanically extracted after the child exits.

    The `--json` stream carries no model slug (verified 2026-09-06: thread.started
    has only thread_id), but codex writes the truth into its own rollout file
    (session_meta.cli_version + turn_context.model).  Map thread_id -> rollout
    under this identity's CODEX_HOME and read it back — the agent is never asked
    to guess its own slug (it only knows the family from its system prompt)."""
    m = re.search(r'"type":"thread\.started".*?"thread_id":"([0-9a-f-]+)"', child_stdout[:4000])
    if not m:
        return None
    truth: dict = {"harness": HARNESS.name, "thread_id": m.group(1),
                   "cli_version": None, "model": None}
    try:
        rollouts = sorted(Path(HARNESS.codex_home).glob(f"sessions/*/*/*/rollout-*{truth['thread_id']}.jsonl"))
        if not rollouts:
            return truth
        with open(rollouts[-1], encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 200 or (truth["cli_version"] and truth["model"]):
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    truth["cli_version"] = p.get("cli_version")
                elif d.get("type") == "turn_context" and p.get("model"):
                    truth["model"] = p.get("model")
    except OSError:
        pass
    return truth


def _stamp_harness_truth(analysis_path: str, truth: dict) -> None:
    """Append mechanical engine lineage into the Analysis JSON before PG storage.

    Additive only (one new top-level key): debate content stays verbatim; this is
    the trusted parent stamping lineage the child cannot know (its exact slug).
    Best-effort — never fail a healthy debate over the stamp."""
    path = Path(analysis_path) if os.path.isabs(analysis_path) else EQUIPOISE / analysis_path
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        report["harness_truth"] = truth
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


def fire(symbol: str, prepare_receipt: str, timeout: int, sandbox: str, model: str | None,
         stall: int = 900, relay_legs: int = 3) -> dict:
    """One debate, up to `relay_legs` child processes (接力续跑, 2026-09-09).

    A child that ends early ("in progress", stall-kill, timeout) is not fatal:
    the file-backed buffers hold the debate state, so the next leg continues
    from them. A leg that adds zero buffer lines means a real blockage —
    stop relaying and fail closed exactly as before."""
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    # R20 object store: the sandboxed child was once denied creating the top-level
    # data/store chain (2026-09-15 MSFT filesystem_blocker); pre-create it out here
    # where no sandbox applies. The child still creates its own <TS> leaf.
    # 2026-10-16 KO 首场: 新标的的 data/debate/<SYM> 与 data/evidence/<SYM>(三条缓冲的父目录)同样建不了, 终稿归档时
    # manifest 已登记而缓冲追加报 PermissionError → 账目错位判 poisoned。三处一起在父层预建。
    for sub in ("store", "debate", "evidence"):
        (EQUIPOISE / "data" / sub / symbol).mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    legs = max(1, relay_legs)
    prev_lines = _buffer_lines(symbol)
    relay_log: list[str] = []
    truth: dict = {}
    result: dict = {}
    leg = 0
    transient = 0
    while leg < legs:
        leg += 1
        out = STATUS_DIR / f"codex_run_{symbol}_{_stamp()}.json"
        cmd = _cmd(_child_prompt(symbol, prepare_receipt, relay_leg=leg, model=model), out, sandbox, model)
        rc, child_stdout, child_stderr, kill_reason = _run_child(cmd, timeout, stall)
        truth = _harness_truth(child_stdout) or truth
        dur = time.monotonic() - t0
        if not kill_reason and rc != 0 and not out.exists():
            marker = _transient_error(child_stdout, child_stderr)
            if marker and transient < len(TRANSIENT_BACKOFF) and time.monotonic() - t0 + TRANSIENT_BACKOFF[transient] < timeout:
                wait = TRANSIENT_BACKOFF[transient]
                transient += 1
                cur_lines = _buffer_lines(symbol)
                relay_log.append(f"leg{leg}: provider transient '{marker}' (buffers +{cur_lines - prev_lines} lines) → "
                                 f"wait {wait}s and retry; relay leg not consumed ({transient}/{len(TRANSIENT_BACKOFF)})")
                print(f"  {symbol}: {relay_log[-1]}", flush=True)
                prev_lines = cur_lines
                time.sleep(wait)
                leg -= 1
                continue
        if kill_reason:
            result = {
                "symbol": symbol,
                "ok": False,
                "receipt": kill_reason,
                "dur": dur,
                "analysis_path": None,
                "evidence_path": None,
                "composite_score": None,
                "operation_type": None,
            }
        else:
            text = out.read_text(encoding="utf-8") if out.exists() else child_stdout
            result = _parse_last_message(symbol, text, child_stderr, dur)
            result["codex_exit"] = rc
            result["last_message_path"] = str(out.relative_to(EQUIPOISE))
            if rc != 0:
                result["ok"] = False
                result["receipt"] = result["receipt"] or (child_stderr or child_stdout)[-2000:]
        result["harness_truth"] = truth
        if result.get("ok"):
            break
        cur_lines = _buffer_lines(symbol)
        relay_log.append(f"leg{leg}: buffers +{cur_lines - prev_lines} lines, "
                         f"exit={result.get('receipt') or 'not-ok'}"[:300])
        if leg < legs and cur_lines > prev_lines:
            prev_lines = cur_lines
            continue
        break  # no buffer progress (real blockage) or legs exhausted → fail closed
    if relay_log:
        result["relay"] = {"legs_used": leg, "transient_retries": transient, "log": relay_log}
    if result.get("ok"):
        # 2026-07-09: 父层不再事后修补子进程产物(原 _normalize_report_artifacts 已删) ——
        # 落盘 verbatim 是 CC 铁律; 产物不合格就 FAIL 报出来, 让 child 按 Completion Gate 自修。
        validation_errors = _validate_analysis(symbol, result.get("analysis_path"))
        evidence_errors = _validate_evidence(result.get("evidence_path"))
        if evidence_errors:
            # 副本级错误先机械自愈(派生重建), 治不了的才烧 repair agent。
            receipt = _rederive_evidence_file(result)
            if receipt:
                result["evidence_rederived"] = receipt
                evidence_errors = _validate_evidence(result.get("evidence_path"))
        validation_errors.extend(evidence_errors)
        if validation_errors:
            # Do not normalize debate content in the trusted parent. Give one fresh child
            # the exact gate errors and let it make a narrow, auditable schema repair.
            repair_out = STATUS_DIR / f"codex_repair_{symbol}_{_stamp()}.json"
            repair_cmd = _cmd(_repair_prompt(symbol, result, validation_errors), repair_out, sandbox, model)
            try:
                repair_proc = subprocess.run(
                    repair_cmd, cwd=str(EQUIPOISE), text=True, capture_output=True,
                    timeout=min(timeout, 1800), env=HARNESS.child_env(),
                    stdin=subprocess.DEVNULL,
                )
                repair_text = repair_out.read_text(encoding="utf-8") if repair_out.exists() else repair_proc.stdout
                repair_result = _parse_last_message(symbol, repair_text, repair_proc.stderr, 0)
                result["repair_attempted"] = True
                result["repair_receipt"] = repair_result.get("receipt")
                result["repair_last_message_path"] = str(repair_out.relative_to(EQUIPOISE))
                validation_errors = _validate_analysis(symbol, result.get("analysis_path"))
                validation_errors.extend(_validate_evidence(result.get("evidence_path")))
            except subprocess.TimeoutExpired:
                result["repair_attempted"] = True
                result["repair_receipt"] = "artifact repair TIMEOUT after 1800s"
            if validation_errors:
                result["ok"] = False
                result["validation_errors"] = validation_errors
    if result.get("ok") and truth and truth.get("model") and result.get("analysis_path"):
        _stamp_harness_truth(result["analysis_path"], truth)
    return result


def write_pg(symbol: str, analysis_path: str | None = None) -> str:
    """唯一写库出口: 跑共用脚本 pg_store_debate.py (2026-07-09 起它一条命令写 debates+evidence,
    "文件为界"架构: 沙盒内 agent 只产 JSON, PG 全在这层写)。回执含三行
    ([pg_store]+[evidence_pg]+[task_pg]; evidence 已迁 evidence_items, blob 表 FROZEN 属正常)。"""
    args = [str(PY), str(PG_STORE), symbol]
    if analysis_path:
        args.append(str(EQUIPOISE / analysis_path if not os.path.isabs(analysis_path) else analysis_path))
    try:
        proc = subprocess.run(args, cwd=str(EQUIPOISE), capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"[pg_store] EXC {type(e).__name__}: {e}"
    msg = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return " | ".join(msg.splitlines()[-3:]) if msg else f"[pg_store] exit={proc.returncode}"


def load_translations(symbol: str, analysis_path: str | None = None) -> str:
    """Publish configured overlays for this report, recovering missing translations on the cheap worker."""
    if analysis_path:
        target = analysis_path if os.path.isabs(analysis_path) else str(EQUIPOISE / analysis_path)
        cmd = [str(PY), str(EQUIPOISE / "scripts" / "ensure_report_translations.py"), target]
    else:
        cmd = [str(PY), str(EQUIPOISE / "scripts" / "translate_reports.py"), "--load-report", symbol]
    try:
        proc = subprocess.run(cmd, cwd=str(EQUIPOISE), env={**os.environ, "CODEX_BIN": str(HARNESS.executable)},
                              capture_output=True, text=True, timeout=1800)
    except Exception as e:
        return f"[i18n] EXC {type(e).__name__}: {e}"
    msg = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return "[i18n] " + (msg.splitlines()[-1] if msg else f"exit={proc.returncode}")


# (2026-07-09: write_evidence_pg / write_news_pg 已删 —— evidence 并进 pg_store_debate.py
#  单时间戳=evidence_generated_at; 新闻备餐时已入 PG, 辩后回填是重复行。)


def _prep_or_skip(symbol: str, prepare_timeout: int, force_prepare: bool) -> tuple[str, str]:
    st, rc = prepare(symbol, timeout=prepare_timeout, force=force_prepare)
    if st == "ibkr-down":
        raise IbkrDown(symbol)
    return st, rc


def run_symbol(symbol: str, timeout: int, sandbox: str, model: str | None, prepare_timeout: int,
               force_prepare: bool, skip_prepare: bool = False, stall: int = 900,
               relay_legs: int = 3) -> dict:
    if skip_prepare:
        st, prep_receipt = "ready", "prepared by trusted serial dispatcher immediately before fire"
    else:
        st, prep_receipt = _prep_or_skip(symbol, prepare_timeout, force_prepare)
    if st == "fresh-exists":
        # 该锚点已有报告(半死盈透/休市给旧bar的正常情况) — 跳过=成功语义, 别 fire 免覆盖历史
        return {"symbol": symbol, "ok": True, "receipt": f"skipped-fresh-exists: {prep_receipt}", "dur": 0}
    if st != "ready":
        return {"symbol": symbol, "ok": False, "receipt": f"prep {st}: {prep_receipt}", "dur": 0}
    result = fire(symbol, prep_receipt, timeout, sandbox, model, stall, relay_legs)
    if result.get("ok"):
        # PG 写库 best-effort (对齐 CC 哲学: 本地 JSON 已在, 写库失败不搞挂本单, 如实报)。
        # pg_store_debate.py 一条命令: debates(主产品) + evidence(best-effort)。
        result["pg_store"] = write_pg(symbol, result.get("analysis_path"))
        if "[pg_store] OK" not in str(result["pg_store"]):
            result["pg_warnings"] = [result["pg_store"]]
        else:
            # 翻译是最后一步(库主 10-25): 主会话的 Translator 已填好 data/i18n_staging/debates_<SYM>_<TS>_<lang>.json,
            # 原文入库后由这层 --load-report 入库; 失败只报不挡。
            result["translations"] = load_translations(symbol, result.get("analysis_path"))
            if '"ok": true' not in result["translations"]:
                result.setdefault("pg_warnings", []).append(result["translations"])
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=sorted(WAVES))
    ap.add_argument("--symbols", help="Comma-separated explicit symbols.")
    ap.add_argument("--group", help="One fixed research group, one arbiter session.")
    ap.add_argument("--resume-group", help="Resume this group's exact formal opening cutoff.")
    ap.add_argument("--timeout", type=int, default=9000,
                    help="Hard wall-clock backstop per debate (seconds).")
    ap.add_argument("--stall-timeout", type=int, default=900,
                    help="Liveness watchdog: kill the child after this many seconds "
                         "without any --json event output (0 disables).")
    ap.add_argument("--prepare-timeout", type=int, help="Seconds for preparation. Default: 180 per single "
                    "symbol or 180 per group member. Group queue wait is excluded.")
    ap.add_argument("--force-prepare", action="store_true",
                    help="Pass --force to prepare_debate_data.py to bypass STALE-ANCHOR/FRESH-EXISTS gates.")
    ap.add_argument("--skip-prepare", action="store_true",
                    help="Fire from data prepared immediately beforehand by a trusted serial dispatcher.")
    ap.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    ap.add_argument("--model", default=None,
                    help="Judgment-role model (default: harness model policy). "
                         "Tier overrides are user-directed. Evidence workers use the selection in .codex/run.md.")
    ap.add_argument("--relay-legs", type=int, default=3,
                    help="Max child processes per debate (接力续跑): a leg that ends without "
                         "final artifacts but advanced the audit buffers hands off to a "
                         "continuation child; zero-progress legs stop the relay (fail closed).")
    ap.add_argument("--harness", choices=HARNESS_CHOICES, default=None,
                    help="Codex identity (executable + CODEX_HOME + quota). "
                         "Default: EQUIPOISE_HARNESS env, else codex1.")
    ap.add_argument("--base", default=None,
                    help="Follow-up (delta) session on this earlier session TS of the ONE symbol given (--symbols SYM): "
                         "relayed to run_step open via EQUIPOISE_DELTA_BASE; see config/delta.yaml.")
    ap.add_argument("--why", default=None, help="One-line reason for the follow-up (EQUIPOISE_DELTA_WHY).")
    ap.add_argument("--calc-check", choices=["remind", "off", "always"], default=None,
                    help="Arithmetic handling for this batch: remind (default; a one-line reminder about "
                         "unit conversions), off (nothing added), always (every derived number is "
                         "computed with a local command).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-artifacts", nargs=3, metavar=("SYMBOL", "ANALYSIS", "EVIDENCE"),
                    help="Repair-pass self-check: run the parent's gate validators on the two "
                         "artifacts, print errors, exit 1 on any (no debate, no PG).")
    args = ap.parse_args()
    if args.base:
        _syms = [s for s in (args.symbols or '').split(',') if s.strip()]
        if args.market or len(_syms) != 1:
            ap.error('--base needs exactly one symbol (--symbols SYM) and no --market')
        os.environ['EQUIPOISE_DELTA_BASE'] = args.base.strip()
        os.environ['EQUIPOISE_DELTA_WHY'] = (args.why or '').strip()
    if args.calc_check:
        os.environ["EQUIPOISE_CALC_CHECK"] = args.calc_check

    global HARNESS
    HARNESS = resolve_codex(args.harness)
    args.model = resolve_model(HARNESS, "debate", args.model, "gpt-6.1-sol")
    if args.resume_group and not args.group:
        ap.error("--resume-group requires --group")
    if args.prepare_timeout is not None and args.prepare_timeout <= 0:
        ap.error("--prepare-timeout must be positive")
    if args.group:
        if args.symbols or args.market or args.base or args.skip_prepare or args.force_prepare or args.validate_artifacts:
            ap.error("--group is a single prepared group job; use --resume-group for an existing opening")
        from group_run import launch
        return launch(args, EQUIPOISE, HARNESS, _cmd, _run_child)

    if args.prepare_timeout is None:
        args.prepare_timeout = 180


    if args.validate_artifacts:
        sym, apath, epath = args.validate_artifacts
        errs = _validate_analysis(sym if sym.isdigit() else sym.upper(), apath)
        errs.extend(_validate_evidence(epath))
        print(json.dumps({"ok": not errs, "validation_errors": errs}, ensure_ascii=False, indent=1))
        return 0 if not errs else 1

    if args.symbols:
        plan = [("explicit", [s.strip() if s.strip().isdigit() else s.strip().upper()
                              for s in args.symbols.split(",") if s.strip()])]
    elif args.market:
        plan = WAVES[args.market]
    else:
        ap.error("Need --symbols or --market")

    print(f"# codex_loop production waves={len(plan)} timeout={args.timeout}s sandbox={args.sandbox} "
          f"harness={HARNESS.describe()} model={args.model}")
    for label, symbols in plan:
        print(f"  [{label}] {' '.join(symbols)}")
    if args.dry_run:
        return 0

    ok = total = 0
    try:
        for label, symbols in plan:
            print(f"### WAVE {label} START", flush=True)
            for symbol in symbols:
                total += 1
                result = run_symbol(symbol, args.timeout, args.sandbox, args.model or None, args.prepare_timeout,
                                    args.force_prepare, args.skip_prepare, args.stall_timeout,
                                    args.relay_legs)
                ok += int(bool(result.get("ok")))
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            print(f"### WAVE {label} DONE", flush=True)
    except IbkrDown as e:
        print(f"# STOP ibkr-down during prepare for {e}", flush=True)
        return 10
    print(f"# done {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
