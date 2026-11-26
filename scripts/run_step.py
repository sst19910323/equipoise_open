#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_step.py — 辩论编排的步骤脚本(重建批 3, 2026-09-30; 两引擎共用)。

流程进代码, 判断留模型: 主会话(仲裁者, CC 的 /run 或 Codex 的 run.md)只在判断点被调用——spawn 辩手、
核定证据、写评注与私记、终裁两遍、再定稿。循环、路由、编号、视图、材料拼装、组装、过闸全部在这里。
协议正文见 orchestration/run-protocol.md; 本脚本的每个子命令 = 协议里的一步, 输入是文件路径, 输出是
一行 JSON 回执(回执里给出下一步该做什么与要 Read 的路径)。

子命令(全部带 --symbol; open 以外带 --ts):
  open        --symbol X --engine cc|codex --model <id> [--worker-model <id>] [--max-rounds 20] [--beat-ref 3 ...]
              数据准备回执 → 会话状态 → 首轮两份 spawn 材料
  route       --ts T --round N --beat K [--zealot beat.json] [--reaper beat.json]
              归档本拍(need_more → beat, final → final); 聚合申报, 去重与闸, 数据调用即刻机械执行,
              Scout / Runner 作业写成材料文件(主会话据此 spawn)
  close-beat  --ts T --round N --beat K --results results.json
              归档 Scout / Runner 结果(带仲裁者核定的 status); 生成各方回执与再唤材料
  tally       --ts T --round N
              双方终稿齐 → 账本整理机械行 + 仲裁者视图 + 评注手册路径
  close-round --ts T --round N --notes notes.json
              归档评注与私记; 硬终止条件归代码; 重建视图; 下轮 spawn 材料
  finalize    --ts T --stage provisional|audit|proposed|objection|ruling|signoff [--input x.json] ...
              终裁两遍 → 拟定终裁 → 异议回合 → 再定稿(过闸) → 签章 → 组装 → 出厂闸
  status      --ts T
              续跑定位: 停在哪一步, 下一条命令, 要用的材料文件
  gate        --analysis <Analysis.json>
              同一条出厂闸(finalize 与写库前都跑它)
  --self-test 用 mock 引擎跑通整条协议(见 debate_driver.py)

退出码: 0 ok / 1 回执 ok:false(参数或状态错) / 10 数据未齐 / 20 本场失败停机。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台防线
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from debate_store import Store, StoreError, ROLES, ARBITER, _round_name  # noqa: E402

# 参考值(库主 2026-09-30 定): 只有 max_rounds 是防爆硬上限(到了强制收场, open 主张记保留未决);
# 其余全是指引——写进材料让辩手 / 仲裁者知道数字并尽量守, 脚本不机械拦截。
DEBATER_CAN_COMPUTE = {"codex"}  # 哪些引擎的辩手有能跑命令的工具(全部计算档要用)
DEFAULT_CAPS = {"beat_ref": 3, "max_rounds": 20, "model_call_ref": 8,
                "objection_rounds": 2, "arbiter_query_ref": 2,
                "round_ref": 5}   # reference session length (owner 2026-10-31): 3-4 rounds is normal, 2 is not; guidance, not a gate
EXPLAIN_ROUND_REF = ("a session usually carries several topics: a small topic closes in one round, a large one takes several, and several run side by side when none is complex")
ROLE_FILES = {"Zealot": ".claude/agents/zealot.md", "Reaper": ".claude/agents/reaper.md",
              "Scout": ".claude/agents/scout.md", "Runner": ".claude/agents/runner.md"}
MANUALS = {"evidence_review": "prompts/agent/manuals/synthesis/arbiter-evidence-review.md",
           "round_notes": "prompts/agent/manuals/synthesis/arbiter-round-notes.md",
           "final_ruling": "prompts/agent/manuals/synthesis/arbiter-final-ruling.md",
           "execution_lines": "prompts/agent/manuals/operation/P06-execution-lines.md",
           "principles": "prompts/agent/principles.md",
           "arbiter_role": "prompts/agent/roles/arbiter.md",
           "format_final": "prompts/agent/formats/final.md",
           "format_evidence": "prompts/agent/formats/evidence.md",
           "format_objection": "prompts/agent/formats/objection.md",
           "format_signoff": "prompts/agent/formats/signoff.md",
           "capability_map": "prompts/agent/manuals/evidence/ibkr_capability_map.md"}
# v3.4.1 wave 3: the multi-lens protocol, one file, loaded only when lenses_of(state) is non-empty (debaters: the
# LENSES block's REQUIRED READ line; arbiter: the receipts' reads). Single-lens material never names it.
LENSES_PROTOCOL = "prompts/agent/formats/lenses.md"
SESSION_BEFORE_RULER = ("session opened before v3.4 (no odds snapshot in _session.json); cannot resume — "
                        "open a new session")
# 3.4 receipts (C6): the arbiter writes the ruling sheet, the weight and the voids; the script derives the rest
PROV_NEXT = ("First pass (3.4): write prov.json = `claims_review` [{claim, claim_rev, verdict adopted|partly|rejected (+ `scale` with partly), reason}], `mixture` {w, reason}, "
                "optional `voided` {effects, candidates} (documented invalidity only), `mentality`, `mentality_basis`, `rationale`. "
                "Do not write `scenario`, `operation_*` or `composite_score`: the script replays Z′ / R′, mixes A at w and derives "
                "the strategy, `operation_*` and `composite_score` (an authored derived field is rejected). Do not run the tally yet. "
                "Then `finalize --stage provisional --input prov.json`")
AUDIT_NEXT = ("Second pass (3.4): the three audit questions on the replayed Z′ / R′ in PROV (did every adopted claim's "
                 "registered effects land, or was the effect voided with a documented reason; did one effect enter twice; "
                 "was a settled topic overturned); write the draft ruling proposed.json = the authored fields of formats/final.md "
                 "(`claims_review`, `mixture`, `voided`, mentality, reasoning, risk and review fields); the script derives "
                 "`scenario` (A), `operation_*` and `composite_score` — never write them. "
                 "`finalize --stage proposed --input proposed.json`")
ENGINE_META = {"cc": {"engine_family": "Claude Code", "runner": "apex-run-debates-cc"},
               "codex": {"engine_family": "GPT/Codex", "runner": "apex-run-debates-codex"},
               "mock": {"engine_family": "mock", "runner": "apex-debate-driver"}}
TOPOLOGY = ("Zealot and Reaper were real, topologically independent spawned subagents; "
            "the main session served as arbiter and ran the step script (run_step.py) for "
            "routing, archiving, views and assembly.")
JSON_ONLY = "Output exactly one JSON object (no explanatory text, markdown markup, or code fences)."
DATA_DENY = "Data-call duplicate gate: default call of this symbol against its own benchmark; the snapshot already supplies it; skipped mechanically"
MODEL_MANUAL_DIRS = ("understanding", "mentality", "operation")
# 合本(2026-10-02): 几个编号住在同一本手册里, 各占一节。编号不变(申报、历史记录、触发信号都还用它),
# 值 = (文件所用的编号, 该编号在那本里的节名)。没列在这里的编号 = 一个编号一本。
CALC_MODES = ("remind", "off", "always")  # 算术提示的档位: 提醒(默认) / 不额外干预 / 全部计算; 文案在语料里
CALC_LINES_FILE = "prompts/agent/roles/calc-lines.md"


def calc_line(mode: str, root: Path) -> str:
    """当前档对应的那一句; off 档或文件里没有这一档 → 空串(任务材料里就不出现这一行)。"""
    if mode not in CALC_MODES or mode == "off":
        return ""
    p = root / CALC_LINES_FILE
    if not p.exists():
        return ""
    for raw in p.read_text(encoding="utf-8").splitlines():
        if raw.startswith(mode + ":"):
            return raw[len(mode) + 1:].strip()
    return ""


MODEL_SECTIONS = {"P02": ("P02", "Crowding"), "P03": ("P02", "Flow attribution"),
                  "P04": ("P05", "Magnitude and threshold"), "P05": ("P05", "Price action"), "P07": ("P05", "Trend structure")}
# 这些编号的判法要读技术快照(ATR、指标现值、摆动点), 给执行者的材料里附上快照路径。
MODELS_NEED_SNAPSHOT = {"P04", "P05", "P06", "P07"}


_STRATEGY_TAIL = re.compile(r" · best .*$")


def public_ruler_lines(lines: list) -> list:
    """The ruler lines as the debaters see them (v3.6, prediction / execution separation): the per-side score line keeps
    `S_ref` and drops everything from `· best` on (strategy winner, q*, x*, per-strategy x*); decomposition and extras
    lines pass through; the header loses the q* / x* legend."""
    out = []
    for ln in lines:
        if ln.startswith("[Scenario ruler"):
            out.append("[Scenario ruler 3.4 — machine-computed per side on its own distribution; S_ref = 100·G/(G+L); "
                       "strategy comparison is the arbiter's and the final report's, not shown here]")
        elif " scenario (" in ln and " · best " in ln:
            out.append(_STRATEGY_TAIL.sub("", ln))
        else:
            out.append(ln)
    return out


class StepError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ts(ts: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", ts or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}Z" if m else ts


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path.as_posix()


def _sig(req: dict) -> str:
    return json.dumps(req, ensure_ascii=False, sort_keys=True)


def drill_command(root: Path, job: dict) -> str | None:
    """演习(PTR)开关: 根目录下有 drill/pack.json 才生效 —— 这条任务不 spawn 执行者, 改跑排队命令(沙箱外的 oracle 按 as_of 作答)。
    生产根目录没有这个文件, 返回 None, 回执与现在一字不差。"""
    if not (root / "drill" / "pack.json").exists():
        return None
    return f"./venv/bin/python scripts/drill/oracle_request.py --job {job['material']} --out {job['output']}"


def model_manual_path(model: str, root: Path = ROOT) -> str | None:
    """P0X → prompts/agent/manuals/<链>/P0X-*.md(唯一一处定义); 合本里的编号指到它所在的那一本。"""
    if not model:
        return None
    file_id = MODEL_SECTIONS.get(model, (model, ""))[0]
    for d in MODEL_MANUAL_DIRS:
        hits = sorted((root / "prompts" / "agent" / "manuals" / d).glob(f"{file_id}-*.md"))
        if hits:
            return hits[0].relative_to(root).as_posix()
    return None


def _safe_validate(doc: dict, path: str, pre_signoff: bool = False) -> tuple[list, list]:
    """校验器自身抛异常 = 一条 fatal(2026-09-30 AAPL 冒烟案: 崩在闸里会把已归档的再定稿悬在半空)。"""
    import validate_debate
    try:
        return validate_debate.validate(doc, path, pre_signoff=pre_signoff)
    except Exception as exc:  # noqa: BLE001
        return [f"Validator exception (engineering defect, not a report problem; report to the user): {type(exc).__name__}: {exc}"], []


def session_cfg(session: "Session") -> dict:
    """The session's odds ruler — the one config entry point for every stage after open (C4): `config_from_snapshot(
    _session.json.odds)`, written at open and never re-read from config/odds.yaml. A session without `odds` was opened
    before v3.4 and cannot be resumed (StepError)."""
    from odds_model import config_from_snapshot
    snap = session.state.get("odds")
    if not (isinstance(snap, dict) and snap):
        raise StepError(SESSION_BEFORE_RULER)
    return config_from_snapshot(snap)


# ---------------- tools (真实 = 子进程; 测试注入 mock) ----------------

class Tools:
    def __init__(self, root: Path):
        self.root = root
        self.py = str(root / "venv" / "bin" / "python") if (root / "venv").exists() else sys.executable

    def evidence_quant(self, req: dict) -> str:
        p = subprocess.run([self.py, "evidence_quant.py", json.dumps(req, ensure_ascii=False)],
                           cwd=str(self.root), capture_output=True, text=True, timeout=300)
        return (p.stdout or p.stderr or "").strip()

    def tally(self, entries: list, **kw) -> dict:
        from ledger_tally import tally
        return tally(entries, **kw)

    def tally_brief(self, res: dict) -> str:
        from ledger_tally import brief
        return brief(res)


class MockTools(Tools):
    def evidence_quant(self, req: dict) -> str:
        mode = req.get("mode")
        if mode == "event_calendar":
            return json.dumps({"ok": True, "mode": mode, "data": {"events": []}}, ensure_ascii=False)
        if req.get("symbol") == "NOPE":
            return json.dumps({"ok": False, "error": "mock: no such symbol", "gaps": ["NOPE"]})
        return json.dumps({"ok": True, "mode": mode, "data": {"note": "mock", "value": 1.23}}, ensure_ascii=False)


# ---------------- session ----------------

class Session:
    def __init__(self, symbol: str, ts: str, root: Path = ROOT, tools: Tools | None = None):
        self.symbol, self.ts, self.root = symbol.upper(), ts, root
        self.store = Store(self.symbol, ts, root)
        try:
            self.store.ensure_dirs()
        except OSError as exc:
            raise StepError(f"Cannot create the object store or buffer directories ({exc}); the parent layer must first create "
                            "data/store/<SYM>, data/debate/<SYM>, data/evidence/<SYM> (a sandboxed subprocess cannot create "
                            "a new symbol's top-level directories)")
        self.state_path = self.store.dir / "_session.json"
        self.tools = tools or Tools(root)
        self.state: dict = {}
        if self.state_path.exists():
            self.state = _read_json(self.state_path)

    # ---------- state ----------
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8", newline="\n")

    def _require(self) -> None:
        if not self.state:
            raise StepError(f"no session at {self.state_path.relative_to(self.root).as_posix()} — run `open` first")
        if self.state.get("status") == "failed":
            raise StepError(f"session failed: {self.state.get('failure')}", 20)

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _write(self, path: Path, text: str) -> str:
        """写材料 / 回执文件, 返回相对工程根的路径(回执与材料里一律相对路径)。"""
        _write_text(path, text)
        return self._rel(path)

    def schema(self) -> str | None:
        """Session schema = the odds snapshot's `schema` (written at open); None = a session opened before v3.4."""
        return (self.state.get("odds") or {}).get("schema")

    def _view_seq(self, view: Path | None) -> int:
        """Highest manifest seq (line position) among the objects a view contains (finals' appendix included); 0 = empty."""
        if view is None or not view.exists():
            return 0
        seqs = {r["id"]: i for i, r in enumerate(self.store.rows(), 1)}
        best = 0
        for raw in view.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            line = json.loads(raw)
            for oid in [line.get("id")] + [a.get("id") for a in line.get("appendix") or [] if isinstance(a, dict)]:
                best = max(best, seqs.get(oid, 0))
        return best

    def _spawned(self, key: str, view: Path | None, path: Path, text: str, floor: int = 0) -> str:
        """Write a material with its `view_seq` header and record it in _session.json.spawns[key] (C5) with `at_seq`
        (manifest length when written: a final at a higher seq was filed after reading it). `floor` = what the same
        thread already saw (a later-beat material keeps the spawn's view)."""
        n = max(self._view_seq(view), floor)
        # header block: the ROLE FILE line stays first (Codex loads the role through it), view_seq sits before TASK
        cut = text.find("TASK: ")
        rel = self._write(path, (text[:cut] + f"view_seq: {n}\n" + text[cut:]) if cut >= 0 else f"view_seq: {n}\n" + text)
        self.state.setdefault("spawns", {})[key] = {"view_seq": n, "material": rel, "at_seq": len(self.store.rows())}
        self.save()
        return rel

    def _fail(self, why: str) -> None:
        self.state["status"] = "failed"
        self.state["failure"] = why
        self.save()
        raise StepError(why, 20)

    @property
    def caps(self) -> dict:
        return self.state.get("caps") or dict(DEFAULT_CAPS)

    # ---------- open ----------
    @classmethod
    def open(cls, symbol: str, root: Path = ROOT, engine: str = "cc", model: str = "",
             worker_model: str = "", caps: dict | None = None, tools: Tools | None = None,
             calc_check: str = "", base: str = "", why: str = "", *, group_ref: dict | None = None) -> "Session":
        """The odds snapshot of config/odds.yaml (its `schema`) is written into _session.json.odds here (C4).
        base (or EQUIPOISE_DELTA_BASE): open a follow-up (delta) session on that earlier session of the same symbol —
        scripts/delta_session.py: its verified Scout evidence is imported as standing evidence, the reference length is
        config/delta.yaml round_ref, lineage is recorded; nothing of its ruling is passed. why (or EQUIPOISE_DELTA_WHY):
        the dispatcher's one-line reason (a line drift, an event), shown to the debaters as the delta focus."""
        from workflows.data_manifest import build_manifest, render_raw_data_files, render_trigger_signals
        m = build_manifest(symbol, root)
        if not m["ok"]:
            raise StepError("Data not ready, required files missing: " + "; ".join(m["missing"])
                            + " — run data preparation prepare_debate_data.py to READY first, then open", 10)
        s = cls(symbol, group_ref["as_of"] if group_ref else m["ts"], root, tools)
        if s.state and s.state.get("group_session") != group_ref:
            raise StepError("A different hearing owns this member and cutoff")
        if s.state and s.state.get("status") not in (None, "failed"):
            return s  # 续跑: 会话已存在, 材料已在
        c = dict(DEFAULT_CAPS)
        c.update(caps or {})
        base = (base or os.environ.get("EQUIPOISE_DELTA_BASE", "") or "").strip()
        if base and group_ref:
            raise StepError("Group opening requires a fresh hearing")
        why = (why or os.environ.get("EQUIPOISE_DELTA_WHY", "") or "").strip()
        lineage, dcfg = None, None
        if base:
            import delta_session as ds
            dcfg = ds.load_cfg(root)
            chk = ds.check_base(symbol, base, m["ts"], root, dcfg)
            if not chk["ok"]:
                raise StepError(f"delta session refused: {chk['why']} — open a full session instead (no --base)", 10)
            if "round_ref" not in (caps or {}):
                c["round_ref"] = int(dcfg.get("round_ref", 3))
            lineage = {"mode": "delta", "base_as_of": base, "depth": chk["depth"], "why": why, "imported": []}
        calc = calc_check or os.environ.get("EQUIPOISE_CALC_CHECK", "")  # 命令行优先, 其次环境变量, 都没有就是 remind
        calc = calc if calc in CALC_MODES else "remind"
        s.state = {
            "symbol": s.symbol, "ts": s.ts, "context_type": m["context_type"], "region": m["region"],
            "benchmark": m["benchmark"], "sector": m["sector"], "lenses": m["lenses"],
            "protection_required": engine in ("cc", "codex"),
            "context_manual": m["context_manual"], "raw_data_files": render_raw_data_files(m),
            "trigger_signals": m["trigger_signals"], "data_files": m["data_files"],
            "warnings": m["warnings"], "engine": engine, "model": model, "worker_model": worker_model or model,
            "caps": c, "calc_check": calc, "executed_sigs": [], "focus": "(opening: independent judgment on each side)", "proactive": [],
            "status": "debating", "round": 1, "beat": {"Zealot": 0, "Reaper": 0},
            "unresolved": [], "event_text": "", "currency": m.get("currency") or "",
        }
        if group_ref:
            s.state["group_session"] = dict(group_ref)
        from odds_snapshot import OddsSnapshotError, build_odds_snapshot
        try:
            s.state["odds"] = build_odds_snapshot(s.state, s.symbol, root)   # C4: the ruler for every later stage
        except OddsSnapshotError as exc:
            raise StepError(str(exc), 20) from exc
        from opening_anchors import build_anchor_lines   # drill r1-rational-3: machine anchors for the opening
        s.state["opening_anchors"] = build_anchor_lines(s.state, root)
        if "lens_weights_default" in s.state["odds"]:   # multi-lens: the snapshot is the frozen source (C4)
            s.state["lens_weights_default"] = s.state["odds"]["lens_weights_default"]
        if lineage:
            import delta_session as ds
            base_odds = Store(symbol, base, root).session_state().get("odds") or {}
            lineage["price_move"] = ds.price_move_text(base_odds.get("reference_price"),
                                                       s.state["odds"].get("reference_price"), s._atr_pct())
            s.state["lineage"] = lineage
            s.state["focus"] = ds.focus_line(lineage)
        if m["region"] != "US":  # 非美: 事件日历前置(本地日历, 不碰盈透), 空也要贴
            out = s.tools.evidence_quant({"type": "data", "mode": "event_calendar", "symbol": s.symbol})
            s.state["event_text"] = out[:3000] or "(no registered events in the window)"
        s.save()
        s.store.dir.mkdir(parents=True, exist_ok=True)
        s._archive_prepared(m["files"])
        if lineage:
            s._import_base(lineage, dcfg)
        return s

    def _import_base(self, lineage: dict, dcfg: dict) -> None:
        """Delta session: re-mint the base session's verified Scout evidence as this session's arbiter evidence and
        write the standing-evidence side file the round-1 material points to."""
        import delta_session as ds
        base_store = Store(self.symbol, lineage["base_as_of"], self.root)
        items = ds.import_evidence(self.store, base_store, lineage["base_as_of"], dcfg, by=ARBITER)
        lineage["imported"] = [it["id"] for it in items]
        lineage["imported_origin"] = {it["id"]: it["origin_id"] for it in items}
        self._write(self.store.dir / "_standing_evidence.md", ds.standing_text(items, lineage["base_as_of"], lineage))
        self.state["lineage"] = lineage
        self.save()

    def _standing_block(self, round_: int) -> str:
        """Round-1 material block of a delta session: where the standing evidence is and the one sentence that keeps the
        escalation path open ('' for a full session and for later rounds, whose views carry the A.EV segments)."""
        lin = self.state.get("lineage") or {}
        if round_ != 1 or lin.get("mode") != "delta":
            return ""
        import delta_session as ds
        p = self._rel(self.store.dir / "_standing_evidence.md")
        n = len(lin.get("imported") or [])
        return (f"\n--- STANDING EVIDENCE (follow-up assessment on base {lin.get('base_as_of')}; {n} items) ---\n"
                f"{p} (Read in full: facts verified in the previous session, re-minted as A.EV ids and reused without routine "
                f"re-verification; the previous ruling is withheld on purpose)\n{ds.STANDING_SENTENCE}\n{ds.CLAIM_RULE}\n")

    def _archive_prepared(self, files: list[dict]) -> None:
        """数据准备已算好的机器数(同业横向 / 趋势结构 / dealer gamma / 非美事件日历)开场即归档成证据:
        辩手从 RAW DATA FILES 直接读同一份文件, 这里保证全文进终报 evidence[].payload_text(全文铁律)。"""
        items = []
        for f in files:
            if "data/evidence_prep/" not in f["path"]:
                continue
            mode = next((k for k in ("peer_compare", "dealer_gamma") if f"_{k}_" in f["path"]), "prepared")
            try:
                text = (self.root / f["path"]).read_text(encoding="utf-8")
            except OSError:
                continue
            items.append((mode, f["desc"], text, f["path"]))
        if self.state.get("event_text"):
            items.append(("event_calendar", "Local event calendar (non-US, loaded before the opening)", self.state["event_text"], None))
        for mode, desc, text, path in items:
            try:
                ok = json.loads(text).get("ok") is not False
            except (ValueError, AttributeError):
                ok = bool(text.strip())
            content = {"kind": "data", "mode": mode, "prepared": True,
                       "request": {"type": "data", "mode": mode, "symbol": self.symbol, "prepared": True},
                       "question": f"Precomputed by data preparation: {desc}",
                       "simple": ("Machine figures from the data-preparation stage; full text in payload_text (debaters read the same file "
                                  "directly from RAW DATA FILES)"
                                  if ok else "NOT_FOUND — data preparation could not compute it (ok:false in the file)"),
                       "detailed": text, "payload_text": text,
                       "source": [f"data:prepare:{mode}:{self.symbol}"]}
            if path:
                content["source_file"] = path
            self.store.put("evidence", content, by=ARBITER, round_=1,
                           meta={"status": "verified" if ok else "not_found", "kind": "data"})

    def _started(self) -> bool:
        return any(r["kind"] in ("beat", "final") for r in self.store.rows())

    def coverage(self) -> dict:
        """充分性的机器事实(不是规则): 本场外呼条数, 以及至今没有任何外呼指向的未结主张。"""
        calls = {"search": 0, "data": 0, "model_call": 0}
        by_round: dict[str, int] = {}
        targeted = ""
        arbiter_queries = 0
        for r in self.store.rows():
            if r["kind"] not in ("evidence", "model_call"):
                continue
            rec = self.store.obj(r["id"])
            c, meta = rec.get("content") or {}, rec.get("meta") or {}
            if c.get("prepared"):
                continue
            k = "model_call" if r["kind"] == "model_call" else (meta.get("kind") or c.get("kind") or "search")
            calls[k] = calls.get(k, 0) + 1
            by_round[_round_name(r.get("round") or 1)] = by_round.get(_round_name(r.get("round") or 1), 0) + 1
            if r.get("by") not in ROLES:
                arbiter_queries += 1
            req = meta.get("model_call_request") or meta.get("request") or {}
            targeted += " " + str(req.get("target_claim") or "") + " " + str(req.get("issue") or "")
        res = self.tools.tally(self._entries()) if self.store.debate_buf.exists() else {}
        untouched = []
        for side in ("zealot", "reaper"):
            for cid, p in (((res.get(side) or {}).get("claims")) or {}).items():
                if p.get("status") == "open" and not re.search(rf"(?<![A-Za-z0-9]){re.escape(cid)}(?![A-Za-z0-9])", targeted):
                    untouched.append({"id": cid, "stake": p.get("stake")})
        untouched.sort(key=lambda x: -(x["stake"] or 0))
        return {"rounds_done": self.store.last_final_round(), "calls": calls, "calls_total": sum(calls.values()),
                "calls_by_round": by_round, "arbiter_queries": arbiter_queries,
                "model_call_ref": self.caps.get("model_call_ref"), "open_claims_without_any_call": untouched}

    def _coverage_text(self) -> str:
        cv = self.coverage()
        c = cv["calls"]
        un = " ".join(f"{u['id']} (stake {u['stake']})" for u in cv["open_claims_without_any_call"]) or "none"
        return (f"Outcalls so far this session: {cv['calls_total']} (evidence search {c['search']} · data call {c['data']} · "
                f"model call {c['model_call']}; model-call reference budget {cv['model_call_ref']}, a guide not a gate). "
                f"Unsettled claims that no outcall has yet addressed: {un}.")

    def where(self) -> dict:
        """续跑定位: 从会话状态与 manifest 推出现在停在哪一步、下一条命令是什么。"""
        st, ids = self.state, self.store.ids()
        S = f"--symbol {self.symbol} --ts {self.ts}"
        d = self._rel(self.store.dir)
        out = {"ok": True, "symbol": self.symbol, "ts": self.ts, "status": st.get("status"),
               "round": st.get("round"), "objects": len(ids), "store": d}
        status = st.get("status")
        if status == "failed":
            out["next"] = f"Session failed and stopped: {st.get('failure')}"
        elif status == "done":
            out["next"] = "Done; pre-write gate: `gate --analysis data/debate/%s/%s_Analysis_%s.json`" % (
                self.symbol, self.symbol, self.ts)
        elif status == "debating":
            n = st.get("round") or 1
            finals = [r for r in ROLES if f"R{n}.{ROLES[r]}" in ids]
            if len(finals) == 2:
                out["next"] = f"R{n} both finals in; `tally {S} --round {n}` → write round notes → `close-round`"
            else:
                open_ids = set(st.get("open_jobs") or [])
                by_owner: dict = {}
                for jp in sorted(self.store.dir.glob(f"_jobs_R{n}_B*.json")):
                    b = int(jp.stem.rsplit("B", 1)[-1])
                    for j in _read_json(jp):
                        if j["job_id"] in open_ids:
                            who = j["by"] if j["by"] in ROLES else ARBITER
                            by_owner.setdefault(who, {"beat": b, "jobs": [], "materials": []})
                            by_owner[who]["jobs"].append(j["job_id"])
                            by_owner[who]["materials"].append(j.get("material"))
                roles, todo = {}, {}
                for role in list(ROLES) + [ARBITER]:
                    if role in finals:
                        roles[role] = {"state": "final"}
                    elif role in by_owner:
                        o = by_owner[role]
                        roles[role] = {"state": "jobs_in_flight", **o,
                                       "next": (f"After executing and verifying, `close-beat {S} --round {n} --beat {o['beat']} "
                                                f"--role {role.lower()} --results results.json`")}
                    elif role in ROLES:
                        k = st.get("beat", {}).get(role, 0)
                        todo[role] = (f"{d}/_spawn_R{n}_{role.lower()}.md" if k == 0
                                      else f"{d}/_resume_R{n}_B{k + 1}_{role.lower()}.md")
                        roles[role] = {"state": "awaiting_beat", "beat": k + 1, "material": todo[role],
                                       "output": self._rel(self._out(f"R{n}_B{k + 1}_{role.lower()}")),
                                       "next": f"When its beat arrives, `route {S} --round {n} --beat {k + 1} --{role.lower()} <OUTPUT path>`"}
                out["roles"] = roles
                if todo:
                    out["materials"] = todo
                out["next"] = (f"R{n}: the two debaters proceed beat by beat independently; follow each one's next under roles "
                               f"(first come first served, do not wait for the other side); a debater whose thread is gone "
                               f"is re-spawned with material plus the path of its previous beat")
        else:  # finalizing
            k = st.get("objection_round") or 1
            rulings = [r for r in self.store.rows() if r["kind"] == "ruling"]
            if "PROV" not in ids:
                out["next"] = f"`finalize {S} --stage provisional` (first pass: the verdict list)"
            elif "PROPOSED" not in ids:
                out["next"] = f"`finalize {S} --stage audit` → write the draft ruling → `finalize {S} --stage proposed --input proposed.json`"
            elif not rulings and not (f"OBJ{k}.Z" in ids and f"OBJ{k}.R" in ids):
                out["materials"] = {r: f"{d}/_spawn_OBJ{k}_{r.lower()}.md" for r in ROLES}
                out["next"] = f"Objection round {k}: spawn both debaters afresh; once received, `finalize {S} --stage objection --round {k} --zealot z.json --reaper r.json`"
            elif not rulings:
                out["next"] = f"Redraft: write ruling.json, then `finalize {S} --stage ruling --input ruling.json`"
            elif not st.get("ruling_version"):
                out["next"] = (f"{rulings[-1]['id']} archived but not yet through the gate: `finalize {S} --stage ruling` (without --input = "
                               f"reassemble and gate the latest archived redraft without minting a new version); if the gate "
                               f"fails, resubmit a revision with --input")
            else:
                v = st["ruling_version"]
                out["materials"] = {r: f"{d}/_spawn_SIGN{v}_{r.lower()}.md" for r in ROLES}
                out["next"] = f"Signoff: spawn both debaters afresh; once received, `finalize {S} --stage signoff --zealot z.json --reaper r.json`"
        return out

    def open_receipt(self) -> dict:
        if self._started():  # 续跑: 已有拍或终稿, 不重发首轮材料
            w = self.where()
            w.update({"resumed": True, "arbiter_reads": self._arbiter_reads(MANUALS["principles"], MANUALS["arbiter_role"]),
                      "caps": self.caps})
            return w
        mats = {r: self.spawn_material(r, 1) for r in ROLES}
        return {"ok": True, "symbol": self.symbol, "ts": self.ts, "context_type": self.state["context_type"],
                "region": self.state["region"], "store": self._rel(self.store.dir),
                "session": self._rel(self.state_path), "warnings": self.state["warnings"],
                "trigger_signals": len(self.state["trigger_signals"]), "caps": self.caps,
                "lineage": self.state.get("lineage"),
                "arbiter_reads": self._arbiter_reads(MANUALS["principles"], MANUALS["arbiter_role"]),
                "spawn": mats,
                "next": "spawn Zealot and Reaper (each material file in full as the spawn message; keep the threads); once both JSON files are in, "
                        "`route --round 1 --beat 1 --zealot z.json --reaper r.json`"}

    # ---------- materials(spawn prompt 只做机械拼接) ----------
    def _out(self, name: str) -> Path:
        return self.store.dir / f"_out_{name}.json"

    def _output_block(self, name: str) -> str:
        """每份材料给定独占输出路径。能写文件的引擎: 子代理把 JSON 一次写进文件、只回路径(不在消息里再贴一遍);
        辩手没有写文件工具的引擎(CC): 子代理直接输出 JSON, 主会话原样落到同一路径。"""
        rel = self._rel(self._out(name))
        if (self.state.get("engine") or "cc") == "cc":
            return f"OUTPUT: {rel} (written to disk by the main session) — {JSON_ONLY}\n"
        return (f"OUTPUT: {rel} — write the complete JSON object into this file once (no explanatory text, markdown markup, "
                f"or code fences), then reply with only this path; do not paste the JSON again in the message.\n")

    @staticmethod
    def _role_line(role_file: str) -> str:
        """每份新 spawn 材料的第一行: Codex 不自动装载角色文件, 这是内容层进入子代理的唯一通道。"""
        return (f"ROLE FILE (first action): {role_file} — Read it in full as your system prompt; no skimming. "
                f"The CC engine loads it automatically when spawning this agent, so there it may be skipped.\n"
                f"LANGUAGE: English for everything you write (speech, filings, notes, JSON values), whatever language this message or the "
                f"conversation used; a quoted source passage keeps its original language inside the quotation.\n")

    def _common_block(self) -> str:
        st = self.state
        s = (f"CONTEXT MANUAL: {st['context_manual']} (one of the first actions; Read it)\n"
             f"\n--- RAW DATA FILES (fetch with Read; the orchestration does not paste data content) ---\n{st['raw_data_files']}\n"
             f"\n--- TRIGGER SIGNALS (the machine judges 'when', you judge 'what it means'; a named model manual is either "
             f"executed or explained as not applicable) ---\n"
             + ("\n".join(st["trigger_signals"]) if st["trigger_signals"] else "(no trigger signals)") + "\n")
        if st.get("opening_anchors"):
            s += ("\n--- OPENING ANCHORS (machine facts from the frozen daily data; references, not limits — principles and the debater book say how they are used) ---\n" + "\n".join(st["opening_anchors"]) + "\n")
        if st.get("event_text"):
            s += f"\n--- EVENT CALENDAR (local calendar; empty is also valid information) ---\n{st['event_text']}\n"
        if st["region"] != "US":
            s += (f"\n--- NON-US QUOTING DISCIPLINE ---\nWrite every level in the contract's trading currency "
                  f"({st.get('currency') or 'see config/symbols.yaml'}); Japanese and Korean symbols use their English tickers, "
                  f"and a quoted local price must state its unit; a market holiday is not a data fault — anchor to the latest "
                  f"data timestamp and say so.\n")
        s += (f"\n--- OUTCALL STATUS (machine facts) ---\n{self._coverage_text()}\n"
              f"Model calls named by trigger signals or tied to the focus topic come first; Read the relevant manual before acting.\n")
        return s

    def _lenses_block(self, stage: str = "debate", round_: int | None = None) -> str:
        """Multi-lens session (C11; wave 3): names the protocol file as a required read, then the frozen lens list with
        each lens's reference price (the exact P0 to write) and T_days, copied mechanically from the odds snapshot, then
        one stage-specific output reminder; '' for a single-lens session. The rules live only in LENSES_PROTOCOL."""
        from lens_views import fmt_level, lenses_of
        lenses = lenses_of(self.state)
        if not lenses:
            return ""
        odds = self.state.get("odds") or {}
        by = odds.get("by_lens") or {}
        rows = "\n".join(f"{L}: reference price {fmt_level(by[L]['reference_price'])} (write it as this lens's scenario P0)"
                         if (by.get(L) or {}).get("reference_price") is not None
                         else f"{L}: reference price missing in the odds snapshot" for L in lenses)
        if stage in ("objection", "signoff"):
            task, remind = stage, "use this task's base format; every revision op carries lens"
        else:
            task = "init" if (round_ or 1) == 1 else f"debate round {round_}"
            remind = "scenario_by_lens; every effect/update op carries lens"
        return (f"\n--- LENSES — THIS SESSION IS MULTI-LENS ---\n"
                f"REQUIRED READ: {LENSES_PROTOCOL}\n"
                f"Read it before drafting; its field overrides apply to the base format.\n"
                f"TASK: {task}\n"
                f"Single-symbol effort references apply. Session lenses ({len(lenses)}), frozen order: {', '.join(lenses)}\n"
                f"{rows}\nT_days: {odds.get('T_days')} (every lens)\n"
                f"OUTPUT REMINDER: {remind}\n\n")

    def _arbiter_reads(self, *paths: str) -> list:
        """The arbiter's reads for a receipt: `paths`, with the multi-lens protocol FIRST in a multi-lens session."""
        from lens_views import lenses_of
        return ([LENSES_PROTOCOL] if lenses_of(self.state) else []) + list(paths)

    def _arbiter_lens_note(self) -> str:
        """One reminder line for the arbiter's ruling receipts in a multi-lens session ('' otherwise)."""
        from lens_views import lenses_of
        lenses = lenses_of(self.state)
        if not lenses:
            return ""
        return (f"Multi-lens session ({', '.join(lenses)}): one ruling sheet and one `w` for every lens; optional "
                f"`lens_weights` / `lens_weights_reason`; never write `by_lens`, `lens_weights_default` or any derived "
                f"field (rules: {LENSES_PROTOCOL}).")

    def _with_lens_note(self, receipt: dict) -> dict:
        note = self._arbiter_lens_note()
        if note:
            receipt["lenses"] = note
        return receipt

    def spawn_material(self, role: str, round_: int) -> str:
        """首拍 spawn 材料: 元信息 + 语境 + 清单 + 视图路径 + 焦点; 写文件, 回执只给路径。"""
        task = "init" if round_ == 1 else "debate"
        cap = self.caps["beat_ref"]
        view = self.store.dir / f"_view_{role.lower()}.jsonl"
        hist = (f"VIEW: {self._rel(view)} (Read in full: both sides' prior-round finals in full + each final's appendix of "
                f"conclusions + the arbiter's procedural announcements + the full text of outcalls you yourself called for)"
                if round_ > 1 else "(empty: this is round 1; the two openings are mutually invisible)")
        text = (self._role_line(ROLE_FILES[role])
                + f"TASK: {task}\nSYMBOL: {self.symbol}\nCONTEXT_TYPE: {self.state['context_type']}\n"
                  f"ROUND: {round_}\nBEAT: 1\n"
                + self._lenses_block("debate", round_)
                + self._common_block()
                + self._calc_block(self.state.get("engine") in DEBATER_CAN_COMPUTE)
                + f"\n--- BEATS ---\nReference beat count per side this round: {cap} (guidance: one outcall round trip takes one beat; "
                  f"three beats allow one follow-up); this is beat 1. Each beat's output is one of two: beat.status=need_more "
                  f"(with a request_evidence filing, optionally with interim results; the script routes it, returns the receipt "
                  f"to you and wakes you again) or beat.status=final (the final, the only thing that enters the record). "
                  f"Exceeding the reference count requires a reason in the filing.\n"
                + self._standing_block(round_)
                + f"\n--- DEBATE HISTORY ---\n{hist}\nFOCUS THIS ROUND: {self.state['focus']}\n"
                + self._output_block(f"R{round_}_B1_{role.lower()}"))
        return self._spawned(f"R{round_}.{ROLES[role]}", view if round_ > 1 else None,
                             self.store.dir / f"_spawn_R{round_}_{role.lower()}.md", text)

    def resume_material(self, role: str, round_: int, beat: int, receipt: str, skipped: list) -> str:
        cap = self.caps["beat_ref"]
        last = (f"Reference beat count {cap} reached (guidance): unless there is a reason that more must be checked (state it in "
                f"the filing), deliver the final this beat." if beat >= cap
                else f"Reference beat count {cap}; this is beat {beat}.")
        prev = self.store.dir / f"R{round_}.{ROLES[role]}.B{beat - 1}.json"
        text = (f"TASK: {'init' if round_ == 1 else 'debate'} (continued beat)\nSYMBOL: {self.symbol}\nROUND: {round_}\nBEAT: {beat}\n"
                + self._lenses_block("debate", round_)
                + f"YOUR PREVIOUS BEAT: {self._rel(prev)}\n"
                f"RECEIPT: {receipt} (Read: the full results of your previous beat's outcalls, each with the arbiter's verified "
                f"status; the arbiter's neutral supplementary checks are included)\n"
                + ("SKIPPED FILINGS: " + "; ".join(skipped) + "\n" if skipped else "")
                + f"Digest the receipt into your argument before submitting the next beat. {last} The final must be a "
                  f"self-contained complete report (the appendix is attached mechanically by the script; do not transcribe "
                  f"evidence text).\n"
                + self._output_block(f"R{round_}_B{beat}_{role.lower()}"))
        key = f"R{round_}.{ROLES[role]}"
        sp = self.state.get("spawns") or {}
        floor = max([v.get("view_seq", 0) for k, v in sp.items()
                     if isinstance(v, dict) and (k == key or k.startswith(key + ".B"))] or [0])
        return self._spawned(f"{key}.B{beat}", self.root / receipt if receipt else None,
                             self.store.dir / f"_resume_R{round_}_B{beat}_{role.lower()}.md", text, floor=floor)

    def objection_material(self, role: str, k: int) -> str:
        self.store.build_view(role.lower(), objection_round=k)
        view = self.store.dir / f"_view_objection{k}_{role.lower()}.jsonl"
        text = (self._role_line(ROLE_FILES[role])
                + f"TASK: objection\nSYMBOL: {self.symbol}\nCONTEXT_TYPE: {self.state['context_type']}\n"
                f"OBJECTION_ROUND: {k} / {self.caps['objection_rounds']}\n"
                + self._lenses_block("objection")
                + f"FORMAT: {MANUALS['format_objection']} (Read)\n"
                f"VIEW: {self._rel(view)} (Read: your last-round final + pointer to the draft ruling side file"
                + (" + both sides' first-round objections verbatim; this round handles only what is new: rebut the other "
                   "side's objections, or press points you raised in the first round that went unanswered; with no new "
                   "issues, submit an empty list" if k > 1 else "") + ")\n"
                "Address only specific content of the draft report: factual error / misreading / omission / double counting / "
                "broken inference / operation or score inconsistent with the accepted reasoning; work mainly from existing "
                "material, do not reopen outcalls by default; 'the score is too high or too low' is not an objection. "
                "With no objections, submit an empty list.\n"
                + self._output_block(f"OBJ{k}_{role.lower()}"))
        return self._spawned(f"OBJ{k}.{ROLES[role]}", view, self.store.dir / f"_spawn_OBJ{k}_{role.lower()}.md", text)

    def signoff_material(self, role: str, version: int) -> str:
        self.store.build_view(role.lower(), signoff=True)
        view = self.store.dir / f"_view_signoff_{role.lower()}.jsonl"
        text = (self._role_line(ROLE_FILES[role])
                + f"TASK: signoff\nSYMBOL: {self.symbol}\nCONTEXT_TYPE: {self.state['context_type']}\n"
                f"RULING_VERSION: {version}\n"
                + self._lenses_block("signoff")
                + f"FORMAT: {MANUALS['format_signoff']} (Read)\n"
                f"SIGNOFF_VIEW: {self._rel(view)} (Read: your last-round final + your objections + pointer to the redraft side "
                "file; objection_dispositions in the redraft is the record of how objections were handled)\n"
                "The signoff is not a second debate: no outcall filings, no redoing the analysis; read only the format, the view and any "
                "required extension named above; write your reservations first (around the remaining differences between the redraft and your "
                "position), then report the composite score you endorse.\n"
                + self._output_block(f"SIGN{version}_{role.lower()}"))
        return self._spawned(f"SIGN{version}.{ROLES[role]}", view, self.store.dir / f"_spawn_SIGN{version}_{role.lower()}.md", text)

    def _job_output_block(self, job: dict) -> str:
        """执行者(Scout / Runner)两种引擎下都有写文件的手段: 一律写文件、回路径。"""
        rel = self._rel(self._out(f"job_{job['job_id']}"))
        return (f"OUTPUT: {rel} — write the complete JSON object into this file once (no explanatory text, markdown markup, "
                f"or code fences), then reply with only this path; if you cannot write a file, output the JSON object directly.\n")

    def _with_as_of(self, req: dict) -> dict:
        """盘中档只许读到本场时刻为止: 请求没写 as_of 就机械补上(日线请求不动)。"""
        if req.get("mode") == "bars" and str(req.get("interval") or "1d").lower() != "1d" and not req.get("as_of"):
            return {**req, "as_of": self.ts}
        return req

    def _calc_block(self, can_compute: bool = True) -> str:
        mode = self.state.get("calc_check") or "remind"
        if mode == "always" and not can_compute:  # 没有工具就不写做不到的要求
            mode = "remind"
        line = calc_line(mode, self.root)
        return f"Arithmetic: {line}\n" if line else ""

    def scout_material(self, job: dict) -> str:
        req = job["req"]
        note = "(This item was downgraded from a data call that returned nothing: data call %s found nothing; answer qualitatively)\n" % json.dumps(job.get("fallback_from"), ensure_ascii=False) \
            if job.get("fallback_from") else ""
        text = (self._role_line(ROLE_FILES["Scout"])
                + f"TASK: scout\nSYMBOL: {self.symbol}\nCONTEXT_TYPE: {self.state['context_type']}\nJOB_ID: {job['job_id']}\n"
                f"REQUESTED_BY: {job['by']}\n"
                f"FORMAT: {MANUALS['format_evidence']} (Read; output one Scout item)\n"
                f"CONTEXT MANUAL: {self.state['context_manual']} (regional first-hand source priority)\n{note}"
                f"EVIDENCE REQUEST (verbatim):\n{json.dumps(req, ensure_ascii=False)}\n" + self._calc_block()
                + self._job_output_block(job))
        return self._write(self.store.dir / f"_job_{job['job_id']}.md", text)

    def runner_material(self, job: dict) -> str:
        req = job["req"]
        manual = model_manual_path(str(req.get("model") or ""), self.root)
        refs_lines = []
        for r in req.get("refs") or []:
            if isinstance(r, str) and (self.root / r).exists():
                refs_lines.append(f"- file {r} (Read as a reference document)")
            elif isinstance(r, str) and (self.store.dir / f"{r}.json").exists():
                seg = self.store._segment({"id": r, "kind": self.store.obj(r)["kind"]})
                refs_lines.append(f"- evidence on the table {r}: {json.dumps(seg, ensure_ascii=False)}")
            else:
                refs_lines.append(f"- {r} (could not be resolved to a file or to evidence on the table)")
        model = str(req.get("model") or "")
        section = MODEL_SECTIONS.get(model, (model, ""))[1]
        prep = []  # 已算好的数据路径: 清单里的 evidence_prep 文件; 判法要读快照的编号再附技术快照
        for line in self.state["raw_data_files"].splitlines():
            if "evidence_prep" in line or (model in MODELS_NEED_SNAPSHOT and "data/technical/" in line):
                prep.append(line)
        diagnostic = ""
        if model == "P11":
            from analogue_audit import receipt
            measured = receipt(self.root, self.state, self.ts, req.get("params"))
            job["p11_payload"] = json.dumps(measured, allow_nan=False)
            diagnostic = "P11 deterministic receipt: " + job["p11_payload"] + "\n"
            prep = []
        text = (self._role_line(ROLE_FILES["Runner"])
                + f"TASK: model_call\nSYMBOL: {self.symbol}\nCONTEXT_TYPE: {self.state['context_type']}\n"
                f"MODEL_CALL_ID: {job['model_call_id']}\nREQUESTED_BY: {job['by']}\n"
                f"MANUAL: {manual or '(manual not located: use the failed shape)'} (Read first, execute by the book)\n"
                + (f"SECTION: do only the \"{section}\" ({model}) section this time; the shared data fetching and definitions still apply\n"
                   if section and manual else "")
                + f"CAPABILITY MAP: {MANUALS['capability_map']}\nFORMAT: {MANUALS['format_evidence']} (one Runner item)\n"
                + f"AS_OF: {self.ts} (this session's time; pass as_of when calling intraday `bars`)\n"
                + diagnostic
                + ("PRECOMPUTED DATA (read first; call afresh only if insufficient):\n" + "\n".join(prep) + "\n" if prep else "")
                + ("REFS:\n" + "\n".join(refs_lines) + "\n" if refs_lines else "")
                + f"REQUEST (verbatim):\n{json.dumps(req, ensure_ascii=False)}\n" + self._calc_block()
                + self._job_output_block(job))
        return self._write(self.store.dir / f"_job_{job['job_id']}.md", text)

    # ---------- route ----------
    def _mechanical_gate(self, req: dict) -> str | None:
        if req.get("type") != "data":
            return None
        mode, sym = req.get("mode"), str(req.get("symbol", "")).upper()
        bench = str(req.get("benchmark", "")).upper()
        own = self.state.get("benchmark") or ""
        intraday = mode == "bars" and str(req.get("interval") or "1d").lower() != "1d"
        if mode in ("bars", "trend_fit") and sym == self.symbol and not intraday:  # 自标的日线在场; 盘中档不在场, 放行
            return DATA_DENY
        if mode == "relative_strength" and sym == self.symbol and bench in (own, ""):
            return DATA_DENY
        if mode == "pair_link" and sym == self.symbol and str(req.get("counterpart", "")).upper() == own \
                and not req.get("window_days"):
            return DATA_DENY
        return None

    def _archive_beat(self, role: str, round_: int, beat: int, content: dict) -> dict:
        st = (content.get("beat") or {}) if isinstance(content, dict) else {}
        status = st.get("status") or "final"
        if status not in ("final", "need_more"):
            raise StepError(f"{role}: beat.status invalid ({status!r}); must be final | need_more")
        if status == "need_more" and not (content.get("request_evidence") or []):
            raise StepError(f"{role}: need_more but request_evidence is empty — either file an outcall or deliver the final")
        if status == "final" and self.state.get("group_session"):
            from group_run import verify_filing
            verify_filing(self, round_, role, content)
        try:
            if status == "final":
                rc = self.store.put("final", content, by=role, round_=round_)
            else:
                rc = self.store.put("beat", content, by=role, round_=round_, beat=beat)
        except StoreError as exc:
            return {"role": role, "status": status, "rejected": str(exc)}
        self.state["beat"][role] = beat
        return {"role": role, "status": status, "id": rc["id"]}

    def route(self, round_: int, beat: int, beats: dict[str, dict]) -> dict:
        self._require()
        if round_ != self.state.get("round"):
            raise StepError(f"session is at round {self.state.get('round')}, got --round {round_}")
        archived = [self._archive_beat(role, round_, beat, c) for role, c in beats.items() if c is not None]
        rejected = [a for a in archived if a.get("rejected")]
        if rejected:
            counts = self.state.setdefault("rejects", {})
            for a in rejected:
                key = f"R{round_}.{a['role']}"
                counts[key] = counts.get(key, 0) + 1
                if counts[key] > 1:
                    self._fail(f"{a['role']} round {round_} final still fails validation after one rewrite: {a['rejected']}")
            self.save()
            return {"ok": False, "archived": archived,
                    "error": "Final failed validation; pass the rejected text back to that debater for one rewrite, then route again (same beat; a second failure stops the session)",
                    "retry": True}
        # 聚合申报: 固定角色序(路由顺序决定铸造位次, 与并发完成时序无关)
        sources = [(role, (beats.get(role) or {}).get("request_evidence") or []) for role in ROLES]
        sources.append((ARBITER, self.state.get("proactive") or []))
        seen = set(self.state["executed_sigs"])
        used = sum(1 for r in self.store.rows() if r["kind"] == "model_call")
        # 同一拍可以分两次路由(一方先交, 另一方后到): 先到那方的在途作业保留, 编号接着排, 不覆盖
        jobs_path = self.store.dir / f"_jobs_R{round_}_B{beat}.json"
        on_file = _read_json(jobs_path) if jobs_path.exists() else []
        still_open = set(self.state.get("open_jobs") or [])
        prior = [j for j in on_file if j.get("job_id") in still_open]
        seen |= {j["sig"] for j in prior if j.get("sig")}
        pfx_next = {}
        for p in ("Z", "R", "A"):
            pfx_next[p] = 1 + sum(1 for r in self.store.rows() if r["kind"] == "model_call"
                                  and (ROLES.get(r.get("by")) or "A") == p) \
                + sum(1 for j in prior if j.get("kind") == "runner" and (ROLES.get(j.get("by")) or "A") == p)
        jobs, skipped, data_done = [], [], []
        n = len(on_file)
        for by, reqs in sources:
            for req in reqs:
                if not isinstance(req, dict):
                    continue
                sig = _sig(req)
                if sig in seen:
                    skipped.append({"by": by, "req": req, "reason": "Literal duplicate (already executed this session)"})
                    continue
                seen.add(sig)
                gate = self._mechanical_gate(req)
                if gate:
                    skipped.append({"by": by, "req": req, "reason": gate})
                    self.state["executed_sigs"].append(sig)
                    continue
                t = req.get("type")
                if t == "data":
                    out = self.tools.evidence_quant(self._with_as_of(req))
                    ok = '"ok": true' in out.lower() or '"ok":true' in out.lower()
                    content = {"kind": "data", "mode": req.get("mode"), "request": req,
                               "question": f"data {req.get('mode')} {req.get('symbol', self.symbol)}",
                               "simple": "(Data-call result; full text in payload_text; debaters pick out the key figures themselves)" if ok
                               else "NOT_FOUND — data call returned nothing", "detailed": out or "(empty)",
                               "payload_text": out or "(empty)",
                               "source": [f"data:evidence_quant:{req.get('mode')}:{req.get('symbol', self.symbol)}"],
                               "verdict": "supports" if ok else "not_found"}
                    rc = self.store.put("evidence", content, by=by, round_=round_, beat=beat,
                                        meta={"status": "verified" if ok else "not_found", "kind": "data"})
                    data_done.append({"by": by, "id": rc["id"], "ok": ok})
                    self.state["executed_sigs"].append(sig)
                    if not ok:  # gap → 搜索降级: 定性可答的问题当拍转派 Scout
                        n += 1
                        q = req.get("question") or req.get("target_claim") or \
                            f"No {req.get('mode')} data available for {req.get('symbol', self.symbol)}; answer the request's question qualitatively"
                        jobs.append({"job_id": f"R{round_}B{beat}-{n}", "kind": "scout", "by": by,
                                     "req": {"type": "search", "target_claim": req.get("target_claim") or "self-check",
                                             "question": q, "would_change_my_mind": req.get("would_change_my_mind") or ""},
                                     "fallback_from": req, "sig": sig + "#fallback"})
                elif t == "model_call":
                    if not model_manual_path(str(req.get("model") or ""), self.root):
                        skipped.append({"by": by, "req": req, "reason": f"Model manual does not exist: {req.get('model')!r}"})
                        self.state["executed_sigs"].append(sig)
                        continue
                    used += 1
                    n += 1
                    p = ROLES.get(by, "A")
                    mc = f"{p}.MC{pfx_next[p]}"
                    pfx_next[p] += 1
                    jobs.append({"job_id": f"R{round_}B{beat}-{n}", "kind": "runner", "by": by, "req": req,
                                 "model_call_id": mc, "sig": sig})
                elif t == "search":
                    n += 1
                    jobs.append({"job_id": f"R{round_}B{beat}-{n}", "kind": "scout", "by": by, "req": req, "sig": sig})
                else:
                    skipped.append({"by": by, "req": req, "reason": f"Unknown filing type {t!r}"})
        # 非美事件兜底(首轮首拍, 日历空, 无事件类申报): 仲裁者强制中立补查
        if (round_ == 1 and beat == 1 and self.state["region"] != "US"
                and ("no registered events" in self.state.get("event_text", "") or '"events": []' in self.state.get("event_text", ""))
                and not any("event" in json.dumps(j["req"], ensure_ascii=False).lower() for j in jobs)):
            n += 1
            jobs.append({"job_id": f"R{round_}B{beat}-{n}", "kind": "scout", "by": ARBITER,
                         "req": {"type": "search", "target_claim": "event blind-spot fallback",
                                 "question": f"Policy / central bank / major calendar events for the {self.state['region']} market in the next 5-10 trading days",
                                 "would_change_my_mind": "Mandatory neutral supplementary check by the arbiter (non-US event blind-spot stopgap)"},
                         "sig": "__event_fallback__"})
        self.state["proactive"] = []
        for j in jobs:
            j["material"] = self.scout_material(j) if j["kind"] == "scout" else self.runner_material(j)
            j["output"] = self._rel(self._out(f"job_{j['job_id']}"))
            cmd = drill_command(self.root, j)
            if cmd:
                j["command"] = cmd
        closed =[j for j in on_file if j.get("job_id") not in still_open]
        jobs_path.write_text(json.dumps(closed + prior + jobs, ensure_ascii=False, indent=1),
                             encoding="utf-8", newline="\n")
        self.state["open_jobs"] = [i for i in (self.state.get("open_jobs") or [])
                                   if i not in {j["job_id"] for j in prior}] + [j["job_id"] for j in prior + jobs]
        self.state["open_beat"] = beat
        sk = self.state.get("last_skipped")
        sk = sk if isinstance(sk, dict) else {}
        for role in [r for r in beats if beats.get(r) is not None]:
            sk[role] = [f"{json.dumps(s['req'], ensure_ascii=False)[:120]} — {s['reason']}"
                        for s in skipped if s["by"] == role]
        self.state["last_skipped"] = sk
        self.save()
        finals = {a["role"]: a["id"] for a in archived if a.get("status") == "final"}
        pending = [a["role"] for a in archived if a.get("status") == "need_more"]
        rc = {"ok": True, "round": round_, "beat": beat, "archived": archived, "finals": finals,
              "pending": pending, "data_calls": data_done, "skipped": skipped,
              "jobs": [{k: v for k, v in j.items()
                        if k in ("job_id", "kind", "by", "model_call_id", "material", "output", "command")} for j in jobs],
              "open_jobs": list(self.state["open_jobs"]),
              "jobs_file": self._rel(jobs_path), "worker_model": self.state.get("worker_model")}
        if prior:
            rc["note"] = (f"In-flight jobs routed earlier in this beat are still open: {[j['job_id'] for j in prior]}; the results "
                          f"for close-beat must cover every job in open_jobs")
        if jobs or prior:
            rc["next"] = (f"Immediately spawn every job returned here, concurrently (material file in full as the message; scout→scout "
                          f"agent, runner→runner agent), without waiting for the other side's beat. Once one side's job results are "
                          f"all in, verify them one by one per {MANUALS['evidence_review']}, write results.json "
                          f"[{{job_id, status, scope?}}|{{job_id, failed}}] (the script reads results from each job's OUTPUT file; "
                          f"do not transcribe), then "
                          f"`close-beat --round {round_} --beat {beat} --role <zealot|reaper> --results results.json`, "
                          f"and send the receipt material on to that debater")
            if any(j.get("command") for j in jobs):
                rc["next"] = ("Drill: jobs carrying `command` are executed by the drill director (see the drill addendum), "
                              "not by spawning an executor. " + rc["next"])
        elif pending:
            rc["next"] = f"No jobs; run `close-beat --round {round_} --beat {beat} --results` with an empty list to generate the receipt, then wake {pending}"
        elif len([r for r in self.store.finals() if r["round"] == round_]) == 2 and not self.state["open_jobs"]:
            rc["next"] = f"Both finals in; `tally --round {round_}`"
        else:
            rc["next"] = "Archived; handle the other side's beat or in-flight jobs when they arrive (first come first served; do not wait for both)"
        return rc

    # ---------- close-beat ----------
    def close_beat(self, round_: int, beat: int, results: list[dict], role: str | None = None) -> dict:
        """归档外呼结果并生成回执。role 给定时只关那一方的作业(辩手各走各的拍, 不等另一方);
        仲裁者自己的补查作业: 有结果就一并关, 没结果留在途; role 省略 = 关本拍全部在途作业。"""
        self._require()
        role = {"zealot": "Zealot", "reaper": "Reaper", "arbiter": ARBITER}.get((role or "").lower(), role)
        jobs_path = self.store.dir / f"_jobs_R{round_}_B{beat}.json"
        still_open = set(self.state.get("open_jobs") or [])
        on_file = [j for j in (_read_json(jobs_path) if jobs_path.exists() else []) if j["job_id"] in still_open]
        got = {r.get("job_id"): r for r in results if isinstance(r, dict)}

        def owner(j):
            return j["by"] if j["by"] in ROLES else ARBITER
        if role:
            jobs = {j["job_id"]: j for j in on_file
                    if owner(j) == role or (owner(j) == ARBITER and j["job_id"] in got)}
        else:
            jobs = {j["job_id"]: j for j in on_file}
        missing = [jid for jid in jobs if jid not in got]
        if missing:
            raise StepError(f"results is missing jobs {missing} (every job needs a result or failed; silent drops are not allowed)")
        # 第一步只验不写: 任何一条结果不合格都在动对象仓之前报错。否则前几条已经归档、后面一条报错,
        # 重试时前几条会被再归档一次, 编号顺延, 与预派的编号对不上(2026-10-02 美国大盘场就死在这里)。
        ready = []
        for jid, job in jobs.items():
            r = got[jid]
            status = r.get("status")
            failed = r.get("failed")
            result = r.get("result") if isinstance(r.get("result"), dict) else None
            if result is None and not failed:  # 一份产物只生成一次: 结果从作业的 OUTPUT 文件读
                out_path = self._out(f"job_{jid}")
                if out_path.exists():
                    try:
                        loaded = _read_json(out_path)
                    except ValueError as exc:
                        raise StepError(f"{jid}: OUTPUT file is not valid JSON ({exc}); have the executor rewrite the file, or report failed")
                    result = loaded if isinstance(loaded, dict) else None
                if result is None:
                    raise StepError(f"{jid}: no result given, and the OUTPUT file {self._rel(out_path)} does not exist or is not an object; "
                                    f"if the executor has not finished, wait for it before closing; if no result can be obtained, "
                                    f"report failed (nothing was archived this time)")
            if result is not None and result.get("status") == "failed":
                failed = failed or result.get("reason") or "worker reported failed"
                result = None
            scope = r.get("scope") if isinstance(r.get("scope"), str) and r.get("scope").strip() else None
            if failed:
                status = "failed"  # 掉线: 执行者挂了 / 接口报错 / 没执行成——不是结果, 可重发
            else:
                if status not in ("verified", "unverified", "not_found"):
                    raise StepError(f"{jid}: status must be verified|unverified|not_found (the arbiter's verification), got {status!r}")
                if not scope:
                    raise StepError(f"{jid}: scope must state in one sentence which part may be used as fact and which part is only "
                                    f"inference or not found (status is just the headline of that sentence)")
            ready.append((jid, job, result, status, scope, failed))
        # 第二步归档: 每归档一条就把它从在途作业里划掉并落状态, 中途断了重试也不会重复归档
        archived = []
        for jid, job, result, status, scope, failed in ready:
            req = job["req"]
            if job["kind"] == "runner":
                content = result or {"model_call_id": job["model_call_id"], "model": req.get("model"),
                                     "issue": req.get("issue"), "target": json.dumps(req, ensure_ascii=False)[:200],
                                     "steps": [], "finding": f"FAILED — {failed or 'runner error'}",
                                     "verdict": "not_found", "suggested_evidence_tier": None, "payload_text": ""}
                if not failed and "p11_payload" in job:
                    content = dict(content, payload_text=job["p11_payload"])
                if (self.store.dir / f"{job['model_call_id']}.json").exists():
                    rc = {"id": job["model_call_id"]}   # 上次已经归档到这个编号(写完对象、没来得及落状态就断了)
                else:
                    rc = self.store.put("model_call", content, by=job["by"], round_=round_, beat=beat,
                                        meta={"status": status, "verified_scope": scope, "model": req.get("model"),
                                              "issue": req.get("issue"),
                                              "suggested_evidence_tier": content.get("suggested_evidence_tier"),
                                              "model_call_request": req})
                if rc["id"] != job["model_call_id"]:
                    self._fail(f"MC pre-assigned id and minted id mismatch: {job['model_call_id']} vs {rc['id']}")
            else:
                content = result or {"question": req.get("question", "search"),
                                     "simple": f"NOT_FOUND — {failed or 'scout error'}", "detailed": "(scout failed)",
                                     "source": ["run_step"], "verdict": "not_found"}
                if job.get("fallback_from"):
                    content = dict(content)
                    content["fallback_from_data"] = job["fallback_from"]
                rc = self.store.put("evidence", content, by=job["by"], round_=round_, beat=beat,
                                    meta={"status": status, "verified_scope": scope, "kind": "search", "request": req})
            if not failed:  # 执行者挂了不算"本场已执行": 同一条申报之后可以再发
                self.state["executed_sigs"].append(job["sig"])
            archived.append({"job_id": jid, "id": rc["id"], "status": status})
            self.state["open_jobs"] = [i for i in (self.state.get("open_jobs") or []) if i != jid]
            self.save()
        self.state["open_jobs"] = [i for i in (self.state.get("open_jobs") or []) if i not in jobs]
        finals = {r["by"]: r["id"] for r in self.store.finals() if r["round"] == round_}
        left = {owner(j) for j in on_file if j["job_id"] in set(self.state["open_jobs"])}
        skipped_by = self.state.get("last_skipped")
        skipped_by = skipped_by if isinstance(skipped_by, dict) else {}
        pending = {}
        for who in ([role] if role in ROLES else [] if role else list(ROLES)):
            if who in finals or self.state["beat"].get(who, 0) != beat or who in left:
                continue
            rcp = self.store.build_receipt(who, round_, beat)
            pending[who] = {"beat": beat + 1, "receipt": rcp["receipt"], "results": rcp["results"],
                            "material": self.resume_material(who, round_, beat + 1, rcp["receipt"],
                                                             skipped_by.get(who) or [])}
        self.save()
        rc = {"ok": True, "round": round_, "beat": beat, "archived": archived, "finals": finals, "pending": pending,
              "open_jobs": list(self.state["open_jobs"])}
        if pending:
            rc["next"] = (f"Immediately send the material file in full to the same thread of {list(pending)} (paths and instructions "
                          f"only; do not transcribe content); after it writes the next beat to the OUTPUT path given in the "
                          f"material, `route --round {round_} --beat {beat + 1} --<zealot|reaper> <that path>` (pass only the side "
                          f"with a new beat; do not wait for the other)")
        elif self.state["open_jobs"]:
            rc["next"] = f"Jobs still in flight {self.state['open_jobs']}: close-beat once their results are in (close per debater)"
        elif len(finals) == 2:
            rc["next"] = f"Both finals in; `tally --round {round_}`"
        else:
            rc["next"] = "Wait for the side that has not submitted its beat; route when it arrives"
        return rc

    # ---------- tally / close-round ----------
    def _entries(self) -> list:
        from ledger_tally import load_entries
        return load_entries(str(self.store.debate_buf)) if self.store.debate_buf.exists() else []

    def tally(self, round_: int) -> dict:
        self._require()
        finals = {r["by"]: r["id"] for r in self.store.finals() if r["round"] == round_}
        if set(finals) != set(ROLES):
            raise StepError(f"R{round_} finals not all in: have {sorted(finals)}")
        if self.state.get("open_jobs"):
            raise StepError(f"R{round_} still has jobs in flight {self.state['open_jobs']}: close-beat first (use --role arbiter for "
                            f"the arbiter's own supplementary checks); every job needs a result or failed")
        session_cfg(self)   # a session opened before v3.4 cannot tally its finals
        res = self.tools.tally(self._entries(), ruler=True, named=self._arbiter_named())
        if self.state.get("closed_records"):
            res = self.tools.tally(self._entries(), closed=self._still_closed(res), ruler=True, named=self._arbiter_named())
        brief = self.tools.tally_brief(res)
        odds = self._scenario_lines(round_)
        public = brief
        if odds["lines"]:
            brief = brief + "\n" + "\n".join(odds["lines"])
            public = public + "\n" + "\n".join(public_ruler_lines(odds["lines"]))
        stall = self._stall_line(round_, odds["by_side"], res.get("open_total") or [])
        if stall:
            brief = brief + "\n" + stall       # the arbiter's copy only: a procedural reference, not debate material
        path = self._write(self.store.dir / f"_tally_R{round_}.txt", brief + "\n")
        self.store.build_view("arbiter")
        self.state["last_tally"] = {"round": round_, "open_total": res.get("open_total") or [],
                                    "brief": brief, "brief_public": public, "odds": odds["by_side"]}
        self.save()
        return {"ok": True, "round": round_, "tally_brief": brief, "tally_file": path,
                "view": self._rel(self.store.dir / "_view_arbiter.jsonl"),
                "reads": self._arbiter_reads(MANUALS["round_notes"], MANUALS["evidence_review"]),
                "sufficiency": self.coverage(),
                "hard_stop": {"open_claims": res.get("open_total") or [], "round": round_,
                              "max_rounds": self.caps["max_rounds"], "round_ref": self.caps.get("round_ref"),
                              "note": ("Hard round cap (blow-out guard) is %d; this is round %d: try to close before the cap; at the cap "
                                       "the session is force-closed and open claims are recorded as unresolved" % (self.caps["max_rounds"], round_))
                              + (" — approaching the cap." if round_ >= self.caps["max_rounds"] - 3 else "")
                              + self._round_ref_note(round_)},
                "next": ("Read the round-notes manual and the arbiter view; write notes.json {arbiter_notes, private_score{composite_score, "
                         "operation_type, operation_target, operation_size, mentality, moved_by[], note}, focus, "
                         f"proactive_requests[reference {self.caps['arbiter_query_ref']} per round, guidance], decision: CONTINUE|TERMINATE}}; "
                         f"`close-round --round {round_} --notes notes.json`")}

    def _round_ref_note(self, round_: int) -> str:
        """Reference session length (guidance, not a gate): closing before it needs one sentence in the notes."""
        ref = self.caps.get("round_ref")
        if not isinstance(ref, int) or ref <= 0:
            return ""
        if (self.state.get("lineage") or {}).get("mode") == "delta":
            import delta_session as ds
            return (f" {ds.ARBITER_SENTENCE} Reference length is {ref} rounds for this follow-up (the standing evidence replaces "
                    f"the opening checks that repeat from session to session; it is not a stopping target): two rounds is normal "
                    f"when the standing evidence covers the topics, one is not; a TERMINATE before round {ref} needs one sentence "
                    f"in the notes on why the topic list is already complete; a material gap reopens the inquiry whatever the round.")
        return (f" Reference length is {ref} rounds (" + EXPLAIN_ROUND_REF + "): three or four rounds is normal, two is not; "
                f"a TERMINATE before round {ref} needs one sentence in the notes on why the topic list is already complete.")

    def _scenario_lines(self, round_: int) -> dict:
        """3.4: per side, the session ruler on that side's scenario with its own contracts only: S_ref, q*, x*, best strategy.
        C11.5 (multi-lens): one line per side per lens on that lens's ruler (`by_side[role][lens]`), plus the side's
        default-weighted S_ref summary line."""
        from lens_views import lenses_of
        if lenses_of(self.state):
            return self._scenario_lines_lenswise(round_, lenses_of(self.state))
        from ruling import side_ruler
        cfg = session_cfg(self)
        lines, by_side = [], {}
        for role in ROLES:
            rec = None
            for r in self.store.finals():
                if r["by"] == role and r["round"] == round_:
                    rec = self.store.obj(r["id"])
            sc = (rec or {}).get("content", {}).get("scenario") if rec else None
            if not isinstance(sc, dict):
                continue
            tag = ROLES[role]
            out = side_ruler(sc, tag, cfg)
            if not out["ok"]:
                lines.append(f"{tag} scenario: INVALID ({'; '.join(out['fatal'][:2])})")
                by_side[role] = out
                continue
            strat = " · ".join(f"{n} x*={x:.2f}" for n, x in out["x_by"].items())
            lines.append(f"{tag} scenario ({out['nodes']} nodes, T={out['T_days']}): S_ref {out['S_ref']} · best {out['best']} "
                         f"q*={out['q']:.3f} x*={out['x']:.3f} · {strat}")
            lines.append(f"  {tag} " + self._decomposition(sc, None))   # drill r1-rational-5
            lines.append(f"  {tag} " + self._tree_extras(sc, None, role, round_, out["S_ref"]))   # v3.6
            by_side[role] = {"ok": True, "S_ref": out["S_ref"], "best": out["best"], "q": out["q"], "x": out["x"],
                             "warn": out["warn"]}
        if lines:
            lines.insert(0, "[Scenario ruler 3.4 — machine-computed per side on its own contracts; S_ref = 100·G/(G+L), "
                            "q* = target exposure, x* = 2|q*−0.5|; each decomposition line splits the odds into a direction ratio "
                            "and a magnitude ratio — display only]")
        return {"lines": lines, "by_side": by_side}

    def _prev_sref(self, role: str, round_: int, lens) -> float | None:
        """The side's S_ref on this lens one round earlier (None in round 1 or when it filed nothing)."""
        if round_ <= 1:
            return None
        from ruling import side_ruler
        rec = None
        for r in self.store.finals():
            if r["by"] == role and r["round"] == round_ - 1:
                rec = self.store.obj(r["id"])
        c = (rec or {}).get("content") or {}
        sc = (c.get("scenario_by_lens") or {}).get(lens) if lens else c.get("scenario")
        if not isinstance(sc, dict):
            return None
        cfg = self._lens_cfg(lens) if lens else session_cfg(self)
        out = side_ruler(sc, ROLES[role], cfg)
        return float(out["S_ref"]) if out.get("ok") else None

    STALL_POINTS = 1.0   # |ΔS_ref| below this on every lens of both sides = the round moved no forecast

    def _stall_deltas(self, round_: int, by_side: dict) -> list | None:
        """ΔS_ref vs the previous round for every (side, lens); None when any is unavailable (round 1, invalid tree)."""
        if round_ < 2:
            return None
        out = []
        for role in ROLES:
            per = by_side.get(role)
            if not isinstance(per, dict) or not per:
                return None
            items = [(None, per)] if "ok" in per else list(per.items())
            for lens, o in items:
                if not isinstance(o, dict) or not o.get("ok"):
                    return None
                prev = self._prev_sref(role, round_, lens)
                if prev is None:
                    return None
                out.append((ROLES[role], lens, float(o["S_ref"]) - prev))
        return out

    def _stall_line(self, round_: int, by_side: dict, open_claims: list) -> str | None:
        """Stall reference (arbiter's tally only; guidance, not a gate): when no forecast moved this round, the round that
        follows has, in the record so far, moved nothing either — the reference is to close every open claim now."""
        ref = self.caps.get("round_ref")
        first = 3 if not isinstance(ref, int) or ref > 3 else 2     # a full session's second round is still opening work
        if round_ < first:
            return None
        deltas = self._stall_deltas(round_, by_side)
        if not deltas or any(abs(d) >= self.STALL_POINTS for _, _, d in deltas):
            return None
        # a claim opened or revised this round is a front the other side has not answered yet: not a stall
        import closure_check as cc
        revs = cc.claim_revs(cc.records_from_store(self.store))
        if any(str(revs.get(c) or "").startswith(f"R{round_}.") for c in open_claims):
            return None
        shown = " · ".join(f"{tag}{(' ' + lens) if lens else ''} {d:+.1f}" for tag, lens, d in deltas)
        back = sum(1 for r in self.store.rows() if r.get("kind") in ("evidence", "model_call") and r.get("round") == round_)
        shown += f"; outcalls returned this round: {back}"
        claims = (f" Open claims: {', '.join(open_claims)}." if open_claims else "")
        return ("[Stall reference — guidance, not a gate] Neither side's S_ref moved this round (ΔS_ref " + shown + "; under "
                f"{self.STALL_POINTS:g} point on every tree; width, tails and paths can still have changed)." + claims
                + " Reference: in this round's notes write the closing record for every open claim that already meets the "
                "closing conditions — all of them at once (`terminal_open`, with your `dispositions` where a side did not "
                "engage an evidence item), not one or two per round. A claim you keep open names what is still pending: "
                "returned evidence the other side has not yet had a round to answer, a claim revision not yet answered, or "
                "the check you file now (`next: filed:<id>`), each with the node it could reprice. When nothing is "
                "pending, the session can end this round.")

    def _pending_effects(self, lens, role: str) -> list:
        """[(tag "Z1#false", ops)] — the side's registered effects not yet carried by one of its updates, on claims still
        in play (its own claims and its challenges' targets), projected on `lens` in a multi-lens session."""
        import closure_check as cc
        import scenario_replay as sr
        from lens_views import lenses_of, project_content
        lenses = lenses_of(self.state)
        entries = []
        for r in cc.records_from_store(self.store):
            if r.get("kind") != "final" or r.get("role") not in ("Z", "R"):
                continue
            content = project_content(r["content"], lens, lenses) if lens else r["content"]
            entries.append({"seq": r["seq"], "id": r["id"], "role": r["role"], "round": r.get("round"), "content": content})
        tag = ROLES[role]
        registry, _ = sr.effects_registry_from_history(entries, tag)
        carried, status = set(), {}
        for e in entries:
            for c in (e["content"].get("claims") or []):
                if isinstance(c, dict) and c.get("id"):
                    status[(e["role"], str(c["id"]))] = c.get("status")
            if e["role"] == tag:
                carried |= {str(u.get("applies")) for u in (e["content"].get("scenario_updates") or [])
                            if isinstance(u, dict) and u.get("applies")}
        out = []
        for cid, ent in registry.items():
            if status.get((ent.get("owner"), cid)) in ("conceded", "withdrawn", "settled_against"):
                continue
            for outcome in ("true", "false"):
                ops = ent.get(outcome) or []
                if ops and f"{cid}#{outcome}" not in carried:
                    out.append((f"{cid}#{outcome}", ops))
        return out

    @staticmethod
    def _skipped_nodes(sc: dict, ops: list) -> list:
        """Probability moves that pass over a node lying between source and destination (nodes ordered by end price):
        ["B→D past C", …]. A concession lands on the node the evidence supports, usually the adjacent one."""
        order = sorted(((float(n["P_T"]), str(n["id"])) for n in (sc.get("nodes") or []) if isinstance(n, dict) and n.get("P_T") is not None))
        idx = {i: k for k, (_, i) in enumerate(order)}
        out = []
        for op in ops or []:
            a, b = str(op.get("from")), str(op.get("to"))
            if isinstance(op, dict) and op.get("op") == "p_move" and a in idx and b in idx and abs(idx[a] - idx[b]) > 1:
                lo, hi = sorted((idx[a], idx[b]))
                ends = {order[lo][0], order[hi][0]}   # a node at the same end price as either end is not in between
                mid = [order[k][1] for k in range(lo + 1, hi) if order[k][0] not in ends]
                if mid:
                    out.append(f"{a}→{b} past {', '.join(mid)}")
        return out

    def _round_updates(self, role: str, round_: int, lens, sc: dict) -> str | None:
        """What this side's updates of the round did, in the terms a reader checks a concession by: which claim, whether
        the registered effect was applied whole, whether mass passed over a node (v3.8 candidate; display only)."""
        rec = None
        for r in self.store.finals():
            if r["by"] == role and r["round"] == round_:
                rec = self.store.obj(r["id"])
        c = (rec or {}).get("content") or {}
        if lens:
            from lens_views import lenses_of, project_content
            c = project_content(c, lens, lenses_of(self.state))
        items = []
        for u in c.get("scenario_updates") or []:
            if not isinstance(u, dict) or not (u.get("ops") or []):
                continue
            skips = self._skipped_nodes(sc, u.get("ops"))
            what = f"{u.get('applies')} registered effect applied whole" if u.get("applies") else f"{u.get('claim') or '?'} free update"
            items.append(what + (f", {'; '.join(skips)}" if skips else ""))
        if not items:
            return None
        return ("this round's updates: " + " · ".join(items) + " (a concession moves what was hit — the fact, the size or the "
                "whole claim — and no more; whole-effect and node-skipping moves are for the arbiter to check against the hit)")

    def _stake_part(self, sc: dict, lens, role: str, s_ref: float) -> str | None:
        """How much of the side's lean (S_ref − 50) a pending registered effect could take back, and how much rests on no
        registered effect at all — lean that no settlement can reach (v3.7; display only)."""
        import scenario_replay as sr
        from ruling import side_ruler
        lean = s_ref - 50.0
        near = abs(lean) < 1.0    # a tree at neutral has no lean to take back, but its registered effects still land somewhere
        cfg = self._lens_cfg(lens) if lens else session_cfg(self)
        sign = 1.0 if lean > 0 else -1.0
        back, shown, against, far, every = {}, [], [], [], {}
        for tag, ops in self._pending_effects(lens, role):
            try:
                out = side_ruler(sr.apply_ops(sc, ops), ROLES[role], cfg)
            except Exception:  # noqa: BLE001 — an effect that no longer executes is the replay's business, not this line's
                continue
            if not out.get("ok"):
                continue
            d = float(out["S_ref"]) - s_ref
            shown.append(f"{tag} {d:+.1f}")
            cid = tag.split("#")[0]
            if abs(d) > abs(every.get(cid, (0.0, None))[0]):
                every[cid] = (d, ops)
            if -sign * d > back.get(cid, 0.0):
                back[cid] = -sign * d
                against = [x for x in against if x[0] != cid] + [(cid, ops)]
            far += [f"{tag} {x}" for x in self._skipped_nodes(sc, ops)]
        if near and not shown:
            return None
        reach = min(abs(lean), sum(v for v in back.values() if v > 0))
        if near:
            text = f"lean vs 50 {lean:+.1f} (at neutral): pending registered effects {', '.join(shown)}"
            against = [(cid, ops) for cid, (d, ops) in every.items()]
            sign = -1.0 if sum(d for d, _ in every.values()) > 0 else 1.0   # so that `over` below reads the side they land on
        else:
            text = (f"lean vs 50 {lean:+.1f}: pending registered effects {', '.join(shown) if shown else 'none'} → settlement can take "
                    f"back {reach:.1f}; {abs(lean) - reach:.1f} rests on no registered effect (no claim's outcome moves it: "
                    f"either side may ask which claim carries it)")
        if against:   # every pending effect that goes against the lean, landed one after another
            worst = sc
            for _, ops in against:
                try:
                    worst = sr.apply_ops(worst, ops)
                except Exception:  # noqa: BLE001
                    continue
            out = side_ruler(worst, ROLES[role], cfg)
            if out.get("ok"):
                w = float(out["S_ref"])
                over = -sign * (w - 50.0)
                text += (f"; all of them landing reads {w:.1f}"
                         + (f" — {over:.1f} past 50: " + ("every registered effect moves a neutral tree the same way"
                                                         if near else "the registered effects pledge more than the lean")
                            if over >= 1.0 else ""))
        if far:
            text += ("; registered moves that pass over a node: " + ", ".join(far)
                     + " (the registered effect is the most a full failure costs; a partial hit moves less, to the node the evidence supports)")
        return text

    def _tree_extras(self, sc: dict, lens, role: str, round_: int, s_ref: float) -> str:
        """v3.6 tally extras (display only): short-term scores from close events, node contribution shares,
        magnitude compensation vs the equal-magnitude score, stall notice vs the previous round."""
        from opening_anchors import contributions, equal_magnitude, short_scores
        try:
            P0 = float(sc["P0"]); nodes = sc.get("nodes") or []
            ss = short_scores(nodes, P0)
            eq = equal_magnitude(nodes, P0)
            parts = ["short-term (close events only): " + " · ".join(f"d+{t} {('n/a' if v is None else f'{v:.0f}')}" for t, v in ss.items()),
                     f"equal-magnitude score {eq:.1f} (S_ref − it = {float(s_ref) - eq:+.1f}: magnitude's share of the lean)",
                     "contributions: " + contributions(nodes, P0)]
            stake = self._stake_part(sc, lens, role, float(s_ref))
            if stake:
                parts.append(stake)
            moved = self._round_updates(role, round_, lens, sc)
            if moved:
                parts.append(moved)
            prev = self._prev_sref(role, round_, lens)
            if prev is not None:
                d = float(s_ref) - prev
                parts.append(f"ΔS_ref vs R{round_ - 1} {d:+.1f}" + (" — no movement this round" if abs(d) < 1.0 else ""))
            return " | ".join(parts)
        except Exception as exc:  # noqa: BLE001
            return f"extras unavailable ({type(exc).__name__})"

    def _decomposition(self, sc: dict, lens) -> str:
        """Drill r1-rational-5: S_ref split into direction ratio x magnitude ratio, per-node contributions; display only."""
        from opening_anchors import decompose
        odds = self.state.get("odds") or {}
        info = (odds.get("by_lens") or {}).get(lens) if lens else dict(odds.get("unit_basis") or {}, reference_price=odds.get("reference_price"))
        try:
            return decompose(sc.get("nodes") or [], float(sc.get("P0") or info["reference_price"]), float(info["s_s"]))
        except Exception as exc:  # noqa: BLE001
            return f"decomposition unavailable ({type(exc).__name__})"

    def _lens_cfg(self, lens: str) -> dict:
        """The session ruler on one lens: the odds snapshot with that lens's reference price / cost / unit basis (C11.1)."""
        from lens_views import LensError, lens_snapshot
        from odds_model import config_from_snapshot
        session_cfg(self)   # raises on a session opened before v3.4
        try:
            return config_from_snapshot(lens_snapshot(self.state["odds"], lens))
        except LensError as exc:
            raise StepError(f"{exc} (C11.1: the session snapshot has no ruler for this lens)")

    def _scenario_lines_lenswise(self, round_: int, lenses: list) -> dict:
        from ruling import side_ruler
        cfgs = {L: self._lens_cfg(L) for L in lenses}
        lam = self.state.get("lens_weights_default") or {}
        lines, by_side = [], {}
        for role in ROLES:
            rec = None
            for r in self.store.finals():
                if r["by"] == role and r["round"] == round_:
                    rec = self.store.obj(r["id"])
            sbl = (rec or {}).get("content", {}).get("scenario_by_lens") if rec else None
            if not isinstance(sbl, dict):
                continue
            tag = ROLES[role]
            per, s_refs = {}, {}
            for L in lenses:
                sc = sbl.get(L)
                out = side_ruler(sc, tag, cfgs[L]) if isinstance(sc, dict) else {"ok": False, "fatal": ["no scenario"]}
                if not out["ok"]:
                    lines.append(f"{tag} {L} scenario: INVALID ({'; '.join(out['fatal'][:2])})")
                    per[L] = out
                    continue
                strat = " · ".join(f"{n} x*={x:.2f}" for n, x in out["x_by"].items())
                lines.append(f"{tag} {L} scenario ({out['nodes']} nodes, T={out['T_days']}): S_ref {out['S_ref']} · best "
                             f"{out['best']} q*={out['q']:.3f} x*={out['x']:.3f} · {strat}")
                lines.append(f"  {tag} {L} " + self._decomposition(sc, L))   # drill r1-rational-5
                lines.append(f"  {tag} {L} " + self._tree_extras(sc, L, role, round_, out["S_ref"]))   # v3.6
                per[L] = {"ok": True, "S_ref": out["S_ref"], "best": out["best"], "q": out["q"], "x": out["x"],
                          "warn": out["warn"]}
                s_refs[L] = out["S_ref"]
            if len(s_refs) == len(lenses) and set(lam) == set(lenses):
                summary = sum(lam[L] * s_refs[L] for L in lenses)
                lines.append(f"{tag} scenario, default-weighted over {'/'.join(lenses)} "
                             f"({' / '.join(f'{lam[L]:g}' for L in lenses)}): S_ref {summary:.1f}")
            by_side[role] = per
        if lines:
            lines.insert(0, "[Scenario ruler 3.4 — machine-computed per side on its own contracts; S_ref = 100·G/(G+L), "
                            "q* = target exposure, x* = 2|q*−0.5|]")
        return {"lines": lines, "by_side": by_side}

    def _atr_pct(self):
        """ATR%14 of the symbol's opening snapshot (for z flags); None when unavailable."""
        try:
            tech = ((self.state.get("data_files") or {}).get("symbol") or {}).get("technical")
            if not tech:
                return None
            snap = json.loads((self.root / tech).read_text(encoding="utf-8"))
            v = snap.get(f"{self.symbol}_daily_atr_pct_14")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
        except Exception:
            return None

    def close_round(self, round_: int, notes: dict) -> dict:
        self._require()
        lt = self.state.get("last_tally") or {}
        if lt.get("round") != round_:
            raise StepError(f"Run `tally --round {round_}` before close-round")
        if not (notes.get("arbiter_notes") or "").strip():
            raise StepError("notes.arbiter_notes missing (with no ruling, write 'no ruling this round')")
        ps = notes.get("private_score")
        if not isinstance(ps, dict) and isinstance(notes.get("private_note"), str) and notes["private_note"].strip():
            ps = {"note": notes["private_note"].strip()}     # a private note (inputs that moved, open points); a score is optional
        if not isinstance(ps, dict) or not (isinstance(ps.get("composite_score"), int) or str(ps.get("note") or "").strip()):
            raise StepError("notes.private_score (score or note) missing — record a line even when nothing moved: which inputs changed, what stays open")
        decision = notes.get("decision")
        if decision not in ("CONTINUE", "TERMINATE"):
            raise StepError("notes.decision must be CONTINUE|TERMINATE")
        session_cfg(self)   # a session opened before v3.4 cannot close
        return self._close_round(round_, notes, lt, ps, decision)

    def _arbiter_named(self, extra: Any = None) -> set:
        """3.4: claims the arbiter ruled on in an archived round's NOTES dispositions (+ `extra`, this round's): a
        self-settled claim closes only when the opponent conceded or the arbiter named it (ledger_tally)."""
        from ledger_tally import arbiter_named
        rows = [self.store.obj(r["id"]).get("content") or {} for r in self.store.rows() if r.get("kind") == "notes"]
        return arbiter_named(rows + [{"dispositions": extra or []}])

    def _still_closed(self, res: dict) -> set:
        """Claims closed in an earlier round whose stored closing record still passes closure_check now."""
        import closure_check as cc
        recs = self.state.get("closed_records") or {}
        out = cc.check(recs, dict(res, open_total=list(recs)), cc.records_from_store(self.store),
                       self.state.get("spawns") or {})
        return set(out["closable"])

    def _close_round(self, round_: int, notes: dict, lt: dict, ps: dict, decision: str) -> dict:
        """3.4 (C5): terminal_open values are closing records checked by closure_check; `unresolved` is script-only
        (written at max_rounds); a TERMINATE with any open claim lacking a passing record is overridden to CONTINUE;
        the arbiter's dispositions[] are archived in NOTES.R<n>. The check runs before anything is archived (the
        NOTES record it reads is the one about to be minted, at the next manifest position); records that pass are
        kept in state.closed_records so the next tally does not list those claims as open again."""
        import closure_check as cc
        terminal = notes.get("terminal_open") or {}
        errs = cc.shape_errors(terminal) + cc.disposition_errors(notes.get("dispositions"))
        if errs:
            raise StepError("; ".join(errs))
        # v3.6: the debaters' copy of the tally carries the forecast's score, decomposition and extras, not the strategy
        # outcome (best / q* / x* / CE) — a tree is scored before contracts are compared, so paths cannot be drawn to fill
        content = {"tally": lt.get("brief_public", lt.get("brief", "")), "arbiter_notes": notes["arbiter_notes"]}
        if terminal:
            content["terminal_open"] = terminal
        if notes.get("dispositions"):
            content["dispositions"] = notes["dispositions"]
        max_rounds = self.caps["max_rounds"]
        # a self-settled claim (or challenge) the arbiter rules on in this round's dispositions — the claim or the target
        # of the challenge — leaves the open list (ledger ruling); the named rule itself is ledger_tally's
        def pending_of(res: dict) -> set:
            return {c for side in ("zealot", "reaper") for c in ((res.get(side) or {}).get("self_settled_pending") or [])}
        before = pending_of(self.tools.tally(self._entries(), ruler=True, named=self._arbiter_named()))
        tres = self.tools.tally(self._entries(), ruler=True, named=self._arbiter_named(notes.get("dispositions")))
        ruled_now = before - pending_of(tres)
        tres["open_total"] = [c for c in (lt.get("open_total") or []) if c not in ruled_now]
        records = cc.records_from_store(self.store)
        records.append({"seq": len(self.store.rows()) + 1, "id": f"NOTES.R{round_}", "kind": "notes", "role": "A",
                        "round": round_, "content": content})
        out = cc.check(terminal, tres, records, self.state.get("spawns") or {}, None, max_rounds, round_)
        self.store.put("notes", content, round_=round_)
        self.store.put("private_score", ps, round_=round_)
        self.state["focus"] = notes.get("focus") or self.state["focus"]
        self.state["proactive"] = [r for r in (notes.get("proactive_requests") or []) if isinstance(r, dict)]
        closed = self.state.setdefault("closed_records", {})
        for cid in out["closable"]:
            closed[cid] = terminal[cid]
        reason = None
        if decision == "TERMINATE" and out["open"] and round_ < max_rounds:
            decision, reason = "CONTINUE", ("Open claims without a passing closing record (C5): "
                                            + "; ".join(f"{c}: {out['reasons'].get(c)}" for c in out["open"]))
        if decision == "CONTINUE" and round_ >= max_rounds:
            decision, reason = "TERMINATE", (f"Round cap {max_rounds} reached; open claims {out['open']} recorded as "
                                             f"unresolved (neither consensus nor concession)")
        if decision == "TERMINATE":
            self.state["deadlocked"] = sorted(set(closed) - set(out["open"]))
            self.state["unresolved"] = list(out["open"]) if round_ >= max_rounds else []
        rc = self._after_close(round_, decision, reason)
        ref = self.caps.get("round_ref")
        if decision == "TERMINATE" and isinstance(ref, int) and round_ < ref:
            rc["early_close"] = (f"closed at round {round_}, below the reference length {ref} (guidance, not a gate): "
                                 "the notes must say in one sentence why the topic list is already complete")
        rc["closure"] = {"closable": out["closable"], "open": out["open"], "reasons": out["reasons"]}
        return rc

    def _after_close(self, round_: int, decision: str, reason: str | None) -> dict:
        rc = {"ok": True, "round": round_, "decision": decision, "override_reason": reason,
              "notes_id": f"NOTES.R{round_}", "private_id": f"PRIVATE.R{round_}"}
        if decision == "CONTINUE":
            self.state["round"] = round_ + 1
            self.state["beat"] = {"Zealot": 0, "Reaper": 0}
            for role in ROLES:
                self.store.build_view(role.lower())
            rc["spawn"] = {r: self.spawn_material(r, round_ + 1) for r in ROLES}
            rc["next"] = (f"Close this round's two debater threads; spawn the new round's Zealot / Reaper with the material files in full; "
                          f"once received, `route --round {round_ + 1} --beat 1 ...`")
        else:
            self.state["status"] = "finalizing"
            rc["unresolved"] = self.state.get("unresolved") or []
            rc["next"] = "`finalize --stage provisional` (first pass of the final ruling: the verdict list; do not look at the ledger scores yet)"
        self.save()
        return rc

    # ---------- finalize ----------
    def _reference_prices(self) -> list:
        """本场参考价: 从开场时登记的技术快照机械取(模型不填), 每个价格带它自己的行情时刻。
        个股与 ETF 一条, 大盘场每个镜头一条; 取不到就没有那一条, 不填零, 不借别的镜头的价。"""
        df = self.state.get("data_files") or {}
        pairs = []
        tech = (df.get("symbol") or {}).get("technical")
        if tech:
            pairs.append((self.symbol, tech))
        pairs += list((df.get("lenses") or {}).items())
        try:
            from workflows.data_manifest import load_symbols_cfg
            contracts = (load_symbols_cfg(self.root) or {}).get("symbol_contracts") or {}
        except Exception:
            contracts = {}
        out = []
        for sym, rel in pairs:
            try:
                snap = json.loads((self.root / rel).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            price = snap.get(f"{sym}_current_snapshot_last_price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
                continue
            cur = (contracts.get(sym) or {}).get("currency") or (self.state.get("currency") if sym == self.symbol else None)
            out.append({"symbol": sym, "price": price, "currency": cur or None,
                        "as_of": snap.get(f"{sym}_latest_data_timestamp"),
                        "session": snap.get(f"{sym}_session_context_current_session"), "source": rel})
        return out

    def _meta(self, dissent: dict | None = None) -> dict:
        st = self.state
        eng = ENGINE_META.get(st.get("engine") or "cc", ENGINE_META["cc"])
        model = st.get("model") or ""
        sig = {"zealot": model, "reaper": model, "arbiter": model}
        objections = []
        for r in self.store.rows():
            if r["kind"] == "objection":
                rec = self.store.obj(r["id"])
                objections.append({"id": rec["id"], "by": rec["by"], "round": rec["round"], **rec["content"]})
        rulings = [r["id"] for r in self.store.rows() if r["kind"] == "ruling"]
        meta = {"symbol": self.symbol, "market_data_timestamp_utc": _iso_from_ts(self.ts),
                "report_generated_at": _now_iso(), "evidence_generated_at": _now_iso(),
                "data_files": st["data_files"], "signatures": sig, "model_signature": sig,
                "engine_family": eng["engine_family"], "runner": eng["runner"], "topology": TOPOLOGY,
                "harness_truth": {"harness": st.get("engine"), "model": model, "worker_model": st.get("worker_model"),
                                  "driver": "scripts/run_step.py", "calc_check": st.get("calc_check") or "remind"},
                "objections": objections, "ruling_version": len(rulings), "unresolved": st.get("unresolved") or [],
                "deadlocked": st.get("deadlocked") or [],
                "reference_prices": self._reference_prices(),
                "atr_pct_14": self._atr_pct(), "caps": self.caps}
        session_cfg(self)   # raises on a session opened before v3.4
        meta["odds"] = json.loads(json.dumps(self.state["odds"]))   # the full snapshot: the report carries its own ruler (C4)
        meta["spawns"] = self.state.get("spawns") or {}   # store positions; report records carry store seqs
        if st.get("group_session"):
            meta["group_session"] = st["group_session"]
        if st.get("lineage"):   # 11-06: follow-up (delta) session lineage — mode, base_as_of, depth, why, imported A.EV ids
            meta["lineage"] = json.loads(json.dumps(st["lineage"]))
        from lens_views import lenses_of
        if lenses_of(self.state):   # C11.6: the release gate reads the frozen lens set from the report, never from config
            meta["lenses"] = lenses_of(self.state)
            meta["lens_weights_default"] = dict(self.state.get("lens_weights_default") or {})
        if dissent:
            meta["dissent"] = dissent
        return meta

    def _assemble(self, ruling: dict, dissent: dict | None, pre_signoff: bool) -> tuple[dict, dict, Path]:
        from assemble_debate_artifacts import assemble
        from next_review_enrich import enrich_next_review
        ruling = dict(ruling)   # 11-06: the machine adds expires_at (horizon end) and known_events to the arbiter's next_review
        ruling["next_review"] = enrich_next_review(ruling.get("next_review"), self.ts,
                                                   (self.state.get("odds") or {}).get("T_days"), self.symbol,
                                                   self.state.get("region") or "US")
        draft = self.store.dir / "_draft_report.json"
        draft.write_text(json.dumps(ruling, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        meta_p = self.store.dir / "_meta.json"
        meta_p.write_text(json.dumps(self._meta(dissent), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        analysis = self.root / "data" / "debate" / self.symbol / f"{self.symbol}_Analysis_{self.ts}.json"
        evidence = self.root / "data" / "evidence" / self.symbol / f"{self.symbol}_Evidence_{self.ts}.json"
        receipt = assemble(draft, self.store.debate_buf, self.store.evidence_buf, analysis, evidence,
                           private_buffer_path=self.store.private_buf, meta_path=meta_p,
                           odds_cfg=session_cfg(self),
                           store_seq={r["id"]: i for i, r in enumerate(self.store.rows(), 1)})
        doc = _read_json(analysis)
        fatal, warn = _safe_validate(doc, str(analysis), pre_signoff=pre_signoff)
        fatal += gate_extras(doc, analysis, self.root)
        return receipt, {"ok": not fatal, "fatal": fatal, "warnings": warn}, analysis

    def _derive(self, inp: dict | None, stage: str) -> dict:
        """3.4 arbiter stages (C6): the script derives scenario = A, operation_*, composite_score before the record is archived."""
        from ruling import RulingError, derive_ruling
        if not isinstance(inp, dict):
            raise StepError(f"{stage}: --input JSON object required")
        try:
            return derive_ruling(self.store, self, inp, stage)
        except RulingError as exc:
            raise StepError(f"{stage} (3.4): {exc} — nothing archived; fix the input and resubmit")

    def _with_endorsement(self, role: str, content: dict, signoff: bool = False) -> dict:
        """C8: verify an optional scenario_revision against the side's current endorsed scenario and store the result as
        `endorsed_scenario`; at signoff the derived `endorsed_composite_score` replaces an authored one (kept as authored_score)."""
        from ruling import RulingError, derived_score, endorsed
        if not isinstance(content, dict):
            raise StepError(f"{role}: JSON object required")
        cfg = session_cfg(self)
        from lens_views import lenses_of
        if lenses_of(self.state):
            return self._with_endorsement_lenswise(role, content, signoff, lenses_of(self.state), cfg)
        try:
            sc, _head = endorsed(self.store, ROLES[role], content.get("scenario_revision"),
                                 p_tolerance=float(cfg.get("p_tolerance", 0.01)))
        except RulingError as exc:
            raise StepError(f"{role} {'signoff' if signoff else 'objection'} (3.4): {exc} — nothing archived")
        out = dict(content)
        out["endorsed_scenario"] = sc
        if signoff:
            score = derived_score(sc, cfg)
            if score is None:
                raise StepError(f"{role} signoff (3.4): the endorsed scenario is invalid — nothing archived")
            if "endorsed_composite_score" in out:
                out["authored_score"] = out["endorsed_composite_score"]
            out["endorsed_composite_score"] = score
        return out

    def _with_endorsement_lenswise(self, role: str, content: dict, signoff: bool, lenses: list, cfg: dict) -> dict:
        """C11.5: the endorsed scenario per lens (`endorsed_scenario_by_lens`; `endorsed_scenario` = the primary lens's);
        the revision's ops must each carry a session lens. At signoff the derived per-lens scores
        (`endorsed_composite_by_lens`, each lens on its own ruler) and their default-weighted
        `endorsed_composite_score` replace an authored score (kept as authored_score)."""
        from lens_views import ops_lens_errors, weighted_score
        from ruling import RulingError, derived_score, endorsed
        stage = "signoff" if signoff else "objection"
        rev = content.get("scenario_revision")
        if isinstance(rev, dict) and isinstance(rev.get("ops"), list):
            errs = ops_lens_errors("scenario_revision", rev["ops"], lenses)
            if errs:
                raise StepError(f"{role} {stage} (3.4): {'; '.join(errs)} — nothing archived")
        by_lens = {}
        for L in lenses:
            try:
                by_lens[L], _head = endorsed(self.store, ROLES[role], rev, p_tolerance=float(cfg.get("p_tolerance", 0.01)),
                                             lens=L)
            except RulingError as exc:
                raise StepError(f"{role} {stage} (3.4): lens {L}: {exc} — nothing archived")
        out = dict(content)
        out["endorsed_scenario_by_lens"] = by_lens
        out["endorsed_scenario"] = by_lens[lenses[0]]
        if signoff:
            scores = {}
            for L in lenses:
                scores[L] = derived_score(by_lens[L], self._lens_cfg(L))
                if scores[L] is None:
                    raise StepError(f"{role} signoff (3.4): the endorsed scenario on lens {L} is invalid — nothing archived")
            if "endorsed_composite_score" in out:
                out["authored_score"] = out["endorsed_composite_score"]
            out["endorsed_composite_by_lens"] = scores
            out["endorsed_composite_score"] = weighted_score(scores, self.state["lens_weights_default"])
        return out

    def _signoff_dissent(self, sides: dict, version: int) -> dict:
        """The report's dissent block from both signoffs (C8; C11.5 adds `<role>_endorsed_scenario_by_lens`, the top-level
        `<role>_endorsed_scenario` being the primary lens's and `<role>_composite_score` the default-weighted summary of
        `<role>_composite_by_lens`)."""
        dissent = {}
        for role in ROLES:
            s = sides[role]
            k = role.lower()
            dissent[f"{k}_composite_score"] = s.get("endorsed_composite_score")
            dissent[f"{k}_reservation"] = s.get("reservation") or "No reservations"
            dissent[f"{k}_dissent_refs"] = s.get("dissent_refs") or []
            dissent[f"{k}_endorsed_scenario"] = s["endorsed_scenario"]
            if "endorsed_scenario_by_lens" in s:
                dissent[f"{k}_endorsed_scenario_by_lens"] = s["endorsed_scenario_by_lens"]
            if "endorsed_composite_by_lens" in s:   # the per-lens dissent scores the gate re-derives (C11.6)
                dissent[f"{k}_composite_by_lens"] = s["endorsed_composite_by_lens"]
        dissent["ruling_version"] = version
        return dissent

    def finalize(self, stage: str, inp: dict | None = None, round_: int | None = None,
                 sides: dict[str, dict] | None = None) -> dict:
        self._require()
        if self.state.get("status") == "debating":
            raise StepError("Debate not closed (close-round has not reached TERMINATE); cannot finalize")
        sides = sides or {}
        if stage == "provisional":
            if inp is None:
                self.store.build_view("arbiter")
                return self._with_lens_note({"ok": True, "stage": stage, "view": self._rel(self.store.dir / "_view_arbiter.jsonl"),
                        "reads": self._arbiter_reads(MANUALS["final_ruling"], MANUALS["execution_lines"], MANUALS["format_final"],
                                                     self.state["context_manual"]),
                        "unresolved": self.state.get("unresolved") or [],
                        "next": PROV_NEXT})
            inp = self._derive(inp, stage)
            from validate_debate import operation_target_error
            # the target is the single derived level for the scenario's own P0, GENERAL sessions included
            _terr = operation_target_error(None, inp.get("operation_target") if isinstance(inp, dict) else None)
            if _terr:
                raise StepError(_terr + " — the first ruling cannot be changed after archiving; fix it first")
            rc = self.store.put("provisional", inp)
            return {"ok": True, "stage": stage, "id": rc["id"], "next": "`finalize --stage audit`"}
        if stage == "audit":
            if "PROV" not in self.store.ids():
                raise StepError("Archive the first ruling (provisional) first")
            res = self.tools.tally(self._entries(), ruler=True, named=self._arbiter_named())
            full = self._write(self.store.dir / "_tally_full.json", json.dumps(res, ensure_ascii=False, indent=1) + "\n")
            return self._with_lens_note({"ok": True, "stage": stage, "tally_full": full, "tally_brief": self.tools.tally_brief(res),
                    "scoreboard": res.get("scoreboard"), "reads": self._arbiter_reads(MANUALS["final_ruling"], MANUALS["format_final"]),
                    "odds": (self.state.get("last_tally") or {}).get("odds") or {},
                    "next": AUDIT_NEXT})
        if stage == "proposed":
            if inp is None:
                raise StepError("--input proposed.json required")
            inp = self._derive(inp, stage)
            rc = self.store.put("proposed", inp)
            mats = {r: self.objection_material(r, 1) for r in ROLES}
            self.state["objection_round"] = 1
            self.save()
            return {"ok": True, "stage": stage, "id": rc["id"], "spawn": mats,
                    "next": "Draft ruling published: spawn each debater afresh (material file in full); once both objection JSON files are in, "
                            "`finalize --stage objection --round 1 --zealot z.json --reaper r.json`"}
        if stage == "objection":
            k = round_ or self.state.get("objection_round") or 1
            for role in ROLES:
                if role not in sides:
                    raise StepError(f"Objection round {k} is missing {role}'s JSON (submit an empty list even with no objections)")
            # C8: verify both revisions before archiving either
            sides = {role: self._with_endorsement(role, sides[role]) for role in ROLES}
            for role in ROLES:
                self.store.put("objection", sides[role], by=role, round_=k)
            any_obj = any((sides[r].get("objections") or sides[r].get("replies")) for r in ROLES)
            needs = {r: sides[r].get("needs_new_evidence") for r in ROLES if sides[r].get("needs_new_evidence")}
            if k < self.caps["objection_rounds"] and any_obj:
                self.state["objection_round"] = k + 1
                self.save()
                mats = {r: self.objection_material(r, k + 1) for r in ROLES}
                return {"ok": True, "stage": stage, "round": k, "spawn": mats, "needs_new_evidence": needs,
                        "next": f"Objection round {k + 1} (may rebut the other side's objections): spawn afresh; once received, "
                                f"`finalize --stage objection --round {k + 1} ...`"}
            ids = [r["id"] for r in self.store.rows() if r["kind"] == "objection"]
            self.store.build_view("arbiter")
            return self._with_lens_note({"ok": True, "stage": stage, "round": k, "objections": ids, "needs_new_evidence": needs,
                    "view": self._rel(self.store.dir / "_view_arbiter.jsonl"),
                    "reads": self._arbiter_reads(MANUALS["final_ruling"], MANUALS["format_final"]),
                    "next": "Redraft: handle each objection (adopted → amend the text; rejected → leave a brief reason); write ruling.json = "
                            "the full set of draft-ruling fields (revised) "
                            "+ objection_dispositions[{id, index, disposition: adopted|rejected|deferred, reason}]; "
                            "an objection that needs new evidence goes to unresolved (written into debate_summary), not silently patched. "
                            "`finalize --stage ruling --input ruling.json`"})
        if stage == "ruling":
            n_rulings = sum(1 for r in self.store.rows() if r["kind"] == "ruling")
            if inp is None:
                # 续跑: 对已归档的最新再定稿重新组装过闸(不铸新版, 不消耗重试机会)
                last = self.store.latest("ruling")
                if last is None:
                    raise StepError("--input ruling.json required (no redraft in the store yet)")
                rc = {"id": last["id"]}
                inp = self.store.obj(last["id"])["content"]
                n_rulings -= 1
            else:
                if n_rulings >= 2:
                    self._fail("Redraft failed the release gate twice → session failed and stopped (no placeholder numbers)")
                if not isinstance(inp.get("objection_dispositions"), list):
                    raise StepError("ruling.objection_dispositions missing (record of objection handling; give an empty list with no objections)")
                inp = self._derive(inp, stage)
                rc = self.store.put("ruling", inp)
            version = int(rc["id"].rsplit("v", 1)[1])
            receipt, verdict, analysis = self._assemble(inp, None, pre_signoff=True)
            if not verdict["ok"]:
                self.save()
                return {"ok": False, "stage": stage, "id": rc["id"], "gate": verdict,
                        "retry": n_rulings == 0,
                        "next": "Fix only judgment fields per the fatal list (mechanically injected fields and the first ruling must not be touched), "
                                "then `finalize --stage ruling --input ruling2.json` (one chance only)"}
            mats = {r: self.signoff_material(r, version) for r in ROLES}
            self.state["ruling_version"] = version
            self.save()
            return {"ok": True, "stage": stage, "id": rc["id"], "gate": verdict, "assembly": receipt,
                    "spawn": mats, "next": "Redraft passed the gate; spawn each debater afresh for signoff; once both JSON files are in, "
                                           "`finalize --stage signoff --zealot z.json --reaper r.json`"}
        if stage == "signoff":
            version = self.state.get("ruling_version") or 0
            if not version:
                raise StepError("A redraft must pass the gate before signoff")
            for role in ROLES:
                if role not in sides:
                    raise StepError(f"Missing {role}'s signoff JSON")
            # C8: endorsed scenario → derived dissent score; a signoff revision never changes A
            sides = {role: self._with_endorsement(role, sides[role], signoff=True) for role in ROLES}
            for role in ROLES:
                self.store.put("signoff", sides[role], by=role, meta={"ruling_version": version})
            dissent = self._signoff_dissent(sides, version)
            ruling = self.store.obj(f"RULING.v{version}")["content"]
            receipt, verdict, analysis = self._assemble(ruling, dissent, pre_signoff=False)
            if not verdict["ok"]:
                self._fail("Release gate failed after signoff: " + "; ".join(verdict["fatal"])[:800])
            done = self.store.complete_buffers()
            self.state["status"] = "done"
            self.save()
            doc = _read_json(analysis)
            return {"ok": True, "stage": stage, "gate": verdict, "assembly": receipt, "buffers": done,
                    "analysis_path": self._rel(analysis),
                    "evidence_path": self._rel(self.root / "data" / "evidence" / self.symbol / f"{self.symbol}_Evidence_{self.ts}.json"),
                    "composite_score": doc.get("composite_score"), "operation_type": doc.get("operation_type"),
                    "operation_target": doc.get("operation_target"), "rounds": self.store.last_final_round(),
                    "dissent": dissent, "unresolved": self.state.get("unresolved") or [],
                    "next": "Session complete; the orchestration layer runs `gate --analysis <path>` again before writing to the database (built into pg_store_debate.py)"}
        raise StepError(f"unknown stage {stage!r}")


# ---------------- gate ----------------

def gate_extras(doc: dict, analysis_path: Path, root: Path = ROOT) -> list[str]:
    """validate_debate 之外的结构契约(原 codex_loop 父层验收, 两引擎共用一条闸)。"""
    errors: list[str] = []
    for k in ("symbol", "market_data_timestamp_utc", "report_generated_at", "data_files", "signatures",
              "engine_family", "runner", "topology", "evidence", "evidence_added", "evidence_generated_at",
              "search_rounds_used", "model_calls", "objections", "next_review", "statement", "debate_summary",
              "reasoning", "risk_management"):
        if k not in doc:
            errors.append(f"Missing field {k}")
    m = re.search(r"_Analysis_(\d{8}T\d{6}Z)\.json$", analysis_path.name)
    if m:
        ts = str(doc.get("market_data_timestamp_utc") or "").replace("-", "").replace(":", "")
        if ts and ts != m.group(1):
            errors.append(f"market_data_timestamp_utc {doc.get('market_data_timestamp_utc')} does not match the filename timestamp {m.group(1)}")
    sig = doc.get("signatures")
    if isinstance(sig, dict) and set(sig) != {"zealot", "reaper", "arbiter"}:
        errors.append("signatures must have exactly the three keys zealot / reaper / arbiter")
    ev = doc.get("evidence")
    if isinstance(ev, list):
        for i, it in enumerate(ev):
            if not isinstance(it, dict) or not it.get("question") or not it.get("simple") \
                    or not it.get("detailed") or not isinstance(it.get("source"), list):
                errors.append(f"evidence[{i}] does not match the shape question/simple/detailed/source[]")
            elif it.get("kind") == "data" and not str(it.get("payload_text") or "").strip()                     and it.get("status") != "failed":
                # 全文铁律只管真用到的数据调用; 执行者掉线(status=failed)的回执本来就是空的, 不是证据, 不能为它要全文
                # (2026-10-13 MU: 一条 failed 的 P10 调用把两次再定稿都卡死在这条上, 仲裁者又不许动机械注入字段)
                errors.append(f"evidence[{i}] data call is missing the payload_text full text")
    ea = doc.get("evidence_added")
    if isinstance(ea, list):
        for i, it in enumerate(ea):
            if not isinstance(it, dict) or not all(isinstance(it.get(k), str) and it.get(k) for k in ("item", "as_of", "status", "source")):
                errors.append(f"evidence_added[{i}] must be an object with non-empty strings item/as_of/status/source")
    if any(str(e.get("round")) == "Signoff" for e in (doc.get("debate_history") or []) if isinstance(e, dict)):
        errors.append("debate_history must not contain Signoff")
    dd = doc.get("dissent")
    if isinstance(dd, dict) and any(str(k).startswith("fulcrum_") for k in dd):
        errors.append("dissent contains retired fulcrum fields")
    if m and isinstance(doc.get("scenario"), dict):   # ruler report (review M5): the report's ruler is the session's, byte for byte (C4)
        sess = root / "data" / "store" / str(doc.get("symbol") or "") / m.group(1) / "_session.json"
        if sess.exists():
            st = None
            try:
                st = _read_json(sess)
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"_session.json unreadable for the odds / spawns comparison: {exc}")
            canon = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
            if st is not None and canon(doc.get("odds")) != canon(st.get("odds")):
                errors.append("odds differs from the session's _session.json.odds snapshot (C4: the report carries the "
                              "ruler frozen at open; it is never edited)")
            # C5: access comes from the session's spawn records, byte for byte
            if st is not None and canon(doc.get("spawns")) != canon(st.get("spawns")):
                errors.append("spawns differs from the session's _session.json.spawns (C5: access is established by the "
                              "spawn records written at each material; the report never edits them)")
            if st and st.get("protection_required"):
                version = st["ruling_version"] if st.get("status") == "done" else doc.get("ruling_version")
                try:
                    if type(version) is not int or version < 1:
                        raise ValueError("ruling version missing")
                    signed = _read_json(sess.parent / f"RULING.v{version}.json")["content"]
                    for lens in [None, *(signed.get("by_lens") or {})]:
                        expected = signed if lens is None else signed["by_lens"][lens]
                        actual = doc if lens is None else (doc.get("by_lens") or {}).get(lens, {})
                        for field in ("downside_protection", "hedge_summary"):
                            if actual.get(field) != expected.get(field):
                                errors.append(f"{lens or doc.get('symbol')}: {field} differs from the archived ruling")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"cannot verify protection against the archived ruling: {exc}")
    return errors


def gate(analysis: Path, root: Path = ROOT) -> dict:
    doc = _read_json(analysis)
    fatal, warn = _safe_validate(doc, str(analysis))
    fatal += gate_extras(doc, analysis, root)
    ev_path = root / "data" / "evidence" / str(doc.get("symbol") or "") / analysis.name.replace("_Analysis_", "_Evidence_")
    if ev_path.exists():
        try:
            side = _read_json(ev_path)
            if side != doc.get("evidence"):
                fatal.append("Evidence side file differs from the report's evidence (the derived copy must be identical verbatim)")
        except (OSError, json.JSONDecodeError) as exc:
            fatal.append(f"Evidence side file unreadable: {exc}")
    return {"ok": not fatal, "fatal": fatal, "warnings": warn, "analysis": str(analysis)}


# ---------------- CLI ----------------

def _load_side(path: str | None) -> dict | None:
    return _read_json(path) if path else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol")
    common.add_argument("--ts")
    common.add_argument("--root", default=None)

    o = sub.add_parser("open", parents=[common])
    o.add_argument("--engine", choices=list(ENGINE_META), default="cc")
    o.add_argument("--model", default="")
    o.add_argument("--worker-model", dest="worker_model", default="")
    o.add_argument("--calc-check", dest="calc_check", choices=list(CALC_MODES), default="",
                   help="arithmetic-hint level: remind (default, reminder only) / off (no extra intervention) / always (compute everything)")
    for k, v in DEFAULT_CAPS.items():
        o.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int, default=None)
    o.add_argument("--base", default="", help="follow-up (delta) session on this earlier session's TS of the same symbol "
                                              "(or EQUIPOISE_DELTA_BASE); see scripts/delta_session.py")
    o.add_argument("--why", default="", help="one-line reason for the follow-up (or EQUIPOISE_DELTA_WHY)")

    r = sub.add_parser("route", parents=[common])
    r.add_argument("--round", dest="round_", type=int, required=True)
    r.add_argument("--beat", type=int, required=True)
    r.add_argument("--zealot")
    r.add_argument("--reaper")

    cb = sub.add_parser("close-beat", parents=[common])
    cb.add_argument("--round", dest="round_", type=int, required=True)
    cb.add_argument("--beat", type=int, required=True)
    cb.add_argument("--role", choices=["zealot", "reaper", "arbiter"], default=None,
                    help="close only this side's jobs (debaters proceed beat by beat independently); omit = close every in-flight job of this beat")
    cb.add_argument("--results", required=True,
                    help="JSON list: [{job_id, status, scope?}|{job_id, failed}]; when result is omitted the job's OUTPUT file is read")

    t = sub.add_parser("tally", parents=[common])
    t.add_argument("--round", dest="round_", type=int, required=True)

    cr = sub.add_parser("close-round", parents=[common])
    cr.add_argument("--round", dest="round_", type=int, required=True)
    cr.add_argument("--notes", required=True)

    f = sub.add_parser("finalize", parents=[common])
    f.add_argument("--stage", required=True,
                   choices=["provisional", "audit", "proposed", "objection", "ruling", "signoff"])
    f.add_argument("--input")
    f.add_argument("--round", dest="round_", type=int)
    f.add_argument("--zealot")
    f.add_argument("--reaper")

    sub.add_parser("status", parents=[common])

    g = sub.add_parser("gate", parents=[common])
    g.add_argument("--analysis", required=True)

    args = ap.parse_args(argv)
    if args.self_test:
        from debate_driver import _self_test
        _self_test()
        return 0
    if not args.cmd:
        ap.error("command required")
    root = Path(args.root) if args.root else ROOT
    try:
        if args.cmd == "gate":
            rc = gate(Path(args.analysis) if Path(args.analysis).is_absolute() else root / args.analysis, root)
        elif args.cmd == "open":
            if not args.symbol:
                ap.error("--symbol required")
            caps = {k: getattr(args, k) for k in DEFAULT_CAPS if getattr(args, k) is not None}
            s = Session.open(args.symbol, root, args.engine, args.model, args.worker_model, caps,
                             calc_check=args.calc_check, base=args.base, why=args.why)
            rc = s.open_receipt()
        else:
            if not args.symbol or not args.ts:
                ap.error("--symbol and --ts required")
            s = Session(args.symbol, args.ts, root)
            if args.cmd == "status":
                s._require() if not s.state else None
                rc = s.where()
            elif args.cmd == "route":
                rc = s.route(args.round_, args.beat, {"Zealot": _load_side(args.zealot), "Reaper": _load_side(args.reaper)})
            elif args.cmd == "close-beat":
                rc = s.close_beat(args.round_, args.beat, _read_json(args.results), role=args.role)
            elif args.cmd == "tally":
                rc = s.tally(args.round_)
            elif args.cmd == "close-round":
                rc = s.close_round(args.round_, _read_json(args.notes))
            else:
                sides = {k: v for k, v in (("Zealot", _load_side(args.zealot)), ("Reaper", _load_side(args.reaper))) if v}
                rc = s.finalize(args.stage, _load_side(args.input), args.round_, sides)
    except StepError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "code": exc.code}, ensure_ascii=False))
        return exc.code
    except (StoreError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(rc, ensure_ascii=False))
    return 0 if rc.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
