#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scenario_replay.py — v3.4 scenario ops, update verification, registered effects, deterministic replay, mixture.

Contracts C1 (op grammar), C2 (claims, effects, freezing), C3 (updates, `applies`), C6.1-6.3 (settlements, Z′ / R′
replay, A = w·Z′ + (1−w)·R′ and its envelope). Pure functions over dicts; no store, no file access.

Records (`entries`) = [{"seq", "id": "R2.Z", "role": "zealot"|"reaper", "round", "content": <round JSON>}] in manifest
order. Registry = {claim_id: {"true": ops, "false": ops, "owner": "Z"|"R", "claim_rev": "R1.Z"}}.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds_model as om  # noqa: E402

P_TOL = float(om.DEFAULT_CONFIG["p_tolerance"])
_DROP_KEYS = ("derived", "mixture", "revisions")
_SETTLING = {"settled_for": "true", "settled_against": "false", "conceded": "false"}
_SIDE = {"Z": "Z", "R": "R", "zealot": "Z", "reaper": "R"}


class OpError(Exception):
    """An op list failed: `index` of the failing op and its `reason`."""

    def __init__(self, index: int, reason: str):
        super().__init__(f"op[{index}]: {reason}")
        self.index = index
        self.reason = reason


# ----------------------------------------------------------------------------------------------------------------
# Canonical form
# ----------------------------------------------------------------------------------------------------------------

def _canon_obj(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _canon_obj(v) for k, v in x.items() if k not in _DROP_KEYS}
    if isinstance(x, list):
        return [_canon_obj(v) for v in x]
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        r = round(float(x), 6)
        return 0.0 if r == 0 else r
    return x


def canonical(sc: Any) -> str:
    """C1 canonical serialization: numbers (int or float) as floats rounded to 6, `derived` / `mixture` / `revisions` dropped, sorted keys."""
    return json.dumps(_canon_obj(sc), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ----------------------------------------------------------------------------------------------------------------
# C1 ops
# ----------------------------------------------------------------------------------------------------------------

def _locate(sc: dict, addr: Any) -> Tuple[Optional[dict], Optional[dict]]:
    """Address `N` or `N/s` → (node, parent or None); (None, None) if missing."""
    if not isinstance(addr, str) or not addr:
        return None, None
    head, _, tail = addr.partition("/")
    top = next((n for n in sc.get("nodes") or [] if n.get("id") == head), None)
    if top is None or not tail:
        return top, None
    sub = next((s for s in top.get("sub") or [] if s.get("id") == tail), None)
    return (sub, top) if sub is not None else (None, None)


def _all_ids(sc: dict) -> set:
    ids = set()
    for n in sc.get("nodes") or []:
        ids.add(n.get("id"))
        ids.update(s.get("id") for s in n.get("sub") or [])
    return ids


def _remove_if_empty(sc: dict, nd: dict, parent: Optional[dict], key: str, tol: float) -> None:
    if float(nd[key]) <= tol:
        holder = parent["sub"] if parent is not None else sc["nodes"]
        holder.remove(nd)


def _amount(op: dict) -> float:
    a = op.get("amount")
    if not om._is_num(a) or float(a) <= 0:
        raise ValueError(f"amount must be a number > 0, got {a!r}")
    return float(a)


def _op_p_move(sc: dict, op: dict, tol: float) -> None:
    src, sp = _locate(sc, op.get("from"))
    dst, dp = _locate(sc, op.get("to"))
    for addr, nd in ((op.get("from"), src), (op.get("to"), dst)):
        if nd is None:
            raise ValueError(f"node {addr} missing")
    if src is dst:
        raise ValueError("from and to are the same node")
    if sp is not dp:
        raise ValueError(f"cross-parent move not expressible ({op['from']} → {op['to']}); restructure with split")
    key = "p_cond" if sp is not None else "p"
    amt = _amount(op)
    if amt > float(src[key]) + tol:
        raise ValueError(f"amount {amt:g} exceeds {key}({op['from']}) {float(src[key]):g}")
    amt = min(amt, float(src[key]))   # within tolerance: move exactly what the source holds
    src[key] = round(float(src[key]) - amt, 12)
    dst[key] = round(float(dst[key]) + amt, 12)
    _remove_if_empty(sc, src, sp, key, tol)


def _leaf_target(sc: dict, addr: Any) -> dict:
    nd, _ = _locate(sc, addr)
    if nd is None:
        raise ValueError(f"node {addr} missing")
    if nd.get("sub"):
        raise ValueError(f"node {addr} has sub-nodes; address a leaf as '{addr}/<sub id>'")
    return nd


def _op_P_T_set(sc: dict, op: dict) -> None:
    nd = _leaf_target(sc, op.get("node"))
    v = op.get("value")
    if not om._is_num(v) or float(v) <= 0:
        raise ValueError(f"P_T value must be a number > 0, got {v!r}")
    v, old, P0 = float(v), float(nd["P_T"]), float(sc["P0"])
    if (old - P0) * (v - P0) < 0:
        raise ValueError(f"P_T {v:g} is on the wrong side of P0 {P0:g} for node {op['node']} (was {old:g}); split instead")
    events = nd.get("events") or []
    closes = [e for e in om._sorted_events(events) if e.get("kind") == "close"]
    if closes:
        closes[-1]["price"] = v
    _check_events(op["node"], events, v)
    nd["P_T"] = v


def _check_events(label: str, events: Any, P_T: float) -> None:
    errs = om._validate_events(label, events, P_T)
    if errs:
        raise ValueError("; ".join(errs))


def _op_path_set(sc: dict, op: dict) -> None:
    nd = _leaf_target(sc, op.get("node"))
    events = copy.deepcopy(op.get("events"))
    _check_events(op["node"], events, float(nd["P_T"]))
    nd["events"] = events


def _op_split(sc: dict, op: dict, tol: float) -> None:
    src, parent = _locate(sc, op.get("node"))
    if src is None:
        raise ValueError(f"node {op.get('node')} missing")
    new = op.get("new_node")
    if not isinstance(new, dict) or not new.get("id"):
        raise ValueError("split needs new_node with an id")
    if new["id"] in _all_ids(sc):
        raise ValueError(f"new node id {new['id']} already exists")
    PT = new.get("P_T")
    if not om._is_num(PT) or float(PT) <= 0:
        raise ValueError(f"new node {new['id']}: P_T must be a number > 0")
    events = copy.deepcopy(new.get("events") or [])
    _check_events(new["id"], events, float(PT))
    key = "p_cond" if parent is not None else "p"
    amt = _amount(op)
    if amt > float(src[key]) + 1e-9:
        raise ValueError(f"amount {amt:g} exceeds {key}({op['node']}) {float(src[key]):g}")
    made = copy.deepcopy(new)
    made.update({"P_T": float(PT), "events": events, key: amt})
    if parent is None:
        made.setdefault("refs", copy.deepcopy(src.get("refs") or []))
    src[key] = round(float(src[key]) - amt, 12)
    (parent["sub"] if parent is not None else sc["nodes"]).append(made)
    _remove_if_empty(sc, src, parent, key, tol)


def _check_mass(sc: dict, tol: float) -> Optional[str]:
    total = sum(float(n.get("p", 0)) for n in sc.get("nodes") or [])
    if abs(total - 1.0) > tol:
        return f"Σ p = {total:.4f} after the ops, outside tolerance {tol} of 1"
    for n in sc.get("nodes") or []:
        if n.get("sub"):
            sp = sum(float(s.get("p_cond", 0)) for s in n["sub"])
            if abs(sp - 1.0) > tol:
                return f"node {n['id']}: Σ p_cond = {sp:.4f} after the ops, outside tolerance {tol} of 1"
    return None


def apply_ops(sc: dict, ops: List[dict], p_tolerance: float = P_TOL) -> dict:
    """Apply an op list atomically to a deep copy; raises OpError(index, reason) on the first failure."""
    out = copy.deepcopy(sc)
    for i, op in enumerate(ops or []):
        kind = op.get("op") if isinstance(op, dict) else None
        try:
            if kind == "p_move":
                _op_p_move(out, op, p_tolerance)
            elif kind == "P_T_set":
                _op_P_T_set(out, op)
            elif kind == "path_set":
                _op_path_set(out, op)
            elif kind == "split":
                _op_split(out, op, p_tolerance)
            else:
                raise ValueError(f"unknown op {kind!r}")
        except ValueError as e:
            raise OpError(i, str(e)) from None
    if ops:
        bad = _check_mass(out, p_tolerance)
        if bad:
            raise OpError(len(ops) - 1, bad)
    return out


# ----------------------------------------------------------------------------------------------------------------
# C3 verification
# ----------------------------------------------------------------------------------------------------------------

def _distribution(sc: dict) -> dict:
    """The part of a scenario the op grammar can change (execution / event are outside it)."""
    return {"P0": sc.get("P0"), "T_days": sc.get("T_days"), "nodes": sc.get("nodes") or []}


def _first_divergence(got: dict, want: dict) -> str:
    gn = {n.get("id"): n for n in got.get("nodes") or []}
    wn = {n.get("id"): n for n in want.get("nodes") or []}
    order = [n.get("id") for n in got.get("nodes") or []] + [i for i in wn if i not in gn]
    for nid in order:
        if nid not in wn:
            return f"node {nid}: present after replay, absent in the filed scenario"
        if nid not in gn:
            return f"node {nid}: in the filed scenario, absent after replay"
        if canonical(gn[nid]) != canonical(wn[nid]):
            return f"node {nid}: replay gives {canonical(gn[nid])}, filed {canonical(wn[nid])}"
    for k in ("P0", "T_days"):
        if canonical(got.get(k)) != canonical(want.get(k)):
            return f"{k}: replay gives {got.get(k)!r}, filed {want.get(k)!r}"
    return "node order differs from the replayed order"


def verify_update(prev_sc: Optional[dict], updates: List[dict], this_sc: dict,
                  p_tolerance: float = P_TOL) -> Tuple[bool, str]:
    """Replay `updates[].ops` in order onto `prev_sc` and compare with `this_sc` canonically (opening: prev_sc None)."""
    if prev_sc is None:
        return (False, "opening scenario: no scenario_updates allowed") if updates else (True, "opening")
    cur = prev_sc
    for i, u in enumerate(updates or []):
        try:
            cur = apply_ops(cur, u.get("ops") or [], p_tolerance)
        except OpError as e:
            return False, f"update[{i}] op[{e.index}]: {e.reason}"
    got, want = _distribution(cur), _distribution(this_sc)
    if canonical(got) == canonical(want):
        return True, "reproduced"
    return False, f"replayed updates do not reproduce this round's scenario — first divergence: {_first_divergence(got, want)}"


def _parse_applies(tag: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(tag, str) or tag.count("#") != 1:
        return None
    cid, outcome = tag.split("#")
    return (cid, outcome) if cid and outcome in ("true", "false") else None


def verify_applies(updates: List[dict], effects_registry: dict) -> List[str]:
    """`applies` tags must name a registered effect#outcome once, with ops equal to it; pass the side's cumulative updates."""
    fatal: List[str] = []
    seen: set = set()
    for i, u in enumerate(updates or []):
        tag = u.get("applies")
        if tag is None:
            continue
        parsed = _parse_applies(tag)
        if parsed is None:
            fatal.append(f"update[{i}]: applies {tag!r} is not '<claim id>#true|#false'"); continue
        cid, outcome = parsed
        if cid not in effects_registry:
            fatal.append(f"update[{i}]: applies {tag}: unknown claim id {cid}"); continue
        if parsed in seen:
            fatal.append(f"update[{i}]: applies {tag}: effect already claimed by an earlier update"); continue
        seen.add(parsed)
        if canonical(u.get("ops") or []) != canonical(effects_registry[cid][outcome]):
            fatal.append(f"update[{i}]: applies {tag} does not match the registered effect")
    return fatal


# ----------------------------------------------------------------------------------------------------------------
# C2 registry with freezing, C6.1 settlements
# ----------------------------------------------------------------------------------------------------------------

def _claims(rec: dict) -> List[dict]:
    return [c for c in (rec.get("content") or {}).get("claims") or [] if isinstance(c, dict) and c.get("id")]


def _effect(c: dict) -> dict:
    eff = c.get("effect") or {}
    return {"true": copy.deepcopy(eff.get("if_true") or []), "false": copy.deepcopy(eff.get("if_false") or [])}


def effects_registry_from_history(entries: List[dict], side: str) -> Tuple[Dict[str, dict], List[str]]:
    """→ (registry, fatal). Freezing per C2: first appearance fixes (text, effect); fatal reported for `side`'s records only."""
    me = _SIDE[side]
    frozen: Dict[Tuple[str, str], dict] = {}      # (owner, claim id) → {"text", "effect", "rev", "kind", "target"}
    fatal: List[str] = []
    for rec in entries:
        owner = _SIDE[rec["role"]]
        for c in _claims(rec):
            key, text, eff = (owner, c["id"]), c.get("claim"), _effect(c)
            cur = frozen.get(key)
            mine = owner == me
            if cur is None:
                frozen[key] = {"text": text, "effect": eff, "rev": rec["id"], "kind": c.get("kind"), "target": c.get("target")}
            elif text != cur["text"]:
                if c.get("revises") != cur["rev"]:
                    if mine:
                        fatal.append(f"{rec['id']}: claim {c['id']} text changed without revises: \"{cur['rev']}\"")
                    continue
                frozen[key] = {"text": text, "effect": eff, "rev": rec["id"], "kind": c.get("kind"), "target": c.get("target")}
            elif canonical(eff) != canonical(cur["effect"]) and mine:
                fatal.append(f"{rec['id']}: claim {c['id']}: effect frozen; revise the claim to change it")
    registry: Dict[str, dict] = {}
    for (owner, cid), f in frozen.items():
        if f["kind"] == "challenge":
            continue
        entry = {"true": [], "false": [], "owner": owner, "claim_rev": f["rev"]}
        if owner == me:
            entry.update(copy.deepcopy(f["effect"]))
        else:
            ch = [g for (o, _), g in frozen.items() if o == me and g["kind"] == "challenge" and g["target"] == cid]
            if ch:
                entry.update(copy.deepcopy(ch[0]["effect"]))
        registry[cid] = entry
    return registry, fatal


def review_scales(claims_review: Any) -> Dict[str, float]:
    """{claim id: scale} for the claims the ruling settled `partly` (v3.8): the claim fails in part, and each side's
    registered `if_false` effect for it lands at that share (0 < scale < 1). Anything else is not in the map."""
    out: Dict[str, float] = {}
    for rv in claims_review if isinstance(claims_review, list) else []:
        if isinstance(rv, dict) and rv.get("verdict") == "partly" and rv.get("claim"):
            try:
                s = float(rv.get("scale"))
            except (TypeError, ValueError):
                continue
            if 0.0 < s < 1.0:
                out[str(rv["claim"])] = s
    return out


def scale_ops(sc: dict, ops: List[dict], scale: float) -> List[dict]:
    """The op list at a share of its size, against the tree as it stands: probability moves and splits carry
    scale × amount, an end-price change goes scale of the way from the node's current P_T to the registered value.
    A `path_set` has no share (ValueError): a partial ruling on such an effect is written as a void plus a revision."""
    out, work = [], copy.deepcopy(sc)
    for op in ops or []:
        op2 = dict(op)
        kind = op.get("op")
        if kind in ("p_move", "split"):
            op2["amount"] = round(_amount(op) * scale, 6)
        elif kind == "P_T_set":
            node = _leaf_target(work, op.get("node"))
            op2["value"] = round(float(node["P_T"]) + scale * (float(op["value"]) - float(node["P_T"])), 6)
        else:
            raise ValueError(f"op {kind!r} cannot be applied in part")
        out.append(op2)
        work = apply_ops(work, [op2])
    return out


def settlements(entries: List[dict], claims_review: Optional[List[dict]]) -> List[Tuple[str, str, int, int]]:
    """C6.1 → [(claim_id, outcome, round, seq)] sorted by (round, seq), one entry per claim.
    Owner status (latest): settled_for → true, settled_against / conceded → false; `withdrawn` on a claim a challenge
    ever targeted counts as `conceded` (→ false; on a never-challenged claim it is a plain withdrawal). Opponent concession: `conceded` /
    `withdrawn` on a side's own CHALLENGE settles the challenge's TARGET claim "true" (both sides' registered effects of
    the target: the owner's if_true on its claim, the challenger's challenge if_true). When both channels settle a claim
    the earlier record (round, seq) fixes it, once. `claims_review` overrides the outcome (adopted → true, rejected →
    false); a reviewed claim nobody settled is appended after the last record."""
    status: Dict[str, Tuple[str, int, int]] = {}
    kind: Dict[str, Tuple[Any, Any]] = {}
    targeted: set = set()
    for rec in entries:
        for c in _claims(rec):
            st = c.get("status")
            kind[c["id"]] = (c.get("kind"), c.get("target"))
            if c.get("kind") == "challenge" and c.get("target"):
                targeted.add(str(c["target"]))
            if c["id"] not in status or status[c["id"]][0] != st:
                status[c["id"]] = (st, int(rec["round"]), int(rec["seq"]))
    best: Dict[str, Tuple[str, str, int, int]] = {}

    def offer(cid: str, outcome: str, rnd: int, seq: int) -> None:
        if cid not in best or (rnd, seq) < (best[cid][2], best[cid][3]):
            best[cid] = (cid, outcome, rnd, seq)
    for cid, (st, rnd, seq) in status.items():
        k, target = kind.get(cid, (None, None))
        if k == "challenge":
            if st in ("conceded", "withdrawn") and target:
                offer(str(target), "true", rnd, seq)   # the challenger concedes the target claim
            continue
        if st in _SETTLING:
            offer(cid, _SETTLING[st], rnd, seq)
        elif st == "withdrawn" and cid in targeted:   # withdrawing a challenged claim is a concession, not an escape
            offer(cid, "false", rnd, seq)
    out: List[Tuple[str, str, int, int]] = list(best.values())
    max_round = max((int(r["round"]) for r in entries), default=0)
    max_seq = max((int(r["seq"]) for r in entries), default=0)
    for i, rv in enumerate(claims_review or []):
        verdict = {"adopted": "true", "rejected": "false", "partly": "false"}.get(rv.get("verdict"))
        if verdict is None or not rv.get("claim"):
            continue
        idx = next((k for k, s in enumerate(out) if s[0] == rv["claim"]), None)
        if idx is not None:
            out[idx] = (rv["claim"], verdict, out[idx][2], out[idx][3])
        else:
            out.append((rv["claim"], verdict, max_round + 1, max_seq + 1 + i))
    return sorted(out, key=lambda s: (s[2], s[3]))


# ----------------------------------------------------------------------------------------------------------------
# C6.2 replay
# ----------------------------------------------------------------------------------------------------------------

def replay_side(opening_sc: dict, accepted_updates: List[dict], settlements: List[Tuple[str, str, int, int]],
                registry: dict, voided_effects: Optional[List[dict]], side: str,
                p_tolerance: float = P_TOL, scales: Optional[Dict[str, float]] = None) -> Tuple[dict, List[dict], List[str]]:
    """Z′ / R′ of `side`: accepted updates in order, then each pending settled effect once in (round, seq) order.
    → (sc′, applied, fatal). A voided entry applies to `side` if its "side" matches or it carries no "side" (the arbiter
    glue, ruling.replay_primes, requires the side). A void is honoured only when attempting the effect's ops fails a
    precondition (the op is skipped with the reason), or when it names an effect this side CARRIED through an `applies`
    update whose outcome contradicts the settlement (e.g. the side carried R2#true and R2 settled false: the void
    `{claim: R2, side, outcome: "true"}` makes the replay skip that update's ops before the settled effect applies;
    status "void_carried"). A void on an executable effect, or on an effect that is neither pending nor carried on that
    side, is FATAL "void rejected: …". A later accepted update that executes on the tree as filed but no longer
    executes once a carried update was voided (it reversed that effect, or edited a node it created) is skipped with it,
    status "void_dependent". `scales` (review_scales): a claim the ruling settled `partly` lands its `if_false` effect at
    that share on this side — whether the side already carried it whole in a round (the excess is taken back; status
    "carried_scaled") or it was still pending ("applied_scaled")."""
    me = _SIDE[side]
    sc = copy.deepcopy(opening_sc)
    as_filed: Optional[dict] = None   # the tree with every carried update applied, kept from the first void_carried on
    applied: List[dict] = []
    fatal = list(verify_applies(accepted_updates, registry))
    carried: set = set()
    outcome_of = {cid: oc for cid, oc, _, _ in settlements}
    voided = {(v.get("claim"), v.get("outcome")): v for v in voided_effects or []
              if v.get("side") is None or _SIDE.get(v.get("side")) == me}
    used: set = set()
    scales = scales or {}

    def _partial(cur: dict, ops: list, cid: str) -> list:
        return scale_ops(cur, ops, scales[cid])
    for i, u in enumerate(accepted_updates or []):
        tag = _parse_applies(u.get("applies"))
        if tag and tag[1] == "false" and tag[0] in scales and outcome_of.get(tag[0]) == "false":
            # carried whole in a round, ruled a partial failure: only the ruled share stays on the tree
            try:
                full = apply_ops(sc if as_filed is None else as_filed, u.get("ops") or [], p_tolerance)
                sc = apply_ops(sc, _partial(sc, u.get("ops") or [], tag[0]), p_tolerance)
            except (OpError, ValueError) as e:
                fatal.append(f"accepted update[{i}] ({tag[0]}#false at scale {scales[tag[0]]:g}): {getattr(e, 'reason', e)}"); continue
            as_filed = full
            carried.add(tag)
            applied.append({"effect": u.get("applies"), "status": "carried_scaled", "update": i, "scale": scales[tag[0]]})
            continue
        if tag and tag[0] in outcome_of and outcome_of[tag[0]] != tag[1]:
            # the side carried one outcome, the ruling settled the other (both sides conceded the same claim in opposite
            # directions, say): the only legal repair is a documented void of the CARRIED outcome — the update is skipped
            if tag in voided:
                used.add(tag)
                applied.append({"effect": u.get("applies"), "status": "void_carried", "update": i,
                                "reason": voided[tag].get("reason"),
                                "settled": f"{tag[0]}#{outcome_of[tag[0]]}"})
                try:
                    as_filed = apply_ops(sc if as_filed is None else as_filed, u.get("ops") or [], p_tolerance)
                except OpError:
                    pass
                continue
            fatal.append(f"update applies {tag[0]}#{tag[1]} conflicts with the settlement {tag[0]}#{outcome_of[tag[0]]} "
                         f"(void the carried effect: voided.effects {{claim: {tag[0]}, side: {side}, outcome: {tag[1]}}} "
                         f"— the replay then skips that update before the settled effect applies)")
        filed_ok = False
        if as_filed is not None:
            try:
                as_filed = apply_ops(as_filed, u.get("ops") or [], p_tolerance); filed_ok = True
            except OpError:
                pass
        try:
            sc = apply_ops(sc, u.get("ops") or [], p_tolerance)
        except OpError as e:
            if filed_ok:
                # executable on the tree as filed, not once a carried update was voided: it rested on the voided effect
                # (an inverse of it, an edit of a node it created) and goes with it — recorded, never silent
                if tag in voided:
                    used.add(tag)
                applied.append({"effect": u.get("applies"), "status": "void_dependent", "update": i,
                                "failed": f"op[{e.index}]: {e.reason}",
                                "reason": "rests on a carried effect the ruling voided"})
                continue
            fatal.append(f"accepted update[{i}] op[{e.index}]: {e.reason}"); continue
        if tag:
            carried.add(tag)
        applied.append({"effect": u.get("applies"), "status": "carried" if tag else "update", "update": i})
    for cid, oc, rnd, seq in sorted(settlements, key=lambda s: (s[2], s[3])):
        ops = (registry.get(cid) or {}).get(oc) or []
        tag = f"{cid}#{oc}"
        if (cid, oc) in carried or not ops:
            continue
        part = oc == "false" and cid in scales
        try:
            new_sc = apply_ops(sc, _partial(sc, ops, cid) if part else ops, p_tolerance)
        except ValueError as e:
            fatal.append(f"{tag} (round {rnd}, seq {seq}) at scale {scales[cid]:g}: {e}"); continue
        except OpError as e:
            if (cid, oc) in voided:   # a void skips the failed op, with the documented reason
                used.add((cid, oc))
                applied.append({"effect": tag, "status": "voided", "round": rnd, "seq": seq,
                                "failed": f"op[{e.index}]: {e.reason}", "reason": voided[(cid, oc)].get("reason")})
                continue
            fatal.append(f"{tag} (round {rnd}, seq {seq}) op[{e.index}]: {e.reason}"); continue
        if (cid, oc) in voided:
            used.add((cid, oc))
            fatal.append(f"void rejected: effect is executable ({tag} on side {me}; a void only skips an op whose "
                         f"precondition fails)")
        sc = new_sc
        applied.append(dict({"effect": tag, "status": "applied_scaled" if part else "applied", "round": rnd, "seq": seq},
                            **({"scale": scales[cid]} if part else {})))
    for (cid, oc) in sorted(set(voided) - used, key=str):
        fatal.append(f"void rejected: {cid}#{oc} is not a pending effect on side {me} (nothing to skip)")
    return sc, applied, fatal


# ----------------------------------------------------------------------------------------------------------------
# C6.3 mixture and envelope
# ----------------------------------------------------------------------------------------------------------------

def _atoms(sc: dict) -> Dict[Tuple[float, str], dict]:
    """Leaves keyed by (round(P_T, 6), canonical events); identical atoms merge (p summed, refs / labels pooled)."""
    labels = {}
    for n in sc.get("nodes") or []:
        labels[n["id"]] = n.get("label")
        for s in n.get("sub") or []:
            labels[f"{n['id']}/{s['id']}"] = s.get("label") or n.get("label")
    out: Dict[Tuple[float, str], dict] = {}
    for leaf in om.expand_leaves(sc):
        key = (round(leaf["P_T"], 6), canonical(leaf["events"]))
        a = out.setdefault(key, {"p": 0.0, "P_T": leaf["P_T"], "events": leaf["events"], "refs": set(), "labels": [], "ids": set()})
        a["p"] += leaf["p"]
        a["refs"].update(leaf["refs"])
        a["ids"].add(str(leaf.get("id")))
        if labels.get(leaf["id"]):
            a["labels"].append(labels[leaf["id"]])
    return out


def mixture(z_prime: dict, r_prime: dict, w: float, reason: Optional[str] = None) -> dict:
    """A = w·Z′ + (1−w)·R′ on the union of atoms, ids M1.. in sorted-key order; A["mixture"] carries w, t, Z′, R′."""
    for k in ("P0", "T_days"):
        if canonical(z_prime.get(k)) != canonical(r_prime.get(k)):
            raise ValueError(f"Z′ {k} {z_prime.get(k)!r} ≠ R′ {k} {r_prime.get(k)!r}")
    w = float(w)
    za, ra = _atoms(z_prime), _atoms(r_prime)
    nodes = []
    mid: Dict[Tuple[float, str], str] = {}
    for key in sorted(set(za) | set(ra)):
        zs, rs = za.get(key), ra.get(key)
        p = round(w * (zs["p"] if zs else 0.0) + (1.0 - w) * (rs["p"] if rs else 0.0), 12)
        if p <= 1e-12:
            continue
        src = [s for s in (zs, rs) if s]
        labels = [l for s in src for l in s["labels"]]
        nodes.append({"id": f"M{len(nodes) + 1}", "label": min(labels, key=lambda l: (len(l), l)) if labels else None,
                      "p": p, "P_T": src[0]["P_T"], "refs": sorted(set().union(*(s["refs"] for s in src))),
                      "events": copy.deepcopy(src[0]["events"])})
        mid[key] = nodes[-1]["id"]
    A = {"schema": z_prime.get("schema"), "T_days": z_prime.get("T_days"), "P0": z_prime["P0"], "nodes": nodes}
    ze, re_ = z_prime.get("event"), r_prime.get("event")
    if isinstance(ze, dict) and isinstance(re_, dict) and ze.get("name") and ze.get("name") == re_.get("name"):
        ev = copy.deepcopy(ze)   # the event both sides name; otherwise A carries none (contracts check their filer's)
        # 2026-11-06: folded_into named the sides' own node ids; the mixture renumbers to M1.. — remap through the atoms
        def _m_ids(atoms, ids):
            out_ids = []
            for i in ids or []:
                si = str(i)
                for key, a in atoms.items():
                    if key in mid and any(x == si or x.startswith(si + "/") for x in a.get("ids", ())) and mid[key] not in out_ids:
                        out_ids.append(mid[key])
            return out_ids
        folded = _m_ids(za, ze.get("folded_into")) + [x for x in _m_ids(ra, re_.get("folded_into")) if x not in _m_ids(za, ze.get("folded_into"))]
        if isinstance(ze.get("folded_into"), list) or isinstance(re_.get("folded_into"), list):
            ev["folded_into_sides"] = {"Z": ze.get("folded_into"), "R": re_.get("folded_into")}
            ev["folded_into"] = sorted(folded, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)
        A["event"] = ev
    A["mixture"] = {"w": w, "t": round(2 * w - 1, 12), "z_prime": copy.deepcopy(z_prime),
                    "r_prime": copy.deepcopy(r_prime), "reason": reason}
    return A


def envelope_check(A: dict, z_prime: dict, r_prime: dict, p_tolerance: float = P_TOL) -> List[str]:
    """FATAL list: an atom of A outside [min, max] of (Z′, R′) ± tol, or no single w (or not the stored w) reproduces A."""
    aa, za, ra = _atoms(A), _atoms(z_prime), _atoms(r_prime)
    keys = sorted(set(aa) | set(za) | set(ra))
    rows = [(k, aa[k]["p"] if k in aa else 0.0, za[k]["p"] if k in za else 0.0, ra[k]["p"] if k in ra else 0.0) for k in keys]
    fatal: List[str] = []
    for (pt, _), pa, pz, pr in rows:
        if not (min(pz, pr) - p_tolerance <= pa <= max(pz, pr) + p_tolerance):
            fatal.append(f"atom P_T {pt:g}: p {pa:.4f} outside [{min(pz, pr):.4f}, {max(pz, pr):.4f}] of (Z′, R′)")

    def misfit(w: float) -> List[str]:
        return [f"P_T {pt:g}: {pa:.4f} vs {w * pz + (1 - w) * pr:.4f}" for (pt, _), pa, pz, pr in rows
                if abs(w * pz + (1 - w) * pr - pa) > p_tolerance]

    (_, pa, pz, pr) = max(rows, key=lambda r: abs(r[2] - r[3])) if rows else (None, 0.0, 0.0, 0.0)
    w = (pa - pr) / (pz - pr) if abs(pz - pr) > 1e-12 else 0.5
    bad = misfit(w)
    if bad or not (-1e-9 <= w <= 1 + 1e-9):
        fatal.append(f"no single w in [0, 1] reproduces A within {p_tolerance} (solved w = {w:.4f}; misfit {bad})")
    stored = (A.get("mixture") or {}).get("w")
    if om._is_num(stored):
        bad = misfit(float(stored))
        if bad:
            fatal.append(f"stored w = {float(stored):g} does not reproduce A: {bad}")
    return fatal


def rebuild_at_half(A: dict) -> dict:
    """A at w = ½ rebuilt from A["mixture"].z_prime / r_prime (no nested mixture key)."""
    m = A["mixture"]
    half = mixture(m["z_prime"], m["r_prime"], 0.5)
    del half["mixture"]
    return half
