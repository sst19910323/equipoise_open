#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_debate.py — 终报出厂校验闸。

写盘后、入库前跑一遍: 结构化契约逐条机检, FATAL 未过 → exit 1, 仲裁者自修一次再验;
二次仍失败 → 本场停机报告用户。编排层写库前再跑同一条闸(两引擎同一条管线)。

用法: python scripts/validate_debate.py data/debate/<SYM>/<SYM>_Analysis_<TS>.json
输出: {"ok":bool, "fatal":[...], "warnings":[...]}; ok=false 时 exit 1。
也可 import: validate(doc) -> (fatal, warnings)。

字段名(重建批 2, 2026-09-30): composite_score / claims[kind, evidence_tier, stake, status] /
mentality[name, weight] / operation_size / short_term_path / ledger_score_z·r / deviation_support[legs[refs,
points], total_points] / arbiter_private_scores / dissent.<side>_composite_score / model_calls[model_call_id,
suggested_evidence_tier]。旧名不再接受。

FATAL(必须修): claims 缺失·状态非法·缺档位 / 收场留 open / 结算幅度超过押分 / move 无 issue /
ledger_score_z·r 缺失或与账本算术不符 / arbiter_log 缺失或空 / round·role 命名违约 / composite_score 越界 /
provisional_ruling 缺失或残缺 / dissent 签章占位或综合评分非整数 / operation_type 非法 / mentality 缺失·拼写非法 / 支持闸 / mentality_basis 缺失 /
deviation_support 缺失(|score-50|≥5) / known_flaws 缺失(AGGRESSIVE·LIQUIDATE) / arbiter_private_scores 缺失 /
asset_tier 非法 / claims_review 缺失·verdict 非法 / 中间带缺 midband_rationale / 震荡无两沿或无 invalid。
WARN(不拦): 终裁落点在两个整理分之外 / 自报末轮与算术漂移 / 终裁≠任一整理分但无 residual_adjustment /
已结算存活的主张在终裁里无着落 / final≠provisional 但无 audit 条目 / 血统闸 / 等待类无结构化触发 /
偏离支撑败牌领分·重复领分·腿无引证 / disposition 非法 / 路径判断与操作反向 / 审计位移与路径方向反向。
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台防线
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger_tally import load_entries, tally  # noqa: E402

STATUS_ENUM = {"open", "settled_for", "settled_against", "conceded",
               "deadlocked", "withdrawn", "uncontested"}
KIND_ENUM = {"own", "challenge", "reinforce"}
TIER_ENUM = {"T1", "T2", "T3", "T4"}

# ENTRY = 辩手与仲裁者写的入场(2026-10-17 晚, 库主定: 市价 / 限价不是选项); 终报里由组装器按价位派生成 MARKET_ENTRY / LIMIT_ENTRY,
# 所以三个名字都合法; 心态合法性按 _norm_op 归一后查。
OP_ENUM = {"ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY", "HOLD_NEUTRAL", "TRIM_POSITION", "LIQUIDATE_NOW"}
_TRADE_OPS = {"ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY", "TRIM_POSITION", "LIQUIDATE_NOW"}


DECLARED_OPS = {"ENTRY", "HOLD_NEUTRAL", "TRIM_POSITION", "LIQUIDATE_NOW"}      # 格式册里辩手与仲裁者可写的枚举
_DECLARED_TRADE_OPS = {"ENTRY", "TRIM_POSITION", "LIQUIDATE_NOW"}


def is_ruler(doc: dict, res: dict | None = None) -> bool:
    """A ruler report (ruler_era.is_ruler_report), or a tally that ran the ruler branch (carries deadlock_requested).
    Reports with none of these (v3.3 and earlier) take the pre-ruler checks."""
    from ruler_era import is_ruler_report
    if is_ruler_report(doc):
        return True
    return any("deadlock_requested" in ((res or {}).get(k) or {}) for k in ("zealot", "reaper"))


def _arbiter_named(doc: dict) -> set:
    """3.4: claims the arbiter ruled on — the archived NOTES dispositions (arbiter_log) and the final claims_review; a
    self-settled claim closes only when the opponent conceded or the arbiter named it (ledger_tally)."""
    from ledger_tally import arbiter_named
    cr = doc.get("claims_review") if isinstance(doc.get("claims_review"), list) else []
    return arbiter_named(list(doc.get("arbiter_log") or []), cr)


def closure(doc: dict, res: dict) -> tuple[list, list]:
    """3.4 release gate for open claims (C5 / C9): rebuild records from the report, take the closing records from the
    archived NOTES (arbiter_log; later rounds override earlier ones, so a claim closed in a CONTINUE round keeps its
    record), run closure_check.check. Report-level `deadlocked` / `unresolved` are not read.
    An open claim without a passing record is FATAL, except at the round cap (arbiter_log rounds == caps.max_rounds),
    where the script recorded it as unresolved (WARN)."""
    import closure_check as cc
    fatal, warn = [], []
    try:
        records = cc.records_from_report(doc)
    except cc.ClosureInputError as exc:
        return [f"closing records: {exc}"], warn
    try:
        spawns = cc.spawns_from_report(doc, records)
    except cc.ClosureInputError as exc:   # fail closed: FATAL, and no record has access to anything
        fatal.append(f"closing records: {exc}")
        spawns = {}
    alog = [a for a in (doc.get("arbiter_log") or []) if isinstance(a, dict)]
    terminal: dict = {}
    for a in alog:
        t = a.get("terminal_open")
        if isinstance(t, dict):
            terminal.update(t)
        elif t:
            fatal.append(f"closing record: terminal_open in {a.get('round')} is not an object of closing records")
    cap = (doc.get("caps") or {}).get("max_rounds") if isinstance(doc.get("caps"), dict) else None
    out = cc.check(terminal, res, records, spawns, None, cap, len(alog))
    fatal += [f"closing record: {f}" for f in out["fatal"]]
    if out["unresolved"]:
        warn.append(f"Round cap {cap} forced the close; unresolved claims kept: {', '.join(out['unresolved'])} "
                    f"(neither consensus nor concession)")
    else:
        for cid in out["open"]:
            fatal.append(f"open claim {cid} at close has no passing closing record: {out['reasons'].get(cid)}")
    return fatal, warn


def _score_of(sc, cfg=None) -> int | None:
    from ruling import derived_score
    return derived_score(sc, cfg) if isinstance(sc, dict) else None


def _doc_cfg(doc: dict) -> dict | None:
    """The session ruler the report was derived with: `config_from_snapshot(doc.odds)`; None when the report carries no
    snapshot (a 3.4 report without one is FATAL in checks — never a fallback to config/odds.yaml)."""
    import odds_model as om
    odds = doc.get("odds")
    return om.config_from_snapshot(odds) if isinstance(odds, dict) and odds else None


def _candidates(doc: dict, sc: dict) -> list | None:
    """Rebuild A's execution candidates from the history's last-round execution per side + voided.candidates."""
    import odds_model as om
    from closure_check import round_no
    last = {}
    for e in doc.get("debate_history") or []:
        c = e.get("content") if isinstance(e, dict) else None
        tag = {"Zealot": "Z", "Reaper": "R"}.get(e.get("role")) if isinstance(e, dict) else None
        if tag and isinstance(c, dict) and isinstance(c.get("scenario"), dict):
            last[tag] = (round_no(e.get("round")), c["scenario"].get("execution") or {}, c["scenario"])
    if set(last) != {"Z", "R"}:
        return None
    A = {k: v for k, v in sc.items() if k != "derived"}
    voided = ((doc.get("voided") or {}) if isinstance(doc.get("voided"), dict) else {}).get("candidates") or []
    return om.build_candidates(last["Z"][1], last["R"][1], A, voided, rounds={t: last[t][0] for t in last}, warn=[],
                               filers={t: last[t][2] for t in last}, atr_abs=om.atr_abs_of({"unit_basis": {"atr_pct_14": doc.get("atr_pct_14")}} if doc.get("atr_pct_14") else _doc_cfg(doc), A.get("P0")))


def _finals(doc: dict) -> list | None:
    """debate_history → scenario_replay records {seq, id, role: Z|R, round, content} in store order; None when an entry
    carries no store seq (the order cannot be rebuilt)."""
    from closure_check import round_no
    tag_of = {"Zealot": "Z", "Reaper": "R"}
    finals = []
    for e in doc.get("debate_history") or []:
        tag = tag_of.get(e.get("role")) if isinstance(e, dict) else None
        if tag and isinstance(e.get("content"), dict):
            if not isinstance(e.get("seq"), int):
                return None
            finals.append({"seq": e["seq"], "id": e.get("id"), "role": tag, "round": round_no(e.get("round")),
                           "content": e["content"]})
    return sorted(finals, key=lambda f: f["seq"])


def _replay(doc: dict, sc: dict, cfg: dict, applied_out: dict | None = None) -> list:
    """C6.2 at release: rebuild Z′ / R′ from `debate_history` through ruling.replay_primes (the arbiter stages' glue:
    frozen opening + accepted updates + the registered effects of the claims settled under the report's `claims_review` /
    `voided.effects`, + the objection-stage revisions in filing order when the ruling folded them in), check
    `claims_review` against the registry, and compare with `scenario.mixture.z_prime` / `r_prime`. → fatal list.
    `applied_out` (lens-wise, C11.6): the applied effects are added to it per side and the mixture rule is left to the
    caller, which runs it once on the union over the lenses (as derive_ruling_lenswise does)."""
    import scenario_replay as sr
    import ruling as rlg
    mix = sc.get("mixture") if isinstance(sc.get("mixture"), dict) else {}
    finals = _finals(doc)
    if finals is None:
        return ["replay: debate_history entry without store seq (cannot rebuild Z′ / R′)"]
    cr = doc.get("claims_review") if isinstance(doc.get("claims_review"), list) else []
    fatal = list(rlg.claims_review_errors(finals, cr))
    v_eff = rlg.norm_voided_effects(doc.get("voided"))
    revisions = None
    if ((sc.get("derived") or {}).get("replay") or {}).get("revisions_included"):
        revisions = {}
        objs = [o for o in doc.get("objections") or [] if isinstance(o, dict) and isinstance(o.get("scenario_revision"), dict)]
        for o in sorted(objs, key=lambda o: int(o.get("round") or 0)):   # one objection per side per round: filing order
            revisions.setdefault(rlg.TAG.get(o.get("by")), []).append((o.get("id"), o["scenario_revision"]))
    tol = float(cfg.get("p_tolerance", sr.P_TOL))
    try:
        primes, applied, f = rlg.replay_primes(finals, cr, v_eff, revisions, tol)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return fatal + [f"replay failed: {exc}"]
    if f:
        return fatal + [f"replay {x}" for x in f]
    # the live ruling's mixture checks (weight, referenced reason, endpoint discards), the same function
    if applied_out is None:
        fatal += rlg.mixture_errors(mix, finals, applied, rlg.evidence_ids_from_report(doc))
    else:
        for t in ("Z", "R"):
            applied_out.setdefault(t, []).extend(applied.get(t) or [])
    for tag, key in (("Z", "z_prime"), ("R", "r_prime")):
        if isinstance(mix.get(key), dict) and sr.canonical(primes[tag]) != sr.canonical(mix[key]):
            fatal.append(f"scenario.mixture.{key} is not reproduced by replaying debate_history under claims_review / voided (C6.2)")
    return fatal


def checks(doc: dict, pre_signoff: bool = False) -> tuple[list, list]:
    """C9 release checks for 3.4. A lens-wise report (C11.6: it carries `by_lens`) goes to checks_lenswise; every other
    ruler report (single-lens symbols, the single-lens ruler GENERAL reports) takes _checks_one unchanged."""
    if isinstance(doc, dict) and "by_lens" in doc:
        return checks_lenswise(doc, pre_signoff)
    return _checks_one(doc, pre_signoff)


def _checks_one(doc: dict, pre_signoff: bool = False, lens: str | None = None,
                _applied: dict | None = None) -> tuple[list, list]:
    """C9 release checks for 3.4: A recomputable from `scenario.mixture` and inside the envelope; every stored
    composite_score equals its derivation under the report's ruler (history finals, provisional_ruling, final, dissent);
    the scenarios those derive from must be present; no execution.cost_bps; no baseline; the stored candidates equal the
    set rebuilt from both sides' last-round contracts (ids and provenance).
    `lens` (C11.6): `doc` is one lens's projection (lens_views.project_report); its operation_target is that lens's
    number (recompute_check summary=False) and the mixture rule is collected into `_applied` for checks_lenswise."""
    import scenario_replay as sr
    fatal, warn = [], []
    cfg = _doc_cfg(doc)
    if cfg is None:
        return ["3.4 report without odds snapshot (the report must carry the session's ruler, C4)"], warn
    sc = doc.get("scenario") if isinstance(doc.get("scenario"), dict) else {}
    mix = sc.get("mixture")
    if not isinstance(mix, dict) or not isinstance(mix.get("z_prime"), dict) or not isinstance(mix.get("r_prime"), dict):
        fatal.append("scenario.mixture {w, z_prime, r_prime} missing (3.4: A must be recomputable)")
    else:
        try:
            again = sr.mixture(mix["z_prime"], mix["r_prime"], mix.get("w"), mix.get("reason"))
            if sr.canonical({"nodes": again["nodes"], "P0": again["P0"], "T_days": again["T_days"]}) !=                     sr.canonical({"nodes": sc.get("nodes"), "P0": sc.get("P0"), "T_days": sc.get("T_days")}):
                fatal.append("scenario is not reproduced by scenario.mixture (w·Z′ + (1−w)·R′)")
        except (TypeError, ValueError, KeyError) as exc:
            fatal.append(f"scenario.mixture cannot be recomputed: {exc}")
        fatal += [f"scenario.mixture envelope: {f}" for f in sr.envelope_check(sc, mix["z_prime"], mix["r_prime"])]
        fatal += _replay(doc, sc, cfg, _applied if lens is not None else None)
    if "baseline" in sc or "baseline" in doc:
        fatal.append("baseline is not part of 3.4")
    stored = (sc.get("derived") or {}).get("candidates")
    for c in stored or []:
        if isinstance(c, dict) and "provenance" not in c:
            fatal.append(f"candidate {c.get('id')} has no provenance")
    rebuilt = _candidates(doc, sc)

    def key(cs):   # G3: the full contract (stop, level, observe, fill, provenance, voided), in id order
        return json.dumps(sorted((c for c in cs or [] if isinstance(c, dict)), key=lambda c: str(c.get("id"))),
                          sort_keys=True, ensure_ascii=False)
    if not isinstance(stored, list):
        fatal.append("scenario.derived.candidates missing (3.4)")
    elif rebuilt is None:
        fatal.append("candidates cannot be rebuilt: a side has no scenario in debate_history")
    elif key(rebuilt) != key(stored):
        fatal.append("scenario.derived.candidates differ from the set rebuilt from both sides' last-round execution "
                     "+ voided.candidates (the full contract: id, provenance, stop, level, observe, fill)")
    if rebuilt is not None and isinstance(sc.get("nodes"), list):
        # C9: re-derive the strategy, operation_* and cost from A, the rebuilt candidates and the report's ruler (the same
        # recompute the assembler runs, here on the rebuilt set): an edited derived field or ruler does not survive the gate
        from ruling import recompute_check
        atr = doc.get("atr_pct_14")
        atr = atr if isinstance(atr, (int, float)) and not isinstance(atr, bool) else None
        fatal += [f"scenario.derived (C9 re-derivation): {x}"
                  for x in recompute_check(sc, cfg, doc, atr, candidates=rebuilt, summary=lens is None)]
    fa = doc.get("composite_score")
    want = _score_of(sc, cfg)
    if fa != want:
        fatal.append(f"composite_score {fa!r} ≠ its derivation round(S_ref(A)) = {want!r}")
    prov = doc.get("provisional_ruling") if isinstance(doc.get("provisional_ruling"), dict) else {}
    if not isinstance(prov.get("scenario"), dict):
        fatal.append("provisional_ruling.scenario missing (3.4: the provisional composite_score derives from it)")
    elif prov.get("composite_score") != _score_of(prov.get("scenario"), cfg):
        fatal.append(f"provisional_ruling.composite_score {prov.get('composite_score')!r} ≠ its derivation "
                     f"{_score_of(prov.get('scenario'), cfg)!r}")
    for i, e in enumerate(doc.get("debate_history") or []):
        c = e.get("content") if isinstance(e, dict) else None
        if not isinstance(c, dict):
            continue
        if not isinstance(c.get("scenario"), dict):
            fatal.append(f"history[{i}]: scenario missing (3.4: every final emits its scenario)")
            continue
        csc = c["scenario"]
        if c.get("composite_score") != _score_of(csc, cfg):
            fatal.append(f"history[{i}]: composite_score {c.get('composite_score')!r} ≠ its derivation {_score_of(csc, cfg)!r}")
        if "cost_bps" in (csc.get("execution") or {}):
            fatal.append(f"history[{i}]: scenario.execution.cost_bps present (3.4: cost comes from the session snapshot)")
        if "baseline" in csc:
            fatal.append(f"history[{i}]: scenario.baseline present (not part of 3.4)")
    dd = doc.get("dissent") if isinstance(doc.get("dissent"), dict) else {}
    fatal += _side_s_ref_errors(doc, dd, cfg)
    for side in ("zealot", "reaper"):
        es = dd.get(f"{side}_endorsed_scenario")
        if not isinstance(es, dict):
            if not pre_signoff:
                fatal.append(f"dissent.{side}_endorsed_scenario missing (3.4: the dissent score derives from it)")
            continue
        if dd.get(f"{side}_composite_score") != _score_of(es, cfg):
            fatal.append(f"dissent.{side}_composite_score {dd.get(f'{side}_composite_score')!r} ≠ its derivation "
                         f"round(S_ref(endorsed)) = {_score_of(es, cfg)!r}")
    return fatal, warn


def _side_s_ref_errors(doc: dict, dissent: dict, cfg: dict) -> list:
    """G3: s_ref_z / s_ref_r rebuilt from dissent.<side>_endorsed_scenario (else that side's last final's scenario), as
    the assembler derives them; on a lens projection these are the lens's own values (by_lens[L].s_ref_z / s_ref_r)."""
    import odds_model as om
    out = []
    for side, role, k in (("zealot", "Zealot", "s_ref_z"), ("reaper", "Reaper", "s_ref_r")):
        sc = dissent.get(f"{side}_endorsed_scenario")
        if not isinstance(sc, dict):
            sc = None
            for e in doc.get("debate_history") or []:
                c = e.get("content") if isinstance(e, dict) else None
                if e.get("role") == role and isinstance(c, dict) and isinstance(c.get("scenario"), dict):
                    sc = c["scenario"]
        want = None
        if isinstance(sc, dict):
            f, _w, n = om.validate_scenario(sc, cfg)
            want = None if f else om.score(n)["S_ref"]
        got = doc.get(k)
        if want is None:
            if got is not None:
                out.append(f"{k} {got!r} has no endorsed / last-final scenario to derive from")
        elif not om._is_num(got) or abs(float(got) - float(want)) > 1e-6:
            out.append(f"{k} {got!r} ≠ S_ref(the {side} endorsed scenario) = {want!r}")
    return out


def _raw_lens_errors(doc: dict, lenses: list) -> list:
    """G3 (C11.2 / C11.4 at release): the raw history's lens tags and the applies-on-every-lens rule, re-verified before
    any projection (a projection silently drops an op tagged with a lens outside the session)."""
    import lens_views as lv
    from validate_role_record import _lens_shape_errors, applies_lens_errors
    out = []
    for i, e in enumerate(doc.get("debate_history") or []):
        c = e.get("content") if isinstance(e, dict) else None
        if isinstance(c, dict) and e.get("role") in ("Zealot", "Reaper"):
            out += [f"history[{i}] ({e.get('id')}): {x}" for x in _lens_shape_errors(c, lenses)]
    finals = _finals(doc)
    if finals:
        for k, f in enumerate(finals):
            out += [f"history {f.get('id')}: {x}" for x in applies_lens_errors(f["content"], f["round"], f["role"], finals[:k])]
    for o in doc.get("objections") or []:
        rev = o.get("scenario_revision") if isinstance(o, dict) else None
        if isinstance(rev, dict) and isinstance(rev.get("ops"), list):
            out += lv.ops_lens_errors(f"objection {o.get('id')}: scenario_revision.ops", rev["ops"], lenses)
    return out


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def checks_lenswise(doc: dict, pre_signoff: bool = False) -> tuple[list, list]:
    """C11.6 release checks for a lens-wise report (one scenario, one replay, one operation per lens; v3.4.1).
    (1) The lens set is the frozen one: `lenses` (the assembler copies `_meta.lenses`) = the `by_lens` keys = the
        `odds.by_lens` keys, in that order; `lens_weights_default` = `odds.lens_weights_default`. Never read from config.
    (2) Per lens L the single-lens checks (_checks_one) run on `lens_views.project_report(doc, L)` — Z′_L / R′_L rebuilt
        from debate_history, A_L, candidates, operation, by_lens[L].composite_score and the history / provisional /
        dissent scores on L recomputed under L's ruler — every message prefixed `lens L: `; the mixture rule runs once on
        the union of the applied effects (derive_ruling_lenswise does the same).
    (3) λ: lens_weights re-checked by lens_views.lens_weights_errors (anchor + reason); composite_score =
        weighted_score(by_lens composites, λ); provisional_ruling likewise with its own λ; dissent.<side>_composite_score
        = weighted_score(<side>_composite_by_lens, default); top-level s_ref / s_ref_z / s_ref_r = the default-weighted
        summaries of the per-lens values.
    (4) operation_target = join_targets(by_lens, lenses); scenario / operation_type / operation_size /
        operation_execution equal by_lens[lenses[0]]'s."""
    import lens_views as lv
    import ruling as rlg
    fatal, warn = [], []
    by = doc.get("by_lens")
    if not isinstance(by, dict):
        return ["by_lens is not an object {lens: {...}} (C11.5)"], warn
    lenses = lv.lenses_of(doc)
    if not lenses:
        return ["lens-wise report (by_lens) without the frozen session lenses (`lenses`, two or more; C11.6)"], warn
    odds = doc.get("odds") if isinstance(doc.get("odds"), dict) else {}
    o_by = odds.get("by_lens") if isinstance(odds.get("by_lens"), dict) else {}
    for L in lenses:
        if not isinstance(by.get(L), dict):
            fatal.append(f"lens {L}: by_lens missing this lens (a lens is never dropped; C11.6)")
        if not isinstance(o_by.get(L), dict):
            fatal.append(f"lens {L}: odds.by_lens missing this lens (C11.1)")
    p_by = (doc.get("provisional_ruling") or {}).get("by_lens") if isinstance(doc.get("provisional_ruling"), dict) else None
    keysets = [("by_lens", list(by)), ("odds.by_lens", list(o_by))]
    if isinstance(p_by, dict):   # the provisional ruling's lens set obeys the same rule as the final's
        keysets.append(("provisional_ruling.by_lens", list(p_by)))
        for L in lenses:
            if not isinstance(p_by.get(L), dict):
                fatal.append(f"lens {L}: provisional_ruling.by_lens missing this lens (a lens is never dropped; C11.6)")
    for name, keys in keysets:
        extra = [k for k in keys if k not in lenses]
        if extra:
            fatal.append(f"{name} carries lenses outside the session lenses {lenses}: {extra}")
        elif keys != lenses and set(keys) == set(lenses):
            fatal.append(f"{name} order {keys} differs from the session lenses {lenses} (the frozen config order)")
    default = doc.get("lens_weights_default")
    if not isinstance(default, dict) or list(default) != lenses:
        fatal.append(f"lens_weights_default must name every session lens in order {lenses} ({default!r})")
    elif _canon(default) != _canon(odds.get("lens_weights_default")):
        fatal.append("lens_weights_default differs from odds.lens_weights_default (frozen at open; C11.1)")
    if fatal:
        return fatal, warn

    raw = _raw_lens_errors(doc, lenses)
    if raw:
        return raw, warn
    for L in lenses:   # G6: the observe-only flag (an index lens) is the frozen snapshot's, never edited
        if bool(by[L].get("observe_only")) != bool(o_by[L].get("observe_only")):
            fatal.append(f"lens {L}: by_lens.observe_only {by[L].get('observe_only')!r} differs from "
                         f"odds.by_lens.{L}.observe_only {o_by[L].get('observe_only')!r} (frozen at open)")

    # (2) every lens through the single-lens checks
    applied: dict = {"Z": [], "R": []}
    for L in lenses:
        pre = f"lens {L}: "
        bsc = by[L].get("scenario")
        d = (bsc.get("derived") or {}) if isinstance(bsc, dict) else {}
        fatal += [f"{pre}scenario: {x}" for x in d.get("fatal") or []]
        warn += [f"{pre}scenario: {x}" for x in d.get("warn") or []]
        try:
            pdoc = lv.project_report(doc, L, lenses)
        except lv.LensError as exc:
            fatal.append(str(exc))
            continue
        f, w = _checks_one(pdoc, pre_signoff, lens=L, _applied=applied)
        fatal += [x if x.startswith(pre) else pre + x for x in f]
        warn += [x if x.startswith(pre) else pre + x for x in w]
    finals = _finals(doc)
    mix = (doc.get("scenario") or {}).get("mixture") if isinstance(doc.get("scenario"), dict) else None
    if finals is not None and isinstance(mix, dict):
        fatal += rlg.mixture_errors(mix, finals, applied, rlg.evidence_ids_from_report(doc))

    # (3) λ and the summary scores
    claim_ids = {str(c["id"]) for e in doc.get("debate_history") or [] if isinstance(e, dict)
                 for c in (((e.get("content") or {}) if isinstance(e.get("content"), dict) else {}).get("claims") or [])
                 if isinstance(c, dict) and c.get("id")}
    ids = claim_ids | set(rlg.evidence_ids_from_report(doc))

    def lam_of(rec: dict, where: str):
        errs, lam = lv.lens_weights_errors(rec.get("lens_weights"), default, lenses, rec.get("lens_weights_reason"), ids)
        fatal.extend(f"{where}{x}" for x in errs)
        return None if errs else lam

    def per_lens(m: dict, key: str):
        vals = {L: (m.get(L) or {}).get(key) if isinstance(m.get(L), dict) else None for L in lenses}
        return vals if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals.values()) else None

    if not isinstance(doc.get("lens_weights"), dict):
        fatal.append("lens_weights missing (a lens-wise report always records the λ used; C11.5)")
    lam = lam_of(doc, "")
    comp = per_lens(by, "composite_score")
    if lam is not None and comp is not None:
        want = lv.weighted_score(comp, lam)
        if doc.get("composite_score") != want:
            fatal.append(f"composite_score {doc.get('composite_score')!r} ≠ round(Σ lens_weights · by_lens composite_score) "
                         f"= {want!r} (C11.5)")
    prov = doc.get("provisional_ruling") if isinstance(doc.get("provisional_ruling"), dict) else {}
    if not isinstance(prov.get("by_lens"), dict):
        fatal.append("provisional_ruling.by_lens missing (a lens-wise session's provisional ruling is derived per lens)")
    else:
        p_lam = lam_of(prov, "provisional_ruling.")
        p_comp = per_lens(prov["by_lens"], "composite_score")
        if p_comp is None:
            fatal.append("provisional_ruling.by_lens: a lens has no composite_score")
        elif p_lam is not None and prov.get("composite_score") != lv.weighted_score(p_comp, p_lam):
            fatal.append(f"provisional_ruling.composite_score {prov.get('composite_score')!r} ≠ round(Σ lens_weights · "
                         f"by_lens composite_score) = {lv.weighted_score(p_comp, p_lam)!r}")
    dd = doc.get("dissent") if isinstance(doc.get("dissent"), dict) else {}
    for side in ("zealot", "reaper"):
        m = dd.get(f"{side}_composite_by_lens")
        if not isinstance(m, dict):
            if dd and not pre_signoff:
                fatal.append(f"dissent.{side}_composite_by_lens missing (the per-lens dissent scores; C11.5)")
            continue
        vals = {L: m.get(L) for L in lenses}
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals.values()):
            fatal.append(f"dissent.{side}_composite_by_lens must carry an integer for every lens {lenses}")
        elif dd.get(f"{side}_composite_score") != lv.weighted_score(vals, default):
            fatal.append(f"dissent.{side}_composite_score {dd.get(f'{side}_composite_score')!r} ≠ round(Σ "
                         f"lens_weights_default · {side}_composite_by_lens) = {lv.weighted_score(vals, default)!r}")
    for k in ("s_ref", "s_ref_z", "s_ref_r"):
        vals = per_lens(by, k)
        if vals is None:
            if k == "s_ref" or doc.get(k) is not None:
                fatal.append(f"{k}: by_lens carries no {k} for every lens (the top-level {k} is their default-weighted summary)")
            continue
        want = sum(default[L] * vals[L] for L in lenses)
        got = doc.get(k)
        if not isinstance(got, (int, float)) or isinstance(got, bool) or abs(float(got) - want) > 1e-6:
            fatal.append(f"{k} {got!r} ≠ the default-weighted summary of by_lens {k} = {want!r} (C11.5)")

    # one mixture for the session (C11.5): every lens's A is mixed with the primary lens's w and reason
    P = lenses[0]

    def _mix(L):
        sc_ = by[L].get("scenario") if isinstance(by[L].get("scenario"), dict) else {}
        m_ = sc_.get("mixture") if isinstance(sc_.get("mixture"), dict) else {}
        return {"w": m_.get("w"), "reason": m_.get("reason")}
    for L in lenses[1:]:
        if _canon(_mix(L)) != _canon(_mix(P)):
            fatal.append(f"lens {L}: scenario.mixture w / reason {_mix(L)!r} differs from the session's one mixture "
                         f"(by_lens.{P}: {_mix(P)!r}; C11.5: one w for every lens)")
    # debater finals: per-lens scores present for every lens, summary = their default-weighted score (C11.3)
    for i, e in enumerate(doc.get("debate_history") or []):
        c = e.get("content") if isinstance(e, dict) else None
        if not isinstance(c, dict):
            continue
        cbl = c.get("composite_by_lens")
        if not isinstance(cbl, dict):
            fatal.append(f"history[{i}]: composite_by_lens missing (a lens-wise final scores every lens)")
            continue
        miss = [L for L in lenses if not isinstance(cbl.get(L), int) or isinstance(cbl.get(L), bool)]
        if miss:
            fatal.append(f"history[{i}]: composite_by_lens has no integer score for lens {', '.join(miss)}")
            continue
        want = lv.weighted_score({L: cbl[L] for L in lenses}, default)
        if c.get("composite_score") != want:
            fatal.append(f"history[{i}]: composite_score {c.get('composite_score')!r} ≠ round(Σ lens_weights_default · "
                         f"composite_by_lens) = {want!r}")

    # (4) the display string and the primary-lens compatibility fields
    try:
        tgt = lv.join_targets(by, lenses)
    except (KeyError, TypeError, ValueError) as exc:
        tgt = None
        fatal.append(f"operation_target: by_lens levels cannot be joined ({exc})")
    if tgt is not None and str(doc.get("operation_target")) != tgt:
        fatal.append(f"operation_target {doc.get('operation_target')!r} ≠ the by_lens levels in session order {tgt!r}")
    for k in ("scenario", "operation_type", "operation_size", "operation_execution"):
        if _canon(doc.get(k)) != _canon(by[P].get(k)):
            fatal.append(f"{k} differs from by_lens.{P}.{k} (the top level carries the primary lens {P}'s fields)")
    # the simple card: a report from before it carries none; otherwise every lens and the top-level summary carry one
    has = ["operation_simple" in (by.get(L) or {}) for L in lenses]
    if any(has) or "operation_simple" in doc:
        if not all(has) or not isinstance(doc.get("operation_simple"), dict):
            fatal.append("operation_simple: missing on the top level or on a lens (every lens and the summary carry it)")
        else:
            try:
                from ruling import simple_summary
                want = simple_summary(by, lenses)
            except (KeyError, TypeError, ValueError) as exc:
                want = None
                fatal.append(f"operation_simple: the by_lens cards cannot be summarised ({exc})")
            if want is not None and _canon(doc["operation_simple"]) != _canon(want):
                fatal.append(f"operation_simple {doc['operation_simple']!r} ≠ the summary of by_lens cards {want!r}")
    return fatal, warn


def _norm_op(op):
    return "ENTRY" if op in ("MARKET_ENTRY", "LIMIT_ENTRY") else op
_NUM = r"\d+(?:\.\d+)?"
_NUM_OR_RANGE = rf"{_NUM}(?:\s*[-~]\s*{_NUM})?"          # 数值 或 数值-数值(震荡区间), 二选一
_LENS = rf"[A-Za-z][A-Za-z0-9.]*\s*=\s*{_NUM_OR_RANGE}"


def operation_target_error(symbol, target) -> str | None:
    """operation_target 只有两种形状(库主 2026-10-16/17): 数值, 或 数值-数值(震荡区间); 单标的一个, 大盘场每个镜头一个
    (镜头=价位, 分号分隔); HOLD 也填价位作锚点, 不写 N/A。分格、成交条件与失效线归 risk_management 与 rationale
    (辩手归 stance_ledger.contingent 与正文)。symbol 为 None 时两种形状都接受(辩手每轮记录没有标的信息)。
    辩手每轮、仲裁者第一判、终报三处都用这一个函数, 字段在每一层都收敛。"""
    tgt = "" if target is None else str(target).strip()
    lens_ok = bool(re.fullmatch(rf"{_LENS}(?:\s*;\s*{_LENS})*\s*;?", tgt))
    num_ok = bool(re.fullmatch(_NUM_OR_RANGE, tgt))
    sym = str(symbol or "").upper()
    if symbol is None:
        ok = num_ok or lens_ok
    elif sym.startswith("GENERAL"):
        ok = lens_ok
    else:
        ok = num_ok
    if ok:
        return None
    return (f"operation_target has only two forms: a number, or number-number ({tgt[:80]!r}); tranches, fill conditions and "
            "invalidation lines go in risk_management and rationale; market-wide sessions write lens=level separated by "
            "semicolons; HOLD also fills a level as anchor, never N/A")
# 心态→操作合法性表(与 prompts/agent/manuals/mentality/mentality.md 的表必须同步, 由 test_corpus_sync 机检)
ADMISSIBLE = {
    "buy_the_dip":            {"ENTRY"},
    "bottom_fishing":         {"ENTRY"},
    "average_down":           {"ENTRY"},
    "average_up":             {"ENTRY"},
    "all_in":                 {"ENTRY"},
    "short_squeeze_chase":    {"ENTRY"},
    "fomo_buy":               {"ENTRY"},
    "profit_taking":          {"TRIM_POSITION", "LIQUIDATE_NOW"},
    "sell_on_strength":       {"TRIM_POSITION", "LIQUIDATE_NOW"},
    "cut_losses_aggressively": {"TRIM_POSITION", "LIQUIDATE_NOW"},
    "deleveraging":           {"TRIM_POSITION", "LIQUIDATE_NOW"},
    "cut_losses_reluctantly": {"TRIM_POSITION", "HOLD_NEUTRAL"},
    "hedge_position":         {"TRIM_POSITION", "HOLD_NEUTRAL"},
    "trapped_hold":           {"HOLD_NEUTRAL", "TRIM_POSITION"},
    "avoid_uncertainty":      {"HOLD_NEUTRAL", "TRIM_POSITION", "LIQUIDATE_NOW"},
    "hold_and_ride_profit":   {"HOLD_NEUTRAL"},
    "wait_for_confirmation":  {"HOLD_NEUTRAL"},
    "wait_and_see":           {"HOLD_NEUTRAL"},
    "contrarian_bet":         _DECLARED_TRADE_OPS,
    "rotate_sector":          _DECLARED_TRADE_OPS,
}
WAITING = {"wait_for_confirmation", "wait_and_see", "waiting"}
# 3.4 (2026-10-29): the mentality is a single decision reason; it summarizes, it does not license operations.
MENTALITY_V34 = ("with_trend", "against_trend", "realizing", "thesis_exit", "exposure_adjustment", "continuing", "waiting")
_BAND_RE = re.compile(r"\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?")


def top_mentality(items):
    """权重最高的心态名(并列取先列者); 非法输入返回 None。"""
    best, best_w = None, -1.0
    for m in items or []:
        if not isinstance(m, dict):
            continue
        w = m.get("weight")
        if isinstance(w, (int, float)) and w > best_w:
            best, best_w = m.get("name"), w
    return best


def main_mentality_name(v) -> str:
    """第一判 / 私记里的主心态: 接受字符串, 也接受与终报同形的权重数组或单个对象; 取不到返回空串。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return str(v.get("name") or "").strip()
    if isinstance(v, list):
        return str(top_mentality(v) or "").strip()
    return ""


def _entries_from_doc(doc: dict) -> list:
    out = []
    for e in doc.get("debate_history") or []:
        if e.get("role") not in ("Zealot", "Reaper"):
            continue
        c = e.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict):
            out.append({"round": e.get("round"), "role": e.get("role"), "content": c})
    return out


def _int_score(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_ok(s) -> bool:
    if not s or not _DATE_RE.match(str(s)[:10]):
        return False
    try:
        import datetime as _d
        _d.date.fromisoformat(str(s)[:10])
        return True
    except ValueError:
        return False


def _days_between(a: str, b: str) -> int:
    import datetime as _d
    return abs((_d.date.fromisoformat(a[:10]) - _d.date.fromisoformat(b[:10])).days)


def _next_review_checks(doc: dict, ruler_era: bool) -> tuple[list, list]:
    """2026-11-06 (owner): events are the reasons, recommend_after is the call time; price lines are retired.
    Shape errors are fatal (fixable in the redraft); everything about judgement is a warning — whether an event matters
    is the arbiter's call, and the machine only says what it could see (`known_events`, filled by the assembler)."""
    fatal, warn = [], []
    nr = doc.get("next_review")
    if not ruler_era or not isinstance(nr, dict):
        return fatal, warn
    for k in ("structured_triggers", "frequency_hint"):
        if nr.get(k):
            warn.append(f"next_review.{k} is retired (2026-11-06): price / sector lines are no longer read; write next_review.events instead")
    events = nr.get("events")
    if events is None:
        warn.append("next_review.events missing: write the expected events that test this judgement ([] when none is known)")
        events = []
    elif not isinstance(events, list):
        fatal.append("next_review.events must be a list")
        events = []
    node_ids = {str(n.get("id")) for n in ((doc.get("scenario") or {}).get("nodes") or []) if isinstance(n, dict)}
    claim_ids: set = set()
    for e in doc.get("debate_history") or []:
        c = e.get("content") if isinstance(e, dict) else None
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                c = None
        if isinstance(c, dict):
            for cl in c.get("claims") or []:
                if isinstance(cl, dict) and cl.get("id"):
                    claim_ids.add(str(cl["id"]))
    spans = []    # (start, end) per dated event, for the call-time and known-event checks
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            fatal.append(f"next_review.events[{i}] is not an object")
            continue
        for k in ("name", "tests", "coverage", "check"):
            if not e.get(k):
                fatal.append(f"next_review.events[{i}] missing {k}")
        prec = e.get("date_precision") or ("date" if e.get("date") else "tbd")
        if prec not in ("exact", "date", "window", "tbd"):
            fatal.append(f"next_review.events[{i}] date_precision {prec!r} not in exact|date|window|tbd")
        if prec != "tbd" and not _date_ok(e.get("date")):
            fatal.append(f"next_review.events[{i}] date must be YYYY-MM-DD (or date_precision tbd)")
        if prec == "window" and not _date_ok(e.get("window_end")):
            fatal.append(f"next_review.events[{i}] date_precision window needs window_end YYYY-MM-DD")
        if e.get("coverage") not in (None, "full", "partial", "none"):
            fatal.append(f"next_review.events[{i}] coverage {e.get('coverage')!r} not in full|partial|none")
        if e.get("tests") is not None and not isinstance(e.get("tests"), list):
            fatal.append(f"next_review.events[{i}] tests must be a list of claim / node ids")
        if prec == "window" and _date_ok(e.get("date")) and _date_ok(e.get("window_end")) and str(e["window_end"])[:10] < str(e["date"])[:10]:
            fatal.append(f"next_review.events[{i}] window_end {e['window_end']} is before date {e['date']}")
        tests = e.get("tests") if isinstance(e.get("tests"), list) else []
        unknown = [str(t) for t in tests if str(t) not in node_ids and str(t) not in claim_ids]
        if unknown and (node_ids or claim_ids):
            warn.append(f"next_review.events[{i}] tests {unknown} name no claim or node of this report")
        if _date_ok(e.get("date")):
            spans.append((str(e["date"])[:10], str(e.get("window_end") or e["date"])[:10] if prec == "window" and _date_ok(e.get("window_end")) else str(e["date"])[:10]))
    ra = str(nr.get("recommend_after") or "")[:10]
    exp = str(nr.get("expires_at") or "")[:10]
    if events and not _date_ok(ra):
        fatal.append("next_review.recommend_after missing or unparseable: the call time is required (an event's date or window start, or the horizon end)")
    if _date_ok(ra):
        if _date_ok(exp) and ra > exp:
            warn.append(f"next_review.recommend_after {ra} is after expires_at {exp}: the tree will have expired by then; the call time must be at or before the horizon end")
        # early is allowed (the owner: write it early when unsure), late is not: within 3 days before an event's start
        # date (a window's start) up to 1 day after it, or within a day of the horizon end
        def _near(target):
            import datetime as _d
            delta = (_d.date.fromisoformat(ra) - _d.date.fromisoformat(target)).days
            return -3 <= delta <= 1
        on_event = any(_near(s) for s, _ in spans)
        on_horizon = _date_ok(exp) and _near(exp)
        if not on_event and not on_horizon:
            what = ", ".join(s for s, _ in spans) if spans else "no events written"
            warn.append(f"next_review.recommend_after {ra} names no event ({what}) and is not the horizon end{(' ' + exp) if _date_ok(exp) else ''}: a call time needs a reason")
    whos = []
    for e in events:
        if isinstance(e, dict) and _date_ok(e.get("date")):
            prec = e.get("date_precision") or "date"
            en = str(e.get("window_end"))[:10] if prec == "window" and _date_ok(e.get("window_end")) else str(e["date"])[:10]
            whos.append((str(e["date"])[:10], en, str(e.get("who") or "").lower()))
    for ke in nr.get("known_events") or []:
        if not isinstance(ke, dict) or not _date_ok(ke.get("date")):
            continue
        kd = str(ke["date"])[:10]
        lo, hi = (ke.get("window") or [kd, kd])[:2] if isinstance(ke.get("window"), list) else (kd, kd)
        kind = str(ke.get("kind") or "")
        want = {"earnings_expected": ("issuer",), "macro": ("macro",)}.get(kind, ())
        def _same_kind(who):
            return (not who) or (not want) or (who in want)
        covered = any(_same_kind(who) and ((s <= hi and en >= lo) or _days_between(s, kd) <= 3) for s, en, who in whos)
        if not covered:
            warn.append(f"known event not in next_review.events: {kind} {ke.get('name') or ''} {kd}".rstrip()
                        + " (write it with coverage none if judged irrelevant)")
    return fatal, warn


def validate(doc: dict, path: str | None = None, pre_signoff: bool = False) -> tuple[list, list]:
    """pre_signoff=True: 再定稿过闸(签章前), 跳过 dissent 签章检查; 其余同。"""
    fatal, warn = [], []
    ruler_era = is_ruler(doc)
    _nf, _nw = _next_review_checks(doc, ruler_era)
    fatal += _nf
    warn += _nw

    # ---- 辩论史逐轮形状 ----
    hist = doc.get("debate_history") or []
    if not hist:
        fatal.append("debate_history missing or empty")
    for i, e in enumerate(hist):
        role, rnd = e.get("role"), e.get("round")
        if role not in ("Zealot", "Reaper"):
            fatal.append(f"history[{i}]: invalid role {role!r} (arbiter records go in arbiter_log, not in history)")
            continue
        if rnd != "Init" and not re.match(r"^(?:Round |回合)\d+$", str(rnd)):   # 回合k = legacy reports
            fatal.append(f"history[{i}]: invalid round name {rnd!r}")
        c = e.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                fatal.append(f"history[{i}]: content is not JSON")
                continue
        if not isinstance(c, dict):
            fatal.append(f"history[{i}]: content is not an object")
            continue
        if not _int_score(c.get("composite_score")):
            fatal.append(f"history[{i}] {role}/{rnd}: composite_score is not an integer in 0-100 ({c.get('composite_score')!r})")
        if not (c.get("analysis_text") or "").strip():
            fatal.append(f"history[{i}] {role}/{rnd}: analysis_text is empty")
        cs = c.get("claims")
        if not isinstance(cs, list) or not cs:
            fatal.append(f"history[{i}] {role}/{rnd}: claims missing or empty")
            continue
        for p in cs:
            pid = p.get("id") if isinstance(p, dict) else None
            if not pid:
                fatal.append(f"history[{i}] {role}/{rnd}: a claim has no id")
                continue
            if p.get("status") not in STATUS_ENUM:
                fatal.append(f"{role}/{rnd} claim {pid}: invalid status ({p.get('status')!r})")
            if p.get("kind") not in KIND_ENUM:
                fatal.append(f"{role}/{rnd} claim {pid}: invalid kind ({p.get('kind')!r})")
            if p.get("evidence_tier") not in TIER_ENUM:
                fatal.append(f"{role}/{rnd} claim {pid}: evidence_tier missing or invalid ({p.get('evidence_tier')!r})")
            if ruler_era:   # 3.4 (C2): the registered effect is an op list per outcome
                eff = p.get("effect")
                if not isinstance(eff, dict) or not isinstance(eff.get("if_true"), list) or not isinstance(eff.get("if_false"), list):
                    fatal.append(f"{role}/{rnd} claim {pid}: effect {{if_true: [ops], if_false: [ops]}} missing (3.4)")
            elif not isinstance(p.get("stake"), (int, float)):
                fatal.append(f"{role}/{rnd} claim {pid}: stake is not numeric")

    # ---- 账本算术 + 收场 open + 结算不超过押分 ----
    res = {}
    try:
        entries = load_entries(path) if path else _entries_from_doc(doc)
        res = (tally(entries, ruler=True, named=_arbiter_named(doc)) if ruler_era else tally(entries, ruler=False))             if entries else {}
    except Exception as e:
        fatal.append(f"tally failed: {e}")
    if res:
        if is_ruler(doc, res):   # 3.4: terminal states rebuilt from the archived NOTES through closure_check; report lists ignored
            f_r, w_r = closure(doc, res)
            fatal += f_r
            warn += w_r
        elif res.get("open_total"):
            # 轮数硬上限强制收场: 编排层登记的保留未决主张(`unresolved`)不是漏结算, 降为警告; 其余照旧 fatal
            cap = (doc.get("caps") or {}).get("max_rounds") if isinstance(doc.get("caps"), dict) else None
            per_role: dict = {}
            for e in entries:
                per_role[e.get("role")] = per_role.get(e.get("role"), 0) + 1
            at_cap = isinstance(cap, int) and cap > 0 and max(per_role.values() or [0]) >= cap
            registered = set(doc.get("unresolved") or []) if at_cap else set()
            kept = [c for c in res["open_total"] if c in registered]
            leaked = [c for c in res["open_total"] if c not in registered]
            if leaked:
                fatal.append(f"open claims remain at close: {', '.join(leaked)}")
            if kept:
                warn.append(f"Round cap {cap} forced the close; unresolved claims kept: {', '.join(kept)} (neither consensus nor concession)")
        for w in res.get("warnings") or []:
            if "exceeds the stake" in w or "no issue" in w:
                fatal.append(w)
            else:
                warn.append(w)
        for w in res.get("forensics") or []:
            warn.append(f"[forensics] {w}")
        for side_key, field in (("zealot", "ledger_score_z"), ("reaper", "ledger_score_r")):
            if ruler_era:
                break   # ruler reports: no stake ledger; the side scores derive from the scenarios
            s = res.get(side_key)
            got = doc.get(field)
            if s and s.get("ledger_score") is not None:
                if got is None:
                    fatal.append(f"Final report is missing {field} (machine figure {s['ledger_score']})")
                elif got != s["ledger_score"]:
                    fatal.append(f"{field}={got} ≠ ledger arithmetic {s['ledger_score']} (machine figures must not be hand-edited)")

    # ---- data_files 路径存在性 ----
    root = Path(__file__).resolve().parent.parent
    for section, files in (doc.get("data_files") or {}).items():
        if isinstance(files, dict):
            files = list(files.values())
        if isinstance(files, str):
            files = [files]
        for f in files or []:
            if f and not (root / f).exists() and not Path(f).exists():
                fatal.append(f"data_files[{section}]: path does not exist {f}")

    # ---- 仲裁者档案 ----
    alog = doc.get("arbiter_log")
    if not isinstance(alog, list) or not alog:
        fatal.append("arbiter_log missing or empty (the round-notes archive must be written to disk)")
    else:
        for j, item in enumerate(alog):
            if not isinstance(item, dict) or not (item.get("notes") or "").strip():
                fatal.append(f"arbiter_log[{j}]: notes text missing")

    slog = doc.get("arbiter_private_scores")
    prov = doc.get("provisional_ruling")
    if not isinstance(slog, list) or not slog:
        fatal.append("arbiter_private_scores missing or empty (every round's private score must be written to disk; postcheck of the decision process depends on it)")
    else:
        for j, item in enumerate(slog):
            if not isinstance(item, dict):
                fatal.append(f"arbiter_private_scores[{j}]: not an object")
                continue
            if not _int_score(item.get("composite_score")) and not (ruler_era and str(item.get("note") or "").strip()):
                fatal.append(f"arbiter_private_scores[{j}]: composite_score is not an integer in 0-100 ({item.get('composite_score')!r})")
            if not item.get("round"):
                fatal.append(f"arbiter_private_scores[{j}]: round missing")
        rounds = {str(e.get("round")) for e in hist}
        if len(slog) < len(rounds):
            warn.append(f"private scores {len(slog)} rows < debate {len(rounds)} rounds — some rounds are unrecorded (record a line even when nothing moved)")

    # ---- reference session length (owner 2026-10-31; guidance): a session that closed below its own round_ref gets a
    # warning, never a block — the anchor is explained in run-protocol §1; pre-anchor reports carry no round_ref and are untouched
    _rref = (doc.get("caps") or {}).get("round_ref") if isinstance(doc.get("caps"), dict) else None
    if isinstance(_rref, int) and _rref > 0 and hist:
        _nr = len({str(e.get("round")) for e in hist if isinstance(e, dict)})
        if _nr < _rref:
            warn.append(f"closed at round {_nr}, below the reference length {_rref} — three or four rounds is normal, two is not; "
                        f"check the notes say in one sentence why the topic list was already complete")

    # ---- 第一判归档 ----
    if not isinstance(prov, dict):
        fatal.append("provisional_ruling missing (first pass of the final ruling not run or not archived)")
        prov = {}
    else:
        pa = prov.get("composite_score")
        if not _int_score(pa):
            fatal.append(f"provisional_ruling.composite_score is not an integer in 0-100 ({pa!r})")
        for k in ("operation_type", "rationale"):
            if not isinstance(prov.get(k), str) or not prov[k].strip():
                fatal.append(f"provisional_ruling.{k} missing or empty")
        if not main_mentality_name(prov.get("mentality")):
            fatal.append("provisional_ruling.mentality missing or empty (main mentality: a string, or a name+weight array in the final report's shape)")
        cr = prov.get("claims_review")
        if not isinstance(cr, list):
            fatal.append("provisional_ruling.claims_review missing (the first ruling must rule on the actionable claims first)")
        elif not cr:
            warn.append("claims_review is empty — are there really no verified actionable claims on the table?")
        else:
            for j, c in enumerate(cr):
                if not isinstance(c, dict) or c.get("verdict") not in ("adopted", "rejected", "partly"):
                    fatal.append(f"claims_review[{j}]: verdict is not adopted|partly|rejected")
                elif not (c.get("reason") or "").strip():
                    warn.append(f"claims_review[{j}]: reason missing — both rejection and adoption need one sentence of reason")

    # ---- 终裁综合评分与账本对照 ----
    fa = doc.get("composite_score")
    if not _int_score(fa):
        fatal.append(f"Final ruling composite_score is not an integer in 0-100 ({fa!r})")
    if ruler_era and not isinstance(doc.get("scenario"), dict):
        fatal.append("scenario (A) missing (3.4: the report's scenario is the mixture A derived at the ruling stage)")
    elif ruler_era:
        # one ruler: the adopted scenario must be valid (data validity is fatal); derived numbers are checked exactly (C9)
        d = (doc["scenario"].get("derived") or {}) if "by_lens" not in doc else {}   # lens-wise: per lens in checks
        for f in d.get("fatal") or []:
            fatal.append(f"scenario: {f}")
        for w in d.get("warn") or []:
            warn.append(f"scenario: {w}")
        f_r, w_r = checks(doc, pre_signoff)
        fatal += f_r
        warn += w_r
        for i, f in enumerate(doc.get("known_flaws") or []):
            if isinstance(f, dict) and f.get("disposition") == "priced" and not f.get("refs"):
                warn.append(f"known_flaws[{i}] is priced but points to no scenario node (refs)")
            # 2026-10-01 定价链: "离整理分中点远近"不再当质量信号(一边倒贴中点 / 兑换率 / 宽价差贴中点三条退役)。
            # 改查遗漏: 已结算存活的主张, 在裁决单、偏离支撑、残余调整里都没有着落 → 提示核对(不拦)。

    # ---- 短期路径: 震荡可证伪 ----
    st = doc.get("short_term_path") or {}
    if not isinstance(st, dict):
        st = {}
    st_dir = str(st.get("direction") or "").strip()
    if st_dir == "震荡":
        _inv = str(st.get("invalid") or "").strip()
        if not _BAND_RE.search(str(st.get("target") or "")):
            fatal.append("short_term_path direction is 震荡 (range) but target is not a range 'lower edge-upper edge' — if you cannot report the two edges, do not fill 震荡; pick a direction")
        if not _inv or _inv.upper() in ("N/A", "NA", "NONE"):
            fatal.append("short_term_path direction is 震荡 (range) but invalid is empty — the two edges are the two-sided invalidation levels; copy the range verbatim into invalid")

    # ---- 操作与心态 ----
    op = doc.get("operation_type")
    if op not in OP_ENUM:
        fatal.append(f"invalid operation_type ({op!r})")
    # operation_target 只填数值(2026-10-16 KB: 把分格、成交条件与灾难线全写进了这个字段): 单标的一个数;
    # 大盘场 镜头=价位 分号分隔(震荡可写区间); HOLD 也要一个价位作锚点(库主 10-16: 不写 N/A)。分格、条件、失效线归 risk_management 与 rationale。
    # 3.4: one derived level for the scenario's own P0 (GENERAL included; no multi-lens string)
    _terr = operation_target_error(None if ruler_era else str(doc.get("symbol") or "X"), doc.get("operation_target"))
    if _terr:
        fatal.append(_terr)
    # 软条件告警(2026-10-17 定案, 先告警不拦; 看两批误报率再决定升 fatal): 成交 / 退出 / 减仓条件只许日线可判的硬条件,
    # 不许与基本面验证、量能倍数、另一标的同破等做 AND 绑定(MP "1.5 倍放量", HYMTF "同步验证公司级损伤", MUFG "N225 同时跌破")
    _soft = re.compile(
        r"同步验证|验证公司|基本面(损伤|恶化)|盘口承接|缩量企稳|同时跌破|同时站上|且\s*[A-Z0-9]{2,8}\s*(跌破|站上|收低|收高)"
        r"|(?i:fundamental\s+(?:verification|confirmation|damage|deterioration|impairment)"
        r"|(?:verif|confirm)\w*\s+(?:of\s+)?(?:the\s+)?company[- ]level|company[- ]level\s+(?:damage|impairment|deterioration)"
        r"|order[- ]book\s+(?:absorption|support)|bid\s+support"
        r"|volume\s+(?:surge|dry[- ]?up|confirmation)|(?:shrinking|drying[- ]up|contracting)\s+volume|stabiliz\w+\s+on\s+(?:shrinking|low)\s+volume"
        r"|simultaneous(?:ly)?\s+(?:break|reclaim|clos)\w*|at\s+the\s+same\s+time\s+(?:break|reclaim|clos)\w*|another\s+symbol)"
        r"|(?:and|while|AND)\s+[A-Z0-9.]{2,8}\s+(?:also\s+)?(?:breaks?|breaking|reclaims?|holds?|closes?|closing)\b")
    _rm = doc.get("risk_management") or {}
    for _where, _txt in (("risk_management.stop_loss", _rm.get("stop_loss")), ("risk_management.take_profit", _rm.get("take_profit")),
                         ("mentality_basis", doc.get("mentality_basis")),
                         ("risk_management.review_triggers", " / ".join(str(x) for x in (_rm.get("review_triggers") or [])))):
        _m = _soft.search(str(_txt or ""))
        if _m:
            warn.append(f"[soft condition] {_where} contains a condition daily bars cannot decide ({_m.group(0)!r}): fill/exit/trim conditions use only levels, comparison operators, closing-bar counts, volume-ratio thresholds or calendar events, never AND-bound to fundamental verification or another symbol breaking too")
    if ((st_dir == "上行" and op in ("TRIM_POSITION", "LIQUIDATE_NOW"))
            or (st_dir == "下行" and op in ("ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY"))):
        warn.append(f"Path and operation point opposite ways: short_term_path={st_dir} while operation={op} — trimming into "
                    f"strength / catching a falling knife both have standing, legal; but the verdict must say in one sentence which it is")
    ment = doc.get("mentality")
    top = top_mentality(ment)
    basis = doc.get("mentality_basis")
    basis = basis.strip() if isinstance(basis, str) else ""
    if top and not basis:
        fatal.append("mentality_basis missing: the main mentality must report the situational fact in one sentence (family premises in the mentality table)")
    elif top in WAITING and basis and not re.search(r"\d", basis):
        warn.append(f"Suspected waiting-as-default: main mentality {top} has a mentality_basis with no date and no level — cannot name the pending change being waited for")
    sig = []
    for m in ment or []:
        if isinstance(m, dict) and isinstance(m.get("weight"), (int, float)) \
                and m["weight"] >= 0.15 and m.get("name"):
            sig.append((m["name"], m["weight"]))
    if ruler_era:
        name = main_mentality_name(ment)
        if not name:
            fatal.append("mentality missing (one decision reason)")
        elif name not in MENTALITY_V34:
            fatal.append(f"mentality {name!r} not one of the decision reasons {MENTALITY_V34}")
        top = name or top
        if top and not basis:
            fatal.append("mentality_basis missing: the decision reason must report its situational fact in one sentence")
        sig = []     # no support gate, no lineage gate for ruler reports: the strategy comparison and mentality_basis carry the check
    elif not top:
        fatal.append("mentality missing or has no valid main item (name+weight)")
    else:
        for name, _w in sig:
            if name not in ADMISSIBLE:
                fatal.append(f"mentality {name!r} not in the enum (spelling?)")
        legal_union = set().union(*(ADMISSIBLE.get(n, set()) for n, _ in sig)) if sig else set()
        if op in OP_ENUM and sig and _norm_op(op) not in legal_union:
            fatal.append(f"Support gate: operation_type {op} is admitted by no significant mentality"
                         f" (present {[n for n, _ in sig]}, legal union {sorted(legal_union)}; an operation needs mentality support)")
        elif op in OP_ENUM and top in ADMISSIBLE and _norm_op(op) not in ADMISSIBLE[top]:
            weights = {n: w for n, w in sig}
            tw = weights.get(top, 0)
            second = max((w for n, w in sig if n != top), default=0)
            if tw >= 0.5 and tw - second >= 0.15:
                warn.append(f"Main mentality {top} ({tw:.2f}) clearly dominates yet does not admit {op}; the operation follows a secondary mentality — legal, but needs one sentence of explanation")
    at = doc.get("asset_tier")
    if at not in ("quality_large", "mid", "small_spec", "na"):
        fatal.append(f"asset_tier missing or invalid ({at!r}; must be quality_large|mid|small_spec|na)")
    elif at == "quality_large" and op == "HOLD_NEUTRAL" and top == "wait_for_confirmation":
        warn.append("Quality name on HOLD waiting for confirmation: if all three real-damage questions are no (false alarm), check whether the quality odds curve calls for a net below instead; keeping wait-for-confirmation needs one sentence of reason")
    if any(n == "wait_for_confirmation" for n, _ in sig) and op == "HOLD_NEUTRAL":
        _nr = doc.get("next_review") or {}
        if not (_nr.get("events") or _nr.get("structured_triggers") or []):
            warn.append("HOLD under wait_for_confirmation does not state in next_review.events what confirmation it waits for")
    # 血统闸
    seen_ment = set()
    for e in hist:
        c = e.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict):
            for m in c.get("mentality") or []:
                if isinstance(m, dict) and m.get("name"):
                    seen_ment.add(m["name"])
    if seen_ment:
        for name in {m.get("name") for m in ment or [] if isinstance(m, dict)} - {None}:
            if name not in seen_ment:
                warn.append(f"Final-report mentality {name} appeared in no round of the debate (lineage gate) — debate_summary must state the evidence and source")
        pm = main_mentality_name(prov.get("mentality"))
        if pm and pm not in seen_ment:
            warn.append(f"First-ruling mentality {pm} did not appear in the debate (lineage gate)")

    # ---- 偏离支撑(综合评分离 50 须指得回证据; 2026-10-01 起不设分数帽) ----
    dev = abs(fa - 50) if isinstance(fa, int) else 0
    ds = doc.get("deviation_support")
    legs = (ds or {}).get("legs") or []
    claim_reg = {}
    for e in hist:
        c = e.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict):
            for p in c.get("claims") or []:
                if isinstance(p, dict) and p.get("id"):
                    claim_reg[p["id"]] = p
    LOST = {"settled_against", "withdrawn", "conceded"}
    if dev >= 5 and not ruler_era:
        if not legs:
            fatal.append(f"deviation_support missing or has no legs (|composite_score-50|={dev} ≥5 requires evidence legs)")
        else:
            all_refs, dup = [], set()
            for i, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    continue
                refs = leg.get("refs") or []
                if not refs:
                    warn.append(f"deviation_support.legs[{i}] has no refs — a leg without citations is not a leg")
                for r in refs:
                    if r in all_refs:
                        dup.add(r)
                    all_refs.append(r)
                for r in refs:
                    p = claim_reg.get(r)
                    if p is not None and p.get("status") in LOST:
                        warn.append(f"legs[{i}] claims points on a lost claim: claim {r} is {p.get('status')} — a conceded argument does not come back; that ref counts 0")
            if dup:
                warn.append(f"deviation_support double-counts: ref {sorted(dup)} appears in several legs (correlated evidence = the same risk)")
            pts_all = [leg.get("points") for leg in legs
                       if isinstance(leg, dict) and isinstance(leg.get("points"), (int, float))]
            tp = (ds or {}).get("total_points")
            if pts_all and isinstance(tp, (int, float)) and sum(pts_all) != tp:
                warn.append(f"deviation_support.total_points={tp} ≠ Σpoints={sum(pts_all)}")

    # ---- 遗漏检查: 已结算存活的主张要在终裁里有着落(裁决单 / 偏离支撑 / 残余调整任一处引到) ----
    placed = set()
    for leg in legs:
        if isinstance(leg, dict):
            placed.update(str(r) for r in (leg.get("refs") or []))
    cr_placed = list(prov.get("claims_review") if isinstance(prov.get("claims_review"), list) else [])
    if ruler_era:   # 3.4 entries name the claim under `claim`; the final claims_review counts too
        cr_placed += doc.get("claims_review") if isinstance(doc.get("claims_review"), list) else []
    for c in cr_placed:
        if isinstance(c, dict):
            placed.update(x for x in re.split(r"[/,\s]+", str(c.get("claim" if ruler_era else "ref") or "")) if x)
    for b in ((doc.get("residual_adjustment") or {}).get("basis") or []):
        if isinstance(b, dict):
            for v in b.values():
                placed.update(re.findall(r"[ZR]\d+[a-z]?", str(v)))
    missing = sorted(pid for pid, p in claim_reg.items()
                     if p.get("status") in ("settled_for", "uncontested") and pid not in placed)
    if missing:
        warn.append(f"Settled surviving claims {missing} are placed nowhere in the verdict list, deviation_support or residual_adjustment — check whether an accepted effect was omitted")

    # ---- 瑕疵定价: 按操作极端度计, 不按评分 ----
    flaws = doc.get("known_flaws") or []
    heavy = doc.get("operation_size") == "AGGRESSIVE_SIZE" or op == "LIQUIDATE_NOW"
    if heavy and not flaws and not ruler_era:
        fatal.append("known_flaws missing: AGGRESSIVE/LIQUIDATE-level operations must price their flaws (a zero-flaw opportunity does not exist)")
    for i, f in enumerate(flaws):
        if isinstance(f, dict) and f.get("disposition") not in ("refuted", "priced"):
            warn.append(f"known_flaws[{i}].disposition invalid ({f.get('disposition')!r}; must be refuted|priced)")

    # ---- 第一判 → 终裁的改判留痕 ----
    p_act = prov.get("composite_score")
    if isinstance(p_act, int) and isinstance(fa, int) and p_act != fa:
        b = (doc.get("residual_adjustment") or {}).get("basis") or []
        # pre-ruler items carry `audit`; ruler items (formats/final.md) are {kind: "audit correction" | "voided entry", issue, reason}
        if not any(isinstance(x, dict) and (x.get("audit") or x.get("kind") == "audit correction") for x in b):
            warn.append(f"Final ruling composite_score {fa} ≠ first ruling {p_act} but residual_adjustment.basis has no audit entry — every change of ruling must leave a trace")
    cr_list = prov.get("claims_review") if isinstance(prov.get("claims_review"), list) else []
    for j, c in enumerate(cr_list):
        if isinstance(c, dict) and c.get("verdict") == "rejected":
            for rid in re.split(r"[/,\s]+", str(c.get("ref") or "")):
                p = claim_reg.get(rid)
                if p and p.get("status") in ("settled_for", "uncontested"):
                    warn.append(f"claims_review[{j}]: rejected claim ref {rid} is {p.get('status')} — rejecting a claim settled in its favor needs a hard reason")
    if isinstance(p_act, int) and isinstance(fa, int) and st_dir in ("上行", "下行"):
        dmove = fa - p_act
        if (dmove > 0 and st_dir == "下行") or (dmove < 0 and st_dir == "上行"):
            warn.append(f"Audit shift {dmove:+d} runs opposite to short_term_path.direction={st_dir} — ledger points are not directional evidence; the audit entry must explain")

    # ---- 签章真实性 ----
    if pre_signoff:
        return fatal, warn
    dd = doc.get("dissent") or {}
    placeholder = {"", "pending signoff", "pending", "tbd", "n/a", "na", "待定", "待签", "待签章"}
    for side in ("zealot", "reaper"):
        r = dd.get(f"{side}_reservation")
        if not isinstance(r, str) or r.strip().lower() in placeholder:
            fatal.append(f"dissent.{side}_reservation missing or a placeholder ({r!r}) — signoff round not executed")
        if not isinstance(dd.get(f"{side}_composite_score"), int):
            fatal.append(f"dissent.{side}_composite_score is not an integer — signoff round not executed or not harvested")
    dz, dr = dd.get("zealot_composite_score"), dd.get("reaper_composite_score")
    if isinstance(dz, int) and isinstance(dr, int) and isinstance(fa, int):
        if (dz > 50 and dr > 50 and fa < 50) or (dz < 50 and dr < 50 and fa > 50):
            warn.append(f"Final ruling {fa} runs opposite to the direction both sides agree on (signoff Z {dz}/R {dr} on the same side) — must cite issue ids to justify")

    return fatal, warn


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "fatal": ["usage: validate_debate.py <Analysis.json>"]}))
        sys.exit(1)
    path = sys.argv[1]
    try:
        doc = json.loads(io.open(path, encoding="utf-8").read())
    except Exception as e:
        print(json.dumps({"ok": False, "fatal": [f"read/parse: {e}"]}, ensure_ascii=False))
        sys.exit(1)
    fatal, warn = validate(doc, path)
    ok = not fatal
    print(json.dumps({"ok": ok, "fatal": fatal, "warnings": warn}, ensure_ascii=False, indent=1))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
