#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ruling.py — v3.4 final-ruling derivation and endorsed scenarios (contracts C6 steps 1-5, C8).

One place for what the script derives at the arbiter stages (provisional / proposed / ruling): the arbiter writes
`claims_review`, `mixture {w, reason}` and optional `voided`; the script replays Z′ / R′ (scenario_replay), mixes A,
checks the envelope, builds the execution candidates from both sides' last-round contracts and derives the strategy,
`operation_*`, `composite_score = round(S_ref(A))` and `scenario.derived` (odds_model). Also: the session's odds config,
the derived score of any scenario, and each side's endorsed scenario (last-round scenario + its revision chain).

Store-facing helpers read only `store.rows()` / `store.obj(id)`; no file access otherwise.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds_model as om          # noqa: E402
import scenario_replay as sr     # noqa: E402
import lens_views as lv          # noqa: E402

AUTHORED_FORBIDDEN = ("scenario", "operation_type", "operation_target", "operation_size", "operation_execution",
                      "operation_simple", "by_lens", "lens_weights_default", "hedge_summary", "downside_protection", "observe_only")
TAG = {"Zealot": "Z", "Reaper": "R", "Z": "Z", "R": "R", "zealot": "Z", "reaper": "R"}


class RulingError(ValueError):
    """A 3.4 arbiter / objection / signoff input the script cannot derive from; nothing has been archived."""


# ------------------------------------------------------------------------------------------------ config and scores

def session_cfg(state: Optional[dict], root: Path = None) -> dict:
    """The session's ruler: the `_session.json.odds` snapshot (C4). Strict like run_step.session_cfg: every stage reads
    the snapshot written at open, never config/odds.yaml — a session without one was opened before v3.4 and raises
    RulingError. (`root` is kept for the callers' signature; it is not read.)"""
    snap = (state or {}).get("odds")
    if not isinstance(snap, dict) or not snap:
        raise RulingError("session opened before v3.4 (no odds snapshot in _session.json); cannot resume — "
                            "open a new session")
    return om.config_from_snapshot(snap)


def derived_score(sc: Any, cfg: Optional[dict] = None) -> Optional[int]:
    """C8: round(S_ref(scenario)); None when the scenario is invalid."""
    fatal, _warn, norm = om.validate_scenario(sc, cfg or om.DEFAULT_CONFIG)
    if fatal:
        return None
    return int(round(om.score(norm)["S_ref"]))


def fmt_level(x: Any) -> str:
    """A price as the plain number string operation_target takes (no exponent, no trailing zeros)."""
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


def primary_lens(symbol: Any, odds: Optional[dict], lenses: Optional[List[str]] = None) -> Optional[str]:
    """GENERAL* sessions: the lens whose price is the scenario's P0 — `odds.unit_basis.data_symbol`, else the first lens.
    None for any other symbol (and for a GENERAL session with neither)."""
    if not str(symbol or "").upper().startswith("GENERAL"):
        return None
    ub = (odds or {}).get("unit_basis") if isinstance((odds or {}).get("unit_basis"), dict) else {}
    lens = (ub or {}).get("data_symbol") or (lenses[0] if lenses else None)
    return str(lens) if lens else None


def target_string(level: Any, symbol: Any, lens: Optional[str]) -> str:
    """operation_target as the report carries it: the level, or `<lens>=<level>` for a GENERAL* session (its P0 is the
    primary lens's price, so the level is that lens's level: _derive_execution and postcheck read it per lens)."""
    if str(symbol or "").upper().startswith("GENERAL"):
        if not lens:
            raise RulingError("GENERAL session without a primary lens (odds.unit_basis.data_symbol / lenses[0]): "
                                "operation_target cannot name the lens its level belongs to")
        return f"{lens}={fmt_level(level)}"
    return fmt_level(level)


def report_lens(report: dict) -> Optional[str]:
    """primary_lens for a report: its odds snapshot, else the first reference price's symbol."""
    lenses = [str(r["symbol"]) for r in report.get("reference_prices") or []
              if isinstance(r, dict) and r.get("symbol") and str(r["symbol"]).upper() != str(report.get("symbol") or "").upper()]
    return primary_lens(report.get("symbol"), report.get("odds") if isinstance(report.get("odds"), dict) else None, lenses)


# ------------------------------------------------------------------------------------------------ store views

def final_records(store) -> List[dict]:
    """Debater finals in manifest order → scenario_replay records {seq, id, role: Z|R, round, content}."""
    out = []
    for i, row in enumerate(store.rows(), 1):
        if row.get("kind") != "final":
            continue
        rec = store.obj(row["id"])
        out.append({"seq": i, "id": row["id"], "role": TAG[rec["by"]], "round": int(row["round"]),
                    "content": rec.get("content") or {}})
    return out


def _side(finals: List[dict], tag: str) -> List[dict]:
    return [f for f in finals if f["role"] == tag]


def last_scenario(own: List[dict]) -> dict:
    """A side's last-round `scenario` ({} when none): the scenario its execution contracts were filed in."""
    sc = (own[-1].get("content") or {}).get("scenario") if own else None
    return sc if isinstance(sc, dict) else {}


def last_execution(own: List[dict]) -> dict:
    """A side's last-round `scenario.execution` ({} when the side filed none)."""
    sc = (own[-1].get("content") or {}).get("scenario") if own else None
    ex = sc.get("execution") if isinstance(sc, dict) else None
    return ex if isinstance(ex, dict) else {}


def revision_chain(store, tag: str) -> List[Tuple[str, dict]]:
    """[(OBJ record id, scenario_revision)] of one side, in manifest order (each verified at its ingestion)."""
    out = []
    for row in store.rows():
        if row.get("kind") == "objection" and TAG.get(row.get("by")) == tag:
            rev = (store.obj(row["id"]).get("content") or {}).get("scenario_revision")
            if isinstance(rev, dict):
                out.append((row["id"], rev))
    return out


def _lens_revision(rev: Any, lens: Optional[str]) -> Any:
    """A scenario_revision on one lens (its ops filtered to `lens`, the tag dropped); unchanged when `lens` is None."""
    if lens is None or not isinstance(rev, dict) or not isinstance(rev.get("ops"), list):
        return rev
    return dict(rev, ops=lv._lens_ops(rev["ops"], lens))


def endorsed(store, tag: str, new_revision: Any = None, finals: Optional[List[dict]] = None,
             p_tolerance: float = sr.P_TOL, lens: Optional[str] = None) -> Tuple[dict, str]:
    """C8 endorsed scenario of a side = last-round scenario + its objection-stage revisions (+ `new_revision`, verified
    here: its predecessor must be the current head, its ops must apply atomically). → (scenario, head record id).
    C11.5: with `lens`, the finals are projected on that lens and every revision's ops filtered to it first."""
    finals = finals if finals is not None else final_records(store)
    if lens is not None:
        try:
            finals = [lv.project_record(f, lens, [lens]) for f in finals]
        except lv.LensError as exc:
            raise RulingError(str(exc)) from exc
        new_revision = _lens_revision(new_revision, lens)
    own = _side(finals, tag)
    if not own:
        raise RulingError(f"side {tag} has no final to endorse")
    head, sc = own[-1]["id"], copy.deepcopy(own[-1]["content"].get("scenario"))
    for rid, rev in revision_chain(store, tag):
        rev = _lens_revision(rev, lens)
        sc = sr.apply_ops(sc, rev.get("ops") or [], p_tolerance)
        head = rid
    if new_revision is not None:
        if not isinstance(new_revision, dict) or not isinstance(new_revision.get("ops"), list) \
                or not str(new_revision.get("why") or "").strip():
            raise RulingError("scenario_revision must be {predecessor, ops: [...], why} (C8)")
        if new_revision.get("predecessor") != head:
            raise RulingError(f"scenario_revision.predecessor {new_revision.get('predecessor')!r} is not the side's current "
                                f"endorsed scenario {head!r} (C8: revisions accumulate from the last-round scenario)")
        try:
            sc = sr.apply_ops(sc, new_revision["ops"], p_tolerance)
        except sr.OpError as exc:
            raise RulingError(f"scenario_revision op[{exc.index}]: {exc.reason}") from exc
    return sc, head


# ------------------------------------------------------------------------------------------------ C6 derivation

def _authored_checks(ruling: Any) -> Tuple[float, Optional[str], list]:
    if not isinstance(ruling, dict):
        raise RulingError("arbiter input must be a JSON object")
    authored = [k for k in AUTHORED_FORBIDDEN if k in ruling]
    if authored:
        raise RulingError(f"derived field authored: {', '.join(authored)} (3.4: the script derives scenario / operation_* "
                            f"(operation_execution included) / composite_score from claims_review, mixture and voided; remove them)")
    mix = ruling.get("mixture")
    w = mixture_weight(mix)
    if w is None:
        raise RulingError(W_ERROR)
    reason = mix.get("reason")   # its content is checked after the replay, by mixture_errors (shared with the gate)
    cr = ruling.get("claims_review")
    if not isinstance(cr, list):
        raise RulingError("claims_review missing (a list; may be empty)")
    for j, c in enumerate(cr):
        if not isinstance(c, dict) or not c.get("claim") or c.get("verdict") not in ("adopted", "rejected", "partly"):
            raise RulingError(f"claims_review[{j}] needs claim (id) and verdict adopted|partly|rejected")
        if c.get("verdict") == "partly":
            try:
                ok = 0.0 < float(c.get("scale")) < 1.0
            except (TypeError, ValueError):
                ok = False
            if not ok or not str(c.get("reason") or "").strip():
                raise RulingError(f"claims_review[{j}] ({c.get('claim')}): `partly` needs `scale` (0 < scale < 1: the share of "
                                  f"the registered if_false effect that lands, on both sides) and a `reason` naming what was "
                                  f"hit — the fact, the size, or one part of a compound claim — and what still stands")
    voided = ruling.get("voided") or {}
    if not isinstance(voided, dict):
        raise RulingError("voided must be {effects: [...], candidates: [...]}")
    for v in voided.get("effects") or []:
        if not isinstance(v, dict) or not v.get("claim") or str(v.get("outcome")) not in ("true", "false") \
                or not str(v.get("reason") or "").strip():
            raise RulingError("voided.effects[] items need claim, side, outcome true|false and reason")
    return float(w), reason, norm_voided_effects(voided)


W_ERROR = "mixture.w missing or outside [0, 1] (C6: the arbiter writes mixture {w, reason})"
NO_DISCARD = "no decision-relevant atoms discarded"
_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")


def mixture_weight(mix: Any) -> Optional[float]:
    """mixture.w as a float in [0, 1], else None."""
    w = mix.get("w") if isinstance(mix, dict) else None
    return float(w) if om._is_num(w) and 0.0 <= float(w) <= 1.0 else None


def _tokens(text: str) -> set:
    """Whole-token ids of a free text: runs of [A-Za-z0-9_.-], trailing '.' / '-' dropped ("R2." → R2, "Z1#true" → Z1)."""
    return {t.rstrip(".-") for t in _TOKEN.findall(text)} - {""}


def discarded_claims(w: float, applied: Dict[str, list]) -> List[str]:
    """At an endpoint the discarded side's prime is R′ (w = 1) or Z′ (w = 0); → the claims whose settled effect was applied
    (or carried by an `applies` update) in that prime, i.e. the atom-bearing material the weight throws away."""
    side = "R" if w == 1.0 else "Z"
    out: List[str] = []
    for a in (applied or {}).get(side) or []:
        if a.get("status") not in ("applied", "carried"):
            continue
        tag = sr._parse_applies(a.get("effect"))
        if tag and tag[0] not in out:
            out.append(tag[0])
    return out


def mixture_errors(mixture: Any, finals: List[dict], applied: Dict[str, list], evidence_ids: Any) -> List[str]:
    """C6 mixture checks, one implementation for the live ruling (derive_ruling) and the release gate (validate_debate):
    `w` ∈ [0, 1]; when |2w−1| > 0.5 `reason` must name (as a whole token) a claim id filed in this session AND a returned
    evidence / model-call id of this session; at w ∈ {0, 1} it must also name every claim whose effect was applied in the
    discarded side's prime (`applied` = replay_primes' second value), or, when there is none, carry the phrase
    "no decision-relevant atoms discarded". → fatal list."""
    w = mixture_weight(mixture)
    if w is None:
        return [W_ERROR]
    if abs(2 * w - 1) <= 0.5:
        return []
    reason = mixture.get("reason")
    text = reason if isinstance(reason, str) else ""
    toks = _tokens(text)
    claim_ids = {str(c["id"]) for f in finals or [] for c in (f.get("content") or {}).get("claims") or []
                 if isinstance(c, dict) and c.get("id")}
    ev_ids = {str(e) for e in evidence_ids or []}
    out: List[str] = []
    if not (toks & claim_ids) or not (toks & ev_ids):
        out.append("mixture.reason: must cite a claim id and an evidence id (|2w−1| > 0.5: name the disputed claim ids "
                   "of this session and the returned evidence / model-call id behind the tilt)")
    if w in (0.0, 1.0):
        gone = discarded_claims(w, applied)
        missing = [c for c in gone if c not in toks]
        if missing:
            out.append(f"mixture.reason: must explain each discarded atom-bearing claim: {', '.join(missing)}")
        elif not gone and NO_DISCARD not in text.lower():
            out.append(f"mixture.reason: must explain each discarded atom-bearing claim: none was applied on the discarded "
                       f"side — write \"{NO_DISCARD}\"")
    return out


def evidence_ids_from_store(store) -> List[str]:
    """The session's returned evidence / model-call ids (store manifest)."""
    return [row["id"] for row in store.rows() if row.get("kind") in ("evidence", "model_call")]


def evidence_ids_from_report(doc: dict) -> List[str]:
    """The report's evidence / model-call ids (the assembler carries the store ids)."""
    out = [str(e.get("model_call_id") or e.get("evidence_id") or e.get("id")) for e in doc.get("evidence") or []
           if isinstance(e, dict) and (e.get("model_call_id") or e.get("evidence_id") or e.get("id"))]
    return out + [str(m["model_call_id"]) for m in doc.get("model_calls") or [] if isinstance(m, dict) and m.get("model_call_id")]


def norm_voided_effects(voided: Any) -> List[dict]:
    """voided.effects as replay_side reads them: outcome as "true" / "false", side as Z / R (an unknown side kept as written)."""
    voided = voided if isinstance(voided, dict) else {}
    return [dict(v, outcome=str(v.get("outcome")), side=TAG.get(v.get("side"), v.get("side")))
            for v in voided.get("effects") or [] if isinstance(v, dict)]


def claims_review_errors(finals: List[dict], claims_review: Any) -> List[str]:
    """claims_review against the claim registry (live derivation and release gate): one entry per claim, each naming a
    non-challenge claim of the session (registered effects are keyed by the target claim, so a verdict on a challenge or
    on an unknown id would settle nothing) at its current claim_rev."""
    registry, _ = sr.effects_registry_from_history(finals, "Z")   # every non-challenge claim, either owner, with claim_rev
    challenge_of: Dict[str, Any] = {}
    for f in finals:
        for c in (f.get("content") or {}).get("claims") or []:
            if isinstance(c, dict) and c.get("id") and c.get("kind") == "challenge":
                challenge_of[str(c["id"])] = c.get("target")
    out: List[str] = []
    seen: Dict[str, int] = {}
    for j, c in enumerate(claims_review if isinstance(claims_review, list) else []):
        cid = c.get("claim") if isinstance(c, dict) else None
        if cid is None:
            continue
        if cid in seen:   # one entry per claim: a second verdict would make the settlement order-dependent
            out.append(f"duplicate claims_review entry for {cid} (claims_review[{seen[cid]}] and claims_review[{j}]); "
                       f"keep one verdict per claim")
            continue
        seen[cid] = j
        if cid not in registry and cid in challenge_of:
            out.append(f"claims_review[{j}]: {cid} is a challenge — rule on the target claim {challenge_of[cid]}; "
                       f"a challenge's verdict is its target's")
        elif cid not in registry:
            out.append(f"claims_review[{j}]: {cid} is not a claim of this session (rule on a filed, non-challenge claim id)")
        elif c.get("claim_rev") != registry[cid]["claim_rev"]:
            out.append(f"claims_review[{j}]: claim_rev {c.get('claim_rev')!r} is not {cid}'s current rev "
                       f"{registry[cid]['claim_rev']!r} (rule on the claim as it now stands)")
    return out


def replay_primes(finals: List[dict], claims_review: Any, voided_effects: List[dict],
                  revisions: Optional[Dict[str, List[Tuple[str, dict]]]], tol: float
                  ) -> Tuple[Dict[str, dict], Dict[str, list], List[str]]:
    """C6.2, the one replay glue (derive_ruling and the release gate): per side the frozen opening + its accepted
    updates (+ its objection-stage revisions in filing order, when `revisions` is given) + the registered effects of the
    claims settled under `claims_review` / `voided_effects`. `finals` = store records in manifest (seq) order.
    → (primes {Z, R}, applied {Z, R}, fatal ["Z: …" registry, "Z′: …" replay])."""
    finals = sorted(finals, key=lambda f: f["seq"])
    setts = sr.settlements(finals, claims_review if isinstance(claims_review, list) else [])
    primes: Dict[str, dict] = {}
    applied: Dict[str, list] = {}
    fatal: List[str] = []
    known, _ = sr.effects_registry_from_history(finals, "Z")   # every non-challenge claim of the session
    for j, v in enumerate(voided_effects or []):
        if v.get("side") not in ("Z", "R"):
            fatal.append(f"voided.effects[{j}]: side {v.get('side')!r} must be Z or R (the side whose replay fails)")
        if v.get("claim") not in known:
            fatal.append(f"voided.effects[{j}]: claim {v.get('claim')!r} is not a registered claim of this session")
    voided_effects = [v for v in voided_effects or [] if v.get("side") in ("Z", "R")]
    for tag in ("Z", "R"):
        own = _side(finals, tag)
        if not own:
            fatal.append(f"{tag}: side has no final")
            continue
        opening = own[0]["content"].get("scenario")
        if not isinstance(opening, dict):
            fatal.append(f"{tag}: opening scenario missing")
            continue
        registry, reg_fatal = sr.effects_registry_from_history(finals, tag)
        fatal += [f"{tag}: {f}" for f in reg_fatal]
        updates = [u for f in own for u in (f["content"].get("scenario_updates") or []) if isinstance(u, dict)]
        for rid, rev in (revisions or {}).get(tag) or []:
            updates.append({"claim": None, "applies": None, "ops": rev.get("ops") or [], "why": rev.get("why"), "revision": rid})
        sc, app, f = sr.replay_side(opening, updates, setts, registry, voided_effects, tag, tol,
                                    sr.review_scales(claims_review))
        fatal += [f"{tag}′: {x}" for x in f]
        primes[tag], applied[tag] = sc, app
    return primes, applied, fatal


def operation_fields(d: dict, symbol: Any, lens: Optional[str]) -> dict:
    """The report fields a derivation fixes (C7): operation_type / operation_target (`<lens>=<level>` for GENERAL) /
    operation_size / operation_execution (conditional only) / composite_score = round(S_ref(A))."""
    out = {"operation_type": d["operation_type"], "operation_target": target_string(d["operation_target"], symbol, lens),
           "operation_size": d["operation_size"], "composite_score": int(round(d["score"]["S_ref"]))}
    if d.get("operation_execution"):
        out["operation_execution"] = d["operation_execution"]
    return out


def simple_fields(d: dict, symbol: Any, lens: Optional[str]) -> dict:
    """`operation_simple`: the simple card (odds_model.derive_simple — neutral / now / one resting limit order, no stop)
    on the same distribution, in the report's vocabulary. ENTRY follows the order: `market` → MARKET_ENTRY, `limit` →
    LIMIT_ENTRY (a buy below the current price; breakout orders are not in the simple card). An alternative shown next
    to the full card, never replacing it and never chained with it."""
    s = d["simple"]
    op = s["operation_type"]
    if op == "ENTRY":
        op = "LIMIT_ENTRY" if s["order"] == "limit" else "MARKET_ENTRY"
    return {"operation_type": op, "operation_target": target_string(s["operation_target"], symbol, lens),
            "operation_size": s["operation_size"], "order": s["order"], "q_star": s["q_star"], "x_star": s["x_star"],
            "display": s["display"], "price": s["operation_target"]}


def simple_summary(by_lens: dict, lenses: List[str]) -> dict:
    """A lens-wise report's top-level `operation_simple`: the primary lens's card with every lens's level joined (the
    same convention as the full card's top level) — a summary; each lens's own card is by_lens[L].operation_simple."""
    prim = by_lens[lenses[0]]["operation_simple"]
    return {**prim, "operation_target": lv.join_targets({L: by_lens[L]["operation_simple"] for L in lenses}, lenses),
            "primary_lens": lenses[0]}


def _redraft_changed(store, ruling: dict) -> bool:
    """C8: an objection-stage revision enters the replay when the redraft's claims_review or mixture differs from PROPOSED."""
    if "PROPOSED" not in store.ids():
        return False
    prop = store.obj("PROPOSED").get("content") or {}
    key = lambda d: json.dumps([d.get("claims_review"), d.get("mixture")], sort_keys=True, ensure_ascii=False)  # noqa: E731
    return key(prop) != key(ruling)


PROTECTION_SUMMARIES = {
    "quiet": "Downside risk is contained. No hedge signal.",
    "emerging": "Downside risk is building. Initiate protection.",
    "persistent": "Downside risk persists. Maintain protection.",
    "easing": "This downside risk has subsided. Remove its hedge.",
    "insufficient": "Downside phase undetermined: insufficient evidence.",
}


def project_protection(record, scopes, required=False, primary=None):
    """Project a neutral assessment onto each supplied frozen symbol or lens."""
    eligible = {symbol for symbol, target in scopes.items() if not target.get("observe_only")}
    if "protection_by_symbol" not in record:
        if eligible and required:
            raise RulingError("protection_by_symbol missing: assess each investable scope, or explain insufficient evidence")
        return
    assessments = record.pop("protection_by_symbol")
    if not isinstance(assessments, dict) or assessments.keys() != eligible:
        raise RulingError("protection_by_symbol must cover exactly the investable frozen symbols")
    for symbol, assessment in assessments.items():
        target = scopes[symbol]
        if not isinstance(assessment, dict):
            raise RulingError(f"{symbol}: protection requires an assessment")
        if set(assessment) != {"phase", "basis", "review"}:
            raise RulingError(f"{symbol}: protection requires exactly phase, basis and review")
        phase = assessment["phase"]
        if not isinstance(phase, str) or phase not in PROTECTION_SUMMARIES:
            raise RulingError(f"{symbol}: unknown protection phase")
        for key, limit in (("basis", 500), ("review", 240)):
            value = assessment[key]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise RulingError(f"{symbol}: protection {key} requires 1 to {limit} characters")
        target["downside_protection"] = assessment
        target["hedge_summary"] = PROTECTION_SUMMARIES[phase]
    if primary is not None and "hedge_summary" in primary:
        record["downside_protection"] = copy.deepcopy(primary["downside_protection"])
        record["hedge_summary"] = primary["hedge_summary"]


def derive_ruling(store, session, ruling_json: dict, stage: str = "ruling") -> dict:
    """C6 steps 1-5 for provisional / proposed / ruling. → the record to archive: the arbiter's fields + `scenario` (= A
    with `mixture` and `derived`), `operation_type`, `operation_target`, `operation_size`, `operation_execution` (when
    conditional) and `composite_score = round(S_ref(A))`; an authored composite_score is kept as `authored_score`.
    Raises RulingError naming the rule; nothing is archived by this function. A multi-lens session (C11.5) is derived
    lens by lens by derive_ruling_lenswise."""
    lenses = lv.lenses_of(getattr(session, "state", None) or {})
    if lenses:
        return derive_ruling_lenswise(store, session, ruling_json, stage, lenses)
    w, reason, voided_effects = _authored_checks(ruling_json)
    state = getattr(session, "state", None) or {}
    cfg = session_cfg(state)
    tol = float(cfg.get("p_tolerance", sr.P_TOL))
    finals = final_records(store)
    sides = {t: _side(finals, t) for t in ("Z", "R")}
    if not sides["Z"] or not sides["R"]:
        raise RulingError("both sides need at least one final before a ruling")
    cr_err = claims_review_errors(finals, ruling_json.get("claims_review"))
    if cr_err:
        raise RulingError("claims_review: " + "; ".join(cr_err))
    with_revisions = stage == "ruling" and _redraft_changed(store, ruling_json)
    revisions = {t: revision_chain(store, t) for t in ("Z", "R")} if with_revisions else None
    primes, applied, fatal = replay_primes(finals, ruling_json.get("claims_review"), voided_effects, revisions, tol)
    if fatal:
        raise RulingError("replay failed (C6.2): " + "; ".join(fatal)
                            + " — void the effect in voided.effects with a reason (a carried effect that contradicts the "
                              "settlement: void the carried outcome on that side), or correct claims_review")
    mix_err = mixture_errors(ruling_json.get("mixture"), finals, applied, evidence_ids_from_store(store))
    if mix_err:
        raise RulingError("; ".join(mix_err))
    replay: Dict[str, Any] = {"revisions_included": with_revisions, **applied}
    try:
        A = sr.mixture(primes["Z"], primes["R"], w, reason)
    except ValueError as exc:
        raise RulingError(f"mixture: {exc}") from exc
    env = sr.envelope_check(A, primes["Z"], primes["R"], tol)
    if env:
        raise RulingError("envelope (C6.3): " + "; ".join(env))
    voided = ruling_json.get("voided") or {}
    warn: List[str] = []
    cands = om.build_candidates(last_execution(sides["Z"]), last_execution(sides["R"]), A,
                                voided.get("candidates") or [],
                                rounds={t: sides[t][-1]["round"] for t in ("Z", "R")}, warn=warn,
                                filers={t: last_scenario(sides[t]) for t in ("Z", "R")},
                                atr_abs=om.atr_abs_of(cfg, A.get("P0")))
    atr = session._atr_pct() if hasattr(session, "_atr_pct") else None
    d = om.derive_for_report(A, cands, cfg, atr_pct=atr)
    if not d.get("ok"):
        raise RulingError("derivation failed: " + "; ".join(d.get("fatal") or []))
    symbol = getattr(session, "symbol", None) or state.get("symbol")
    fields = operation_fields(d, symbol, primary_lens(symbol, state.get("odds"), state.get("lenses")))
    record = copy.deepcopy(ruling_json)
    record.pop("operation_execution", None)   # derived exclusively (refused above when authored)
    if "composite_score" in record:
        record["authored_score"] = record.pop("composite_score")
    A["derived"] = dict(d, s_ref=d["score"]["S_ref"], unit_basis=cfg.get("unit_basis"), warn=list(d.get("warn") or []) + warn,
                        replay=replay, stage=stage)
    record["scenario"] = A
    record.update(fields)
    record["operation_simple"] = simple_fields(d, symbol, primary_lens(symbol, state.get("odds"), state.get("lenses")))
    scopes = {} if (state.get("odds") or {}).get("observe_only") else {symbol: record}
    project_protection(record, scopes, state.get("protection_required") and stage in ("proposed", "ruling"))
    return record


def _lens_voided(items: Any, lens: str, records: Any = None) -> List[dict]:
    """`voided` entries of one lens (C11.4), the tag dropped: those naming this lens, and the unscoped ones — for effect
    voids (`records` given: the unprojected finals as (role, content)) only on the lenses the effect has ops on, so an
    unscoped void must fail on each lens it touches and is not tried where the effect has no ops."""
    out = []
    for v in items or []:
        if isinstance(v, dict) and lv.void_applies(v, lens, records):
            out.append({k: x for k, x in v.items() if k != "lens"})
    return out


def derive_ruling_lenswise(store, session, ruling_json: dict, stage: str, lenses: List[str]) -> dict:
    """C11.5: derive_ruling for a multi-lens session. Settlements are lens-agnostic (one claims_review, one mixture w);
    per lens L the finals are projected (lens_views.project_record), Z′_L / R′_L replayed, A_L mixed and envelope-checked,
    the candidates built from both sides' last-round contracts on L (+ voids of L or of every lens) and the operation
    derived with L's ruler (odds.by_lens[L]: reference price, cost, unit basis, ATR). The mixture rule runs once on the
    union of applied effects; λ (`lens_weights`, default = the frozen config weights) is checked by
    lens_views.lens_weights_errors and touches only the summary composite. → the record to archive: the arbiter's fields
    + `by_lens[L] = {scenario, operation_type, operation_target (number), operation_size, operation_execution?,
    composite_score, s_ref}` + the primary lens's (lenses[0]) scenario / operation_type / operation_size /
    operation_execution at the top level, `operation_target = "<L>=<level>; …"` in session order,
    `composite_score = round(Σ λ_L · composite_L)`, `lens_weights` (λ used), `lens_weights_default`, `lens_weights_reason`."""
    w, reason, voided_effects = _authored_checks(ruling_json)
    state = getattr(session, "state", None) or {}
    cfg = session_cfg(state)
    odds = state.get("odds") or {}
    by_odds = odds.get("by_lens") if isinstance(odds.get("by_lens"), dict) else {}
    missing = [L for L in lenses if not isinstance(by_odds.get(L), dict)]
    if missing:
        raise RulingError(f"multi-lens session without odds.by_lens for {', '.join(missing)} (C11.1); cannot derive")
    default = state.get("lens_weights_default") or odds.get("lens_weights_default")
    if not isinstance(default, dict) or set(default) != set(lenses):
        raise RulingError("multi-lens session without lens_weights_default for every lens (C11.1); cannot derive")
    tol = float(cfg.get("p_tolerance", sr.P_TOL))
    finals = final_records(store)
    sides = {t: _side(finals, t) for t in ("Z", "R")}
    if not sides["Z"] or not sides["R"]:
        raise RulingError("both sides need at least one final before a ruling")
    cr_err = claims_review_errors(finals, ruling_json.get("claims_review"))
    if cr_err:
        raise RulingError("claims_review: " + "; ".join(cr_err))
    with_revisions = stage == "ruling" and _redraft_changed(store, ruling_json)
    revisions = {t: revision_chain(store, t) for t in ("Z", "R")} if with_revisions else None
    voided = ruling_json.get("voided") or {}
    bad = [f"voided.{k}[{j}]: lens {v['lens']!r} is not a session lens {list(lenses)}"
           for k in ("effects", "candidates") for j, v in enumerate(voided.get(k) or [])
           if isinstance(v, dict) and "lens" in v and v["lens"] not in lenses]
    if bad:
        raise RulingError("; ".join(bad) + " (C11.4: a void names the lens on which the effect fails; an unscoped void applies on every lens the effect touches)")
    per: Dict[str, dict] = {}
    applied_all: Dict[str, list] = {"Z": [], "R": []}
    fatal: List[str] = []
    for L in lenses:
        try:
            fin_L = [lv.project_record(f, L, lenses) for f in finals]
        except lv.LensError as exc:
            raise RulingError(str(exc)) from exc
        rev_L = ({t: [(rid, _lens_revision(rev, L)) for rid, rev in revisions[t]] for t in revisions}
                 if revisions is not None else None)
        primes, applied, f = replay_primes(fin_L, ruling_json.get("claims_review"),
                                           _lens_voided(voided_effects, L, [(x["role"], x["content"]) for x in finals]),
                                           rev_L, tol)
        fatal += [f"lens {L}: {x}" for x in f]
        for t in ("Z", "R"):
            applied_all[t] += applied.get(t) or []
        per[L] = {"finals": fin_L, "primes": primes, "applied": applied}
    if fatal:
        raise RulingError("replay failed (C6.2): " + "; ".join(fatal)
                            + " — void the effect in voided.effects with a reason, or correct claims_review")
    mix_err = mixture_errors(ruling_json.get("mixture"), finals, applied_all, evidence_ids_from_store(store))
    if mix_err:
        raise RulingError("; ".join(mix_err))
    claim_ids = {str(c["id"]) for f in finals for c in (f.get("content") or {}).get("claims") or []
                 if isinstance(c, dict) and c.get("id")}
    lam_err, lam = lv.lens_weights_errors(ruling_json.get("lens_weights"), default, lenses,
                                          ruling_json.get("lens_weights_reason"),
                                          claim_ids | set(evidence_ids_from_store(store)))
    if lam_err:
        raise RulingError("; ".join(lam_err))
    by_lens: Dict[str, dict] = {}
    for L in lenses:
        primes, fin_L = per[L]["primes"], per[L]["finals"]
        try:
            A = sr.mixture(primes["Z"], primes["R"], w, reason)
        except ValueError as exc:
            raise RulingError(f"lens {L}: mixture: {exc}") from exc
        env = sr.envelope_check(A, primes["Z"], primes["R"], tol)
        if env:
            raise RulingError(f"lens {L}: envelope (C6.3): " + "; ".join(env))
        sides_L = {t: _side(fin_L, t) for t in ("Z", "R")}
        warn: List[str] = []
        cands = om.build_candidates(last_execution(sides_L["Z"]), last_execution(sides_L["R"]), A,
                                    _lens_voided(voided.get("candidates"), L),
                                    rounds={t: sides_L[t][-1]["round"] for t in ("Z", "R")}, warn=warn,
                                    filers={t: last_scenario(sides_L[t]) for t in ("Z", "R")},
                                    atr_abs=om.atr_abs_of({"unit_basis": by_odds[L]}, A.get("P0")))
        try:
            cfg_L = om.config_from_snapshot(lv.lens_snapshot(odds, L))
        except lv.LensError as exc:
            raise RulingError(str(exc)) from exc
        d = om.derive_for_report(A, cands, cfg_L, atr_pct=by_odds[L].get("atr_pct_14"))
        if not d.get("ok"):
            raise RulingError(f"lens {L}: derivation failed: " + "; ".join(d.get("fatal") or []))
        A["derived"] = dict(d, s_ref=d["score"]["S_ref"], unit_basis=cfg_L.get("unit_basis"),
                            warn=list(d.get("warn") or []) + warn,
                            replay={"revisions_included": with_revisions, **per[L]["applied"]}, stage=stage)
        fields = operation_fields(d, None, None)
        entry = {"scenario": A, "operation_type": fields["operation_type"], "operation_target": float(d["operation_target"]),
                 "operation_size": fields["operation_size"]}
        if "operation_execution" in fields:
            entry["operation_execution"] = fields["operation_execution"]
        entry["composite_score"] = fields["composite_score"]
        entry["s_ref"] = d["score"]["S_ref"]
        simple = simple_fields(d, None, None)
        simple["operation_target"] = float(d["simple"]["operation_target"])
        entry["operation_simple"] = simple
        if by_odds[L].get("observe_only"):   # G6: an index lens (frozen at open) — observation lines only, no shares
            entry["observe_only"] = True
        by_lens[L] = entry
    record = copy.deepcopy(ruling_json)
    record.pop("operation_execution", None)   # derived exclusively (refused above when authored)
    if "composite_score" in record:
        record["authored_score"] = record.pop("composite_score")
    prim = by_lens[lenses[0]]
    record["by_lens"] = by_lens
    record["scenario"] = prim["scenario"]
    record["operation_type"] = prim["operation_type"]
    record["operation_target"] = lv.join_targets(by_lens, lenses)
    record["operation_size"] = prim["operation_size"]
    if "operation_execution" in prim:
        record["operation_execution"] = prim["operation_execution"]
    record["operation_simple"] = simple_summary(by_lens, lenses)
    record["composite_score"] = lv.weighted_score({L: by_lens[L]["composite_score"] for L in lenses}, lam)
    record["lens_weights"] = dict(lam)
    record["lens_weights_default"] = dict(default)
    project_protection(record, by_lens, state.get("protection_required") and stage in ("proposed", "ruling"), primary=prim)
    return record


ENTRY_AT_MARKET_TOL_PCT = 0.01   # = assemble_debate_artifacts._ENTRY_AT_MARKET_TOL_PCT (display rounding; a test pins it)


def entry_family(level, ref) -> str:
    """ENTRY -> MARKET_ENTRY when the level equals the reference price within display rounding, else LIMIT_ENTRY; no
    usable reference -> ENTRY (undeterminable, as the assembler keeps it)."""
    if not om._is_num(ref) or float(ref) <= 0 or not om._is_num(level):
        return "ENTRY"
    dev = abs(float(level) - float(ref)) / float(ref) * 100
    return "MARKET_ENTRY" if dev <= ENTRY_AT_MARKET_TOL_PCT else "LIMIT_ENTRY"


def recompute_check(A: dict, cfg: dict, report: Optional[dict] = None, atr_pct: Optional[float] = None,
                    candidates: Optional[List[dict]] = None, summary: bool = True) -> List[str]:
    """Re-derive A's strategy under `cfg` from `candidates` (the release gate passes the set it rebuilt from
    debate_history), else from A's stored candidates; → ["recompute mismatch: <field>", ...]. Compared: scenario.derived's
    chosen / q_star / x_star / operation_size / operation_type / operation_target / cost_bps, its cost_bps against the
    ruler's, and with `report` the report's composite_score / operation_size / operation_type (MARKET_ENTRY / LIMIT_ENTRY
    read as ENTRY) / operation_target (`<lens>=<level>` for GENERAL). Also the consumer-facing aliases: the derived
    operation_execution (the conditional / stop / fill of the chosen candidate; on the report its `conditional` / `stop`
    keys), scenario.derived.s_ref / score.S_ref and the report's s_ref against S_ref(A), and scenario.derived.unit_basis
    against the ruler's (odds) unit_basis.
    `summary=False` (C11.6): `report` is one lens's projection of a lens-wise report (lens_views.project_report), whose
    top-level summary fields (the `<lens>=<level>; …` display string, the λ-weighted composite, the default-weighted
    s_ref) are checked by validate_debate.checks_lenswise; here the projected `operation_target` is that lens's number
    and is compared as a level, and composite_score / s_ref are the lens's own (the projection put them there)."""
    d0 = A.get("derived") or {}
    sc = {k: v for k, v in A.items() if k != "derived"}
    cands = candidates if candidates is not None else (d0.get("candidates") or [])
    d = om.derive_for_report(sc, cands, cfg, atr_pct=atr_pct)
    if not d.get("ok"):
        return [f"recompute failed: {'; '.join(d.get('fatal') or [])}"]
    out = []
    for k in ("chosen", "q_star", "x_star", "operation_size", "operation_type", "cost_bps"):
        if json.dumps(d.get(k)) != json.dumps(d0.get(k)):
            out.append(f"recompute mismatch: {k} (stored {d0.get(k)!r}, derived {d.get(k)!r})")
    try:
        same_target = fmt_level(d["operation_target"]) == fmt_level(d0.get("operation_target"))
    except (TypeError, ValueError):
        same_target = False
    if not same_target:
        out.append("recompute mismatch: operation_target")
    canon = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)   # noqa: E731
    want_exec = d.get("operation_execution") or None
    if canon(d0.get("operation_execution") or None) != canon(want_exec):
        out.append(f"recompute mismatch: scenario.derived.operation_execution (stored {d0.get('operation_execution')!r}, "
                   f"derived {want_exec!r})")
    s_ref = d["score"]["S_ref"]

    def _same(v) -> bool:
        return om._is_num(v) and abs(float(v) - float(s_ref)) <= 1e-9
    if not _same(d0.get("s_ref")):
        out.append(f"recompute mismatch: scenario.derived.s_ref ({d0.get('s_ref')!r}, derived {s_ref!r})")
    if not _same((d0.get("score") or {}).get("S_ref") if isinstance(d0.get("score"), dict) else None):
        out.append("recompute mismatch: scenario.derived.score.S_ref")
    # G3: the consumer-facing derived text and the comparison table (q / x per strategy) are re-derived too
    if d0.get("display") != d.get("display"):
        out.append(f"recompute mismatch: scenario.derived.display (stored {d0.get('display')!r}, derived {d.get('display')!r})")

    def _table(cmp_):
        st = cmp_.get("strategies") if isinstance(cmp_, dict) else None
        if not isinstance(st, dict):
            return None
        return {n: [(v or {}).get("q"), (v or {}).get("x")] for n, v in st.items()}
    if canon(_table(d0.get("comparison"))) != canon(_table(d.get("comparison"))):
        out.append("recompute mismatch: scenario.derived.comparison (the strategy table's q / x per strategy)")
    if om.conditional_fill_policy(cfg) == "strict_next_open_v1":
        stored_comparison = d0.get("comparison")
        stored_coverage = stored_comparison.get("conditional_execution") if isinstance(stored_comparison, dict) else None
        if canon(stored_coverage) != canon(d["comparison"].get("conditional_execution")):
            out.append("recompute mismatch: scenario.derived.comparison.conditional_execution")
    if canon(d0.get("unit_basis")) != canon(cfg.get("unit_basis")):
        out.append("recompute mismatch: scenario.derived.unit_basis ≠ the ruler's odds.unit_basis")
    ruler_cost = float(cfg["cost_bps"]) if om._is_num(cfg.get("cost_bps")) else 0.0
    if not om._is_num(d0.get("cost_bps")) or float(d0["cost_bps"]) != ruler_cost:
        out.append(f"recompute mismatch: scenario.derived.cost_bps {d0.get('cost_bps')!r} ≠ the ruler's cost_bps {ruler_cost!r}")
    if report is not None:
        sym = report.get("symbol")
        try:
            want = operation_fields(d, sym, report_lens(report)) if summary else operation_fields(d, None, None)
        except RulingError as exc:
            return out + [f"recompute failed: {exc}"]
        if report.get("composite_score") != want["composite_score"]:
            out.append("recompute mismatch: composite_score")
        if report.get("operation_size") != want["operation_size"]:
            out.append("recompute mismatch: operation_size")
        op = report.get("operation_type")
        want_op = want["operation_type"]
        if want_op == "ENTRY":   # G3: the family from the level against the frozen snapshot reference price
            odds = report.get("odds") if isinstance(report.get("odds"), dict) else {}
            want_op = entry_family(d["operation_target"], odds.get("reference_price"))
        if op != want_op:
            out.append(f"recompute mismatch: operation_type ({op!r}, derived {want_op!r} from the level against "
                       f"odds.reference_price)")
        rex0 = report.get("operation_execution")
        if want_op in ("MARKET_ENTRY", "LIMIT_ENTRY") and isinstance(rex0, dict) and rex0.get("derived") not in (None, want_op):
            out.append(f"recompute mismatch: operation_execution.derived ({rex0.get('derived')!r}, derived {want_op!r})")
        if not summary:
            try:
                tgt_ok = fmt_level(report.get("operation_target")) == fmt_level(d["operation_target"])
            except (TypeError, ValueError):
                tgt_ok = False
            if not tgt_ok:
                out.append(f"recompute mismatch: operation_target ({report.get('operation_target')!r}, derived "
                           f"{fmt_level(d['operation_target'])})")
        elif str(report.get("operation_target")) != want["operation_target"]:
            out.append(f"recompute mismatch: operation_target ({report.get('operation_target')!r}, derived "
                       f"{want['operation_target']!r})")
        rex = report.get("operation_execution")
        contract = {k: rex[k] for k in ("conditional", "stop") if k in rex} if isinstance(rex, dict) else {}
        if canon(contract) != canon(want_exec or {}):
            out.append(f"recompute mismatch: operation_execution (contract {contract!r}, derived {want_exec!r})")
        if not _same(report.get("s_ref")):
            out.append(f"recompute mismatch: s_ref ({report.get('s_ref')!r}, derived {s_ref!r})")
    out += _simple_check(d, d0, report, summary, canon, required=om.stop_fill_mode(cfg) == "next_price")
    return out


def _simple_check(d: dict, d0: dict, report: Optional[dict], summary: bool, canon, required: bool = False) -> List[str]:
    """The simple card re-derived: scenario.derived.simple and the report's operation_simple. A session whose ruler
    predates the simple card (snapshot without stop_fill_without_open = next_price) may carry neither; a session on the
    current ruler must carry both; one without the other, or a different value, is a mismatch."""
    has_derived, has_report = "simple" in d0, report is not None and "operation_simple" in report
    if not has_derived and not has_report and not required:
        return []
    out = []
    if canon(d0.get("simple")) != canon(d.get("simple")):
        out.append(f"recompute mismatch: scenario.derived.simple (stored {d0.get('simple')!r}, derived {d.get('simple')!r})")
    if report is not None:
        try:
            want = simple_fields(d, report.get("symbol"), report_lens(report)) if summary else simple_fields(d, None, None)
        except RulingError as exc:
            return out + [f"recompute failed: operation_simple: {exc}"]
        if not summary:
            want["operation_target"] = float(d["simple"]["operation_target"])
        got = report.get("operation_simple")
        if summary and isinstance(report.get("by_lens"), dict):
            return out   # a lens-wise report's top level: validate_debate.checks_lenswise rebuilds it from by_lens
        if canon(got) != canon(want):
            out.append(f"recompute mismatch: operation_simple (report {got!r}, derived {want!r})")
    return out


# ------------------------------------------------------------------------------------------------ tally ruler

def side_ruler(sc: dict, tag: str, cfg: dict) -> dict:
    """Per-side ruler for the tally: S_ref, q*, x*, best strategy over that side's own contracts only."""
    fatal, warn, norm = om.validate_scenario(sc, cfg)
    if fatal:
        return {"ok": False, "fatal": fatal}
    own = sc.get("execution")
    cands = om.build_candidates(own if tag == "Z" else {}, own if tag == "R" else {}, norm, [], warn=warn)
    cmp_ = om.compare_strategies(norm, cands, cfg)
    best = cmp_["strategies"][cmp_["best"]]
    return {"ok": True, "S_ref": om.score(norm)["S_ref"], "best": cmp_["best"], "q": best["q"], "x": best["x"],
            "x_by": {n: v["x"] for n, v in cmp_["strategies"].items()}, "warn": warn + cmp_["warn"],
            "nodes": len(norm.get("nodes") or []), "T_days": norm.get("T_days")}
