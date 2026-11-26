# -*- coding: utf-8 -*-
"""Lens projection (C11): one lens at a time, so every scalar ruler function stays untouched.

A multi-lens session (GENERAL = SPY/QQQ/DIA, GENERAL_EU = DAX/IAEX) carries `scenario_by_lens` and lens-tagged ops.
The functions here project a record / report / odds snapshot onto one lens; the projected document has the
single-lens shape, so scenario_replay, the pricing chain etc. run on it unchanged.

Must not import scripts/ruling.py at module level (ruling imports this module).
"""
from __future__ import annotations

import copy
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

LAMBDA_SUM_TOL = 0.01
LAMBDA_DEV_MAX = 0.1
LAMBDA_DEV_EPS = 1e-9   # G7: a move of exactly LAMBDA_DEV_MAX (0.5/0.3 -> 0.4/0.4) is inside the anchor despite float error
_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")   # same whole-token rule as ruling._tokens


class LensError(ValueError):
    pass


def fmt_level(x: Any) -> str:
    """Same formatting as ruling.fmt_level (kept in sync by a test): plain number string, no exponent."""
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


def lenses_of(obj: Any) -> List[str]:
    """Session state / report / _meta -> its lens list when >= 2, else [] (single-lens)."""
    ls = (obj or {}).get("lenses") if isinstance(obj, dict) else None
    if not isinstance(ls, (list, tuple)) or len(ls) < 2:
        return []
    return [str(x) for x in ls]


def is_multi(obj: Any) -> bool:
    return len(lenses_of(obj)) >= 2


def _lens_ops(ops: Any, lens: str) -> Any:
    if not isinstance(ops, list):
        return ops
    out = []
    for op in ops:
        if isinstance(op, dict) and op.get("lens") == lens:
            o = dict(op)
            o.pop("lens", None)
            out.append(o)
    return out


def _project_revision(rev: Any, lens: str) -> None:
    if isinstance(rev, dict) and "ops" in rev:
        rev["ops"] = _lens_ops(rev["ops"], lens)


def project_content(content: dict, lens: str, lenses: List[str]) -> dict:
    """Deep copy of a record content on one lens; `lenses == []` -> plain deep copy."""
    c = copy.deepcopy(content)
    if not lenses or not isinstance(c, dict):
        return c
    if "scenario_by_lens" in c:
        sbl = c.pop("scenario_by_lens")
        if not isinstance(sbl, dict) or lens not in sbl:
            raise LensError(f"lens {lens}: scenario_by_lens missing this lens")
        c["scenario"] = sbl[lens]
    else:
        raise LensError(f"lens {lens}: scenario_by_lens missing this lens")
    for claim in c.get("claims") or []:
        eff = claim.get("effect") if isinstance(claim, dict) else None
        if isinstance(eff, dict):
            for k in ("if_true", "if_false"):
                if k in eff:
                    eff[k] = _lens_ops(eff[k], lens)
    for upd in c.get("scenario_updates") or []:
        if isinstance(upd, dict) and "ops" in upd:
            upd["ops"] = _lens_ops(upd["ops"], lens)
    _project_revision(c.get("scenario_revision"), lens)
    if "endorsed_scenario_by_lens" in c:
        ebl = c.pop("endorsed_scenario_by_lens")
        if isinstance(ebl, dict) and lens in ebl:
            c["endorsed_scenario"] = ebl[lens]
    if "composite_by_lens" in c:
        cbl = c.pop("composite_by_lens")
        if isinstance(cbl, dict) and lens in cbl:
            c["composite_score"] = cbl[lens]
    return c


def project_record(rec: dict, lens: str, lenses: List[str]) -> dict:
    r = dict(rec)
    r["content"] = project_content(rec.get("content") or {}, lens, lenses)
    return r


def lens_snapshot(odds: dict, lens: str) -> dict:
    """Odds snapshot on one lens: reference_price / cost_bps / unit_basis from odds.by_lens[lens]. Identity copy without by_lens."""
    o = copy.deepcopy(odds)
    by = o.get("by_lens") if isinstance(o, dict) else None
    if not isinstance(by, dict):
        return o
    if lens not in by or not isinstance(by[lens], dict):
        raise LensError(f"lens {lens}: odds.by_lens missing this lens")
    b = by[lens]
    for k in ("reference_price", "cost_bps"):
        if k not in b:
            raise LensError(f"lens {lens}: odds.by_lens.{k} missing")
        o[k] = b[k]
    o["unit_basis"] = {k: v for k, v in b.items() if k not in ("reference_price", "cost_bps")}
    return o


_PRIMARY_KEYS = ("operation_type", "operation_target", "operation_size", "operation_execution", "operation_simple",
                 "composite_score", "s_ref", "s_ref_z", "s_ref_r")


def project_report(doc: dict, lens: str, lenses: List[str]) -> dict:
    """A final report on one lens (single-lens shape). Keys it does not know are left untouched."""
    d = copy.deepcopy(doc)
    if not lenses:
        return d
    by = d.get("by_lens")
    if not isinstance(by, dict) or lens not in by or not isinstance(by[lens], dict):
        raise LensError(f"lens {lens}: by_lens missing this lens")
    b = by[lens]
    records = [(h.get("role"), copy.deepcopy(h.get("content"))) for h in d.get("debate_history") or []
               if isinstance(h, dict)]   # unprojected: the lens tags decide which lenses an unscoped void applies on
    if "scenario" not in b:
        raise LensError(f"lens {lens}: by_lens.scenario missing")
    d["scenario"] = b["scenario"]
    for k in _PRIMARY_KEYS:   # a key the lens does not carry (a HOLD lens's operation_execution) is not inherited
        if k in b:
            d[k] = b[k]
        else:
            d.pop(k, None)
    for h in d.get("debate_history") or []:
        if isinstance(h, dict) and isinstance(h.get("content"), dict):
            h["content"] = project_content(h["content"], lens, lenses)
    prov = d.get("provisional_ruling")
    if isinstance(prov, dict) and isinstance(prov.get("by_lens"), dict):   # a derived ruling record (C11.5)
        pb = prov["by_lens"].get(lens)
        if not isinstance(pb, dict) or "scenario" not in pb:
            raise LensError(f"lens {lens}: provisional_ruling.by_lens missing this lens")
        prov["scenario"] = pb["scenario"]
        for k in _PRIMARY_KEYS:
            if k in pb:
                prov[k] = pb[k]
            else:
                prov.pop(k, None)
    elif isinstance(prov, dict):
        d["provisional_ruling"] = project_content(prov, lens, lenses)
    vo = d.get("voided")
    if isinstance(vo, dict):   # C11.4: a void names one lens, or none (effects: every lens the effect touches)
        for k in ("effects", "candidates"):
            if isinstance(vo.get(k), list):
                vo[k] = [{a: x for a, x in v.items() if a != "lens"} for v in vo[k]
                         if isinstance(v, dict) and void_applies(v, lens, records if k == "effects" else None)]
    dis = d.get("dissent")
    if isinstance(dis, dict):
        for key in list(dis):
            if key.endswith("_endorsed_scenario_by_lens"):
                m = dis.pop(key)
                if not isinstance(m, dict) or lens not in m:
                    raise LensError(f"lens {lens}: dissent.{key} missing this lens")
                dis[key[: -len("_by_lens")]] = m[lens]
            elif key.endswith("_composite_by_lens"):
                m = dis.pop(key)
                if not isinstance(m, dict) or lens not in m:
                    raise LensError(f"lens {lens}: dissent.{key} missing this lens")
                dis[key[: -len("_by_lens")] + "_score"] = m[lens]
    if isinstance(d.get("odds"), dict):
        d["odds"] = lens_snapshot(d["odds"], lens)
        atr = (d["odds"].get("by_lens") or {}).get(lens, {}).get("atr_pct_14")
        if atr is not None:
            d["atr_pct_14"] = atr
    for ob in d.get("objections") or []:
        if isinstance(ob, dict):
            _project_revision(ob.get("scenario_revision"), lens)
    return d


_SIDE_TAG = {"Z": "Z", "R": "R", "Zealot": "Z", "Reaper": "R", "zealot": "Z", "reaper": "R"}


def effect_lenses(records: Any, claim: Any, side: Any, outcome: Any) -> List[str]:
    """Lenses on which `side`'s registered effect `claim#outcome` has ops (C11.4): the claim's own effect when the side
    files the claim, else the side's challenges targeting it (the registry's rule). `records`: (role, content) pairs of
    the unprojected finals, role Z / R / Zealot / Reaper."""
    tag, oc = _SIDE_TAG.get(side, side), str(outcome).lower()
    out: List[str] = []
    for role, content in records or []:
        if _SIDE_TAG.get(role, role) != tag or not isinstance(content, dict):
            continue
        for c in content.get("claims") or []:
            if not isinstance(c, dict):
                continue
            own = c.get("id") == claim and c.get("kind") != "challenge"
            if not (own or (c.get("kind") == "challenge" and c.get("target") == claim)):
                continue
            eff = c.get("effect") if isinstance(c.get("effect"), dict) else {}
            for op in eff.get(f"if_{oc}") or []:
                if isinstance(op, dict) and op.get("lens") is not None and op["lens"] not in out:
                    out.append(op["lens"])
    return out


def void_applies(v: dict, lens: str, records: Any = None) -> bool:
    """C11.4: a void names the lens on which the effect fails; an unscoped effect void applies on every lens the effect
    touches (and must fail on each of them) — on every lens when the effect touches none (`records` None: every lens)."""
    if "lens" in v:
        return v["lens"] == lens
    if records is None:
        return True
    touched = effect_lenses(records, v.get("claim"), v.get("side"), v.get("outcome"))
    return not touched or lens in touched


def ops_lens_errors(where: str, ops: list, lenses: List[str]) -> List[str]:
    errs: List[str] = []
    for i, op in enumerate(ops or []):
        has = isinstance(op, dict) and "lens" in op
        if lenses:
            if not has:
                errs.append(f"{where}: op[{i}] has no lens")
            elif op["lens"] not in lenses:
                errs.append(f"{where}: op[{i}] lens {op['lens']!r} is not a session lens {list(lenses)}")
        elif has:
            errs.append(f"{where}: op[{i}] carries lens in a single-lens session")
    return errs


def default_weights(root: Path, symbol: str) -> Dict[str, float]:
    """Config lens weights of `symbol`, normalised to one, rounded to 4 dp."""
    root = Path(root)
    for p in (str(root), str(root / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from workflows.data_manifest import load_symbols_cfg
    contract = ((load_symbols_cfg(root).get("symbol_contracts") or {}).get(symbol)) or {}
    items = contract.get("lenses") or []
    w: Dict[str, float] = {}
    for it in items:
        if not isinstance(it, dict) or "symbol" not in it or it.get("weight") is None:
            raise LensError(f"lens config for {symbol}: entry {it!r} has no symbol/weight")
        w[str(it["symbol"])] = float(it["weight"])
    if not w:
        raise LensError(f"lens config for {symbol}: no lenses with weights in symbols.yaml")
    tot = sum(w.values())
    if tot <= 0:
        raise LensError(f"lens config for {symbol}: weights sum to {tot}")
    return {k: round(v / tot, 4) for k, v in w.items()}


def lens_weights_errors(given: Any, default: Dict[str, float], lenses: List[str], reason: Any,
                        ids: set) -> Tuple[List[str], Dict[str, float]]:
    """The lambda rules: every lens present, each >= 0, sum within 0.01 of one (normalised), any lambda more than
    0.1 from its default needs lens_weights_reason naming a session claim / evidence / model-call id."""
    if given is None:
        return [], default
    if not isinstance(given, dict):
        return ["lens_weights must be an object {lens: weight}"], default
    errs: List[str] = []
    for L in lenses:
        if L not in given:
            errs.append(f"lens_weights: lens {L} missing")
    for L in given:
        if L not in lenses:
            errs.append(f"lens_weights: {L!r} is not a session lens {list(lenses)}")
    vals: Dict[str, float] = {}
    for L in lenses:
        if L not in given:
            continue
        v = given[L]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append(f"lens_weights: lens {L} weight {v!r} is not a number")
        elif not math.isfinite(v):   # G7: NaN / inf would normalise every lambda to NaN
            errs.append(f"lens_weights: lens {L} weight {v!r} is not a finite number")
        elif v < 0:
            errs.append(f"lens_weights: lens {L} weight {v} is negative")
        else:
            vals[L] = float(v)
    if errs:
        return errs, default
    tot = sum(vals.values())
    if abs(tot - 1.0) > LAMBDA_SUM_TOL:
        return [f"lens_weights: sum {tot:.4f} is not within {LAMBDA_SUM_TOL} of 1"], default
    lam = {L: vals[L] / tot for L in lenses}
    if any(abs(lam[L] - float(default.get(L, 0.0))) > LAMBDA_DEV_MAX + LAMBDA_DEV_EPS for L in lenses):
        toks = {t.rstrip(".-") for t in _TOKEN.findall(reason if isinstance(reason, str) else "")} - {""}
        if not (toks & set(ids)):
            errs.append(f"lens_weights: a lens differs from its default by more than {LAMBDA_DEV_MAX}; "
                        "lens_weights_reason must name at least one session claim or evidence/model-call id")
    return errs, lam


def weighted_score(scores: Dict[str, float], lam: Dict[str, float]) -> int:
    """round(Σ λ_L · score_L); the two dicts must name the same lenses (LensError naming the missing / extra lens)."""
    missing = [L for L in lam if L not in scores]
    extra = [L for L in scores if L not in lam]
    if missing or extra:
        raise LensError(f"weighted_score: scores and weights name different lenses (no score for {missing}; "
                        f"no weight for {extra})")
    return int(round(sum(lam[L] * scores[L] for L in lam)))


def join_targets(by_lens: dict, lenses: List[str]) -> str:
    return "; ".join(f"{L}={fmt_level(by_lens[L]['operation_target'])}" for L in lenses)
