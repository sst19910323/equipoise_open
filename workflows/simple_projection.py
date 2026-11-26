"""Read-time projection of the simple card (`operation_simple`, v3.5) onto reports archived before it existed.

The simple card is derived from the adopted distribution and the session's frozen odds snapshot, both of which every
v3.4+ report carries (`scenario`, `odds`; lens-wise: `by_lens[L].scenario`). The candidate resting orders come from each
side's last-round `scenario.execution` in `debate_history`, exactly as the ruling builds them. Nothing is written back:
the archived report text never changes; the projected card carries `source: "projected"` so readers can tell it from a
card the ruling produced (`source: "report"`). Reports without a scenario tree (before v3.4) get no card.

project_simple(report) mutates the report in place and returns it; never raises.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
_ROLE = {"Zealot": "Z", "Reaper": "R", "Z": "Z", "R": "R"}


def _finals(report: Dict[str, Any]) -> List[dict]:
    """debate_history → store-shaped finals [{role: Z/R, round: int, content}] in order."""
    out = []
    for rec in report.get("debate_history") or []:
        if not isinstance(rec, dict) or _ROLE.get(rec.get("role")) is None:
            continue
        rnd = str(rec.get("round") or "")
        n = 1 if rnd.lower() == "init" else (int(rnd.split()[-1]) + 1 if rnd.split() and rnd.split()[-1].isdigit() else 1)
        out.append({"role": _ROLE[rec["role"]], "round": n, "content": rec.get("content") or {}})
    return out


def _card(scenario: dict, odds_snap: dict, finals: List[dict], voided: list, symbol, lens) -> dict:
    import odds_model as om
    from ruling import _side, last_execution, last_scenario, simple_fields
    cfg = om.config_from_snapshot(odds_snap)
    fatal, warn, norm = om.validate_scenario(scenario, cfg)
    if fatal:
        raise ValueError("; ".join(fatal[:2]))
    sides = {t: _side(finals, t) for t in ("Z", "R")}
    cands = om.build_candidates(last_execution(sides["Z"]), last_execution(sides["R"]), norm, voided or [],
                                rounds={t: (sides[t][-1]["round"] if sides[t] else 1) for t in ("Z", "R")}, warn=[],
                                filers={t: last_scenario(sides[t]) for t in ("Z", "R")})
    d = om.derive_for_report(norm, cands, cfg)
    if not d.get("ok"):
        raise ValueError("; ".join(d.get("fatal") or []))
    if lens:                                            # per-lens card: as ruling.derive_ruling (lens-wise) writes it
        card = simple_fields(d, None, None)
        card["operation_target"] = float(d["simple"]["operation_target"])
    else:                                               # single-lens report: as ruling.derive_ruling writes it
        from ruling import primary_lens
        card = simple_fields(d, symbol, primary_lens(symbol, odds_snap, None))
    card["source"] = "projected"
    return card


def now_simple(op_type: Any, order: Any) -> Any:
    """The simple card's action word, in the vocabulary of the execution card's `now` (hold / buy_now / sell_now /
    sell_all_now / place_limit_buy / place_limit_sell); None when the card is absent or not recognisable."""
    op, order = str(op_type or ""), str(order or "")
    if op == "HOLD_NEUTRAL":
        return "hold"
    if op == "LIQUIDATE_NOW":
        return "sell_all_now"
    if op in ("MARKET_ENTRY", "LIMIT_ENTRY", "ENTRY"):
        return "place_limit_buy" if (op == "LIMIT_ENTRY" or order == "limit") else "buy_now"
    if op == "TRIM_POSITION":
        return "place_limit_sell" if order == "limit" else "sell_now"
    return None


_ROW_CACHE: Dict[Any, Any] = {}


def now_simple_for_row(row: Dict[str, Any], symbol: str, load_report) -> Any:
    """`now_simple` of one /api/reports row. A stored card is read from the row's projected columns (`simple_op`,
    `simple_order`); a v3.4 report without one (`has_tree`) is projected once from its archived report —
    `load_report(symbol, as_of)` — and remembered (an archived report never changes). Before v3.4: None."""
    if row.get("simple_op"):
        return now_simple(row.get("simple_op"), row.get("simple_order"))
    if not row.get("has_tree"):
        return None
    key = (str(symbol).upper(), str(row.get("as_of")))
    if key not in _ROW_CACHE:
        word = None
        try:
            card = project_simple(load_report(symbol, row.get("as_of")) or {}).get("operation_simple")
            if isinstance(card, dict):
                word = now_simple(card.get("operation_type"), card.get("order"))
        except Exception:  # noqa: BLE001 — the list never fails on one row
            word = None
        if len(_ROW_CACHE) > 5000:
            _ROW_CACHE.clear()
        _ROW_CACHE[key] = word
    return _ROW_CACHE[key]


def project_simple(report: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(report, dict):
        return report
    if isinstance(report.get("operation_simple"), dict):
        return report                      # the ruling's own card: left exactly as archived (the gate re-derives and compares it)
    odds, scenario = report.get("odds"), report.get("scenario")
    if not isinstance(odds, dict) or not isinstance(scenario, dict) or not scenario.get("nodes"):
        return report                      # before v3.4: no tree to project from
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    try:
        finals = _finals(report)
        voided = (report.get("voided") or {}).get("candidates") or []
        lenses = report.get("lenses") if isinstance(report.get("lenses"), list) else None
        by = report.get("by_lens") if isinstance(report.get("by_lens"), dict) else None
        if lenses and by:
            import lens_views as lv
            from ruling import _lens_voided, simple_summary
            for L in [str(x) for x in lenses]:
                v = by.get(L)
                if not isinstance(v, dict) or not isinstance(v.get("scenario"), dict):
                    return report
                fin_L = [lv.project_record(f, L, [str(x) for x in lenses]) for f in finals]
                v["operation_simple"] = _card(v["scenario"], lv.lens_snapshot(odds, L), fin_L,
                                              _lens_voided(voided, L), None, L)
            top = simple_summary(by, [str(x) for x in lenses])
            top["source"] = "projected"
            report["operation_simple"] = top
        else:
            report["operation_simple"] = _card(scenario, odds, finals, voided, report.get("symbol"), None)
    except Exception as exc:  # noqa: BLE001 — a projection failure never breaks the API
        report["operation_simple_note"] = f"simple card not projected: {type(exc).__name__}: {str(exc)[:160]}"
    return report
