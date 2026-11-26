#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate one role record before it becomes immutable audit history.

重建批 2(2026-09-30)字段名: composite_score / claims[kind own|challenge|reinforce, evidence_tier] /
stance_ledger.moves[issue, delta]。旧名(action / pillars / tier / boost)不再接受——终稿在铸造前过此检。
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


STATUS = {"open", "settled_for", "settled_against", "conceded", "deadlocked",
          "withdrawn", "uncontested"}
KINDS = {"own", "challenge", "reinforce"}
TIERS = {"T1", "T2", "T3", "T4"}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("record must be a JSON object")
    return value


def _content(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("content", record)
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("record content must be a JSON object")
    return value


def _known_claims(history_path: Path | None, current: dict[str, Any]) -> dict[str, float]:
    stakes: dict[str, float] = {}
    if history_path and history_path.exists():
        for line_no, raw in enumerate(history_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                content = _content(json.loads(raw))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"history line {line_no} is invalid: {exc}") from exc
            for c in content.get("claims") or []:
                if isinstance(c, dict) and c.get("id") and isinstance(c.get("stake"), (int, float)):
                    stakes[str(c["id"])] = c["stake"]
    for c in current.get("claims") or []:
        if isinstance(c, dict) and c.get("id") and isinstance(c.get("stake"), (int, float)):
            stakes[str(c["id"])] = c["stake"]
    return stakes


def validate(record: dict[str, Any], history_path: Path | None = None, ref_price: float | None = None) -> list[str]:
    errors: list[str] = []
    try:
        content = _content(record)
    except (json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    ruler_sc = content.get("scenario")
    if isinstance(ruler_sc, dict):   # a ruler record (CLI path; the store calls ingest directly); no scenario = pre-ruler
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from closure_check import round_no
        try:
            hist = _history_from_jsonl(history_path)
        except (json.JSONDecodeError, ValueError) as exc:
            return [f"history is invalid: {exc}"]
        rnd = round_no(record.get("round")) if record.get("round") else 1 + max((h["round"] for h in hist), default=0)
        # CLI lint has no session: without --ref-price the record's own P0 stands in (the binding pin is the store's,
        # from odds.reference_price frozen at open — debate_store passes it to ingest, which refuses a missing one)
        if ref_price is None:
            ref_price = ruler_sc.get("P0")
        errs, _ = ingest(content, record.get("role") or "Zealot", rnd, hist, ref_price=ref_price)
        return errs
    if record.get("content") is not None:
        if record.get("role") not in {"Zealot", "Reaper"}:
            errors.append("wrapper role must be Zealot or Reaper")
        if not record.get("round"):
            errors.append("wrapper round is required")
    score = content.get("composite_score")
    if not isinstance(score, int) or not 0 <= score <= 100:
        errors.append(f"composite_score must be an integer from 0 to 100, got {score!r}")
    if not isinstance(content.get("analysis_text"), str) or not content.get("analysis_text", "").strip():
        errors.append("analysis_text must be non-empty")
    # 每轮最上面那个核心字段必须收敛成一个数(库主 2026-10-17): 分格与条件在正文和 contingent 里讨论, 字段只放一个价位
    from validate_debate import operation_target_error
    _terr = operation_target_error(None, content.get("operation_target"))
    if _terr:
        errors.append(_terr)
    claims = content.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims must be a non-empty list")
        claims = []
    for idx, c in enumerate(claims):
        if not isinstance(c, dict):
            errors.append(f"claim[{idx}] must be an object")
            continue
        cid = c.get("id") or f"index {idx}"
        if c.get("status") not in STATUS:
            errors.append(f"claim {cid}: invalid status {c.get('status')!r}")
        if c.get("kind") not in KINDS:
            errors.append(f"claim {cid}: invalid kind {c.get('kind')!r}")
        if c.get("evidence_tier") not in TIERS:
            errors.append(f"claim {cid}: invalid evidence_tier {c.get('evidence_tier')!r}")
        if not isinstance(c.get("stake"), (int, float)):
            errors.append(f"claim {cid}: stake must be numeric")
    try:
        stakes = _known_claims(history_path, content)
    except ValueError as exc:
        errors.append(str(exc))
        stakes = {}
    for idx, move in enumerate((content.get("stance_ledger") or {}).get("moves") or []):
        if not isinstance(move, dict):
            errors.append(f"move[{idx}] must be an object")
            continue
        issue, delta = move.get("issue"), move.get("delta")
        if not issue:
            errors.append(f"move[{idx}] is missing issue")
        if not isinstance(delta, (int, float)):
            errors.append(f"move {move.get('id') or idx}: delta must be numeric")
        elif delta == 0:
            errors.append(f"move {move.get('id') or idx}: delta 为 0 不是结算(没动就写进 held)")
        elif issue and issue not in stakes:
            errors.append(f"move {move.get('id') or idx}: issue {issue!r} 不是在场的主张编号"
                          f"(一笔移动只挂一条主张; 牵涉两条就拆成两笔)")
        elif issue in stakes and abs(delta) > stakes[issue]:
            errors.append(f"move {move.get('id') or idx}: |delta|={abs(delta)} 超过 {issue} stake={stakes[issue]}"
                          f"(押分是上限; 部分证实可在押分以内折价)")
    return errors


# ------------------------------------------------------------------------------------------------ 3.4 (C2 / C3 / C8)

OPS = {"p_move", "P_T_set", "path_set", "split"}
_TAG = {"Zealot": "Z", "Reaper": "R", "Z": "Z", "R": "R", "zealot": "Z", "reaper": "R"}


def _ops_errors(where: str, ops: Any) -> list[str]:
    if not isinstance(ops, list):
        return [f"{where} must be a list of ops (C1)"]
    errs = [f"{where}[{i}]: op must be one of {sorted(OPS)}" for i, o in enumerate(ops)
            if not isinstance(o, dict) or o.get("op") not in OPS]
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lens_views import ops_lens_errors
    return errs + ops_lens_errors(where, ops, [])   # single-lens (or one projected lens): a `lens` key is refused


P0_PIN_TOL = 0.0005   # 0.05%: display rounding of the session reference price, not a band


def _ingest_one(content: Any, role: str, round_: int, history: list[dict], cfg: dict | None = None,
                ref_price: float | None = None) -> tuple[list[str], Any]:
    """Schema-3.4 ingestion of one debater final (C2 claims and effects, freezing, C3 update / `applies` verification,
    C3 execution, C8 derived score). `history` = the earlier finals as store records [{seq, id, role, round, content}]
    in manifest order. `scenario.T_days` must equal the ruler's `T_days`; `ref_price` (the session reference price frozen
    at open as `odds.reference_price`: the symbol's opening snapshot price, the primary lens's for GENERAL) is mandatory —
    missing or malformed → the final is refused ("reference price missing; session cannot ingest") — and `scenario.P0`
    must match it within display rounding (P0_PIN_TOL); it is stored as that canonical value. → (errors, content to archive): on success
    `composite_score := round(S_ref(scenario))` and an authored value moves to `authored_score`."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import odds_model as om
    import scenario_replay as sr
    from validate_debate import operation_target_error
    if not isinstance(content, dict):
        return ["record content must be a JSON object"], content
    cfg = cfg or om.DEFAULT_CONFIG
    tol = float(cfg.get("p_tolerance", 0.01))
    tag = _TAG[role]
    rid = f"R{round_}.{tag}"
    errors: list[str] = []
    if not isinstance(content.get("analysis_text"), str) or not content["analysis_text"].strip():
        errors.append("analysis_text must be non-empty")
    if "scenario_by_lens" in content:   # projected lens content has none: only a single-lens session gets here with it
        errors.append("scenario_by_lens: a single-lens session writes one scenario (remove scenario_by_lens)")
    terr = operation_target_error(None, content.get("operation_target"))
    if terr:
        errors.append(terr)
    sc = content.get("scenario")
    if not isinstance(sc, dict):
        errors.append("scenario missing (3.4: emit the full scenario every round)")
        sc = None
    else:
        want = str(cfg.get("schema") or om.DEFAULT_CONFIG["schema"])
        if str(sc.get("schema")) != want:
            errors.append(f'scenario.schema must be "{want}" (the schema of the session ruler)')
        # I4: the mixture needs one P0 and one T_days on both sides, and no op changes either — pin them at ingestion
        t_ruler = cfg.get("T_days")
        if om._is_num(t_ruler) and (not om._is_num(sc.get("T_days")) or float(sc["T_days"]) != float(t_ruler)):
            errors.append(f"scenario.T_days {sc.get('T_days')!r} ≠ the session's T_days {t_ruler!r} (odds snapshot); "
                          f"write T_days: {t_ruler}")
        if not (om._is_num(ref_price) and float(ref_price) > 0):
            errors.append(f"reference price missing; session cannot ingest (3.4 pins scenario.P0 to the session reference "
                          f"price frozen at open as odds.reference_price; got {ref_price!r})")
        else:
            p0 = sc.get("P0")
            if not om._is_num(p0) or abs(float(p0) - float(ref_price)) > P0_PIN_TOL * float(ref_price):
                errors.append(f"scenario.P0 {p0!r} ≠ the session reference price {float(ref_price):g} (opening snapshot; "
                              f"display rounding {100 * P0_PIN_TOL:g}% allowed); write P0: {float(ref_price):g}")
            else:   # store the canonical value so both sides' scenarios share one P0
                content = copy.deepcopy(content)
                content["scenario"]["P0"] = float(ref_price)
                sc = content["scenario"]
        if "baseline" in sc:
            errors.append("scenario.baseline is not part of 3.4 (one neutral reference; remove it)")
        fatal, _warn, _norm = om.validate_scenario(sc, cfg)
        errors.extend(f"scenario: {f}" for f in fatal)
        ex_fatal, _raw = om.validate_execution(sc.get("execution"), sc)
        errors.extend(f"scenario.{f}" for f in ex_fatal)
    if "execution" in content:
        ex_fatal, _raw = om.validate_execution(content.get("execution"), sc)
        errors.extend(ex_fatal)
    claims = content.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims must be a non-empty list")
        claims = []
    for idx, c in enumerate(claims):
        if not isinstance(c, dict):
            errors.append(f"claim[{idx}] must be an object")
            continue
        cid = c.get("id") or f"index {idx}"
        if not c.get("id"):
            errors.append(f"claim[{idx}] has no id")
        if c.get("status") not in STATUS:
            errors.append(f"claim {cid}: invalid status {c.get('status')!r}")
        if c.get("kind") not in KINDS:
            errors.append(f"claim {cid}: invalid kind {c.get('kind')!r}")
        if c.get("evidence_tier") not in TIERS:
            errors.append(f"claim {cid}: invalid evidence_tier {c.get('evidence_tier')!r}")
        if c.get("kind") == "challenge" and not c.get("target"):
            errors.append(f"claim {cid}: a challenge names its target claim")
        eff = c.get("effect")
        if not isinstance(eff, dict) or "if_true" not in eff or "if_false" not in eff:
            errors.append(f"claim {cid}: effect {{if_true: [ops], if_false: [ops]}} missing (C2; [] = already inside)")
        else:
            errors.extend(_ops_errors(f"claim {cid}: effect.if_true", eff.get("if_true")))
            errors.extend(_ops_errors(f"claim {cid}: effect.if_false", eff.get("if_false")))
        ans = c.get("answers", [])
        if not isinstance(ans, list) or not all(isinstance(a, str) and a for a in ans):
            errors.append(f"claim {cid}: answers must be a list of evidence / model-call ids")
    updates = content.get("scenario_updates", [])
    if not isinstance(updates, list):
        errors.append("scenario_updates must be a list")
        updates = []
    for i, u in enumerate(updates):
        if not isinstance(u, dict) or not u.get("claim") or not str(u.get("why") or "").strip():
            errors.append(f"scenario_updates[{i}]: needs claim, ops[] and why")
            continue
        errors.extend(_ops_errors(f"scenario_updates[{i}].ops", u.get("ops")))
        if u.get("applies") is not None and not isinstance(u.get("applies"), str):
            errors.append(f"scenario_updates[{i}]: applies is '<claim id>#true|#false' or null")
    if errors:
        return errors, content
    history = sorted(history or [], key=lambda r: r["seq"])
    own = [r for r in history if _TAG.get(r["role"]) == tag]
    if not own:
        if updates:
            errors.append("scenario_updates must be [] in the opening round (C3: no previous scenario to update)")
    else:
        ok, msg = sr.verify_update(own[-1]["content"].get("scenario"), updates, sc, tol)
        if not ok:
            errors.append(f"scenario_updates: {msg} (C3: replaying this round's ops onto your previous scenario must "
                          f"reproduce this round's scenario)")
    this = {"seq": (history[-1]["seq"] + 1) if history else 1, "id": rid, "role": tag, "round": round_, "content": content}
    registry, reg_fatal = sr.effects_registry_from_history(history + [this], tag)
    errors.extend(f for f in reg_fatal if f.startswith(rid + ":"))
    prior = [u for r in own for u in (r["content"].get("scenario_updates") or []) if isinstance(u, dict)]
    for f in sr.verify_applies(prior + updates, registry):
        head = f.split(":", 1)[0]
        n = int(head[len("update["):-1]) if head.startswith("update[") and head.endswith("]") else len(prior)
        if n >= len(prior):
            detail = f[len(head) + 1:].strip()
            if detail.endswith("does not match the registered effect"):   # C3 wording
                detail = f"applies does not match the registered effect ({detail.split(' does not match')[0]})"
            errors.append(f"scenario_updates[{n - len(prior)}]: {detail}")
    # G2 (C3): a registered effect carried without naming it (`applies: null`, the claim's registered ops) would be
    # applied twice — once as the update, once at settlement — and would bypass the carried-effect freeze below
    # keyed on the ops, never on the update's `claim` (a challenge id or an unregistered id must not slip through)
    for i, u in enumerate(updates):
        if not isinstance(u, dict) or u.get("applies") is not None:
            continue
        ops = sr.canonical(u.get("ops") or [])
        hit = next(((cid, oc) for cid in sorted(registry) for oc in ("true", "false")
                    if (registry[cid].get(oc) or []) and ops == sr.canonical(registry[cid][oc])), None)
        if hit:
            errors.append(f"scenario_updates[{i}]: ops equal the registered effect of {hit[0]}#{hit[1]} — write "
                          f"applies: '{hit[0]}#{hit[1]}' (an independent increment needs its own claim)")
    # I3: a revision (new text + revises + new effect) may not change an effect this side already carried through an
    # earlier update's `applies` — the carried ops would no longer match the registry, and no stage could replay the side
    prior_registry, _ = sr.effects_registry_from_history(history, tag)
    carried_by = {}
    for r in own:
        for k, u in enumerate(r["content"].get("scenario_updates") or []):
            parsed = sr._parse_applies(u.get("applies")) if isinstance(u, dict) else None
            if parsed and parsed not in carried_by:
                carried_by[parsed] = f"{r['id']}.scenario_updates[{k}]"
    for (cid, oc), where in sorted(carried_by.items()):
        before, after = (prior_registry.get(cid) or {}).get(oc), (registry.get(cid) or {}).get(oc)
        if before is None or sr.canonical(before) == sr.canonical(after or []):
            continue
        revised = [str(c.get("id")) for c in claims if isinstance(c, dict) and c.get("revises")
                   and (c.get("id") == cid or (c.get("kind") == "challenge" and c.get("target") == cid))] or [cid]
        errors.append(f"claim {', '.join(revised)}: effect already carried by update {where} (applies {cid}#{oc}); "
                      f"file a new claim instead (a new id; on an opponent's claim a `reinforce`, never a second challenge)")
    targets: dict[str, set] = {}
    for r in own + [this]:
        for c in r["content"].get("claims") or []:
            if isinstance(c, dict) and c.get("kind") == "challenge" and c.get("target"):
                targets.setdefault(str(c["target"]), set()).add(str(c.get("id")))
    for t, ids in sorted(targets.items()):
        if len(ids) > 1:
            errors.append(f"duplicate challenge target {t}: {sorted(ids)} (one challenge per target per side; revise the "
                          f"existing challenge instead)")
    if errors:
        return errors, content
    _fatal, _w, norm = om.validate_scenario(sc, cfg)
    out = dict(content)
    if "composite_score" in out:
        out["authored_score"] = out["composite_score"]
    out["composite_score"] = int(round(om.score(norm)["S_ref"]))
    return [], out


def _lens_shape_errors(content: dict, lenses: list[str]) -> list[str]:
    from lens_views import ops_lens_errors
    errs: list[str] = []
    if "scenario" in content:
        errs.append("scenario: a multi-lens session writes scenario_by_lens")
    sbl = content.get("scenario_by_lens")
    if not isinstance(sbl, dict):
        errs.append(f"scenario_by_lens missing (a multi-lens session writes one scenario per lens {list(lenses)})")
    else:
        errs.extend(f"scenario_by_lens: missing lens {L}" for L in lenses if L not in sbl)
        errs.extend(f"scenario_by_lens: unknown lens {L}" for L in sbl if L not in lenses)
        errs.extend(f"scenario_by_lens.{L} must be an object" for L in lenses if L in sbl and not isinstance(sbl[L], dict))
    for c in content.get("claims") or []:
        eff = c.get("effect") if isinstance(c, dict) else None
        if isinstance(eff, dict):
            for k in ("if_true", "if_false"):
                if isinstance(eff.get(k), list):
                    errs.extend(ops_lens_errors(f"claim {c.get('id')}: effect.{k}", eff[k], lenses))
    for i, u in enumerate(content.get("scenario_updates") or []):
        if isinstance(u, dict) and isinstance(u.get("ops"), list):
            errs.extend(ops_lens_errors(f"scenario_updates[{i}].ops", u["ops"], lenses))
    rev = content.get("scenario_revision")
    if isinstance(rev, dict) and isinstance(rev.get("ops"), list):
        errs.extend(ops_lens_errors("scenario_revision.ops", rev["ops"], lenses))
    return errs


def applies_lens_errors(content: dict, round_: int, tag: str, history: list[dict]) -> list[str]:
    """C11.4: an update that applies a registered effect carries that effect's ops on every lens the effect has ops on.
    `content` = one multi-lens final (unprojected) of side `tag` (Z / R), `history` = the earlier finals as store records
    in seq order. Shared by ingestion and the release gate (which re-checks the raw history before projecting)."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import scenario_replay as sr
    errors: list[str] = []
    updates = content.get("scenario_updates") if isinstance(content, dict) else None
    if not isinstance(updates, list):
        return errors
    this = {"seq": (history[-1]["seq"] + 1) if history else 1, "id": f"R{round_}.{tag}", "role": tag,
            "round": round_, "content": content}
    registry, _fatal = sr.effects_registry_from_history(list(history) + [this], tag)
    for i, u in enumerate(updates):
        parsed = sr._parse_applies(u.get("applies")) if isinstance(u, dict) else None
        if not parsed or parsed[0] not in registry:
            continue
        want = {o.get("lens") for o in registry[parsed[0]][parsed[1]] if isinstance(o, dict)}
        have = {o.get("lens") for o in (u.get("ops") or []) if isinstance(o, dict)}
        missing = want - have
        if missing:
            errors.append(f"scenario_updates[{i}]: applies {u['applies']} must carry the effect's ops on every lens "
                          f"(missing {sorted(missing)})")
    return errors


def ingest(content: Any, role: str, round_: int, history: list[dict], cfg: dict | None = None,
           ref_price: float | None = None, lenses: list[str] | None = None,
           ref_prices: dict[str, float] | None = None, lens_weights: dict[str, float] | None = None) -> tuple[list[str], Any]:
    """Ingest one debater final. Single-lens (`lenses` empty / None): `_ingest_one` unchanged. Multi-lens (C11): shape and
    lens-tag rules, then `_ingest_one` once per lens on the projected content and history against `ref_prices[lens]`
    (errors prefixed "lens L: "), the C11.4 `applies` rule, and the archived content keeps `scenario_by_lens` (each lens's
    canonical P0 pinned) with `composite_by_lens` and the lens-weighted `composite_score` (authored -> `authored_score`)."""
    if not lenses or len(lenses) < 2:
        return _ingest_one(content, role, round_, history, cfg, ref_price)
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import scenario_replay as sr
    from lens_views import LensError, project_content, project_record, weighted_score
    if not isinstance(content, dict):
        return ["record content must be a JSON object"], content
    lenses = list(lenses)
    errors = _lens_shape_errors(content, lenses)
    if errors:
        return errors, content
    ref_prices = ref_prices or {}
    history = sorted(history or [], key=lambda r: r["seq"])
    outs: dict[str, Any] = {}
    try:
        for L in lenses:
            errs, out = _ingest_one(project_content(content, L, lenses), role, round_,
                                    [project_record(r, L, lenses) for r in history], cfg, ref_prices.get(L))
            errors.extend(f"lens {L}: {e}" for e in errs)
            outs[L] = out
    except LensError as exc:
        return errors + [str(exc)], content
    errors.extend(applies_lens_errors(content, round_, _TAG[role], history))
    if errors:
        return errors, content
    out = copy.deepcopy(content)
    cbl = {L: int(outs[L]["composite_score"]) for L in lenses}
    for L in lenses:
        out["scenario_by_lens"][L] = outs[L]["scenario"]   # canonical P0 pinned
    if "composite_score" in out:
        out["authored_score"] = out["composite_score"]
    out["composite_by_lens"] = cbl
    if not lens_weights:   # the frozen lens_weights_default; never a uniform fallback
        return ["multi-lens final: lens_weights (the session's frozen lens_weights_default) missing; cannot score"], content
    try:
        out["composite_score"] = weighted_score(cbl, lens_weights)
    except LensError as exc:
        return [str(exc)], content
    return [], out


def _history_from_jsonl(history_path: Path | None) -> list[dict]:
    from closure_check import round_no
    out: list[dict] = []
    if history_path and history_path.exists():
        for raw in history_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            tag = _TAG.get(row.get("role"))
            if not tag:
                continue
            n = round_no(row.get("round"))
            out.append({"seq": len(out) + 1, "id": f"R{n}.{tag}", "role": tag, "round": n, "content": _content(row)})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    parser.add_argument("history", nargs="?", type=Path)
    parser.add_argument("--ref-price", type=float, default=None,
                        help="3.4: the session reference price (odds.reference_price) to pin scenario.P0 against")
    args = parser.parse_args()
    try:
        record = _read_json(args.record)
        errors = validate(record, args.history, args.ref_price)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [f"read/parse: {exc}"]
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
