#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""execution_card — the deterministic projection of a report's execution contract (2026-11-03, owner + GPT review).

Why: after v3.4 the operation is a contract (conditional entries, close / touch stops, trigger windows, lenses), and the
top-level `operation_type` / `operation_target` alone misleads: AMD read "LIMIT_ENTRY 520.4" while the contract was
"buy at the next open only after a close above 520.4". Every reader (dashboard, rebalance skill, outside clients, the
pre-round brief) was re-deriving it and each got it wrong once. This module derives it once, from the machine fields,
and every reader shows the card.

Rules (council/20261103-1857-execution-card/):
  - the card is a projection: it never invents an order type from a price relation (a level above the reference with
    no condition is "wait for the level", not a breakout order); `resting_order` is true / false / None = "the contract
    explicitly is a plain resting limit" / "explicitly not" / "cannot be told" — not "can I place it today";
  - lens-wise reports get one card per lens on `by_lens[L]`, the top level follows the primary lens;
  - `one_line` is a HEADLINE (schema 1.1, owner 2026-11-03 evening): "what to do now" first, then the trigger in a few
    words, at most about twelve words; the stop, the window and the "no resting limit" caveat are NOT in it — they are
    structured fields (`stop`, `valid`, `resting_order`) and the reader's contract sheet renders them row by row;
  - `size` is in the reader's units (schema 1.1): `direction` (overweight / underweight / none), `move` = x* (how many
    standard units this step moves), `target` = q* (how many remain afterwards), `baseline` 0.5 = neutral; the storage
    enum `tier` stays for filters (AGGRESSIVE_SIZE means a full-unit move, never "all in"), `q_star` stays for readers
    that already use it;
  - English only here; the Chinese is the i18n overlay's business (leaf path `execution_card.one_line`).
Also builds `unresolved_claims` (deadlocked / unresolved claim ids → the final claim text, by id + claim_rev), the display
copy the i18n layer translates; the debate record itself stays English.

The top-level enums are not touched; the postcheck keeps reading `operation_execution`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from scripts.odds_model import _parse_observe

SCHEMA = "1.1"   # 1.1 (2026-11-03): one_line is a headline; size carries direction / move / target / baseline
KINDS = ("market_entry_now", "entry_at_level", "conditional_entry", "hold", "trim_now", "trim_at_level",
         "trim_on_condition", "exit_now", "undeterminable")
_AT_MARKET_TOL_PCT = 0.01   # mirrors assemble_debate_artifacts._ENTRY_AT_MARKET_TOL_PCT (display rounding only)
_REL = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below"}
_FILL = {"next_open": "at the next regular-session open",
         "at_level": "at the level when it is reached (a gap through it fills at the open)"}
_OBS_WORD = {"close": "a regular-session close", "open": "a regular-session open", "touch": "a trade", "gap_open": "a gap open"}


def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def _parse_target(t) -> Tuple[str, Any]:
    """→ ("single", level) | ("range", (lo, hi)) | ("multi", None) | ("none", None)."""
    s = str(t if t is not None else "").strip()
    if not s or s == "-":
        return "none", None
    if "=" in s:
        return "multi", None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)", s)
    if m:
        return "range", (float(m.group(1)), float(m.group(2)))
    v = _num(s)
    return ("single", v) if v else ("none", None)


def _reference(report: Dict, symbol: str, lens: Optional[str] = None) -> Optional[float]:
    odds = report.get("odds") if isinstance(report.get("odds"), dict) else {}
    if lens:
        o_l = (odds.get("by_lens") or {}).get(lens) if isinstance(odds.get("by_lens"), dict) else None
        v = _num((o_l or {}).get("reference_price")) if isinstance(o_l, dict) else None
        if v:
            return v
    v = _num(odds.get("reference_price")) if not lens else None
    if v:
        return v
    refs = report.get("reference_prices") or []
    want = (lens or symbol or "").upper()
    for r in refs:
        if isinstance(r, dict) and str(r.get("symbol") or "").upper() == want and _num(r.get("price")):
            return _num(r["price"])
    if not lens and len(refs) == 1 and isinstance(refs[0], dict):
        return _num(refs[0].get("price"))
    return None


def _chosen(view: Dict) -> Optional[Dict]:
    sc = view.get("scenario") if isinstance(view.get("scenario"), dict) else {}
    d = sc.get("derived") if isinstance(sc.get("derived"), dict) else {}
    cid = d.get("chosen")
    for c in d.get("candidates") or []:
        if isinstance(c, dict) and c.get("id") == cid:
            return c
    return {"id": cid, "strategy": str(cid).split("[")[0]} if isinstance(cid, str) and cid else None


def _parse_conditional(ex: Dict, chosen: Optional[Dict]) -> Tuple[Optional[Dict], Optional[str]]:
    """→ (trigger dict, error). The contract on operation_execution first; the chosen candidate's parsed fields as fallback."""
    cond = ex.get("conditional") if isinstance(ex, dict) else None
    if isinstance(cond, dict) and cond.get("observe"):
        observation = _parse_observe(cond["observe"])
        if observation is None:
            return None, f"unparseable observe {cond.get('observe')!r}"
        field, comparator, level = observation
        fill = str(cond.get("fill") or "next_open")
        if fill not in _FILL:
            return None, f"unknown fill {fill!r}"
        return {"observe": str(cond["observe"]).strip(), "field": field, "comparator": comparator,
                "level": level, "fill": fill}, None
    if chosen and chosen.get("observe") and chosen.get("kind") in _OBS_WORD and chosen.get("op") in _REL:
        return {"observe": str(chosen["observe"]), "field": chosen["kind"], "comparator": chosen["op"],
                "level": _num(chosen.get("level")), "fill": str(chosen.get("fill") or "next_open")}, None
    return None, None


def _stop(ex: Dict, chosen: Optional[Dict], direction: str) -> Optional[Dict]:
    st = ex.get("stop") if isinstance(ex, dict) else None
    if not (isinstance(st, dict) and _num(st.get("level"))):
        st = chosen.get("stop") if isinstance(chosen, dict) and isinstance(chosen.get("stop"), dict) else None
    if not (isinstance(st, dict) and _num(st.get("level"))):
        return None
    sem = str(st.get("semantics") or "touch")
    if direction == "underweight":
        return {"level": float(st["level"]), "semantics": sem, "action": "buy_back", "comparator": ">=",
                "fill": "next open after the close" if sem == "close" else "at the level"}
    return {"level": float(st["level"]), "semantics": sem, "action": "sell_back", "comparator": "<=",
            "fill": "next open after the close" if sem == "close" else "at the level"}


def _valid(report: Dict) -> Optional[Dict]:
    """The order window from the report's own words (postcheck_quant is the authority); the year for a month-day date comes
    from the report, never from the clock."""
    try:
        from workflows.postcheck_quant import _order_valid_days, _order_valid_until
    except Exception:
        return None
    as_of = None
    for k in ("market_data_timestamp_utc", "report_generated_at"):
        v = report.get(k)
        if isinstance(v, str) and v:
            try:
                as_of = datetime.fromisoformat(v.replace("Z", "+00:00"))
                break
            except ValueError:
                continue
    until = _order_valid_until(report, as_of)
    days, src = _order_valid_days(report)
    return {"days": None if until else days, "until": until, "source": "report" if until else src,
            "note": "bounds the trigger; a condition met on the last session still fills at the next open"}


_SIZE_BASELINE = 0.5   # q of the neutral reference (odds.yaml q_ref); x* = 2·|q* − baseline|


def _size(view: Dict, kind: Optional[str] = None) -> Dict:
    """Reader's units: direction / move (x*, units moved now) / target (q*, units held afterwards) / baseline (0.5).
    `tier` is the storage enum (a display prototype of x*: light < 37.5% ≤ standard < 75% ≤ heavy = full unit);
    `q_star` is kept for readers that already use it. Pre-3.4 reports (no derived) get tier + direction only."""
    sc = view.get("scenario") if isinstance(view.get("scenario"), dict) else {}
    d = sc.get("derived") if isinstance(sc.get("derived"), dict) else {}
    chosen = str(d.get("chosen") or "")
    if chosen.startswith("overweight"):
        direction = "overweight"
    elif chosen.startswith("underweight"):
        direction = "underweight"
    elif chosen == "neutral":
        direction = "none"
    elif kind in ("market_entry_now", "entry_at_level", "conditional_entry"):
        direction = "overweight"
    elif kind in ("trim_now", "trim_at_level", "trim_on_condition", "exit_now"):
        direction = "underweight"
    else:
        direction = "none"
    out: Dict[str, Any] = {"tier": view.get("operation_size"), "direction": direction}
    q = d.get("q_star") if isinstance(d.get("q_star"), (int, float)) and not isinstance(d.get("q_star"), bool) else None
    x = d.get("x_star") if isinstance(d.get("x_star"), (int, float)) and not isinstance(d.get("x_star"), bool) else None
    if q is not None and x is None:
        x = 2.0 * abs(float(q) - _SIZE_BASELINE)
    out["move"] = round(float(x), 3) if x is not None else None
    out["target"] = round(float(q), 3) if q is not None else None
    out["baseline"] = _SIZE_BASELINE
    if q is not None:
        out["q_star"] = d["q_star"]
    if d.get("unit_basis") is not None:
        out["unit_basis"] = d["unit_basis"]
    return out


def _fmt(v) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else str(v)


_OBS_SHORT = {"close": "a close", "open": "an open", "touch": "a trade", "gap_open": "a gap open"}
_FILL_SHORT = {"next_open": "at next open", "at_level": "at the level"}


def _one_line(c: Dict) -> str:
    """Headline, at most about twelve words: what to do now, then the trigger. No stop, no window, no caveats — those
    are structured fields; the contract sheet shows them as rows and the dashboard shows this line alone."""
    k, lvl, tr = c["kind"], c.get("level"), c.get("trigger")
    if k == "conditional_entry":
        return f"Hold; buy {_FILL_SHORT[tr['fill']]} after {_OBS_SHORT[tr['field']]} {_REL[tr['comparator']]} {_fmt(tr['level'])}"
    if k == "trim_on_condition":
        return f"Hold; trim {_FILL_SHORT[tr['fill']]} after {_OBS_SHORT[tr['field']]} {_REL[tr['comparator']]} {_fmt(tr['level'])}"
    if k == "market_entry_now":
        return "Buy now at market"
    if k == "entry_at_level":
        if c.get("resting_order") is True:
            return f"Limit buy at {_fmt(lvl)} or lower"
        return f"Hold; wait for {_fmt(lvl)} (no order type stated)"
    if k == "trim_now":
        return "Trim now at market"
    if k == "trim_at_level":
        if c.get("resting_order") is True:
            return f"Limit sell at {_fmt(lvl)} or higher"
        return f"Hold; wait for {_fmt(lvl)} (no order type stated)"
    if k == "exit_now":
        return "Exit now at market"
    if k == "hold":
        return "Hold; no order"
    return "No order; execution unresolved"


def _card_notices(view: Dict, report: Dict) -> List[Dict]:
    """Reader-facing disclosures for the selected full card, in display order."""
    odds = report.get("odds")
    if not isinstance(odds, dict) or odds.get("conditional_fill_policy") != "strict_next_open_v1":
        return []
    scenario = view.get("scenario")
    derived = scenario.get("derived") if isinstance(scenario, dict) else None
    comparison = derived.get("comparison") if isinstance(derived, dict) else None
    meta = comparison.get("conditional_execution") if isinstance(comparison, dict) else None
    candidates = meta.get("candidates") if isinstance(meta, dict) else None
    if not isinstance(meta, dict) or meta.get("policy") != "strict_next_open_v1" or not isinstance(candidates, dict) \
            or any(not isinstance(c, dict) or type(c.get("eligible")) is not bool for c in candidates.values()):
        return [{"severity": "warning", "message": "Information about which plans could be compared is unavailable."}]
    if any(not candidate["eligible"] for candidate in candidates.values()):
        return [{"severity": "warning", "message": "Some conditional plans lacked a next-session opening price. The recommendation compares the remaining plans."}]
    return []


def build_card(view: Dict, report: Dict, symbol: str, reference: Optional[float], lens: Optional[str] = None) -> Dict:
    op = str(view.get("operation_type") or "").upper()
    ex = view.get("operation_execution") if isinstance(view.get("operation_execution"), dict) else {}
    chosen = _chosen(view)
    strategy = str((chosen or {}).get("strategy") or "")
    tkind, tval = _parse_target(view.get("operation_target"))
    level = tval if tkind == "single" else None
    level_range = list(tval) if tkind == "range" else None
    trig, trig_err = _parse_conditional(ex, chosen)
    reason = None

    entry_ops = ("ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY")
    if trig_err:
        kind, reason = "undeterminable", trig_err
    elif trig:
        if strategy.startswith("underweight") or op == "TRIM_POSITION":
            kind = "trim_on_condition"
        elif strategy.startswith("overweight") or op in entry_ops:
            kind = "conditional_entry"
        else:
            kind, reason = "undeterminable", f"condition with operation {op or '?'}"
        level = trig.get("level") or level
    elif strategy:
        if strategy == "neutral":
            kind = "hold"
        elif strategy == "overweight_now":
            kind = "market_entry_now"
        elif strategy == "underweight_now":
            kind = "exit_now" if op == "LIQUIDATE_NOW" else "trim_now"
        elif strategy.endswith("_on_condition"):
            kind, reason = "undeterminable", "chosen strategy is conditional but no condition is recorded"
        else:
            kind, reason = "undeterminable", f"unknown strategy {strategy}"
    else:
        if op == "MARKET_ENTRY":
            kind = "market_entry_now"
        elif op == "HOLD_NEUTRAL":
            kind = "hold"
        elif op == "LIQUIDATE_NOW":
            kind = "exit_now"
        elif op == "LIMIT_ENTRY":
            kind = "entry_at_level" if level else "undeterminable"
            if not level:
                reason = "range or multi-lens target without an execution rule" if tkind in ("range", "multi") else "no level"
        elif op == "TRIM_POSITION":
            if level and reference and abs(level / reference - 1) * 100 <= _AT_MARKET_TOL_PCT:
                kind = "trim_now"
            elif level:
                kind = "trim_at_level"
            else:
                kind = "trim_now" if tkind == "none" else "undeterminable"
                reason = None if kind == "trim_now" else "range or multi-lens target without an execution rule"
        else:
            kind, reason = "undeterminable", f"operation {op or 'missing'} not derivable"

    relation, dist = "unknown", None
    if level and reference:
        dist = round((level / reference - 1) * 100, 2)
        relation = "at" if abs(dist) <= _AT_MARKET_TOL_PCT else ("above" if dist > 0 else "below")

    if kind in ("conditional_entry", "trim_on_condition"):
        resting = False
    elif kind == "entry_at_level":
        resting = True if relation == "below" else None
    elif kind == "trim_at_level":
        resting = True if relation == "above" else None
    else:
        resting = None

    now = {"market_entry_now": "buy_now", "trim_now": "sell_now", "exit_now": "sell_all_now", "hold": "hold",
           "conditional_entry": "hold", "trim_on_condition": "hold", "undeterminable": "none"}.get(kind)
    if kind == "entry_at_level":
        now = "place_limit_buy" if resting else "hold"
    if kind == "trim_at_level":
        now = "place_limit_sell" if resting else "hold"

    direction = "underweight" if kind in ("trim_now", "trim_at_level", "trim_on_condition", "exit_now") else "overweight"
    card = {
        "schema": SCHEMA, "symbol": lens or symbol, "kind": kind, "now": now, "pending": kind in ("conditional_entry", "trim_on_condition"),
        "resting_order": resting, "level": level, "level_range": level_range, "reference": reference,
        "relation": relation, "distance_pct": dist, "trigger": trig,
        "stop": _stop(ex, chosen, direction) if kind != "hold" else _stop(ex, chosen, direction),
        "valid": _valid(report) if (trig or kind in ("entry_at_level", "trim_at_level")) else None,
        "size": _size(view, kind), "chosen": (chosen or {}).get("id"), "operation_type": op or None,
        "reason": reason,
    }
    card["one_line"] = _one_line(card)
    notices = _card_notices(view, report)
    if notices:
        card["notices"] = notices
    return card


def _fingerprint(view: Dict, reference: Optional[float], report: Dict) -> str:
    basis = {"op": view.get("operation_type"), "target": view.get("operation_target"), "size": view.get("operation_size"),
             "ex": view.get("operation_execution"), "chosen": ((view.get("scenario") or {}).get("derived") or {}).get("chosen")
             if isinstance(view.get("scenario"), dict) else None,
             "ref": reference, "valid": _valid(report), "schema": SCHEMA}
    notices = _card_notices(view, report)
    if notices:
        basis["notices"] = notices
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]


def build_unresolved(report: Dict) -> List[Dict]:
    """deadlocked / unresolved claim ids → [{id, claim_rev, state, text}] from the final filing of each claim (by seq)."""
    ids: List[Tuple[str, str]] = []
    for state in ("deadlocked", "unresolved"):
        for cid in report.get(state) or []:
            if isinstance(cid, str) and cid not in [i for i, _ in ids]:
                ids.append((cid, state))
    if not ids:
        return []
    hist = [e for e in (report.get("debate_history") or []) if isinstance(e, dict)]
    hist.sort(key=lambda e: (e.get("seq") if isinstance(e.get("seq"), (int, float)) else float("inf")))
    member = report.get("symbol") if report.get("debate_ref") else None
    # Compact group rows (also used by translation) keep member-local display
    # claims but omit the shared history. In hydrated rows IDs repeat by member.
    archived = {c["id"]: c for c in report.get("unresolved_claims") or []
                if isinstance(c, dict) and isinstance(c.get("id"), str)
                and isinstance(c.get("text"), str) and c["text"].strip()} if member else {}
    out = []
    for cid, state in ids:
        saved = archived.get(cid, {})
        text, rev = None, None
        for e in reversed(hist):
            content = e.get("content")
            if member and isinstance(content, dict):
                filings = content.get("members")
                content = filings.get(member) if isinstance(filings, dict) else None
            if not isinstance(content, dict):
                continue
            for c in content.get("claims") or []:
                if isinstance(c, dict) and c.get("id") == cid and isinstance(c.get("claim"), str) and c["claim"].strip():
                    text, rev = c["claim"], e.get("id")
                    break
            if text:
                break
        if saved and (text is None or text == saved["text"]):
            text, rev = saved["text"], rev or saved.get("claim_rev")
        out.append({"id": cid, "claim_rev": rev, "state": state, "text": text})
    return out


def ensure_display_fields(report: Dict, source: str = "api") -> Dict:
    """Idempotent: adds / refreshes `execution_card` (per lens too) and `unresolved_claims` in place; returns the report.
    A card whose fingerprint still matches its inputs is kept (the assembler's card survives the API)."""
    if not isinstance(report, dict):
        return report
    symbol = str(report.get("symbol") or "")
    lenses = report.get("lenses") if isinstance(report.get("lenses"), list) else None
    by = report.get("by_lens") if isinstance(report.get("by_lens"), dict) else None

    def _refresh(view: Dict, ref: Optional[float], lens: Optional[str]) -> Dict:
        fp = _fingerprint(view, ref, report)
        old = view.get("execution_card")
        if isinstance(old, dict) and old.get("fingerprint") == fp and old.get("schema") == SCHEMA \
                and "comparison_scope" not in old and old.get("notices", []) == _card_notices(view, report):
            if not str(old.get("reason") or "").startswith("unparseable observe"):
                return old
        card = build_card(view, report, symbol, ref, lens)
        card["fingerprint"], card["source"] = fp, source
        view["execution_card"] = card
        return card

    if lenses and by:
        cards = {}
        for L in [str(x) for x in lenses]:
            v = by.get(L)
            if isinstance(v, dict):
                cards[L] = _refresh(v, _reference(report, symbol, L), L)
        prim = str(lenses[0]) if lenses else None
        if prim in cards:
            top = copy.deepcopy(cards[prim])
            top["lens"], top["lenses"] = prim, [str(x) for x in lenses]
            report["execution_card"] = top
    else:
        _refresh(report, _reference(report, symbol), None)
    if "debate_history" in report or "deadlocked" in report or "unresolved" in report:
        report["unresolved_claims"] = build_unresolved(report)
    from workflows.simple_projection import project_simple   # 2026-11-09: simple card projected onto pre-v3.5 reports
    project_simple(report)
    return report


def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
