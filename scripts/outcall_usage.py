#!/usr/bin/env python3
"""outcall_usage.py — read-only: what happened downstream to every outcall result (search evidence and model calls)
embedded in the v3.4+ reports. Writes nothing to PG.

    ./venv/bin/python scripts/outcall_usage.py [--since 2026-10-31T08:50] [--json out.json]

Per evidence item, the use level is the highest of (council/20261112-outcall-evidence-review, drill session's three steps):
    0 unused        no claim lists the item in `answers`, and no review reason names it
    1 cited         some claim lists it in `answers` (or the arbiter's review reason names it)
    2 with_effect   such a claim registers a non-empty effect (if_true or if_false ops)
    3 moved         such a claim was reviewed and the branch the ruling selects has ops
                    (adopted -> if_true, rejected -> if_false, partly -> either): the ruling moved the tree through it
Repeats: an item is a `repeat` when an earlier item of the same symbol within 3 days (cross-session) asks a question whose
content-word overlap (Jaccard) with it is >= 0.6; `same_session_dup` when the earlier one is in the same session.
Eras are the guard table's (scripts/drill/zero_token/guard.py ERAS) so the two tables line up."""
from __future__ import annotations

import argparse
import collections as C
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ERAS = [("v3.4", "202610310000", "202611090817"), ("v3.5-3.7", "202611090817", "202611101417"),
        ("v3.7.1", "202611101417", "202611110219"), ("v3.8", "202611110219", "209912310000")]
LEVELS = ("unused", "cited", "with_effect", "moved")
STOP = set("the a an of to in on for and or is are was were be by with from as at that this it its whether what which "
           "latest current recent any has have had not do does did if than then into over under about verify confirm "
           "check find identify report reported data date dated source sources primary official".split())


def era_of(as_of) -> str:
    k = as_of.strftime("%Y%m%d%H%M")
    return next((n for n, lo, hi in ERAS if lo <= k < hi), "pre")


def words(q: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z0-9][a-z0-9.%-]+", (q or "").lower()) if w not in STOP and len(w) > 2)


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def claim_table(rep: dict) -> dict:
    """claim id -> {'answers': set of evidence ids, 'has_effect': bool, 'if_true': bool, 'if_false': bool} over the
    whole debate (a claim's later revisions are unioned)."""
    out: dict = {}
    for e in rep.get("debate_history") or []:
        c = e.get("content") if isinstance(e, dict) else None
        if not isinstance(c, dict):
            continue
        for cl in c.get("claims") or []:
            if not isinstance(cl, dict) or not cl.get("id"):
                continue
            d = out.setdefault(str(cl["id"]), {"answers": set(), "if_true": False, "if_false": False})
            d["answers"].update(str(a) for a in cl.get("answers") or [])
            eff = cl.get("effect") if isinstance(cl.get("effect"), dict) else {}
            d["if_true"] |= bool(eff.get("if_true"))
            d["if_false"] |= bool(eff.get("if_false"))
    return out


def use_level(eid: str, claims: dict, reviews: list) -> int:
    rv = {str(r.get("claim")): r for r in reviews if isinstance(r, dict)}
    lvl = 0
    if any(re.search(rf"(?<![A-Za-z0-9.]){re.escape(eid)}(?![0-9])", str(r.get("reason") or "")) for r in rv.values()):
        lvl = 1
    for cid, d in claims.items():
        if eid not in d["answers"]:
            continue
        lvl = max(lvl, 1)
        if d["if_true"] or d["if_false"]:
            lvl = max(lvl, 2)
        v = (rv.get(cid) or {}).get("verdict")
        if (v == "adopted" and d["if_true"]) or (v == "rejected" and d["if_false"]) or (v == "partly" and (d["if_true"] or d["if_false"])):
            lvl = 3
    return lvl


def collect(since: str):
    import yaml
    from datalayer.pg.db import connect
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "symbols.yaml").read_text(encoding="utf-8"))
    etfs = set(cfg["analysis_targets"]["etfs"]) | {"SPY", "QQQ"}
    typ = lambda s: "general" if s.startswith("GENERAL") else ("etf" if s in etfs else "stock")
    rows, seen = [], C.defaultdict(list)      # seen[symbol] = [(as_of, words, session_key)]
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT symbol, as_of, report FROM debates WHERE as_of >= %s ORDER BY as_of", (since,))
        for sym, as_of, rep in cur.fetchall():
            claims, reviews = claim_table(rep), rep.get("claims_review") or []
            for e in rep.get("evidence") or []:
                if not isinstance(e, dict):
                    continue
                e = dict(e, evidence_id=e.get("evidence_id") or e.get("model_call_id") or e.get("id"))
                if not e["evidence_id"]:
                    continue
                src = str(e.get("source") or "")
                if e.get("kind") == "search":
                    kind = "search"
                elif "model" in src or e.get("model") or re.search(r"\.MC\d", str(e["evidence_id"])):
                    m = re.search(r"P\d\d", str(e.get("model") or "")) or re.search(r"P\d\d", src) or re.search(r"P\d\d", str(e.get("request") or ""))
                    kind = "model:" + (m.group(0) if m else "?")
                else:
                    continue                  # data-preparation items are on the table for everyone; not an outcall
                w = words(e.get("question") or str(e.get("request") or ""))
                rep_kind = "new"
                if kind == "search":
                    for a0, w0, k0 in reversed(seen[sym]):
                        if (as_of - a0).days > 3:
                            break
                        if jaccard(w, w0) >= 0.6:
                            rep_kind = "same_session_dup" if k0 == as_of else "repeat"
                            break
                    seen[sym].append((as_of, w, as_of))
                rows.append({"symbol": sym, "type": typ(sym), "era": era_of(as_of), "kind": kind, "round": str(e.get("round")),
                             "by": e.get("requested_by"), "status": e.get("status"), "verdict": e.get("verdict"),
                             "level": use_level(str(e["evidence_id"]), claims, reviews), "repeat": rep_kind})
    return rows


def table(rows, key, title):
    g = C.defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    print(f"\n== {title}")
    print(f"{'group':28s} {'n':>5s}  " + "  ".join(f"{l:>11s}" for l in LEVELS) + "   mixed  repeat")
    for k in sorted(g, key=lambda x: (-len(g[x]), str(x))):
        v = g[k]
        n = len(v)
        lv = C.Counter(r["level"] for r in v)
        print(f"{str(k)[:28]:28s} {n:5d}  " + "  ".join(f"{lv[i] / n:11.0%}" for i in range(4))
              + f"  {sum(r['verdict'] == 'mixed' for r in v) / n:6.0%}  {sum(r['repeat'] != 'new' for r in v) / n:6.0%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-31T08:50")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = collect(a.since)
    s = [r for r in rows if r["kind"] == "search"]
    m = [r for r in rows if r["kind"] != "search"]
    print(f"items: {len(rows)} (search {len(s)}, model calls {len(m)})")
    table(rows, lambda r: "search" if r["kind"] == "search" else "model call", "all outcalls")
    table(s, lambda r: r["era"], "search, by era")
    table(s, lambda r: r["type"], "search, by symbol type")
    table(s, lambda r: "Init" if r["round"] == "Init" else ("Round 1" if r["round"].endswith(" 1") or r["round"].endswith("回合1") else "later"), "search, by stage")
    table(s, lambda r: r["by"], "search, by requester")
    table(s, lambda r: r["verdict"], "search, by verdict")
    table(s, lambda r: r["repeat"], "search, by repeat")
    table(m, lambda r: r["kind"], "model calls, by manual")
    table(m, lambda r: (r["kind"], r["era"]), "model calls, by manual and era")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
