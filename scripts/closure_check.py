#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""closure_check.py — v3.4 closing check (contract C5): the engagement predicate and the closing-record check.

The only implementation. `run_step close-round` (live store) and `validate_debate` (release gate, rebuilt from the
assembled report) both call `check`; the two adapters `records_from_store` / `records_from_report` produce the same
record shape so the rules run once:

    record = {"seq": <manifest position, 1-based>, "id": "R2.Z" | "Z.EV3" | "R.MC1" | "NOTES.R2",
              "kind": "final" | "evidence" | "model_call" | "notes", "role": "Z" | "R" | "A", "round": <int>,
              "content": <final JSON> | <filing fields: target_claim / issue / claim> | <notes dict>}

`spawns` = `_session.json.spawns`: {"R3.R": {"view_seq", "at_seq"}, "R3.R.B2": {...}, "OBJ1.Z": ...}; a record filed
from a material saw every object with seq <= view_seq; a later-beat material counts for a final written after it
(`at_seq` < the final's seq). Pure functions over dicts; no file access except through the store adapter.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

EVIDENCE_KINDS = ("evidence", "model_call")
RECORD_KINDS = ("final", "evidence", "model_call", "notes")
_TAG = {"Zealot": "Z", "Reaper": "R", "Z": "Z", "R": "R", "zealot": "Z", "reaper": "R"}
_ROLE = {"Z": "Zealot", "R": "Reaper"}
_OPP = {"Z": {"R"}, "R": {"Z"}}
_STATUS_CHANGES = ("conceded", "withdrawn")
_INACTIVE = ("withdrawn", "conceded", "settled_against")
UNRESOLVED_RULE = ("'unresolved' is script-only (C5: the script writes it at max_rounds); the arbiter closes a claim with a "
                   "deadlocked closing record or keeps it open")
RECORD_RULE = ("a closing record {state: deadlocked, claim_rev, remaining, tried: [{evidence, engaged_by: []}], "
               "next: 'filed:<id>' | 'none: <why>'} is required (C5); a bare label is not accepted")


# ------------------------------------------------------------------------------------------------ small helpers

def round_no(x: Any) -> int:
    """Store round int / report round name ("Init" = 1, "Round k" = k+1; legacy "回合k" = k+1) / "R3" → int."""
    if isinstance(x, int) and not isinstance(x, bool):
        return x
    s = str(x or "")
    if s == "Init":
        return 1
    m = re.match(r"(?:Round |回合)(\d+)$", s)
    if m:
        return int(m.group(1)) + 1
    m = re.match(r"R?(\d+)$", s)
    return int(m.group(1)) if m else 1


def round_name(n: int) -> str:
    return "Init" if n == 1 else f"Round {n - 1}"


def _names(text: Any, token: str) -> bool:
    if not token or text is None:
        return False
    if isinstance(text, (list, tuple)):
        return any(_names(t, token) for t in text)
    return re.search(rf"(?<![A-Za-z0-9.]){re.escape(str(token))}(?!\.?[A-Za-z0-9])", str(text)) is not None


def _view_seq(spawns: Optional[dict], rid: str) -> int:
    v = (spawns or {}).get(rid)
    if isinstance(v, dict):
        v = v.get("view_seq")
    return v if isinstance(v, int) and not isinstance(v, bool) else -1


def _access(spawns: Optional[dict], r: dict) -> int:
    """What a final had access to: its round's spawn material (`R3.R`) or any later-beat material of the same round/role
    (`R3.R.B2`) written before the final (`at_seq` < the final's seq); the highest view_seq wins (same thread)."""
    best = _view_seq(spawns, r["id"])
    prefix = r["id"] + ".B"
    for k, v in (spawns or {}).items():
        if str(k).startswith(prefix) and isinstance(v, dict) and isinstance(v.get("at_seq"), int) \
                and v["at_seq"] < r["seq"]:
            best = max(best, _view_seq(spawns, k))
    return best


def _claims(rec: dict) -> List[dict]:
    c = rec.get("content") if isinstance(rec.get("content"), dict) else {}
    return [p for p in (c.get("claims") or []) if isinstance(p, dict) and p.get("id")]


def claims_latest(records: Iterable[dict]) -> Dict[str, dict]:
    """claim id → latest {owner, kind, target, status, claim} across the finals, in record order."""
    out: Dict[str, dict] = {}
    for r in records:
        if r.get("kind") != "final":
            continue
        for p in _claims(r):
            out[str(p["id"])] = {"owner": r["role"], "kind": p.get("kind"), "target": p.get("target"),
                                 "status": p.get("status"), "claim": p.get("claim")}
    return out


def claim_revs(records: Iterable[dict]) -> Dict[str, str]:
    """claim id → claim_rev (C2): the store id of the final where the claim's current text first appeared.
    Computed by scenario_replay's registry (freezing + `revises`); challenges are passed through as own claims so they
    get a revision too (the registry itself only keeps non-challenge claims)."""
    from scenario_replay import effects_registry_from_history
    entries = []
    for r in records:
        if r.get("kind") != "final" or r.get("role") not in _OPP:
            continue
        entries.append({"seq": r.get("seq"), "id": r["id"], "role": r["role"], "round": r.get("round"),
                        "content": {"claims": [dict(p, kind="own") for p in _claims(r)]}})
    registry, _ = effects_registry_from_history(entries, "Z")
    return {cid: e["claim_rev"] for cid, e in registry.items()}


def entries_from_records(records: Iterable[dict]) -> List[dict]:
    """Finals → ledger_tally entries {round (name), role, content}."""
    return [{"round": round_name(round_no(r.get("round"))), "role": _ROLE[r["role"]], "content": r["content"]}
            for r in records if r.get("kind") == "final" and r.get("role") in _ROLE]


def targets(ev: dict, cid: str, info: Dict[str, dict]) -> bool:
    """E targets C iff its filing (target_claim / issue / claim) names C or C's target."""
    c = ev.get("content") if isinstance(ev.get("content"), dict) else {}
    fields = [c.get(k) for k in ("target_claim", "issue", "claim") if c.get(k)]
    tgt = (info.get(cid) or {}).get("target")
    return any(_names(f, cid) or (tgt and _names(f, tgt)) for f in fields)


def _related(cid: str, info: Dict[str, dict]) -> set:
    tgt = (info.get(cid) or {}).get("target")
    return {cid} | ({str(tgt)} if tgt else set())


# ------------------------------------------------------------------------------------------------ shapes

def shape_errors(terminal_open: Any) -> List[str]:
    """Structural errors in notes.terminal_open (the arbiter's closing records)."""
    if terminal_open in (None, {}):
        return []
    if not isinstance(terminal_open, dict):
        return [f"notes.terminal_open must be an object of closing records keyed by claim id: {RECORD_RULE}"]
    errs = []
    for cid, v in terminal_open.items():
        label = v if isinstance(v, str) else (v.get("state") if isinstance(v, dict) else None)
        if label == "unresolved":
            errs.append(f"terminal_open[{cid}]: {UNRESOLVED_RULE}")
        elif not isinstance(v, dict):
            errs.append(f"terminal_open[{cid}]: {RECORD_RULE}")
        elif v.get("state") != "deadlocked":
            errs.append(f"terminal_open[{cid}]: state must be 'deadlocked' (got {v.get('state')!r}); {RECORD_RULE}")
    return errs


def disposition_errors(dispositions: Any) -> List[str]:
    """notes.dispositions = [{claim, claim_rev, evidence: [ids], ruling}] (C5)."""
    if dispositions in (None, []):
        return []
    if not isinstance(dispositions, list):
        return ["notes.dispositions must be a list of {claim, claim_rev, evidence: [ids], ruling}"]
    errs = []
    for i, d in enumerate(dispositions):
        if not isinstance(d, dict) or not d.get("claim") or not d.get("claim_rev") \
                or not isinstance(d.get("evidence"), list) or not d["evidence"] or not str(d.get("ruling") or "").strip():
            errs.append(f"notes.dispositions[{i}]: needs claim, claim_rev, evidence [ids] (non-empty) and ruling")
    return errs


# ------------------------------------------------------------------------------------------------ predicate

def _final_engages(r: dict, E: str, C: str, rel: set, prev: Dict[tuple, dict]) -> List[str]:
    """→ the claim ids (C or a related id) this final engages E on; [] = none."""
    content = r.get("content") if isinstance(r.get("content"), dict) else {}
    hits: List[str] = []
    for p in _claims(r):
        answers = p.get("answers") if isinstance(p.get("answers"), list) else []
        if E not in answers:
            continue
        if p.get("kind") == "challenge" and str(p.get("target") or "") == C:
            hits.append(C)                                                  # challenge on C answering E
        pid = str(p["id"])
        before = prev.get((r["role"], pid))
        if pid in rel and before is not None:                               # status change on its own claim
            if (p.get("status") in _STATUS_CHANGES and before.get("status") != p.get("status")) \
                    or p.get("claim") != before.get("claim"):
                hits.append(pid)
    for u in content.get("scenario_updates") or []:
        if not isinstance(u, dict) or str(u.get("claim") or "") not in rel:
            continue
        refs = u.get("refs") if isinstance(u.get("refs"), list) else []
        if E in refs or _names(u.get("why"), E):
            hits.append(str(u["claim"]))
    return hits


def _disposes(n: dict, E: str, rel: set, revs: Dict[str, str]) -> bool:
    c = n.get("content") if isinstance(n.get("content"), dict) else {}
    for d in c.get("dispositions") or []:
        if not isinstance(d, dict) or str(d.get("claim") or "") not in rel:
            continue
        if E in (d.get("evidence") or []) and d.get("claim_rev") == revs.get(str(d["claim"])) \
                and str(d.get("ruling") or "").strip():
            return True
    return False


def _with_notes(records: List[dict], notes_history: Optional[Iterable[dict]]) -> List[dict]:
    have = {r.get("id") for r in records}
    extra = [n for n in (notes_history or []) if isinstance(n, dict) and n.get("id") not in have
             and isinstance(n.get("seq"), int)]
    if not extra:
        return list(records)
    return sorted(list(records) + [dict(n, kind="notes") for n in extra], key=lambda r: r.get("seq") or 0)


def engaged(E: str, C: str, records: List[dict], spawns: Optional[dict], notes_history: Optional[Iterable[dict]] = None,
            *, _info: Optional[dict] = None, _revs: Optional[dict] = None) -> List[str]:
    """Record ids that engage evidence E on claim C (C5): a final by the side opposite E's filer, at seq > seq(E), from a
    material with view_seq >= seq(E), carrying a challenge on C with answers ∋ E / a scenario update on C whose refs or
    why name E / a status change (conceded, withdrawn, revised) on its own claim C with answers ∋ E; or a later
    NOTES.R<n> whose dispositions name C at its current claim_rev and E. Text mentions never count."""
    recs = _with_notes(records, notes_history)
    ev = next((r for r in recs if r.get("id") == E and r.get("kind") in EVIDENCE_KINDS), None)
    if ev is None:
        return []
    info = _info if _info is not None else claims_latest(recs)
    revs = _revs if _revs is not None else claim_revs(recs)
    rel = _related(C, info)
    sides = _OPP.get(ev.get("role"), {"Z", "R"})
    s_e = ev["seq"]
    seq_of = {r.get("id"): r.get("seq") for r in recs}
    out: List[str] = []
    prev: Dict[tuple, dict] = {}
    for r in recs:
        if r.get("kind") == "final":
            hits = _final_engages(r, E, C, rel, prev) \
                if r.get("role") in sides and r["seq"] > s_e and _access(spawns, r) >= s_e else []
            # an engagement against a superseded text does not count: the engaging record must not predate the record
            # that introduced the engaged claim's current claim_rev (that record itself may answer E, e.g. a revision)
            if any(r["seq"] >= (seq_of.get(revs.get(h)) or 0) for h in hits):
                out.append(r["id"])
            for p in _claims(r):
                prev[(r["role"], str(p["id"]))] = {"status": p.get("status"), "claim": p.get("claim")}
        elif r.get("kind") == "notes" and r["seq"] > s_e and _disposes(r, E, rel, revs):
            out.append(r["id"])
    return out


# ------------------------------------------------------------------------------------------------ the check

def _current_revs(tally_result: Optional[dict], records: List[dict]) -> Dict[str, str]:
    revs = claim_revs(records)
    for side in ("zealot", "reaper"):
        for cid, rev in (((tally_result or {}).get(side) or {}).get("claim_rev") or {}).items():
            if rev:
                revs[cid] = rev
    return revs


def _record_problems(cid: str, rec: dict, recs: List[dict], spawns: Optional[dict], info: dict, revs: dict) -> List[str]:
    why: List[str] = []
    cur = revs.get(cid)
    if rec.get("claim_rev") != cur:
        why.append(f"stale claim_rev {rec.get('claim_rev')!r} (current {cur!r})")
    if not str(rec.get("remaining") or "").strip():
        why.append("remaining missing")
    tried = rec.get("tried")
    if not isinstance(tried, list) or not tried:
        why.append("tried is empty")
        tried = []
    by_id = {r.get("id"): r for r in recs}
    for t in tried:
        E = str((t or {}).get("evidence") or "") if isinstance(t, dict) else ""
        ev = by_id.get(E)
        if ev is None or ev.get("kind") not in EVIDENCE_KINDS:
            why.append(f"tried {E or '?'}: not returned (no such evidence or model-call object)")
            continue
        if not targets(ev, cid, info):
            why.append(f"tried {E}: does not target {cid}")
            continue
        eng = engaged(E, cid, recs, spawns, _info=info, _revs=revs)
        named = set(t.get("engaged_by") or []) if isinstance(t.get("engaged_by"), list) else set()
        if not (named & set(eng)) and not any(x.startswith("NOTES.") for x in eng):
            why.append(f"tried {E}: not engaged (engaged_by {sorted(named)} does not satisfy the predicate; "
                       f"qualifying records: {eng or 'none'})")
    nxt = str(rec.get("next") or "")
    if nxt.startswith("filed:"):
        why.append(f"next is {nxt}: not closable this round")
    elif not (nxt.startswith("none:") and nxt[5:].strip()):
        why.append("next must be 'filed:<id>' or 'none: <why>'")
    # an evidence-backed challenge on this claim that the claim's side has not engaged blocks deadlocked
    for x, p in info.items():
        if p.get("kind") != "challenge" or str(p.get("target") or "") != cid or p.get("status") in _INACTIVE:
            continue
        for ev in recs:
            if ev.get("kind") in EVIDENCE_KINDS and ev.get("role") == p.get("owner") and targets(ev, x, info) \
                    and not engaged(ev["id"], x, recs, spawns, _info=info, _revs=revs):
                why.append(f"challenge {x} backed by {ev['id']} not engaged")
    return why


def check(terminal_open: Any, tally_result: Optional[dict], records: List[dict], spawns: Optional[dict],
          notes_history: Optional[Iterable[dict]] = None, max_rounds: Optional[int] = None,
          round: Optional[int] = None) -> dict:  # noqa: A002 (contract name)
    """→ {ok, fatal[], open[], closable[], reasons{claim: why}, unresolved[]}.

    `open` = claims of tally_result.open_total still open after the rules (no or incomplete record, `filed:` next,
    unengaged evidence-backed challenge, stale claim_rev); `closable` = claims whose record passes; `fatal` = structural
    errors (bare labels, arbiter-authored `unresolved`); `unresolved` = what is still open when `round >= max_rounds`
    (the script writes it). ok = no fatal and nothing open (or at the cap)."""
    recs = _with_notes(list(records or []), notes_history)
    fatal = shape_errors(terminal_open)
    terminal = terminal_open if isinstance(terminal_open, dict) else {}
    info = claims_latest(recs)
    revs = _current_revs(tally_result, recs)
    open_, closable, reasons = [], [], {}
    for cid in list((tally_result or {}).get("open_total") or []):
        rec = terminal.get(cid)
        if not isinstance(rec, dict) or rec.get("state") != "deadlocked":
            open_.append(cid)
            reasons[cid] = "no closing record"
            continue
        why = _record_problems(cid, rec, recs, spawns, info, revs)
        if why:
            open_.append(cid)
            reasons[cid] = "; ".join(why)
        else:
            closable.append(cid)
    at_cap = isinstance(max_rounds, int) and isinstance(round, int) and max_rounds > 0 and round >= max_rounds
    return {"ok": not fatal and (not open_ or at_cap), "fatal": fatal, "open": open_, "closable": closable,
            "reasons": reasons, "unresolved": list(open_) if at_cap else []}


# ------------------------------------------------------------------------------------------------ adapters

def _filing(ev_kind: str, request: Any, issue: Any = None, target_claim: Any = None) -> dict:
    """Filing fields that say what an evidence object targets: search → target_claim, model call → issue,
    data → claim (if present)."""
    req = request if isinstance(request, dict) else {}
    if ev_kind == "model_call":
        out = {"issue": issue or req.get("issue")}
    elif ev_kind == "data":
        out = {"claim": req.get("claim")}
    else:
        out = {"target_claim": req.get("target_claim") or target_claim}
    out = {k: v for k, v in out.items() if v}
    out["ev_kind"] = ev_kind
    return out


def records_from_store(store) -> List[dict]:
    """Live path: the store manifest in order (seq = manifest line position) → records."""
    out = []
    for i, row in enumerate(store.rows(), 1):
        k = row.get("kind")
        if k not in RECORD_KINDS:
            continue
        rec = store.obj(row["id"])
        content = rec.get("content") if isinstance(rec.get("content"), dict) else {}
        meta = rec.get("meta") or {}
        if k == "final":
            role, body = _TAG.get(rec.get("by"), "A"), content
        elif k == "notes":
            role, body = "A", content
        else:
            role = row["id"].split(".")[0]
            if k == "model_call":
                body = _filing("model_call", meta.get("model_call_request"), meta.get("issue") or content.get("issue"))
            else:
                ek = meta.get("kind") or content.get("kind") or "search"
                body = _filing(ek, content.get("request") if ek == "data" else meta.get("request"))
        out.append({"seq": i, "id": row["id"], "kind": k, "role": role, "round": row.get("round"), "content": body})
    return out


class ClosureInputError(ValueError):
    """The report lacks what closure records are rebuilt from; release fails closed."""


_OWNER = {"Zealot": "Z", "Reaper": "R", "Arbiter": "A"}


def records_from_report(doc: dict) -> List[dict]:
    """Release path: the assembled report → records. Per round: returned evidence / model calls (in the report's
    evidence order), then finals (debate_history order), then the NOTES of that round (arbiter_log); seq follows.
    Fails closed (ClosureInputError) when an evidence item has no store id (`evidence_id` / `id` / `model_call_id`) or
    no known `requested_by` (Zealot / Reaper / Arbiter): ids and owners are never guessed. When every record carries its
    store manifest position (`seq` on debate_history entries, evidence items and arbiter_log entries, written by the
    assembler) records are ordered by it and keep it; a 3.4 report missing any of them fails closed."""
    buckets: Dict[int, Dict[str, list]] = {}

    def bucket(n: int) -> Dict[str, list]:
        return buckets.setdefault(n, {"ev": [], "fin": [], "notes": []})

    mc_req = {str(m.get("model_call_id")): m.get("request") for m in (doc.get("model_calls") or [])
              if isinstance(m, dict) and m.get("model_call_id")}
    missing_id, missing_owner = [], []
    for i, item in enumerate(doc.get("evidence") or []):
        if not isinstance(item, dict):
            continue
        n = round_no(item.get("round") or 1)
        owner = _OWNER.get(str(item.get("requested_by") or ""))
        if owner is None:
            missing_owner.append(f"evidence[{i}]")
            continue
        if not (item.get("model_call_id") or item.get("evidence_id") or item.get("id")):
            missing_id.append(f"evidence[{i}]")
            continue
        if item.get("model_call_id"):
            rid = str(item["model_call_id"])
            req = mc_req.get(rid) or item.get("request")
            bucket(n)["ev"].append({"id": rid, "kind": "model_call", "role": owner, "round": n, "_seq": item.get("seq"),
                                    "content": _filing("model_call", req, item.get("issue"))})
            continue
        rid = str(item.get("evidence_id") or item.get("id"))
        ek = "data" if item.get("kind") == "data" else "search"
        bucket(n)["ev"].append({"id": rid, "kind": "evidence", "role": owner, "round": n, "_seq": item.get("seq"),
                                "content": _filing(ek, item.get("request"), target_claim=item.get("target_claim"))})
    if missing_id:
        raise ClosureInputError(f"evidence ids missing; cannot rebuild closure records ({', '.join(missing_id[:8])})")
    if missing_owner:
        raise ClosureInputError("evidence requested_by missing or unknown; cannot rebuild closure records "
                                f"({', '.join(missing_owner[:8])})")
    for e in doc.get("debate_history") or []:
        tag = _TAG.get(str((e or {}).get("role") or ""))
        if not tag or not isinstance(e.get("content"), dict):
            continue
        n = round_no(e.get("round"))
        bucket(n)["fin"].append({"id": str(e.get("id") or f"R{n}.{tag}"), "kind": "final", "role": tag, "round": n,
                                 "_seq": e.get("seq"), "content": e["content"]})
    for a in doc.get("arbiter_log") or []:
        if not isinstance(a, dict):
            continue
        n = round_no(a.get("round"))
        bucket(n)["notes"].append({"id": f"NOTES.R{n}", "kind": "notes", "role": "A", "round": n, "_seq": a.get("seq"),
                                   "content": dict(a)})
    flat = [r for n in sorted(buckets) for part in ("ev", "fin", "notes") for r in buckets[n][part]]
    seqs = [r.pop("_seq") for r in flat]
    if flat and all(isinstance(q, int) and not isinstance(q, bool) for q in seqs):
        # the assembler carried each object's store manifest position: rebuild in archive order, keep the store seqs so
        # the session's spawns (view_seq / at_seq, store positions) compare like for like
        return [{"seq": q, **r} for q, r in sorted(zip(seqs, flat), key=lambda t: t[0])]
    if _is_ruler_doc(doc):
        missing = [r["id"] for r, q in zip(flat, seqs) if not isinstance(q, int) or isinstance(q, bool)]
        raise ClosureInputError("store seq missing on report records; cannot rebuild closure records in archive order "
                                f"({', '.join(missing[:8])})")
    return [{"seq": i, **r} for i, r in enumerate(flat, 1)]


def _is_ruler_doc(doc: dict) -> bool:
    """A ruler report (ruler_era.is_ruler_report)."""
    from ruler_era import is_ruler_report
    return is_ruler_report(doc)


def spawns_from_report(doc: dict, records: List[dict]) -> Dict[str, dict]:
    """The report's `spawns` (the assembler carries `_session.json.spawns`). A 3.4 report without them (absent or empty)
    fails closed — ClosureInputError "spawns missing; cannot establish access": what a record had access to is never
    inferred from round numbers. Pre-ruler reports keep the old inference (each round-n final's material is taken to have
    seen everything archived before round n)."""
    if isinstance(doc.get("spawns"), dict) and doc["spawns"]:
        return doc["spawns"]
    if _is_ruler_doc(doc):
        raise ClosureInputError("spawns missing; cannot establish access (a 3.4 report carries _session.json.spawns; "
                                "previous-round visibility is never inferred)")
    out = {}
    for r in records:
        if r.get("kind") == "final":
            out[r["id"]] = {"view_seq": max((x["seq"] for x in records if round_no(x.get("round")) < r["round"]),
                                            default=0)}
    return out
