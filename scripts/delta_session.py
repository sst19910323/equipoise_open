#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Follow-up ("delta") sessions (owner 2026-11-06; council/20261106-1530-incremental-sessions, GPT two rounds).

A delta session is a NEW session — new as_of, new reference price, new odds snapshot, full ruling chain, the same
closure check and hard cap — that differs from a full session in exactly two things:
  1. it imports the previous session's still-valid Scout evidence as *standing evidence* (facts the debaters may cite
     or challenge without calling for them again; their applicability must be re-judged, their old `supports` are not
     mapped to this session's claims);
  2. its reference session length is shorter (config/delta.yaml `round_ref`, default 3) because the opening checks that
     repeat from session to session are already on the table.
Nothing of the previous ruling, score, operation, levels, mixture, finals or settled status is passed — not to the
debaters and not to the arbiter ("continuity must be earned, not inherited"). It is not a resume: the previous
session's state and threads are untouched.

What is imported (v1, conservative): evidence objects whose `meta.kind` is in `import_kinds` (search = Scout) and whose
`meta.status` is in `import_status` (verified). Never: the data-preparation machine figures (recomputed by the new
preparation), model calls (P07 structure / P06 execution / P01 pricing read the new bars), unverified or failed calls.
Imported objects are re-minted through the normal allocator as the arbiter's `A.EV…` (the id grammar and visibility
rules stay intact) with `origin_session` / `origin_id` / `source_as_of` / `last_verified_at` in meta — never reset.

Eligibility (machine): the base session exists and is done, is older than the new one, is not older than
`max_base_age_days`, and the chain is not deeper than `max_chain` (a delta of a delta …; the third forces a fresh root).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {"max_chain": 3, "max_base_age_days": 7, "round_ref": 3,
                            "import_kinds": ["search"], "import_status": ["verified"]}

# Wording per the GPT council of 2026-11-06 evening (council/20261106-1935-delta-material-trust): the first follow-up
# session re-ran like a full one (9 fresh outcalls, 4 rounds) because the material said the focus "sets the starting
# priority, not the topic boundary". The owner's intent — this is an incremental judgement on previously verified facts,
# not a session from zero — is stated outright; what is promised is only "verified then, within that scope and date".
STANDING_SENTENCE = ("This is a follow-up assessment: reuse the inherited evidence as previously verified facts within its "
                     "recorded scope and as-of date without routine re-verification, focus inquiry on changes since the base "
                     "and their affected premises, reopen any material coverage gap or contradiction you identify, and "
                     "independently reassess applicability at today's price and date without inheriting any prior judgment. "
                     "A full opening scenario is still required from each side.")

CLAIM_RULE = ("Claim admission in a follow-up: every claim you open names (1) the change since the base it rests on — a "
              "price move, an event, time passing, a data revision — or the specific coverage gap or contradiction you found; "
              "(2) which fact's applicability or which premise it affects; (3) what answer would change this session's "
              "scenario, probabilities or execution. 'The price moved' alone is not a basis: write the transmission. A request "
              "to re-verify a covered fact says which part is uncovered or stale.")

ARBITER_SENTENCE = ("Run this as a follow-up assessment: admit disputes tied to changes since the base or to an explicitly "
                    "identified material coverage gap or contradiction, require a specific reason for repeating a covered "
                    "factual check, merge duplicate disputes at registration, and apply the same closure standard to both "
                    "sides without treating inherited evidence as a prior verdict or proof of present applicability.")


def load_cfg(root: Path) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    p = Path(root) / "config" / "delta.yaml"
    try:
        import yaml
        got = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        if isinstance(got, dict):
            cfg.update({k: v for k, v in got.items() if v is not None})
    except Exception:
        pass
    return cfg


def _ts_dt(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(ts), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def check_base(symbol: str, base_ts: str, new_ts: str, root: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{ok, why, depth, base_state}: the machine's eligibility (the dispatcher chooses delta vs full; this only refuses
    what cannot be a delta)."""
    root = Path(root)
    d = root / "data" / "store" / symbol / str(base_ts)
    sp = d / "_session.json"
    if not re.match(r"^\d{8}T\d{6}Z$", str(base_ts)) or not sp.exists():
        return {"ok": False, "why": f"base session {symbol}/{base_ts} not found", "depth": 0, "base_state": {}}
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "why": f"base session state unreadable ({type(exc).__name__})", "depth": 0, "base_state": {}}
    if st.get("status") != "done":
        return {"ok": False, "why": f"base session status is {st.get('status')!r}, not done", "depth": 0, "base_state": st}
    b, n = _ts_dt(base_ts), _ts_dt(new_ts)
    if b is None or n is None or not b < n:
        return {"ok": False, "why": f"base {base_ts} is not earlier than the new session {new_ts}", "depth": 0, "base_state": st}
    age = (n - b).total_seconds() / 86400.0
    if age > float(cfg.get("max_base_age_days", 7)):
        return {"ok": False, "why": f"base is {age:.1f} days old, above max_base_age_days {cfg.get('max_base_age_days')}", "depth": 0, "base_state": st}
    depth = int(((st.get("lineage") or {}).get("depth") or 0)) + 1
    if depth > int(cfg.get("max_chain", 3)):
        return {"ok": False, "why": f"chain depth {depth} exceeds max_chain {cfg.get('max_chain')}: a fresh full session is required", "depth": depth, "base_state": st}
    return {"ok": True, "why": None, "depth": depth, "base_state": st}


def select_importable(base_store, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows + objects of the base store that pass the import rule, in archive order."""
    kinds = set(cfg.get("import_kinds") or DEFAULTS["import_kinds"])
    status = set(cfg.get("import_status") or DEFAULTS["import_status"])
    out = []
    for r in base_store.rows():
        if r.get("kind") != "evidence":
            continue
        try:
            rec = base_store.obj(r["id"])
        except Exception:
            continue
        m = rec.get("meta") or {}
        if str(m.get("kind")) not in kinds or str(m.get("status")) not in status:
            continue
        if not isinstance(rec.get("content"), dict):
            continue
        out.append(rec)
    return out


def import_evidence(session_store, base_store, base_ts: str, cfg: Dict[str, Any], by: str = "Arbiter") -> List[Dict[str, Any]]:
    """Mint the importable base objects into the new store as the arbiter's evidence (round 1). Returns summaries."""
    done = []
    for rec in select_importable(base_store, cfg):
        c = dict(rec["content"])
        m = dict(rec.get("meta") or {})
        origin = {"session": base_ts, "id": rec["id"], "by": rec.get("by"), "archived_at": rec.get("ts"),
                  "verified_scope": m.get("verified_scope")}
        c["reused_from"] = origin
        c["question"] = c.get("question") or (m.get("request") or {}).get("question") or ""
        # chained follow-ups (GPT review 11-06): the verification stamps travel from the first session, never reset
        meta = {"status": m.get("status"), "kind": m.get("kind"), "reused": True,
                "origin_session": base_ts, "origin_id": rec["id"], "origin_by": rec.get("by"),
                "source_as_of": m.get("source_as_of") or rec.get("ts"), "last_verified_at": m.get("last_verified_at") or rec.get("ts"),
                "origin_chain": list(m.get("origin_chain") or []) + [base_ts]}
        for k in ("verified_scope", "request"):
            if m.get(k) is not None:
                meta[k] = m[k]
        rc = session_store.put("evidence", c, by=by, round_=1, meta=meta)
        done.append({"id": rc["id"], "origin_id": rec["id"], "origin_by": rec.get("by"), "origin_session": base_ts,
                     "archived_at": rec.get("ts"), "status": m.get("status"), "verified_scope": m.get("verified_scope"),
                     "question": c.get("question"), "simple": c.get("simple") or c.get("finding") or "",
                     "verdict": c.get("verdict"), "source": c.get("source")})
    return done


def standing_text(items: List[Dict[str, Any]], base_ts: str, lineage: Dict[str, Any]) -> str:
    """The side file the round-1 debaters Read: every standing item with its id, origin, dates, question and conclusion."""
    head = [f"# Standing evidence — follow-up session on base {base_ts}", "",
            "Facts verified in the previous session of this symbol, re-minted here as the arbiter's evidence (`A.EV…`). They are",
            "evidence, not conclusions: the previous session's ruling, scores, operation, levels and settled claims are deliberately",
            "withheld. Cite an item by its id as you would any outcall result. Re-judge its applicability at today's price and",
            "date by reasoning first; re-request only when the fact's freshness, verified scope or source shows a specific gap.",
            "`verified_scope` says what was actually verified; an old `supports` or claim number does not carry over to this",
            "session's claims.", "",
            f"Why this follow-up: {lineage.get('why') or '(not stated)'}",
            f"Price since the base: {lineage.get('price_move') or '(not computed)'}", "", STANDING_SENTENCE, "", CLAIM_RULE, ""]
    if not items:
        head.append("(no importable evidence in the base session: every outcall must be made afresh)")
    for it in items:
        head += [f"## {it['id']}  (was {it['origin_by']} {it['origin_id']} at {it['archived_at']}; status {it['status']})",
                 f"question: {it.get('question') or ''}",
                 f"verified_scope: {it.get('verified_scope') or ''}",
                 f"conclusion: {it.get('simple') or ''}",
                 f"verdict: {it.get('verdict') or ''}",
                 f"source: {', '.join(it['source']) if isinstance(it.get('source'), list) else (it.get('source') or '')}", ""]
    return "\n".join(head)


def focus_line(lineage: Dict[str, Any]) -> str:
    return (f"(follow-up assessment on base {lineage.get('base_as_of')}: {lineage.get('why') or 'no reason stated'}; "
            f"price since the base {lineage.get('price_move') or 'n/a'}; the standing evidence in the material is previously "
            f"verified fact, reused without routine re-verification; inquiry is focused on changes since the base and their "
            f"affected premises; independent judgment on each side, no prior ruling inherited)")


def price_move_text(base_ref: Any, new_ref: Any, atr_pct: Any) -> Optional[str]:
    try:
        b, n = float(base_ref), float(new_ref)
        pct = (n / b - 1.0) * 100.0
        s = f"{b:g} → {n:g} ({pct:+.2f}%)"
        if atr_pct:
            s += f", {((n - b) / (b * float(atr_pct) / 100.0)):+.2f} ATR"
        return s
    except (TypeError, ValueError, ZeroDivisionError):
        return None
