#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""debate_driver.py — 全脚本驱动器: run_step 的步骤 + 无状态模型调用 = 一场辩论(重建批 3)。

与主会话编排(CC /run、Codex run.md 按 orchestration/run-protocol.md 一步步调 run_step.py)并行共存:
这里把"主会话"也换成代码——每个判断点是一次无状态 `codex exec`(或 mock), 读物是 run_step 写好的材料
文件(callee 自己 Read), 输出一个 JSON 对象。对象仓 manifest 即断点: 任何一步死掉, 重跑同一命令从最后
一个已铸对象继续。

    ./venv/bin/python scripts/debate_driver.py NVDA [--judge-model gpt-6-sol --worker-model gpt-6-luna] [--pg-store]
    ./venv/bin/python scripts/debate_driver.py --self-test        # mock 引擎 + mock 工具跑通整条协议

退出码: 0 ok / 10 数据未齐(先数据准备) / 20 本场失败。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from debate_store import ROLES, role_record  # noqa: E402
from run_step import Session, StepError, MockTools, MANUALS, JSON_ONLY  # noqa: E402


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    try:
        v = json.loads(raw)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        v = json.loads(raw[start:end + 1])
        if isinstance(v, dict):
            return v
    raise ValueError("no JSON object in model output")


# ---------------- engines ----------------

class CodexEngine:
    """One judgment = one fresh `codex exec` session (stateless, tools available)."""

    def __init__(self, root: Path, timeout: int):
        from workflows.harness_resolver import resolve_codex
        self.harness = resolve_codex()
        self.root = root
        self.timeout = timeout

    def call(self, tag: str, prompt: str, model: str, timeout: int | None = None) -> str:
        status_dir = self.root / "data" / "status"
        status_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f"drv_{tag}_", suffix=".txt", delete=False,
                                         dir=str(status_dir)) as f:
            out_path = f.name
        cmd = [self.harness.executable, "--ask-for-approval", "never", "--search", "exec",
               "-C", str(self.root), "--sandbox", "workspace-write", "--json",
               "-o", out_path, "--model", model, prompt]
        try:
            subprocess.run(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=self.harness.child_env(), timeout=timeout or self.timeout, check=False)
            return Path(out_path).read_text(encoding="utf-8", errors="replace")
        finally:
            Path(out_path).unlink(missing_ok=True)


class MockEngine:
    """Deterministic canned responses for the dry-run self-test."""

    def __init__(self, script: dict[str, list]):
        self.script = script
        self.calls: list[str] = []

    def call(self, tag: str, prompt: str, model: str, timeout: int | None = None) -> str:
        self.calls.append(tag)
        for key, queue in self.script.items():
            if tag == key and queue:
                return json.dumps(queue.pop(0), ensure_ascii=False)
        raise RuntimeError(f"mock has no response for tag {tag}")


# ---------------- driver ----------------

class Driver:
    def __init__(self, symbol: str, root: Path, engine, judge_model: str, worker_model: str,
                 call_timeout: int = 2400, caps: dict | None = None, tools=None, pg_store: bool = False):
        self.symbol, self.root, self.engine = symbol.upper(), root, engine
        self.judge_model, self.worker_model, self.call_timeout = judge_model, worker_model, call_timeout
        self.caps, self.tools, self.pg_store = caps, tools, pg_store
        self.engine_name = "mock" if isinstance(engine, MockEngine) else "codex"

    def call_json(self, tag: str, prompt: str, model: str, check=None) -> dict:
        last_err = None
        for attempt in (1, 2):
            text = self.engine.call(tag, prompt if attempt == 1 else
                                    prompt + f"\n\n[RESEND] previous output was not acceptable: {last_err}. " + JSON_ONLY,
                                    model, self.call_timeout)
            try:
                obj = self._harvest(text)
                if check:
                    err = check(obj)
                    if err:
                        raise ValueError(err)
                return obj
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = str(exc)[:400]
        raise StepError(f"model call {tag} failed twice: {last_err}", 20)

    def _harvest(self, text: str) -> dict:
        """一份产物只生成一次: 子代理把 JSON 写进材料给定的 OUTPUT 文件、只回路径; 回的是 JSON 本体也照收。"""
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError):
            import re
            for m in re.finditer(r"[\w./\\:-]*_out_[\w.-]+?\.json", text or ""):
                p = Path(m.group(0))
                p = p if p.is_absolute() else self.root / p
                if p.exists():
                    return _extract_json(p.read_text(encoding="utf-8"))
            raise

    def _text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def _arbiter_prompt(self, step: str, body: str, reads: list[str]) -> str:
        return (f"You are the arbiter of this debate session (SYMBOL {self.symbol}). The loop and glue are run by the driver; this call does only: {step}.\n"
                f"First Read: {', '.join([MANUALS['principles'], MANUALS['arbiter_role']] + reads)}. "
                f"You do not Read any raw data file (context minimization).\n" + body + "\n" + JSON_ONLY)

    # ---------- one round ----------
    def run_round(self, s: Session) -> None:
        round_ = s.state["round"]
        beat = 1
        have = s.store.ids()
        beats: dict[str, dict | None] = {}

        def first(role: str):
            if f"R{round_}.{ROLES[role]}" in have:
                return None  # 续跑: 终稿已铸
            mat = s.store.dir / f"_spawn_R{round_}_{role.lower()}.md"
            if not mat.exists():
                s.spawn_material(role, round_)
            return self.call_json(f"beat_{role.lower()}_r{round_}_b1", mat.read_text(encoding="utf-8"),
                                  self.judge_model, check=lambda o: None if o.get("analysis_text") else "missing analysis_text")

        with ThreadPoolExecutor(max_workers=2) as pool:
            beats = dict(zip(ROLES, pool.map(first, ROLES)))
        while True:
            rc = s.route(round_, beat, beats)
            if not rc["ok"]:
                # 终稿未过校验: 把错误转回该辩手改一次
                fixed = {}
                for a in rc["archived"]:
                    if a.get("rejected"):
                        role = a["role"]
                        mat = (s.store.dir / f"_spawn_R{round_}_{role.lower()}.md").read_text(encoding="utf-8") \
                            if beat == 1 else (s.store.dir / f"_resume_R{round_}_B{beat}_{role.lower()}.md").read_text(encoding="utf-8")
                        fixed[role] = self.call_json(f"beat_{role.lower()}_r{round_}_b{beat}_fix",
                                                     mat + f"\n[RESUBMIT] previous final failed validation: {a['rejected'][:600]}", self.judge_model)
                rc = s.route(round_, beat, fixed)
                if not rc["ok"]:
                    raise StepError(f"R{round_} 终稿两次未过校验: {rc['archived']}", 20)
            results = []
            if rc["jobs"]:
                def work(j: dict) -> dict:
                    try:
                        out = self.call_json(f"{j['kind']}_{j['job_id']}", self._text(j["material"]), self.worker_model)
                        return {"job_id": j["job_id"], "result": out}
                    except StepError as exc:
                        return {"job_id": j["job_id"], "failed": str(exc)}
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(work, rc["jobs"]))
                digest = "\n".join(f"[{r['job_id']}] " + json.dumps(r.get("result") or {"failed": r.get("failed")},
                                                                       ensure_ascii=False)[:1500] for r in results)
                review = self.call_json(
                    f"review_r{round_}_b{beat}",
                    self._arbiter_prompt("evidence verification", "The results returned this beat are below; verify each one's status (verified|unverified|not_found):\n"
                                         + digest + '\nOutput {"stamps": [{"job_id": "...", "status": "...", "scope": '
                                         '"one sentence: which part may be used as fact, which part is only inference or was not found (required for every item)"}]}',
                                         [MANUALS["evidence_review"]]),
                    self.judge_model, check=lambda o: None if isinstance(o.get("stamps"), list) else "missing stamps")
                stamps = {st.get("job_id"): st for st in review["stamps"] if isinstance(st, dict)}
                for r in results:
                    if "failed" not in r:
                        st = stamps.get(r["job_id"]) or {}
                        r["status"] = st.get("status") or "unverified"
                        r["scope"] = st.get("scope") or "(arbiter did not write a verification scope)"
            rc2 = s.close_beat(round_, beat, results)
            if not rc2["pending"]:
                break
            beat += 1
            beats = {}
            for role, p in rc2["pending"].items():
                beats[role] = self.call_json(f"beat_{role.lower()}_r{round_}_b{beat}", self._text(p["material"]),
                                             self.judge_model)
        t = s.tally(round_)
        notes = self.call_json(
            f"notes_r{round_}",
            self._arbiter_prompt("round notes + private score + whether the debate is exhausted",
                                 f"Ledger tally mechanical lines (cite verbatim):\n{t['tally_brief']}\nArbiter view: {t['view']} (Read).\n"
                                 f"Hard conditions: {json.dumps(t['hard_stop'], ensure_ascii=False)}\n"
                                 '输出 {"arbiter_notes": "...", "private_score": {"composite_score": <int>, "operation_type": "...", '
                                 '"operation_target": "...", "operation_size": "...", "mentality": "...", "moved_by": [], "note": "..."}, '
                                 '"focus": "...", "proactive_requests": [], "decision": "CONTINUE|TERMINATE"}',
                                 t["reads"]),
            self.judge_model,
            check=lambda o: None if o.get("arbiter_notes") and o.get("decision") in ("CONTINUE", "TERMINATE") else "need notes+decision")
        s.close_round(round_, notes)

    # ---------- final phase ----------
    def finalize(self, s: Session) -> dict:
        have = s.store.ids()
        if "PROV" not in have:
            rc = s.finalize("provisional")
            prov = self.call_json("provisional", self._arbiter_prompt(
                "first pass of the final ruling (claims review; do not look at the ledger tally scores yet)", f"Arbiter view: {rc['view']} (Read). Still unresolved: {rc['unresolved']}. "
                "Output the provisional object (claims_review[] / composite_score / operation_type / operation_target / "
                "operation_size / mentality / rationale[, midband_rationale])", rc["reads"]),
                self.judge_model, check=lambda o: None if "claims_review" in o else "missing claims_review")
            s.finalize("provisional", prov)
        if "PROPOSED" not in have:
            rc = s.finalize("audit")
            proposed = self.call_json("proposed", self._arbiter_prompt(
                "second pass of the final ruling (ledger audit) + draft ruling", f"Full ledger tally: {rc['tally_full']} (Read); mechanical lines:\n{rc['tally_brief']}\n"
                "Output the draft ruling = the full set of arbiter-written fields in formats/final.md (do not write ledger_score_z/r)", rc["reads"]),
                self.judge_model, check=lambda o: None if isinstance(o.get("mixture"), dict) else "missing mixture")
            rc = s.finalize("proposed", proposed)
        k = s.state.get("objection_round") or 1
        while True:
            if f"OBJ{k}.Z" in s.store.ids() and f"OBJ{k}.R" in s.store.ids():
                rc = {"spawn": None}
                # 已归档: 判断是否还有下一回合由 finalize 决定, 这里重放一次空调用不合适 → 直接进再定稿
                break
            sides = {}
            for role in ROLES:
                mat = s.store.dir / f"_spawn_OBJ{k}_{role.lower()}.md"
                if not mat.exists():
                    s.objection_material(role, k)
                sides[role] = self.call_json(f"objection_{role.lower()}_{k}", mat.read_text(encoding="utf-8"),
                                             self.judge_model,
                                             check=lambda o: None if isinstance(o.get("objections"), list) else "missing objections[]")
            rc = s.finalize("objection", round_=k, sides=sides)
            if rc.get("spawn"):
                k += 1
                continue
            break
        if not s.state.get("ruling_version"):
            view = s.store.build_view("arbiter")["view"]
            body = (f"Arbiter view: {view} (Read; includes PROPOSED and all OBJ objects). Dispose of each objection, then output the redraft = the full set of draft-ruling fields (revised) "
                    "+ objection_dispositions[{id, index, disposition, reason}]")
            ruling = self.call_json("ruling", self._arbiter_prompt("redraft", body, [MANUALS["final_ruling"], MANUALS["format_final"]]),
                                    self.judge_model)
            rc = s.finalize("ruling", ruling)
            if not rc["ok"]:
                if not rc.get("retry"):
                    raise StepError("ruling failed the gate twice", 20)
                fix = self.call_json("ruling_fix", self._arbiter_prompt(
                    "gate validation fix (one chance; judgment fields only)", f"Validation errors: {json.dumps(rc['gate'], ensure_ascii=False)[:2000]}\n"
                    f"Current redraft: {s.store.dir / 'RULING.v1.json'}", [MANUALS["final_ruling"]]), self.judge_model)
                rc = s.finalize("ruling", fix)
                if not rc["ok"]:
                    raise StepError(f"validate failed twice: {rc['gate']}", 20)
        version = s.state["ruling_version"]
        sides = {}
        for role in ROLES:
            mat = s.store.dir / f"_spawn_SIGN{version}_{role.lower()}.md"
            if not mat.exists():
                s.signoff_material(role, version)
            sides[role] = self.call_json(f"signoff_{role.lower()}", mat.read_text(encoding="utf-8"), self.judge_model,
                                         check=lambda o: None if isinstance(o, dict) else "signoff is not a JSON object")
        rc = s.finalize("signoff", sides=sides)
        if self.pg_store and self.engine_name != "mock":
            py = str(self.root / "venv" / "bin" / "python") if (self.root / "venv").exists() else sys.executable
            subprocess.run([py, "scripts/pg_store_debate.py", self.symbol,
                            str(self.root / rc["analysis_path"])], cwd=str(self.root), timeout=600)
            # last step (owner 2026-10-25): translation — original first, then one cheap-model pass per language → staging → load
            rc["translations"] = self._translate_last_step(py, str(self.root / rc["analysis_path"]))
        return rc

    def _translate_last_step(self, py: str, analysis_path: str) -> list:
        """extract → --fill (this engine's cheap model: claude→haiku / codex→gpt-6-luna) → --load, per language in config/i18n.yaml."""
        import json as _json
        langs = []
        try:
            import yaml as _yaml
            langs = list(((_yaml.safe_load((self.root / "config" / "i18n.yaml").read_text(encoding="utf-8")) or {}).get("translate_to") or []))
        except Exception:
            langs = ["zh"]
        engine = "claude" if str(self.engine_name).lower().startswith(("cc", "claude")) else "codex"
        done = []
        for lang in langs:
            try:
                ex = subprocess.run([py, "scripts/translate_reports.py", "--extract", "--table", "debates", "--analysis", analysis_path, "--to", lang],
                                    cwd=str(self.root), capture_output=True, text=True, timeout=120)
                rcpt = _json.loads((ex.stdout or "").strip().splitlines()[-1]) if ex.stdout.strip() else {}
                if not rcpt.get("ok") or not rcpt.get("strings"):
                    continue
                staging = rcpt["staging"]
                subprocess.run([py, "scripts/translate_reports.py", "--fill", staging, "--engine", engine], cwd=str(self.root), timeout=900)
                ld = subprocess.run([py, "scripts/translate_reports.py", "--load", staging], cwd=str(self.root), capture_output=True, text=True, timeout=120)
                done.append({"lang": lang, "staging": staging, "load": (ld.stdout or "").strip().splitlines()[-1] if ld.stdout.strip() else ld.stderr[-200:]})
            except Exception as exc:  # the original stands; translation failures are reported, never fatal
                done.append({"lang": lang, "error": str(exc)[:200]})
        return done

    def run(self) -> dict:
        s = Session.open(self.symbol, self.root, engine=self.engine_name, model=self.judge_model,
                         worker_model=self.worker_model, caps=self.caps, tools=self.tools)
        if s.state.get("status") == "done":
            return {"ok": True, "symbol": self.symbol, "resumed": "already done"}
        s.open_receipt()
        while s.state.get("status") == "debating":
            self.run_round(s)
        return self.finalize(s)


# ---------------- self test (mock engine + mock tools, 全流程干跑) ----------------

def _mock_root(tmp: Path) -> str:
    (tmp / "config").mkdir(parents=True)
    (tmp / "config" / "symbols.yaml").write_text(json.dumps({
        "analysis_targets": {"stocks": ["TEST"], "etfs": [], "generals": [], "benchmarks": []},
        "symbol_contracts": {"TEST": {"symbol": "TEST", "benchmark": "BENCH", "sector": None,
                                      "region": "US", "currency": "USD"}}}), encoding="utf-8")
    ts = "20260930T000000Z"
    for sym in ("TEST", "BENCH"):
        d = tmp / "data" / "technical" / sym
        d.mkdir(parents=True)
        (d / f"{sym}_technical_{ts}.json").write_text("{}", encoding="utf-8")
    for p, c in ((f"data/fear_greed/fear_greed_latest_{ts}.json", "{}"),
                 (f"data/news/GENERAL/GENERAL_news_{ts}.json", "[]"),
                 (f"data/news/TEST/TEST_news_{ts}.json", "[]"),
                 (f"data/economic/economic_US_{ts}.txt", "econ"),
                 (f"data/evidence_prep/TEST/TEST_peer_compare_{ts}.json",
                  json.dumps({"ok": True, "note": "mock peer compare", "trigger_signals": []})),
                 ("prompts/agent/manuals/understanding/P02-participation-and-flow.md", "# P02 mock manual")):
        f = tmp / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c, encoding="utf-8")
    return ts


# The mock ruler session (proposal §1.4 worked example; contracts C2 / C3 / C6 / C8), shared with scripts/test_v34_flow.py.
# Z: A .55→110 / B .30→100 / C .15→90; R: A .25→108 / B .35→98 / C .40→88. Z1 (guidance raised) settles for Z and is
# carried by R's `applies: "Z1#true"` update; R2 (multiple compression) is deadlocked with a complete closing record; the
# arbiter sets w = 0.5 → A has S_ref 58.9 and composite 59. A forged `applies` and a debater `cost_bps` are rejected at
# ingestion with retry. Wrong authored scores everywhere are overwritten and kept as authored_score.

def _node(nid, p, P_T, label, refs):
    return {"id": nid, "label": label, "p": p, "P_T": float(P_T), "refs": list(refs),
            "events": [{"t": 15, "kind": "close", "price": float(P_T)}]}


def ruler_sc(nodes, execution=None):
    import copy
    return {"schema": "3.4", "T_days": 15, "P0": 100.0, "nodes": copy.deepcopy(nodes),
            "execution": copy.deepcopy(execution if execution is not None else {})}


def ruler_claim(cid, text, kind="own", status="open", target=None, if_true=(), if_false=(), **extra):
    c = {"id": cid, "kind": kind, "claim": text, "evidence_tier": "T2", "status": status,
         "effect": {"if_true": list(if_true), "if_false": list(if_false)}}
    if target:
        c["target"] = target
    c.update(extra)
    return c


def ruler_final(score, claims, scenario, updates=(), status="final", req=None):
    """A debater round JSON (the authored composite_score is deliberately wrong: the script derives it)."""
    import copy
    r = role_record(score, claims[0]["id"], status=status)
    r["claims"] = copy.deepcopy(list(claims))
    r["scenario"] = copy.deepcopy(scenario)
    r["scenario_updates"] = copy.deepcopy(list(updates))
    r["mentality"] = "with_trend"
    r["request_evidence"] = req or []
    return r


def _apply_ops(sc, ops):
    import scenario_replay as sr
    return sr.apply_ops(sc, ops)


Z_EXEC = {"overweight": {"stop": {"level": 95.0, "semantics": "close"}, "conditional": []}}
R_EXEC = {"underweight": {"stop": {"level": 106.0, "semantics": "close"}, "conditional": []}}
Z14 = ruler_sc([_node("A", 0.55, 110, "beat and raise", ["Z1"]), _node("B", 0.30, 100, "flat", ["Z1"]),
                _node("C", 0.15, 90, "miss", ["R2"])], Z_EXEC)
R14 = ruler_sc([_node("A", 0.25, 108, "raise", ["Z1"]), _node("B", 0.35, 98, "drift lower", ["R2"]),
                _node("C", 0.40, 88, "multiple compression", ["R2"])], R_EXEC)
MOVE_Z1 = [{"op": "p_move", "from": "C", "to": "A", "amount": 0.10}]
R14_AFTER_Z1 = _apply_ops(R14, MOVE_Z1)
Z_C1 = ruler_claim("Z1", "guidance raised")
R_C1 = ruler_claim("R1", "guidance raise is already priced", kind="challenge", target="Z1", if_true=MOVE_Z1)
R_C2 = ruler_claim("R2", "multiple compression")
Z_FIX_SETTLED = dict(Z_C1, status="settled_for")
R_C1_DONE = dict(R_C1, status="withdrawn")
R_C2_DEAD = dict(R_C2, status="deadlocked")
UPD_Z1 = {"claim": "Z1", "applies": "Z1#true", "ops": MOVE_Z1, "why": "Z1 verified: guidance raised"}
UPD_R2 = {"claim": "R2", "applies": None, "ops": [], "why": "Z.EV1 does not distinguish the multiples", "refs": ["Z.EV1"]}
FORGED = [{"op": "p_move", "from": "C", "to": "A", "amount": 0.15}]
ARBITER = {
    "reasoning": {"key_drivers": [{"direction": "bullish", "category": "fundamental", "weight": "medium",
                                   "factor": "guidance", "evidence": ["Z1"]}], "risks": []},
    "asset_tier": "quality_large",
    "short_term_path": {"direction": "上行", "target": "110", "invalid": "95", "horizon_days": 5, "rationale": "r"},
    "mentality": "with_trend",
    "mentality_basis": "A at w 0.5 leans up on Z1 settled; stop 95 close",
    "claims_review": [{"claim": "Z1", "claim_rev": "R1.Z", "verdict": "adopted", "reason": "Z.EV1 confirms guidance"}],
    "mixture": {"w": 0.5, "reason": None},
    "risk_management": {"stop_loss": "95 close confirmation", "take_profit": "110", "max_drawdown_tolerance": "5%",
                        "review_triggers": ["below 95"]},
    "known_flaws": [{"flaw": "compression may continue", "disposition": "priced", "how": "R2 atoms", "refs": ["M1"]}],
    "next_review": {"recommend_after": "2026-10-03", "frequency_hint": "2-day",
                    "structured_triggers": [{"type": "price_below", "symbol": "TEST", "value": 95}], "notes": "n"},
    "residual_adjustment": {"basis": []},
    "statement": "one sentence", "debate_summary": "Z1 settled for Z; R2 deadlocked",
}
CLOSING_R2 = {"state": "deadlocked", "claim_rev": "R1.R", "remaining": "C p 0.15 vs 0.30",
              "tried": [{"evidence": "Z.EV1", "engaged_by": ["R2.R"]}],
              "next": "none: both routes exhausted; no source distinguishes the multiples"}
Z_SIGN_REV = {"predecessor": "R2.Z", "ops": [{"op": "p_move", "from": "A", "to": "B", "amount": 0.05}],
              "why": "the redraft's R2 reading persuades me a little"}
R_OBJ_REV = {"predecessor": "R2.R", "ops": [{"op": "p_move", "from": "C", "to": "B", "amount": 0.05}],
             "why": "after reading the draft: compression is milder than I filed"}


def ruler_script() -> dict:
    """The mock engine's canned answers for the ruler session (fresh copy per call: MockEngine pops them)."""
    return {
        "beat_zealot_r1_b1": [ruler_final(99, [Z_C1], Z14, status="need_more",
                                          req=[{"type": "search", "target_claim": "R2", "question": "Is the multiple compressing?",
                                                "would_change_my_mind": "w"}])],
        "beat_reaper_r1_b1": [ruler_final(1, [R_C1, R_C2], R14)],
        "scout_R1B1-1": [{"question": "Is the multiple compressing?", "simple": "Forward P/E flat at 24x over 3 months",
                          "detailed": "[conclusion] ...", "source": ["src"], "verdict": "refutes"}],
        "review_r1_b1": [{"stamps": [{"job_id": "R1B1-1", "status": "verified", "scope": "P/E level verified"}]}],
        "beat_zealot_r1_b2": [ruler_final(99, [Z_C1], Z14)],
        "notes_r1": [{"arbiter_notes": "no ruling this round", "private_note": "Z1 open; R2 open",
                      "focus": "Z1", "proactive_requests": [], "decision": "CONTINUE"}],
        # round 2: Z's first final carries cost_bps, R's first final forges `applies`; both are rejected and rewritten once
        "beat_zealot_r2_b1": [ruler_final(98, [Z_FIX_SETTLED], dict(Z14, execution=dict(Z_EXEC, cost_bps=10)))],
        "beat_reaper_r2_b1": [ruler_final(2, [R_C1_DONE, R_C2_DEAD], _apply_ops(R14, FORGED),
                                          [dict(UPD_Z1, ops=FORGED), UPD_R2])],
        "beat_zealot_r2_b1_fix": [ruler_final(98, [Z_FIX_SETTLED], Z14)],
        "beat_reaper_r2_b1_fix": [ruler_final(2, [R_C1_DONE, R_C2_DEAD], R14_AFTER_Z1, [UPD_Z1, UPD_R2])],
        "notes_r2": [{"arbiter_notes": "Z1 settled; R2 deadlocked", "private_note": "A p contested on C",
                      "focus": "(close)", "decision": "TERMINATE", "terminal_open": {"R2": CLOSING_R2},
                      "dispositions": []}],
        "provisional": [{"claims_review": ARBITER["claims_review"], "mixture": {"w": 0.5, "reason": None},
                         "rationale": "Z1 adopted; R2 deadlocked", "mentality": "with_trend", "composite_score": 97}],
        "proposed": [dict(ARBITER, composite_score=96)],
        "objection_zealot_1": [{"objections": [], "replies": [], "needs_new_evidence": []}],
        "objection_reaper_1": [{"objections": [], "replies": [], "needs_new_evidence": [], "scenario_revision": R_OBJ_REV}],
        "ruling": [dict(ARBITER, composite_score=95, objection_dispositions=[])],
        "signoff_zealot": [{"reservation": "z", "dissent_refs": ["Z1"], "endorsed_composite_score": 11,
                            "scenario_revision": Z_SIGN_REV}],
        "signoff_reaper": [{"reservation": "r", "dissent_refs": [], "endorsed_composite_score": 12}],
    }


def ruler_root(tmp: Path) -> str:
    """The mock root plus the live config/odds.yaml and an opening technical snapshot (price 100, ATR%14 = 8/√15 → s_s =
    0.08 = sigma_ref, as in the §1.4 worked example)."""
    ts = _mock_root(tmp)
    import yaml as _y
    live = _y.safe_load((ROOT / "config" / "odds.yaml").read_text(encoding="utf-8"))
    live["mean_shrink"] = 1.0   # the worked-example fixtures are the ruler without shrinkage; test_mean_shrink covers it
    (tmp / "config" / "odds.yaml").write_text(_y.safe_dump(live, sort_keys=False, allow_unicode=True), encoding="utf-8")
    snap = tmp / "data" / "technical" / "TEST" / f"TEST_technical_{ts}.json"
    snap.write_text(json.dumps({"TEST_current_snapshot_last_price": 100.0, "TEST_daily_atr_pct_14": 8 / 15 ** 0.5,
                                "TEST_latest_data_timestamp": "2026-09-30T00:00:00+00:00",
                                "TEST_session_context_current_session": "closed"}), encoding="utf-8")
    return ts


def run_ruler_session(tmp: Path, script: dict, zero_cost: bool = True, engine=None):
    """Open a ruler session in a mock root and drive it to the end; → (driver receipt, ts, captured route receipts).
    zero_cost=False keeps the snapshot's real cost_bps (the mock root's base + 5 fallback); §1.4 is stated without fees."""
    from unittest import mock
    ts = ruler_root(tmp)
    s = Session.open("TEST", tmp, engine="mock", model="mock-judge", tools=MockTools(tmp))
    if zero_cost:
        s.state["odds"]["cost_bps"] = 0.0
        s.save()
    captured = []
    orig = Session.route

    def spy(self, *a, **k):
        rc = orig(self, *a, **k)
        captured.append(rc)
        return rc
    with mock.patch.object(Session, "route", spy):
        drv = Driver("TEST", tmp, engine or MockEngine(script), "mock-judge", "mock-worker", tools=MockTools(tmp))
        rc = drv.run()
    return rc, ts, captured


def _self_test() -> None:
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="drv_test_"))
    cwd = os.getcwd()
    try:
        os.chdir(tmp)  # data_files 存在性检查按相对路径
        engine = MockEngine(ruler_script())
        rc, ts, routes = run_ruler_session(tmp, None, engine=engine)
        assert rc["ok"] and rc["rounds"] == 2, rc
        assert rc["gate"]["ok"], rc["gate"]
        assert sum(1 for r in routes if not r["ok"]) == 1, routes   # forged applies + debater cost_bps: one rejected route
        s = Session("TEST", ts, tmp)
        ids = s.store.ids()
        for want in ("R1.Z.B1", "R1.R", "Z.EV1", "R1.Z", "NOTES.R1", "PRIVATE.R1", "R2.Z", "R2.R", "NOTES.R2", "PROV",
                     "PROPOSED", "OBJ1.Z", "OBJ1.R", "RULING.v1", "SIGN1.Z", "SIGN1.R"):
            assert want in ids, f"missing {want}: {sorted(ids)}"
        assert s.store.verify()["ok"]
        a = json.loads((tmp / rc["analysis_path"]).read_text(encoding="utf-8"))
        assert a["composite_score"] == 59 and a["provisional_ruling"]["composite_score"] == 59, a["composite_score"]
        assert a["operation_type"] == "MARKET_ENTRY" and a["scenario"]["mixture"]["w"] == 0.5
        assert a["odds"]["schema"] == a["schema_version"] == s.schema()
        assert a["dissent"]["zealot_composite_score"] == 77 and a["dissent"]["reaper_composite_score"] == 42, a["dissent"]
        assert s.state.get("deadlocked") == ["R2"] and s.state["status"] == "done"
        assert a["signatures"] == {"zealot": "mock-judge", "reaper": "mock-judge", "arbiter": "mock-judge"}
        assert a["market_data_timestamp_utc"] == "2026-09-30T00:00:00Z"
        for m in sorted(s.store.dir.glob("_spawn_*.md")):  # Codex 不自动装载角色文件: 每份 spawn 材料的第一行必须是装载指令
            first = m.read_text(encoding="utf-8").splitlines()[0]
            assert first.startswith("ROLE FILE (first action): prompts/agent/") or \
                first.startswith("ROLE FILE (first action): .claude/agents/"), (m.name, first)
        zv = (s.store.dir / "_view_zealot.jsonl").read_text(encoding="utf-8")
        assert "PRIVATE" not in zv and "[ARB" not in zv   # 视图不含私记与评分公示
        # 幂等续跑: 已完成的场重跑零模型调用
        n_calls = len(engine.calls)
        rc2 = Driver("TEST", tmp, engine, "mock-judge", "mock-worker", tools=MockTools(tmp)).run()
        assert rc2["ok"] and len(engine.calls) == n_calls, rc2
        # 出厂闸独立可跑
        from run_step import gate
        g = gate(tmp / rc["analysis_path"], tmp)
        assert g["ok"], g
        assert "Done" in s.where()["next"], s.where()
        # 再定稿归档后崩溃的续跑: 不带 --input 重新过闸, 不铸新版, 不消耗重试机会
        for b in (s.store.debate_buf, s.store.evidence_buf, s.store.private_buf):
            c = b.with_name(b.name.replace(".jsonl", ".complete.jsonl"))
            if c.exists():
                c.rename(b)
        s3 = Session("TEST", ts, tmp, MockTools(tmp))
        s3.state["status"], s3.state["ruling_version"] = "finalizing", None
        s3.save()
        w3 = s3.where()
        assert "--stage ruling" in w3["next"] and "without --input" in w3["next"], w3
        rc3 = s3.finalize("ruling")
        assert rc3["ok"] and rc3["id"] == "RULING.v1" and "RULING.v2" not in s3.store.ids(), rc3
        assert s3.state["ruling_version"] == 1 and "signoff" in s3.where()["next"], s3.where()
        o3 = Session.open("TEST", tmp, engine="mock", tools=MockTools(tmp)).open_receipt()
        assert o3.get("resumed") and "spawn" not in o3, o3
        # a session opened before v3.4 (no odds snapshot) cannot be resumed
        s3.state.pop("odds")
        s3.state["status"] = "debating"
        try:
            s3.tally(1)
            raise AssertionError("a session without odds tallied")
        except StepError as exc:
            assert "before v3.4" in str(exc), exc
        print(json.dumps({"ok": True, "self_test": "passed", "calls": n_calls}, ensure_ascii=False))
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--judge-model", default="gpt-6-sol")
    ap.add_argument("--worker-model", default="gpt-6-luna")
    ap.add_argument("--call-timeout", type=int, default=2400)
    ap.add_argument("--max-rounds", type=int, default=None)
    ap.add_argument("--beat-ref", type=int, default=None)
    ap.add_argument("--pg-store", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    if not args.symbol:
        ap.error("symbol required (or --self-test)")
    caps = {k: v for k, v in (("max_rounds", args.max_rounds), ("beat_ref", args.beat_ref)) if v}
    try:
        engine = CodexEngine(ROOT, args.call_timeout)
        drv = Driver(args.symbol, ROOT, engine, args.judge_model, args.worker_model, args.call_timeout,
                     caps=caps, pg_store=args.pg_store)
        receipt = drv.run()
    except StepError as exc:
        print(json.dumps({"ok": False, "symbol": args.symbol, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(exc.code)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
