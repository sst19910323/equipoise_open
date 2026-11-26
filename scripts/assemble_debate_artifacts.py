#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mechanically assemble final Equipoise debate artifacts from audited JSONL buffers.

The arbiter still authors every judgment-bearing field in ``draft.json``.  This
helper only copies the bulky audit fields from their canonical buffers, checks
their shapes, and atomically writes Analysis/Evidence JSON.  It deliberately
does not repair or reinterpret debate content.

2026-09-07 protocol catch-up (R19.1 left this script behind and strict GPT-6
children rightly halted on the mismatch):
- ``--private-buffer``: arbiter private-score lines live in the dedicated third buffer
  ``_inflight_private_<TS>.jsonl`` (batch 3 name; ``--shadow-buffer`` still accepted for
  the old ``_inflight_shadow_<TS>.jsonl``); when given, ``arbiter_private_scores`` is read
  from it (legacy two-buffer runs keep the evidence-buffer fallback).
- ``--meta`` (batch 3): a JSON file of mechanical metadata the orchestration layer knows
  and the arbiter never writes (symbol / market_data_timestamp_utc / report_generated_at /
  data_files / signatures / engine_family / runner / topology / harness_truth / dissent /
  objections / ruling_version ...). Injected verbatim on top of the draft.
- ``evidence_added`` and ``search_rounds_used`` are derived mechanically from the evidence
  buffer rows (index of every archived call + the count of rounds that fired a search);
  a draft-supplied value is overridden.
- 2026-09-30 (rebuild batch 2) field names: model_call_id / model /
  suggested_evidence_tier / question / composite_score / arbiter_private_scores /
  model_calls / ledger_score_z, ledger_score_r. Legacy buffer keys (task_id /
  playbook / tier_eligible / task / action / arbiter_shadow) are still read and
  mapped forward until the object store is renamed in batch 3.
- duplicate task_id rows resolve mechanically here (most complete content wins,
  superseded interim stubs named in the receipt) — an accidental double-archive
  is NOT a poisoned buffer and never a reason to abort a run.
- ``model_calls`` ledger: one record per model-call row in the evidence buffer,
  mapped verbatim (request = the debater's type:"model_call" declaration,
  summary = simple, detail = detailed, evidence_refs = that row's own EV
  position); the arbiter never hand-copies these arrays.
- ``ledger_score_z`` / ``ledger_score_r`` are injected from ledger_tally (machine
  numbers; the arbiter never writes them).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger_tally import tally as _ledger_tally  # noqa: E402


# 对象仓旧键 → 新键(批 3 改仓前的过渡映射; 新键优先)
_ROW_KEY_MAP = {"task_id": "model_call_id", "playbook": "model", "tier_eligible": "suggested_evidence_tier"}
_LEGACY_OF = {v: k for k, v in _ROW_KEY_MAP.items()}


def _row_get(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    legacy = _LEGACY_OF.get(key)
    return row.get(legacy) if legacy else None


class AssemblyError(ValueError):
    pass


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"cannot read JSON {path}: {exc}") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AssemblyError(f"cannot read JSONL {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssemblyError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise AssemblyError(f"JSONL {path}:{line_no} must contain an object")
        rows.append(row)
    if not rows:
        raise AssemblyError(f"JSONL buffer is empty: {path}")
    return rows


def _history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = []
    for idx, row in enumerate(rows, 1):
        if row.get("role") not in {"Zealot", "Reaper"}:
            continue
        content = row.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise AssemblyError(f"debate row {idx} content is not JSON: {exc}") from exc
        if not row.get("round") or not isinstance(content, dict):
            raise AssemblyError(f"debate row {idx} lacks round/role/content object")
        history.append({"round": row["round"], "role": row["role"], "content": content})
    if not history:
        raise AssemblyError("debate buffer has no Zealot/Reaper history")
    return history


def _source_list(value: Any, fallback: str) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return [fallback]


def _evidence_item(row: dict[str, Any], idx: int, ruler_era: bool = False) -> dict[str, Any] | None:
    if any(key in row for key in ("arbiter_notes", "arbiter_shadow", "arbiter_private_score", "provisional_ruling")):
        return None
    wrapped = row.get("content")
    item = dict(wrapped) if isinstance(wrapped, dict) else dict(row)
    if not any(item.get(key) for key in ("question", "task", "target", "simple", "finding", "detailed", "payload_text")):
        return None

    question = item.get("question") or item.get("task") or item.get("target") or row.get("request") or f"evidence-{idx}"
    if isinstance(question, (dict, list)):
        question = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
    simple = item.get("simple") or item.get("finding") or item.get("verdict")
    detailed = item.get("detailed")
    if not detailed:
        detail_payload = {key: item[key] for key in ("finding", "steps", "legs", "verdict") if key in item}
        detailed = json.dumps(detail_payload, ensure_ascii=False, separators=(",", ":"))
    if not isinstance(question, str) or not question.strip() or not isinstance(simple, str) or not simple.strip():
        raise AssemblyError(f"evidence row {idx} lacks question/simple (or target/finding)")
    if not isinstance(detailed, str) or not detailed.strip():
        raise AssemblyError(f"evidence row {idx} lacks detailed evidence")

    model = _row_get(row, "model")
    kind = item.get("kind") or ("data" if item.get("mode") or model else "search")
    fallback = f"data:model:{model}" if model else "archived:evidence-buffer"
    result = dict(item)
    result.pop("task", None)
    result.update({
        "question": question,
        "simple": simple,
        "detailed": detailed,
        "source": _source_list(item.get("source"), fallback),
        "kind": kind,
    })
    if kind == "data" and not isinstance(result.get("payload_text"), str):
        result["payload_text"] = json.dumps(wrapped if isinstance(wrapped, dict) else row,
                                             ensure_ascii=False, separators=(",", ":"))
    for key in ("round", "requested_by", "status", "verified_scope", "issue"):
        if key in row and key not in result:
            result[key] = row[key]
    for key in ("model_call_id", "model", "suggested_evidence_tier"):
        val = _row_get(row, key)
        if val is not None and key not in result:
            result[key] = val
    if ruler_era:   # 3.4 release gate (C5): closure records are rebuilt from the store id, the owner and the filing
        if row.get("evidence_id") and "evidence_id" not in result:
            result["evidence_id"] = row["evidence_id"]
        if isinstance(row.get("request"), dict) and "request" not in result:
            result["request"] = row["request"]
    return result


def _resolve_duplicate_calls(evidence_rows: list[dict[str, Any]]) -> tuple[set[int], list[str]]:
    """Duplicate model_call_id rows: keep the most complete record, supersede the rest.

    Append-only log semantics (2026-09-10, BRK.B TK5 case): a parent may slip and
    archive an interim verdict stub before the full task record. The buffer is
    immutable, so the resolution happens here mechanically — among rows sharing a
    task_id, the one with the longest serialized ``content`` wins (tie: the later
    line); superseded lines are excluded from evidence/tasks derivation and named
    in the receipt. Nothing is rewritten in the buffer itself."""
    by_id: dict[str, list[int]] = {}
    for idx, row in enumerate(evidence_rows, 1):
        tid = _row_get(row, "model_call_id")
        if tid:
            by_id.setdefault(str(tid), []).append(idx)
    skip: set[int] = set()
    warnings: list[str] = []
    for tid, idxs in by_id.items():
        if len(idxs) < 2:
            continue
        def _completeness(i: int) -> tuple[int, int]:
            row = evidence_rows[i - 1]
            try:
                size = len(json.dumps(row.get("content"), ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                size = 0
            return (size, i)
        keep = max(idxs, key=_completeness)
        dropped = sorted(i for i in idxs if i != keep)
        skip.update(dropped)
        warnings.append(f"duplicate model_call_id {tid}: kept line {keep}, superseded interim line(s) {dropped}")
    return skip, warnings


def _audit_fields(rows: list[dict[str, Any]],
                  private_rows: list[dict[str, Any]] | None = None,
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    arbiter_log = []
    provisional = None
    for row in rows:
        if "arbiter_notes" in row:
            notes = row["arbiter_notes"]
            if isinstance(notes, dict):
                # Preserve an already archived nested arbiter record verbatim.
                if set(notes) != {"round", "tally", "notes"} or not isinstance(notes["notes"], str):
                    raise AssemblyError("unsupported nested arbiter_notes record")
                if "round" in row or "tally" in row:
                    raise AssemblyError("ambiguous nested arbiter_notes record")
                arbiter_log.append(dict(notes))
            else:
                entry = {
                    "round": row.get("round"),
                    "tally": row.get("tally", ""),
                    "notes": notes,
                }
                for key in ("terminal_open", "dispositions"):   # closing records (C5); absent in pre-ruler rows
                    if key in row:
                        entry[key] = row[key]
                arbiter_log.append(entry)
        if isinstance(row.get("provisional_ruling"), dict):
            provisional = dict(row["provisional_ruling"])
    # private-score lines live in the dedicated third buffer (visibility wall =
    # file boundary); legacy two-buffer runs still carry them in the evidence buffer.
    shadow_source = "private buffer" if private_rows is not None else "evidence buffer"
    shadow_log = []
    for row in (private_rows if private_rows is not None else rows):
        rec = row.get("arbiter_private_score")
        if not isinstance(rec, dict):
            rec = row.get("arbiter_shadow")
        if isinstance(rec, dict):
            shadow_log.append(_forward_score(dict(rec)))
        elif private_rows is not None and row.get("round") is not None \
                and ("composite_score" in row or "action" in row):
            shadow_log.append(_forward_score(dict(row)))  # bare line without the wrapper key
    if not arbiter_log:
        raise AssemblyError("evidence buffer has no arbiter_notes records")
    if not shadow_log:
        raise AssemblyError(f"{shadow_source} has no arbiter private-score records")
    if provisional is None:
        raise AssemblyError("evidence buffer has no provisional_ruling record")
    return arbiter_log, shadow_log, _forward_score(provisional)


def _forward_score(rec: dict[str, Any]) -> dict[str, Any]:
    """Legacy `action` -> `composite_score` (verbatim otherwise)."""
    if "composite_score" not in rec and "action" in rec:
        rec["composite_score"] = rec.pop("action")
    return rec


def _model_call_record(row: dict[str, Any], item: dict[str, Any], ev_ref: int) -> dict[str, Any]:
    """Map one model-call row from the evidence buffer to the report `model_calls` ledger shape.

    request = the debater's original type:"model_call" declaration (task_request wins:
    some rows also carry the machine-leg param dict under `request`);
    summary/detail come from ``item`` — the same converted record `_evidence_item`
    produced (content unwrapped, detailed synthesized from finding/legs/verdict),
    so a content-wrapped runner payload (DAX 2026-09-23 case) fills the ledger
    identically to the evidence array; routing keys stay on the raw ``row``;
    evidence_refs = this row's own 1-based position in the assembled evidence
    array (the runner payload is archived there), same EV shape as PG."""
    request = None
    for key in ("model_call_request", "task_request", "request"):
        cand = row.get(key)
        if isinstance(cand, dict) and cand.get("type") in ("model_call", "task"):
            request = cand
            break
    if request is None:
        cand = row.get("model_call_request") or row.get("task_request") or row.get("request")
        request = cand if isinstance(cand, dict) else {"question": item.get("question")}
    return {
        "model_call_id": _row_get(row, "model_call_id"),
        "model": _row_get(row, "model"),
        "issue": row.get("issue"),
        "requested_by": row.get("requested_by"),
        "round": row.get("round"),
        "status": row.get("status"),
        "suggested_evidence_tier": (_row_get(row, "suggested_evidence_tier")
                                    or item.get("suggested_evidence_tier") or item.get("tier_eligible")),
        "request": request,
        "summary": item.get("simple") or "",
        "detail": item.get("detailed") or "",
        "evidence_refs": [f"EV{ev_ref}"] if ev_ref else [],
    }


def _as_of_of(row: dict[str, Any], fallback: str) -> str:
    for key in ("ts", "as_of", "generated_at"):
        v = row.get(key)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return fallback


def _evidence_index(evidence: list[dict[str, Any]], fallback_date: str) -> list[dict[str, Any]]:
    """evidence_added = mechanical index of every archived call (frontend contract:
    item / as_of / status(answered|blocked) / requested_by / source)."""
    out = []
    for item in evidence:
        blocked = item.get("status") in ("not_found", "failed") or item.get("verdict") == "not_found"
        src = item.get("source") or []
        out.append({
            "item": (item.get("simple") or item.get("question") or "")[:300],
            "as_of": _as_of_of(item, fallback_date),
            "status": "blocked" if blocked else "answered",
            "requested_by": item.get("requested_by") or "",
            "source": ", ".join(str(x) for x in src[:3]) if src else "archived",
        })
    return out


def _search_rounds_used(evidence: list[dict[str, Any]]) -> int:
    rounds = {str(e.get("round")) for e in evidence
              if e.get("kind") == "search" and e.get("round") is not None}
    return len(rounds)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


_ENTRY_AT_MARKET_TOL_PCT = 0.01  # 只吸收显示舍入(0.01%, 如 745.6 对 745.63): "填现价"是把开场快照价抄进字段; 不是噪音带, 不是给辩手的规则。


def _core_lang() -> str:
    """原文语言, 只认 config/i18n.yaml 的 core_lang; 读不到按 en(2026-10-26 起核心英文)。译文不在这里, 在 PG translations 表。"""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "i18n.yaml").read_text(encoding="utf-8")) or {}
        return str(cfg.get("core_lang") or "en")
    except Exception:
        return "en"


def _derive_execution(report: dict) -> None:
    """2026-10-17 晚(库主定: 市价 / 限价不是辩手的选项, 由价位派生): 仲裁者写 operation_type=ENTRY, 这里按 operation_target 与
    reference_prices 派生终报记录的 MARKET_ENTRY / LIMIT_ENTRY——下游 PG / 复盘 / 对照册沿用旧枚举, 存储不改。
    单标的: 一个数, 等于参考价(舍入级容差) → MARKET_ENTRY, 否则 LIMIT_ENTRY(等那个价, 不论远近); 区间 → LIMIT_ENTRY。
    大盘场: 每个镜头各判, 全部镜头都有参考价且都等于参考价才算市价; 有镜头缺参考价时——其余镜头已有一个不等 → LIMIT_ENTRY, 否则判不了, 保留 ENTRY。
    参考价 = 开场登记的技术快照价(run_step._reference_prices), 不是终裁时点的盘中价。派生依据记入 operation_execution。"""
    import re as _re
    op = str(report.get("operation_type") or "").upper()
    if op != "ENTRY":
        return
    refs: dict[str, float] = {}
    for r in report.get("reference_prices") or []:
        if isinstance(r, dict) and r.get("symbol") and isinstance(r.get("price"), (int, float)) \
                and not isinstance(r.get("price"), bool) and r["price"] > 0:
            refs[str(r["symbol"]).upper()] = float(r["price"])
    tgt = str(report.get("operation_target") or "").strip()
    sym = str(report.get("symbol") or "").upper()
    pairs: list[tuple[str, str]] = []
    if "=" in tgt:
        for part in tgt.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs.append((k.strip().upper(), v.strip()))
    else:
        pairs.append((sym, tgt))
    basis, at_market, missing = [], True, False
    for s, v in pairs:
        ref = refs.get(s)
        if ref is None:
            missing = True
            basis.append(f"{s}: {v} (no reference price)")
            continue
        m = _re.fullmatch(r"\d+(?:\.\d+)?", v)
        if not m:
            at_market = False
            basis.append(f"{s}: {v} range → wait for level")
            continue
        dev = abs(float(m.group(0)) - ref) / ref * 100
        at_market = at_market and dev <= _ENTRY_AT_MARKET_TOL_PCT
        basis.append(f"{s}: {v} vs reference {ref:g} ({dev:.2f}%)")
    # 3.4: a derived conditional contract (overweight_on_condition) stays on operation_execution next to the derivation basis
    prev = report.get("operation_execution")
    keep = {k: prev[k] for k in ("conditional", "stop") if k in prev} if isinstance(prev, dict) and "conditional" in prev else {}
    if missing and (at_market or not pairs):
        report["operation_execution"] = {**keep, "declared": "ENTRY", "derived": None, "basis": "; ".join(basis) + " → undeterminable, ENTRY kept"}
        return
    derived = "MARKET_ENTRY" if at_market else "LIMIT_ENTRY"
    report["operation_type"] = derived
    report["operation_execution"] = {**keep, "declared": "ENTRY", "derived": derived, "reference": "opening_snapshot",
                                     "tolerance_pct": _ENTRY_AT_MARKET_TOL_PCT, "basis": "; ".join(basis)}


def _derive_execution_lenswise(report: dict, lenses: list) -> None:
    """C11.5 (lens-wise report): _derive_execution per lens on `by_lens[L]` — that lens's level against that lens's
    reference price, so `by_lens[L].operation_type` becomes MARKET_ENTRY / LIMIT_ENTRY where it is ENTRY (the derived
    contract kept next to the derivation basis, as for one symbol); the top-level operation_type / operation_execution
    follow the primary lens (lenses[0]). A lens missing from by_lens is left to the release gate (FATAL there)."""
    import copy
    from lens_views import fmt_level
    by =report.get("by_lens") if isinstance(report.get("by_lens"), dict) else {}
    for L in lenses:
        b = by.get(L)
        if not isinstance(b, dict):
            continue
        try:
            level = fmt_level(b.get("operation_target"))
        except (TypeError, ValueError):
            level = str(b.get("operation_target"))
        contract = b.get("operation_execution")
        # G3: L's frozen snapshot reference price (odds.by_lens[L].reference_price), the one the release gate re-derives
        # the family from; the report's reference_prices only when the snapshot has none
        o_L = ((report.get("odds") or {}).get("by_lens") or {}).get(L) if isinstance(report.get("odds"), dict) else None
        snap_px = o_L.get("reference_price") if isinstance(o_L, dict) else None
        refs = ([{"symbol": L, "price": snap_px}] if isinstance(snap_px, (int, float)) and not isinstance(snap_px, bool)
                else report.get("reference_prices"))
        sub = {"symbol": L, "operation_type": b.get("operation_type"), "operation_target": level,
               "reference_prices": refs, "operation_execution": contract}
        _derive_execution(sub)
        b["operation_type"] = sub["operation_type"]
        ex = sub.get("operation_execution")
        if isinstance(contract, dict) and isinstance(ex, dict) and ex is not contract:
            ex = {**contract, **ex}
        if ex is not None:
            b["operation_execution"] = ex
    prim = by.get(lenses[0]) if lenses else None
    if isinstance(prim, dict):
        report["operation_type"] = prim.get("operation_type")
        if "operation_execution" in prim:
            report["operation_execution"] = copy.deepcopy(prim["operation_execution"])
        else:
            report.pop("operation_execution", None)


def _carry_store_seq(report: dict, store_seq: dict[str, int]) -> None:
    """3.4 (C5 at release): every closure record keeps its store manifest position — debate_history entries (with their
    store id), evidence items, model_calls, arbiter_log entries — so the release gate rebuilds archive order and compares
    the session's spawns (store positions) like for like. An object the store does not know gets no seq (gate fails closed)."""
    from closure_check import round_no
    for e in report.get("debate_history") or []:
        tag = {"Zealot": "Z", "Reaper": "R"}.get(e.get("role"))
        if tag:
            rid = f"R{round_no(e.get('round'))}.{tag}"
            if rid in store_seq:
                e["id"], e["seq"] = rid, store_seq[rid]
    for item in list(report.get("evidence") or []) + list(report.get("model_calls") or []):
        oid = item.get("model_call_id") or item.get("evidence_id")
        if oid in store_seq:
            item["seq"] = store_seq[oid]
    for a in report.get("arbiter_log") or []:
        nid = f"NOTES.R{round_no(a.get('round'))}"
        if nid in store_seq:
            a["seq"] = store_seq[nid]


def _derive_odds(report: dict, history: list, cfg: dict | None) -> None:
    """3.4 (C6 / C8): `scenario` is A as derived at the ruling (with `mixture` and `derived`); nothing is mapped from
    operation_type. Re-derive from A and the stored candidates; any difference goes to scenario.derived.fatal
    ("recompute mismatch: <field>"), which the release gate raises. s_ref_z / s_ref_r come from the endorsed scenarios."""
    from odds_model import config_from_snapshot, validate_scenario, score
    from ruling import recompute_check
    if cfg is None:   # 3.4 rulers come from the session snapshot, never from config/odds.yaml (C4)
        snap = report.get("odds") if isinstance(report.get("odds"), dict) else {}
        cfg = config_from_snapshot(snap)
    sc = report.get("scenario")
    if isinstance(sc, dict):
        d = sc.get("derived")
        if not isinstance(d, dict):
            sc["derived"] = {"ok": False, "fatal": ["scenario.derived missing (3.4: derived at the ruling stage)"]}
        else:
            atr = report.get("atr_pct_14")
            if isinstance(d.get("score"), dict):
                report["s_ref"] = d["score"]["S_ref"]   # before the recompute: the report's alias is checked with the rest
            bad = recompute_check(sc, cfg, report, atr if isinstance(atr, (int, float)) else None)
            if bad:
                d["fatal"] = list(d.get("fatal") or []) + bad
    report["schema_version"] = str(cfg.get("schema"))   # the session ruler's schema
    dissent = report.get("dissent") if isinstance(report.get("dissent"), dict) else {}
    for role, field in (("Zealot", "s_ref_z"), ("Reaper", "s_ref_r")):
        last = dissent.get(f"{role.lower()}_endorsed_scenario")
        if not isinstance(last, dict):
            for e in history:
                c = e.get("content")
                if e.get("role") == role and isinstance(c, dict) and isinstance(c.get("scenario"), dict):
                    last = c["scenario"]
        if isinstance(last, dict):
            f, _w, n = validate_scenario(last, cfg)
            if not f:
                report[field] = score(n)["S_ref"]


def _derive_odds_lenswise(report: dict, history: list, lenses: list) -> None:
    """C11.5 / C11.6 (lens-wise report): _derive_odds per lens. For each lens L the report is projected on L
    (lens_views.project_report) and `recompute_check(..., summary=False)` re-derives A_L under L's ruler
    (config_from_snapshot(lens_snapshot(odds, L))); any difference goes to `by_lens[L].scenario.derived.fatal`, which the
    release gate raises with the lens named. Per lens: `s_ref` from A_L, `s_ref_z` / `s_ref_r` from that side's endorsed
    scenario on L (else its last final's scenario on L). Top level: `s_ref`, `s_ref_z`, `s_ref_r` are the default-weighted
    (lens_weights_default, never λ) summaries of the per-lens values — anything authored there is overwritten, or
    dropped when a lens has no value; `scenario` is the primary lens's A (copied after the per-lens derivation)."""
    import copy
    import lens_views as lv
    from odds_model import config_from_snapshot, validate_scenario, score
    from ruling import recompute_check
    snap = report.get("odds") if isinstance(report.get("odds"), dict) else {}
    report["schema_version"] = str(config_from_snapshot(snap).get("schema"))
    by = report.get("by_lens") if isinstance(report.get("by_lens"), dict) else {}
    o_by = snap.get("by_lens") if isinstance(snap.get("by_lens"), dict) else {}
    dissent = report.get("dissent") if isinstance(report.get("dissent"), dict) else {}
    for L in lenses:
        b = by.get(L)
        if not isinstance(b, dict):
            continue   # the gate names the missing lens
        for field in ("s_ref", "s_ref_z", "s_ref_r"):
            b.pop(field, None)
        sc = b.get("scenario")
        if not isinstance(sc, dict):
            continue
        d = sc.get("derived")
        if not isinstance(d, dict):
            sc["derived"] = {"ok": False, "fatal": ["scenario.derived missing (3.4: derived at the ruling stage)"]}
            continue
        try:
            cfg_L = config_from_snapshot(lv.lens_snapshot(snap, L))
        except lv.LensError as exc:
            d["fatal"] = list(d.get("fatal") or []) + [str(exc)]
            continue
        if isinstance(d.get("score"), dict):
            b["s_ref"] = d["score"]["S_ref"]   # before the recompute: the lens's alias is checked with the rest
        try:
            pdoc = lv.project_report(report, L, lenses)
        except lv.LensError as exc:
            d["fatal"] = list(d.get("fatal") or []) + [str(exc)]
            continue
        atr = (o_by.get(L) or {}).get("atr_pct_14") if isinstance(o_by.get(L), dict) else None
        bad = recompute_check(pdoc["scenario"], cfg_L, pdoc, atr if isinstance(atr, (int, float)) else None,
                              summary=False)
        if bad:
            d["fatal"] = list(d.get("fatal") or []) + bad
        for role, field in (("Zealot", "s_ref_z"), ("Reaper", "s_ref_r")):
            last = (dissent.get(f"{role.lower()}_endorsed_scenario_by_lens") or {}).get(L) \
                if isinstance(dissent.get(f"{role.lower()}_endorsed_scenario_by_lens"), dict) else None
            if not isinstance(last, dict):
                for e in history:
                    c = e.get("content")
                    sbl = c.get("scenario_by_lens") if isinstance(c, dict) else None
                    if e.get("role") == role and isinstance(sbl, dict) and isinstance(sbl.get(L), dict):
                        last = sbl[L]
            if isinstance(last, dict):
                f, _w, n = validate_scenario(last, cfg_L)
                if not f:
                    b[field] = score(n)["S_ref"]
    default = report.get("lens_weights_default") if isinstance(report.get("lens_weights_default"), dict) else {}
    for field in ("s_ref", "s_ref_z", "s_ref_r"):
        vals = [(default.get(L), (by.get(L) or {}).get(field)) for L in lenses if isinstance(by.get(L), dict)]
        if len(vals) == len(lenses) and all(isinstance(w, (int, float)) and isinstance(v, (int, float)) for w, v in vals):
            report[field] = round(sum(w * v for w, v in vals), 6)
        else:
            report.pop(field, None)
    prim = by.get(lenses[0]) if lenses else None
    if isinstance(prim, dict) and isinstance(prim.get("scenario"), dict):
        report["scenario"] = copy.deepcopy(prim["scenario"])


def assemble(draft_path: Path, debate_path: Path, evidence_buffer_path: Path,
             analysis_path: Path, evidence_path: Path,
             private_buffer_path: Path | None = None, meta_path: Path | None = None,
             shadow_buffer_path: Path | None = None, odds_cfg: dict | None = None,
             store_seq: dict[str, int] | None = None) -> dict[str, Any]:
    report = _load_json(draft_path)
    if not isinstance(report, dict):
        raise AssemblyError("draft must be a JSON object")
    private_buffer_path = private_buffer_path or shadow_buffer_path
    meta = _load_json(meta_path) if meta_path else {}
    if not isinstance(meta, dict):
        raise AssemblyError("meta must be a JSON object")
    debate_rows = _load_jsonl(debate_path)
    evidence_rows = _load_jsonl(evidence_buffer_path)
    private_rows = _load_jsonl(private_buffer_path) if private_buffer_path else None
    history = _history(debate_rows)
    _sc = report.get("scenario")
    # a ruler (v3.4) report: the session snapshot travels in meta, or the draft carries `scenario`; neither = pre-ruler
    ruler_era = bool(isinstance(meta.get("odds"), dict) and meta.get("odds")) or isinstance(_sc, dict)
    dup_skip, dup_warnings = _resolve_duplicate_calls(evidence_rows)
    evidence, model_calls = [], []
    for idx, row in enumerate(evidence_rows, 1):
        if idx in dup_skip:
            continue
        item = _evidence_item(row, idx, ruler_era=ruler_era)
        if item is None:
            continue
        evidence.append(item)
        if _row_get(row, "model_call_id"):
            model_calls.append(_model_call_record(row, item, len(evidence)))
    if not evidence:
        raise AssemblyError("evidence buffer has no evidence records")
    arbiter_log, shadow_log, provisional = _audit_fields(evidence_rows, private_rows)
    ledger = _ledger_tally(history)

    report.update(meta)  # 机械元数据(编排层全知, 仲裁者不写): 覆盖 draft 里同名字段
    report["lang"] = _core_lang()   # 2026-10-24: 报告自带原文语言(config/i18n.yaml core_lang); 没有这个键的旧报告 = 中文时代
    from lens_views import lenses_of
    lenses = lenses_of(report) if ruler_era and isinstance(report.get("by_lens"), dict) else []   # C11.5 lens-wise
    contract = report.get("operation_execution") if ruler_era else None
    if lenses:
        _derive_execution_lenswise(report, lenses)   # per lens on by_lens[L]; the top level follows the primary lens
    else:
        _derive_execution(report)   # ENTRY → MARKET_ENTRY / LIMIT_ENTRY, 按价位与本场参考价派生
    if not lenses and isinstance(contract, dict) and isinstance(report.get("operation_execution"), dict) \
            and report["operation_execution"] is not contract:
        report["operation_execution"] = {**contract, **report["operation_execution"]}   # 3.4: keep the derived contract
    gen_at = str(report.get("evidence_generated_at") or report.get("report_generated_at") or "")[:10]
    report["evidence_added"] = _evidence_index(evidence, gen_at)
    report["search_rounds_used"] = _search_rounds_used(evidence)
    report["debate_history"] = history
    report["evidence"] = evidence
    report["model_calls"] = model_calls
    report["arbiter_log"] = arbiter_log
    report["arbiter_private_scores"] = shadow_log
    # 2026-11-03 执行卡: 合约的确定性投影(kind / now / trigger / stop / valid / one_line), 每镜头一张; 未让步主张的展示副本
    # (要在合约合并与 debate_history 落位之后算)。顶层枚举不动; 接口对老报告读时现算同一函数。
    from workflows.execution_card import ensure_display_fields
    ensure_display_fields(report, source="assembler")
    report["provisional_ruling"] = provisional
    if store_seq:
        _carry_store_seq(report, store_seq)
    if ruler_era and lenses:   # C11.5 / C11.6: every lens re-derived; top-level summaries default-weighted
        _derive_odds_lenswise(report, history, lenses)
    elif ruler_era:   # schema_version, s_ref, s_ref_z / s_ref_r derived; no stake ledger
        _derive_odds(report, history, odds_cfg)
    else:     # pre-ruler (v3.3 and earlier): the stake ledger's machine scores
        for side_key, field in (("zealot", "ledger_score_z"), ("reaper", "ledger_score_r")):
            side = ledger.get(side_key) or {}
            if side.get("ledger_score") is not None:
                report[field] = side["ledger_score"]
    _atomic_json(evidence_path, evidence)
    _atomic_json(analysis_path, report)
    return {
        "ok": True,
        "analysis_path": str(analysis_path.relative_to(ROOT) if analysis_path.is_relative_to(ROOT) else analysis_path),
        "evidence_path": str(evidence_path.relative_to(ROOT) if evidence_path.is_relative_to(ROOT) else evidence_path),
        "history_records": len(history),
        "evidence_records": len(evidence),
        "model_call_records": len(model_calls),
        "model_call_dedup": dup_warnings,
        "arbiter_records": len(arbiter_log),
        "private_score_records": len(shadow_log),
        "private_score_source": "private-buffer" if private_rows is not None else "evidence-buffer",
        "meta_keys": sorted(meta),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--debate-buffer", required=True, type=Path)
    parser.add_argument("--evidence-buffer", required=True, type=Path)
    parser.add_argument("--private-buffer", type=Path, default=None,
                        help="dedicated arbiter private-score buffer (_inflight_private_<TS>.jsonl); "
                             "omit only for legacy two-buffer runs.")
    parser.add_argument("--shadow-buffer", type=Path, default=None,
                        help="legacy alias of --private-buffer (_inflight_shadow_<TS>.jsonl).")
    parser.add_argument("--meta", type=Path, default=None,
                        help="mechanical metadata JSON injected verbatim (data_files / signatures / dissent / ...).")
    parser.add_argument("--analysis-out", required=True, type=Path)
    parser.add_argument("--evidence-out", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = assemble(args.draft, args.debate_buffer, args.evidence_buffer,
                           args.analysis_out, args.evidence_out,
                           private_buffer_path=args.private_buffer or args.shadow_buffer,
                           meta_path=args.meta)
    except (AssemblyError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
