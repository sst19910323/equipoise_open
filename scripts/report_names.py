#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_names.py — 把旧形状的终报 JSON 改成新键名(2026-09-30 重建的改名)。

纯函数, 不碰数据库; `scripts/migrate_pg_names_2026_10.py` 用它改写 PG 里的历史报告。对照表见
`docs/handoff/FIELD-RENAMES-2026-10.md`。

规则:
- 只改键名与少数枚举值(证据结论、主张种类、申报类型、仲裁者的旧称呼), 不动内容。
- 编号的**值**不改(历史里的 TK1 仍是 TK1): 正文里到处引用它, 改了结构化字段就对不上正文了。
- 可重复执行: 已经是新名的报告再跑一遍, 结果不变。新旧两个键同时存在时保留新键的值。
- 不认识的键原样保留。更早年代的形状(带 final_report 包装等)不处理, 原样返回并说明原因。

用法:
    python scripts/report_names.py <报告.json>            # 打印会改哪些键(不写文件)
    python scripts/report_names.py <报告.json> --write    # 原地改写
"""
from __future__ import annotations

import io
import json
import re
import sys
from typing import Any, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERDICT = {"supported": "supports", "refuted": "refutes", "verified": "supports"}
CLAIM_KIND = {"pillar": "own", "boost": "reinforce"}
LEGACY_ARBITER = {"Q-PARENT", "PARENT", "ARBITER"}
_ID_LIKE = re.compile(r"^(?:[A-Z]\.)?TK\d+(?:\.EV\d+)?$")


class _Log(list):
    def mv(self, d: Any, old: str, new: str, where: str) -> None:
        """改键名。新键已在就丢掉旧键(新键为准)。"""
        if not isinstance(d, dict) or old not in d:
            return
        v = d.pop(old)
        if new not in d:
            d[new] = v
        self.append(f"{where}{old} → {new}")

    def val(self, d: Any, key: str, mapping: dict, where: str) -> None:
        if isinstance(d, dict) and isinstance(d.get(key), str) and d[key] in mapping:
            self.append(f"{where}{key}: {d[key]} → {mapping[d[key]]}")
            d[key] = mapping[d[key]]


def _each(v: Any):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _request(log: _Log, rq: Any, where: str) -> None:
    """外呼申报(辩论轮的 request_evidence[] 项, 或模型调用台账里存的 request)。"""
    if not isinstance(rq, dict):
        return
    log.val(rq, "type", {"task": "model_call"}, where)
    log.mv(rq, "playbook", "model", where)


def _score_keys(log: _Log, d: Any, where: str) -> None:
    log.mv(d, "action", "composite_score", where)
    log.mv(d, "operation_volume", "operation_size", where)
    log.mv(d, "short_term", "short_term_path", where)


def _round_content(log: _Log, c: dict, where: str) -> None:
    _score_keys(log, c, where)
    log.mv(c, "preliminary_mentality", "mentality", where)
    for m in _each(c.get("mentality")):
        log.mv(m, "probability", "weight", where + "mentality[].")
    log.mv(c, "pillars", "claims", where)
    for p in _each(c.get("claims")):
        log.mv(p, "tier", "evidence_tier", where + "claims[].")
        log.val(p, "kind", CLAIM_KIND, where + "claims[].")
    sl = c.get("stance_ledger")
    if isinstance(sl, dict):
        for part in ("moves", "held"):
            for m in _each(sl.get(part)):
                log.mv(m, "tier", "evidence_tier", f"{where}stance_ledger.{part}[].")
    for rq in _each(c.get("request_evidence")):
        _request(log, rq, where + "request_evidence[].")


def _arbiter_name(log: _Log, d: dict, where: str) -> None:
    """发起方的旧称呼 → Arbiter。历史数据里 requested_by 有时是列表(多方共同发起), 逐项处理。"""
    v = d.get("requested_by")
    if isinstance(v, str) and v in LEGACY_ARBITER:
        d["requested_by"] = "Arbiter"
        log.append(f"{where}requested_by: {v} → Arbiter")
    elif isinstance(v, list) and any(isinstance(x, str) and x in LEGACY_ARBITER for x in v):
        d["requested_by"] = ["Arbiter" if (isinstance(x, str) and x in LEGACY_ARBITER) else x for x in v]
        log.append(f"{where}requested_by[]: 旧称呼 → Arbiter")


def _evidence_item(log: _Log, it: dict) -> None:
    w = "evidence[]."
    if "task" in it and "question" not in it:
        v = it.get("task")
        if isinstance(v, str) and _ID_LIKE.match(v.strip()):
            # 旧的模型调用条目里 task 放的是执行者自报的编号, 不是问题; 问题在 target 里。
            it["question"] = it.get("target") or ""
            log.append(f"{w}task 是编号, 保留; question 取自 target")
        else:
            log.mv(it, "task", "question", w)
    log.mv(it, "task_id", "model_call_id", w)
    log.mv(it, "playbook", "model", w)
    log.mv(it, "legs", "steps", w)
    log.mv(it, "tier_eligible", "suggested_evidence_tier", w)
    log.val(it, "verdict", VERDICT, w)
    _arbiter_name(log, it, w)
    _request(log, it.get("request"), w + "request.")


def upgrade_report(doc: Any) -> Tuple[Any, list, str]:
    """返回 (报告, 改动清单, 跳过原因)。改动清单为空 = 没有可改的; 跳过原因非空 = 这种形状不处理。原地修改。"""
    log = _Log()
    if not isinstance(doc, dict):
        return doc, log, "不是 JSON 对象"
    if "final_report" in doc:
        return doc, log, "带 final_report 包装的更早形状"
    if "action" not in doc and "composite_score" not in doc:
        return doc, log, "顶层既无 action 也无 composite_score"

    _score_keys(log, doc, "")
    for m in _each(doc.get("mentality")):
        log.mv(m, "probability", "weight", "mentality[].")
    log.mv(doc, "organized_z", "ledger_score_z", "")
    log.mv(doc, "organized_r", "ledger_score_r", "")
    log.mv(doc.get("residual_adjustment"), "delta_vs_organized", "delta_vs_ledger", "residual_adjustment.")

    log.mv(doc, "conviction_budget", "deviation_support", "")
    ds = doc.get("deviation_support")
    if isinstance(ds, dict):
        log.mv(ds, "total_claim", "total_points", "deviation_support.")
        for leg in _each(ds.get("legs")):
            log.mv(leg, "leg", "basis", "deviation_support.legs[].")
            log.mv(leg, "claim", "points", "deviation_support.legs[].")

    log.mv(doc, "arbiter_shadow_log", "arbiter_private_scores", "")
    for row in _each(doc.get("arbiter_private_scores")):
        _score_keys(log, row, "arbiter_private_scores[].")
    _score_keys(log, doc.get("provisional_ruling"), "provisional_ruling.")

    dis = doc.get("dissent")
    log.mv(dis, "zealot_final_action", "zealot_composite_score", "dissent.")
    log.mv(dis, "reaper_final_action", "reaper_composite_score", "dissent.")

    log.mv(doc, "tasks", "model_calls", "")
    for t in _each(doc.get("model_calls")):
        w = "model_calls[]."
        log.mv(t, "task_id", "model_call_id", w)
        log.mv(t, "playbook", "model", w)
        log.mv(t, "tier_eligible", "suggested_evidence_tier", w)
        _arbiter_name(log, t, w)
        _request(log, t.get("request"), w + "request.")

    for it in _each(doc.get("evidence")):
        _evidence_item(log, it)

    for e in _each(doc.get("debate_history")):
        c = e.get("content")
        as_text = isinstance(c, str)
        if as_text:
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if not isinstance(c, dict):
            continue
        before = len(log)
        _round_content(log, c, "debate_history[].content.")
        if as_text and len(log) > before:
            e["content"] = json.dumps(c, ensure_ascii=False)   # 原来是字符串就写回字符串
    return doc, sorted(set(log)), ""


def main(argv: list) -> int:
    write = "--write" in argv
    args = [a for a in argv if a != "--write"]
    if len(args) != 1:
        print("用法: python scripts/report_names.py <报告.json> [--write]", file=sys.stderr)
        return 1
    with io.open(args[0], encoding="utf-8") as fh:
        doc = json.load(fh)
    doc, changes, skipped = upgrade_report(doc)
    if skipped:
        print(f"跳过: {skipped}")
        return 0
    print(f"{len(changes)} 类改动" + (":" if changes else "(已经是新名)"))
    for c in changes:
        print("  " + c)
    if write and changes:
        with io.open(args[0], "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        print("已写回")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
