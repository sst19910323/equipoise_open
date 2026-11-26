#!/usr/bin/env python3
"""director.py — 演习导演: 接过仲裁者的杂活(派辩手、执行外呼、续会话), 每个 close-round 后停下给库主看。

    ./venv/bin/python scripts/drill/director.py next  --pack <root> [--message "..."] [--harness codex3] [--model gpt-6.1-sol]
    ./venv/bin/python scripts/drill/director.py probe --pack <root>
    ./venv/bin/python scripts/drill/director.py status --pack <root>

仲裁者(sol)是一个独立 codex 会话, 整场续同一上下文; 它照 .codex/run.md + 协议走, 但不 spawn, 改回派单(见
prompts/drill/arbiter-addendum.md)。导演执行派单: 辩手 → 各自独立 sol 进程(同回合追问续它自己的会话); job → 跑
oracle_request(场外 oracle 作答)。结果续给仲裁者, 直到它回 round_closed / 完成 JSON / 看不懂的回复, 然后退出并打印
一行状态 JSON(含本段各 sol 进程的越界读取)。状态在 drill/director.json。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sol_proc as S  # noqa: E402

HEARTBEAT_MAX_AGE_S = 120
MAX_TURNS = 200
CLAUDE_MODEL = "claude-opus-5-5"
ROLE_FILES = ("zealot", "reaper", "scout", "runner")
SCOPE = ("Session status: this run's Equipoise root is {root} and every path you need is under it; read nothing outside it "
         "(instructions elsewhere to read /opt/equipoise do not apply to this run).")
GO_ON ="DIRECTOR: the owner has reviewed round {n}; continue the protocol from where you stopped."


class DirectorError(Exception):
    pass


# ---------- state ----------
def state_path(root: Path) -> Path:
    return root / "drill" / "director.json"


def load_state(root: Path) -> dict:
    p = state_path(root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"arbiter": None, "threads": {}, "status": "new",
                                                                          "turns": 0, "log": []}


def save_state(root: Path, st: dict) -> None:
    state_path(root).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def daemon_alive(root: Path, now: float | None = None) -> bool:
    hb = root / "drill" / "queue" / ".daemon_alive"
    return hb.exists() and ((now or time.time()) - hb.stat().st_mtime) <= HEARTBEAT_MAX_AGE_S


# ---------- prompts ----------
def _drill_prompt(root: Path, name: str) -> str:
    p = root / "prompts" / "drill" / name
    if not p.exists():
        p = HERE.parents[1] / "prompts" / "drill" / name
    return p.read_text(encoding="utf-8")


def prepare_receipt(pack: dict) -> str:
    return (f"[{pack['symbol']}] READY (anchor {pack['as_of']}) | data frozen for this run; the RAW DATA FILES manifest "
            "is rebuilt by `run_step open` | STATUS: ready")


def arbiter_prompt(root: Path, model: str) -> str:
    pack = S.pack_of(root)
    parts = [(root / ".codex" / "run.md").read_text(encoding="utf-8"), "",
             "## COMPLETE ROLE BLOCKS (runner-injected reference copies; subagents Read the files)\n"]
    for r in ROLE_FILES:
        f = root / ".claude" / "agents" / f"{r}.md"
        parts.append(f"<<<BEGIN {r.upper()}_ROLE_FILE {f}>>>\n{f.read_text(encoding='utf-8')}\n<<<END {r.upper()}_ROLE_FILE>>>\n")
    parts += ["## Runtime Input\n", f"SYMBOL: {pack['symbol']}", f"EQUIPOISE_ROOT: {root}", f"JUDGE_MODEL: {model}",
              f"PREPARE_RECEIPT: {prepare_receipt(pack)}", "", _drill_prompt(root, "arbiter-addendum.md")]
    return "\n".join(parts)


# ---------- reply parsing ----------
def parse_reply(text: str) -> dict | None:
    raw = (text or "").strip()
    for cand in (raw, raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else ""):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    return None


# ---------- dispatch ----------
def _abs(root: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else root / q


def run_job(root: Path, item: dict, py: list[str] | None = None) -> dict:
    py = py or [str(root / "venv" / "bin" / "python")]
    p = subprocess.run([*py, "scripts/drill/oracle_request.py", "--job", item["material"], "--out", item["output"]],
                       cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = _abs(root, item["output"])
    status = None
    try:
        status = json.loads(out.read_text(encoding="utf-8")).get("status")
    except Exception:  # noqa: BLE001
        pass
    return {"kind": "job", "job_id": item.get("job_id"), "output": item["output"], "written": out.exists(),
            "status": status, "rc": p.returncode}


def run_debater(cfg: S.SolConfig, st: dict, item: dict) -> dict:
    side = str(item.get("side", "")).lower()
    if side not in ("zealot", "reaper"):
        return {"kind": "debater", "side": side, "error": "side must be zealot or reaper"}
    mode = item.get("mode", "new")
    if item.get("material"):
        prompt = _abs(cfg.root, item["material"]).read_text(encoding="utf-8")
        if mode == "new":   # 协议许可的一句中性会话状态(全局 AGENTS.md 会把进程引去读 /opt/equipoise)
            prompt = SCOPE.format(root=cfg.root) + "\n\n" + prompt
    else:
        prompt = str(item.get("message") or "")
    if not prompt.strip():
        return {"kind": "debater", "side": side, "mode": mode, "error": "no material or message"}
    thread = None
    if mode == "follow_up":
        thread = st["threads"].get(side)
        if not thread:
            return {"kind": "debater", "side": side, "mode": mode, "error": "no open thread for this side"}
    s = S.run(cfg, side, prompt, thread)
    landed = land_output(cfg.root, prompt, s["reply"])
    return {"kind": "debater", "side": side, "mode": mode, "thread": s["thread"], "rc": s["rc"], "reply": s["reply"].strip(),
            "out_of_root": s["out_of_root"], "run": s["n"], "landed": landed}


def land_output(root: Path, material: str, reply: str) -> str | None:
    """辩手没有写文件、直接回了 JSON(协议 2.1: 没有写文件工具的引擎) → 原样落到材料最后一行给的 OUTPUT 路径。"""
    m = re.findall(r"^OUTPUT:\s*(\S+)", material, re.M)
    if not m:
        return None
    out = _abs(root, m[-1].strip("`"))
    if out.exists():
        return None
    obj = parse_reply(reply)
    if not obj or "dispatch" in obj:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return out.relative_to(root).as_posix() if out.is_relative_to(root) else str(out)


def dispatch(cfg: S.SolConfig, st: dict, items: list, py: list[str] | None = None,
             debater_cfg: "S.SolConfig | dict | None" = None) -> list[dict]:
    def one(it):
        if it.get("kind") == "job":
            return run_job(cfg.root, it, py)
        if it.get("kind") == "debater":
            dc = debater_cfg.get(str(it.get("side", "")).lower()) if isinstance(debater_cfg, dict) else debater_cfg
            return run_debater(dc or cfg, st, it)
        return {"kind": it.get("kind"), "error": "unknown kind (use debater | job)"}
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        res = list(ex.map(one, items))
    for r in res:
        if r.get("kind") == "debater" and r.get("thread"):
            st["threads"][r["side"]] = r["thread"]
    return res


def result_message(res: list[dict]) -> str:
    lines = ["DIRECTOR DISPATCH RESULT"]
    for r in res:
        if r.get("error"):
            lines.append(f"- {r.get('kind')} {r.get('side') or r.get('job_id') or ''}: NOT EXECUTED — {r['error']}")
        elif r["kind"] == "debater":
            tail = r["reply"][-3000:] if r["reply"] else "(no reply; the process ended without a final message)"
            lines.append(f"- {r['side']} ({r['mode']}, rc={r['rc']}): reply: {tail}")
        else:
            lines.append(f"- job {r['job_id']}: OUTPUT {'written' if r['written'] else 'MISSING'}: {r['output']}"
                         + (f" (status: {r['status']})" if r["status"] else ""))
    lines.append("Continue the protocol from here.")
    return "\n".join(lines)


# ---------- the loop ----------
def _arbiter(cfg: S.SolConfig, st: dict, prompt: str, flagged: list) -> dict:
    s = S.run(cfg, "arbiter", prompt, st.get("arbiter"))
    st["arbiter"] = s["thread"] or st.get("arbiter")
    st["turns"] += 1
    flagged += [{"role": "arbiter", "run": s["n"], "cmd": c} for c in s["out_of_root"]]
    return s


def advance(cfg: S.SolConfig, message: str | None = None, py: list[str] | None = None, max_turns: int = MAX_TURNS,
            debater_cfg: "S.SolConfig | dict | None" = None) -> dict:
    root = cfg.root
    if not daemon_alive(root):
        raise DirectorError("oracle daemon heartbeat missing or stale: start "
                            f"`./venv/bin/python scripts/drill/oracle_daemon.py --pack {root}` first")
    st = load_state(root)
    if st["status"] == "done":
        return {"status": "done", "final": st.get("final")}
    flagged: list = []
    if not st.get("arbiter"):
        prompt = arbiter_prompt(root, cfg.model) + ("\n\n" + message if message else "")   # a fresh arbiter on a resumed session
    else:
        prompt = message or (GO_ON.format(n=st.get("round", "?")) if st["status"] == "paused"
                             else "DIRECTOR: continue the protocol from where you stopped.")
    out: dict = {}
    for _ in range(max_turns):
        s = _arbiter(cfg, st, prompt, flagged)
        reply = parse_reply(s["reply"])
        st["log"].append({"run": s["n"], "reply_kind": (next(iter(reply)) if reply else "unparsed")})
        if reply and isinstance(reply.get("dispatch"), list):
            res = dispatch(cfg, st, reply["dispatch"], py, debater_cfg)
            flagged += [{"role": r["side"], "run": r.get("run"), "cmd": c} for r in res for c in r.get("out_of_root") or []]
            save_state(root, st)
            prompt = result_message(res)
            continue
        if reply and "round_closed" in reply:
            st.update(status="paused", round=reply["round_closed"], decision=reply.get("decision"))
            out = {"status": "paused", "round": reply["round_closed"], "decision": reply.get("decision"),
                   "terminate": str(reply.get("decision", "")).upper() == "TERMINATE"}
        elif reply and "ok" in reply:
            st.update(status="done", final=reply)
            out = {"status": "done", "final": reply}
        else:
            st["status"] = "stopped"
            out = {"status": "stopped", "why": "arbiter reply is neither a dispatch sheet, round_closed nor the completion JSON",
                   "reply": s["reply"][-2000:]}
        break
    else:
        st["status"] = "stopped"
        out = {"status": "stopped", "why": f"no pause after {max_turns} arbiter turns"}
    st.setdefault("flagged", []).extend(flagged)
    save_state(root, st)
    out["out_of_root_reads"] = flagged
    return out


def probe(cfg: S.SolConfig) -> dict:
    pack = S.pack_of(cfg.root)
    text = _drill_prompt(cfg.root, "knowledge-probe.md").replace("{AS_OF}", pack["as_of"])
    s = S.run(cfg, "probe", text)
    sealed = Path(pack["sealed"])
    sealed.mkdir(parents=True, exist_ok=True)
    rec = {"model": cfg.model, "reply": s["reply"], "run": s["n"], "rc": s["rc"]}
    (sealed / "probe.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    # 回复只进封存目录; 沙箱里的 runs/ 留痕去掉回复正文, 免得辩手进程翻到
    runs = cfg.root / "drill" / "runs"
    (runs / f"{s['n']:03d}-probe.reply.txt").unlink(missing_ok=True)
    ev = runs / f"{s['n']:03d}-probe.events.jsonl"
    if ev.exists():
        ev.replace(sealed / "probe.events.jsonl")
    js = runs / f"{s['n']:03d}-probe.json"
    js.write_text(json.dumps({**json.loads(js.read_text(encoding="utf-8")), "reply": "(sealed)"}, ensure_ascii=False,
                             indent=1), encoding="utf-8")
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["next", "probe", "status"])
    ap.add_argument("--pack", required=True)
    ap.add_argument("--message")
    ap.add_argument("--harness")
    ap.add_argument("--model", default=S.MODEL)
    ap.add_argument("--debater-engine", choices=["codex", "claude"], help="天花板对照: 辩手换引擎(仲裁者仍是 --model)")
    ap.add_argument("--debater-model")
    ap.add_argument("--reaper-engine", choices=["codex", "claude"], help="只换 Reaper 的引擎(对照: 同一个 sol Zealot / 仲裁者)")
    a = ap.parse_args(argv)
    root = Path(a.pack)
    if not (root / "drill" / "pack.json").exists():
        print(f"not a drill pack: {root}", file=sys.stderr)
        return 2
    if a.cmd == "status":
        print(json.dumps(load_state(root), ensure_ascii=False, indent=1))
        return 0
    cfg = S.SolConfig(root=root, exe=S.default_exe(a.harness), model=a.model)
    st = load_state(root)
    deb = st.get("debaters") or {}
    if a.debater_engine:
        deb = {"engine": a.debater_engine, "model": a.debater_model or (CLAUDE_MODEL if a.debater_engine == "claude" else a.model)}
        if st.get("arbiter") and deb != st.get("debaters"):
            print(json.dumps({"status": "refused", "why": "debater engine is fixed for a run once it has started"}))
            return 3
        st["debaters"] = deb
        save_state(root, st)
    if a.reaper_engine:
        want = {"reaper": a.reaper_engine}
        if st.get("arbiter") and st.get("side_engines") != want:
            print(json.dumps({"status": "refused", "why": "debater engine is fixed for a run once it has started"}))
            return 3
        st["side_engines"] = want
        save_state(root, st)
    dcfg = None
    if deb.get("engine") == "claude":
        dcfg = S.SolConfig(root=root, exe=["claude"], model=deb["model"], engine="claude")
    elif deb.get("model"):
        dcfg = S.SolConfig(root=root, exe=cfg.exe, model=deb["model"])
    se = st.get("side_engines") or {}
    if se:
        mk = lambda e: (S.SolConfig(root=root, exe=["claude"], model=CLAUDE_MODEL, engine="claude") if e == "claude"
                        else S.SolConfig(root=root, exe=cfg.exe, model=cfg.model))
        dcfg = {side: (mk(se[side]) if side in se else dcfg) for side in ("zealot", "reaper")}
    try:
        out = probe(cfg) if a.cmd == "probe" else advance(cfg, a.message, debater_cfg=dcfg)
    except DirectorError as e:
        print(json.dumps({"status": "refused", "why": str(e)}, ensure_ascii=False))
        return 3
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
