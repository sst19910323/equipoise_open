#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger_tally.py — 账本整理计算器。

v3.4 起(带 scenario 的记录 = 一把尺记录)辩手不再押分(claims 无 stake、无 moves): 本脚本对这类记录只做主张终态、
claim_rev、自结算闸与外呼统计, 押分相关的告警不触发; 综合分由 odds_model 从各方 scenario 派生。v3.3 及更早(无 scenario)
的记录照旧走押分账本。

为什么存在: 终裁曾有大比例落在双方末轮中点, 因为"开局分加已结算押分"从头到尾只是给模型的嘱咐,
没有任何环节真的算数。本脚本把整理变成机器数: 解析辩论 buffer(或 Analysis JSON), 机械算出每方
`ledger_score = 开局综合评分 ± Σ已结算押分`; 组装器把它注入终报 ledger_score_z / ledger_score_r,
仲裁者偏离整理分的部分只能走 residual_adjustment(引僵持议题、作废账目或审计条目)。

用法:
    python scripts/ledger_tally.py data/debate/<SYM>/_inflight_<TS>.jsonl          # 全量 JSON 回执(终裁审计用)
    python scripts/ledger_tally.py data/debate/<SYM>/_inflight_<TS>.jsonl --brief  # 每轮机械行(评注前置用)
    也接受 Analysis JSON(读其 debate_history)。

字段名(重建批 2, 2026-09-30): 读 composite_score / claims[evidence_tier] / request_evidence[type=model_call,
model]; 旧名(action / pillars / tier / type=task, playbook)仍可读——同尺重评旧报告要用。
输出: {"ok":true, "zealot":{init, ledger_score, last_reported, settled:[...], claims:{...}}, "reaper":{...},
       "open_total":[...], "scoreboard":{...}, "warnings":[...], "forensics":[...]}
- ledger_score 是机器数; 作废账目(修辞对价、无对应主张)不在机器裁量内——仲裁者在终报里引编号修正。
- warnings 逐条列: 结算幅度超过押分(押分是上限, 部分证实可折价) / 同一项影响净计入越过押分 / move 无 issue / 辩手自报综合评分与账本算术漂移 / 收场仍 open 的主张。
"""
from __future__ import annotations

import io
import json
import re
import sys

if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台防线
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fail(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(1)


def round_key(name: str) -> int:
    if name == "Init":
        return 0
    m = re.match(r"(?:Round |回合)(\d+)$", str(name))   # legacy reports carry 回合k
    return int(m.group(1)) if m else 999


# ---- 字段读取(新名优先, 旧名兜底: 旧报告同尺重评要能读) ----
def score_of(c: dict):
    return c.get("composite_score", c.get("action"))


def claims_of(c: dict) -> list:
    v = c.get("claims")
    if v is None:
        v = c.get("pillars")
    return v or []


def tier_of(obj: dict):
    return obj.get("evidence_tier", obj.get("tier"))


def is_model_call(rq: dict) -> bool:
    return rq.get("type") in ("model_call", "task")


def model_of(rq: dict):
    return rq.get("model", rq.get("playbook"))


def load_entries(path: str) -> list:
    """inflight jsonl 或 Analysis JSON → [{round, role, content(dict)}] (只留 Zealot/Reaper)。"""
    with io.open(path, encoding="utf-8") as fh:
        raw = fh.read()
    entries = []
    if path.endswith(".jsonl"):
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                pass  # 半行损坏不致命, warnings 由行数对不上体现
    else:
        doc = json.loads(raw)
        entries = doc.get("debate_history") or []
    out = []
    for e in entries:
        role = e.get("role")
        if role not in ("Zealot", "Reaper"):
            continue
        c = e.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict):
            out.append({"round": e.get("round"), "role": role, "content": c})
    return out


def is_ruler(entries: list) -> bool:
    """A ruler (v3.4) buffer: some final carries a `scenario` object; none → pre-ruler (v3.3 and earlier)."""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from ruler_era import is_ruler_entries
    return is_ruler_entries(entries)


def _claim_revs(entries: list) -> dict:
    """3.4: claim id → claim_rev (C2), via closure_check / scenario_replay; record ids from the round names."""
    from closure_check import claim_revs
    tag = {"Zealot": "Z", "Reaper": "R"}
    recs = [{"seq": i, "id": f"R{round_key(e['round']) + 1}.{tag[e['role']]}", "kind": "final", "role": tag[e["role"]],
             "round": round_key(e["round"]) + 1, "content": e["content"]} for i, e in enumerate(entries, 1)]
    return claim_revs(recs)


def arbiter_named(notes: list, claims_review: list | None = None) -> set:
    """3.4: the claims the arbiter ruled on — every `dispositions[].claim` of the NOTES contents given, plus every
    `claims_review[].claim` (the `named` set of tally; live adapter in run_step, release adapter in validate_debate)."""
    out = set()
    for content in notes or []:
        for d in (content.get("dispositions") or []) if isinstance(content, dict) else []:
            if isinstance(d, dict) and d.get("claim"):
                out.add(str(d["claim"]))
    for c in claims_review or []:
        if isinstance(c, dict) and c.get("claim"):
            out.add(str(c["claim"]))
    return out


def tally(entries: list, closed: set | None = None, ruler: bool | None = None, named: set | None = None) -> dict:
    """`ruler` True (None = auto-detected: some final carries a `scenario`): per side `claim_rev` and `deadlock_requested`;
    `open` = latest status open ∪ (deadlocked − `closed`) ∪ self-settled claims; `closed` = claims whose closing record
    passed closure_check. A claim's owner cannot self-settle it (ledger ruling, 2nd fix wave): an owner-filed
    `settled_for` / `uncontested` stays open unless the opponent conceded (its challenge on the claim `conceded` /
    `withdrawn`) or the arbiter named it (`named`: claims of a NOTES disposition or of claims_review); `uncontested` is
    honoured on its own only when no challenge ever targeted the claim. A challenger's own `settled_for` / `uncontested`
    on its challenge is no exemption: the challenge stays open unless the target's owner conceded or withdrew the target,
    or the arbiter named the challenge or its target. Such claims are listed per side in `self_settled_pending`. No first-three-rounds forensic. Pre-ruler entries (no
    `scenario`): the stake-ledger output of v3.3 and earlier, unchanged."""
    ruler_era = is_ruler(entries) if ruler is None else bool(ruler)
    revs = _claim_revs(entries) if ruler_era else {}
    closed = set(closed or ())
    named = set(named or ())
    challenged, conceded_targets, owner_gave_up = set(), set(), set()
    if ruler_era:   # every challenge ever filed, and the targets its filer conceded (latest status of each challenge)
        ch_latest = {}
        for e in sorted(entries, key=lambda e: round_key(e["round"])):
            for p in claims_of(e["content"]) if isinstance(e.get("content"), dict) else []:
                if isinstance(p, dict) and p.get("id") and p.get("kind") == "challenge" and p.get("target"):
                    challenged.add(str(p["target"]))
                    ch_latest[(e["role"], p["id"])] = (str(p["target"]), p.get("status"))
        conceded_targets = {t for t, st in ch_latest.values() if st in ("conceded", "withdrawn")}
        own_latest = {}
        for e in sorted(entries, key=lambda e: round_key(e["round"])):
            for p in claims_of(e["content"]) if isinstance(e.get("content"), dict) else []:
                if isinstance(p, dict) and p.get("id") and p.get("kind") != "challenge":
                    own_latest[str(p["id"])] = p.get("status")
        owner_gave_up = {c for c, st in own_latest.items() if st in ("conceded", "withdrawn")}

    def _self_settled(pid: str, p: dict) -> bool:
        st = p.get("status")
        if pid in named or pid in closed or st not in ("settled_for", "uncontested"):
            return False
        if p.get("kind") == "challenge":   # the challenger cannot self-settle its challenge either
            tgt = str(p.get("target") or "")
            return not (tgt in named or tgt in owner_gave_up)
        if pid in conceded_targets:
            return False
        return st == "settled_for" or pid in challenged
    sides = {"Zealot": None, "Reaper": None}
    warnings = []   # 账目算术类: 仲裁者记账裁判的本职材料, 进盘中 --brief
    forensics = []  # 行为归因类: 只归档供后验机数, 不进 --brief, 不干预辩论
    per = {}
    for role in ("Zealot", "Reaper"):
        rows = sorted([e for e in entries if e["role"] == role],
                      key=lambda e: round_key(e["round"]))
        if not rows:
            warnings.append(f"{role}: no round records")
            continue
        init_score = score_of(rows[0]["content"])
        last = rows[-1]["content"]
        claims_latest = {}
        moves = []
        model_calls = []
        held_text = []
        deadlock_first = {}
        responses = 0   # 3.4 responses: scenario_updates, answers, challenges filed, status changes on own claims
        prev_status = {}
        for e in rows:
            c = e["content"]
            if ruler_era:
                responses += len([u for u in (c.get("scenario_updates") or []) if isinstance(u, dict)])
                for p in claims_of(c):
                    if not isinstance(p, dict) or not p.get("id"):
                        continue
                    responses += len(p["answers"]) if isinstance(p.get("answers"), list) else 0
                    if p.get("kind") == "challenge" and p["id"] not in prev_status:
                        responses += 1
                    if p["id"] in prev_status and prev_status[p["id"]] != p.get("status"):
                        responses += 1
                    prev_status[p["id"]] = p.get("status")
            for p in claims_of(c):
                if isinstance(p, dict) and p.get("id"):
                    claims_latest[p["id"]] = p
                    if p.get("status") == "deadlocked" and p["id"] not in deadlock_first:
                        deadlock_first[p["id"]] = round_key(e["round"])
            for h in (c.get("stance_ledger") or {}).get("held") or []:
                if isinstance(h, dict):
                    held_text.append(json.dumps(h, ensure_ascii=False))
            for m in (c.get("stance_ledger") or {}).get("moves") or []:
                if isinstance(m, dict):
                    moves.append({"round": e["round"], "id": m.get("id"),
                                  "issue": m.get("issue"), "delta": m.get("delta"),
                                  "evidence_tier": tier_of(m)})
            for rq in c.get("request_evidence") or []:
                if isinstance(rq, dict) and is_model_call(rq):
                    model_calls.append({"round": e["round"], "model": model_of(rq),
                                        "issue": rq.get("issue")})
                    if not rq.get("issue"):
                        warnings.append(f"{role} model call ({model_of(rq)}): no issue — "
                                        f"a model call is the unit of work for an issue; an untargeted filing is a candidate for rejection")
        settled_sum = 0
        for m in moves:
            d = m.get("delta")
            if not isinstance(d, (int, float)):
                warnings.append(f"{role} {m.get('id')}: delta is not numeric ({d!r}), counted as 0")
                d = 0
            settled_sum += d
            if not m.get("issue"):
                warnings.append(f"{role} {m.get('id')}: move has no issue (settlement without a stake; candidate for an invalid ledger entry)")
        ledger_score = (init_score + round(settled_sum)) if isinstance(init_score, (int, float)) else None
        last_reported = score_of(last)
        if ledger_score is not None and isinstance(last_reported, (int, float)) \
                and last_reported != ledger_score and not ruler_era:   # 3.4: scores are derived from the scenario, no ledger
            warnings.append(f"{role}: self-reported final-round composite score {last_reported} ≠ ledger arithmetic {ledger_score}"
                            f" (opening {init_score} + Σ settled {round(settled_sum)}) — drift for the arbiter to rule on")
        open_ids = [pid for pid, p in claims_latest.items() if p.get("status") == "open"]
        self_pending = []
        if ruler_era:   # a debater's deadlocked is a request (C2): open until the arbiter's closing record passes
            self_pending = [pid for pid, p in claims_latest.items() if _self_settled(pid, p)]
            open_ids = [pid for pid, p in claims_latest.items()
                        if p.get("status") == "open" or (p.get("status") == "deadlocked" and pid not in closed)
                        or pid in self_pending]
        per[role] = {
            "init": init_score, "ledger_score": ledger_score, "last_reported": last_reported,
            "settled_sum": round(settled_sum),
            "settled": moves,
            "model_calls": model_calls,
            "rounds": len(rows),
            "claims": {pid: {"kind": p.get("kind"), "stake": p.get("stake"),
                             "evidence_tier": tier_of(p), "status": p.get("status"),
                             "target": p.get("target")}
                       for pid, p in claims_latest.items()},
            "open": open_ids,
            "held_text": held_text,
            "held_count": len(held_text),
            "deadlock_first": deadlock_first,
        }
        if ruler_era:
            per[role]["responses"] = responses
            per[role]["claim_rev"] = {pid: revs.get(pid) for pid in claims_latest}
            per[role]["deadlock_requested"] = [pid for pid, p in claims_latest.items() if p.get("status") == "deadlocked"]
            per[role]["self_settled_pending"] = self_pending
        sides[role] = per[role]
    # 结算幅度 vs 押分 (跨双方找主张: issue 可能是对方主张, 但通常引自己的)
    all_claims = {}
    for role in per:
        all_claims.update(per[role]["claims"])
    for role in per:
        for m in per[role]["settled"]:
            iss, d = m.get("issue"), m.get("delta")
            if iss and iss in all_claims and isinstance(d, (int, float)):
                stake = all_claims[iss].get("stake")
                if isinstance(stake, (int, float)) and abs(d) > stake:
                    warnings.append(f"{role} {m.get('id')}: |delta|={abs(d)} exceeds the stake {stake} on claim {iss} (the stake is the ceiling)")
    # 同一人对同一项影响的净计入(带方向相加): 更正与撤回会相互抵消, 不算重复领取; 净额越过押分才提示。
    for role in per:
        net_by_issue = {}
        for m in per[role]["settled"]:
            iss, d = m.get("issue"), m.get("delta")
            if iss and isinstance(d, (int, float)):
                net_by_issue[iss] = net_by_issue.get(iss, 0) + d
        for iss, net in net_by_issue.items():
            stake = (all_claims.get(iss) or {}).get("stake")
            if isinstance(stake, (int, float)) and abs(net) > stake:
                warnings.append(f"{role}: net credited on {iss} {net:+g} is over the stake {stake} (the same impact collected twice)")
    # 僵持复核: 僵持是中间带的入口。一条主张的押分已经结算完(某一方在它上面的净计入达到押分), 就没有剩下可僵持
    # 的部分; 只结算了一部分的, 剩余争议可以僵持(部分采纳是定价链允许的), 不算矛盾。
    full_settled = set()
    for r2 in per:
        net = {}
        for m in per[r2]["settled"]:
            if m.get("issue") and isinstance(m.get("delta"), (int, float)):
                net[m["issue"]] = net.get(m["issue"], 0) + m["delta"]
        for iss, v in net.items():
            stake = (all_claims.get(iss) or {}).get("stake")
            if isinstance(stake, (int, float)) and stake > 0 and abs(v) >= stake:
                full_settled.add(iss)
    for role in per:
        for pid, p in per[role]["claims"].items():
            if p.get("status") == "deadlocked" and pid in full_settled:
                forensics.append(f"{role} claim {pid}: stake fully settled yet still marked deadlocked — no remaining dispute to deadlock; "
                                 f"treat as closed by evidence / default by silence")
    # 应战闭环归因: 事后素材——只归档供后验机数, 不干预辩论
    other = {"Zealot": "Reaper", "Reaper": "Zealot"}
    for role in per:
        opp = other.get(role)
        if opp not in per:
            continue
        o = per[opp]
        for pid, rnd in per[role]["deadlock_first"].items():
            if rnd <= 3 and not ruler_era:
                forensics.append(f"{role} claim {pid}: marked deadlocked as early as round {rnd} (rejected by default in the first 3 rounds)")
        for pid, p in per[role]["claims"].items():
            if p.get("kind") != "challenge" or not p.get("target"):
                continue
            tgt = p["target"]
            engaged = (
                any(m.get("issue") in (pid, tgt) for m in o["settled"])
                or o["claims"].get(tgt, {}).get("status") in ("settled_against", "conceded", "withdrawn")
                or any(pid in t or tgt in t for t in o["held_text"])
            )
            if p.get("status") in ("deadlocked", "settled_for") and not engaged and not ruler_era:   # 3.4: closure_check's predicate
                forensics.append(f"{role} challenge {pid}→{tgt}: unanswered across rounds (no move/held/concession from the other side)")
            if p.get("status") == "settled_for" \
                    and o["claims"].get(tgt, {}).get("status") in ("settled_for", "uncontested"):
                forensics.append(f"settlement contradiction: {role} challenge {pid} recorded as won while {opp} claim {tgt} is also recorded as won")
        if not per[role]["settled"] and not per[role]["held_count"] and not per[role].get("responses") \
                and len(o["claims"]) >= 2:
            forensics.append(f"{role}: zero responses all session (no move, no held) while the other side has {len(o['claims'])} claims on the table")
    if len(per) == 2 and per.get("Zealot") and per.get("Reaper"):
        z, r = per["Zealot"], per["Reaper"]
        if z["init"] == z["last_reported"] and r["init"] == r["last_reported"] \
                and not z["settled"] and not r["settled"]:
            forensics.append("both positions frozen all session: both final rounds = opening and zero settlements (parallel monologues)")
    open_all = sum((per[r]["open"] for r in per), [])
    # 战果榜: "谁有理"的机器读数——逐议题结算的战果按主张状态×押分汇总。
    def _stake_sum(cl, statuses):
        return sum(p.get("stake") for p in cl.values()
                   if p.get("status") in statuses and isinstance(p.get("stake"), (int, float)))

    board = {}
    for role in per:
        cl = per[role]["claims"]
        board[role.lower()] = {
            "lost": _stake_sum(cl, {"settled_against", "conceded"}),
            "survived": _stake_sum(cl, {"settled_for", "uncontested"}),
            "deadlocked": _stake_sum(cl, {"deadlocked"}),
            "fallen_t12": [pid for pid, p in cl.items()
                           if p.get("status") in ("settled_against", "conceded")
                           and p.get("evidence_tier") in ("T1", "T2")],
        }
    if "zealot" in board and "reaper" in board:
        board["net"] = board["reaper"]["lost"] - board["zealot"]["lost"]  # >0 = 乐观者赢面
        zs, rs = board["zealot"]["survived"], board["reaper"]["survived"]
        board["survived_ratio"] = (round(max(zs, rs) / min(zs, rs), 2) if zs and rs else None)
    return {"ok": True, "zealot": per.get("Zealot"), "reaper": per.get("Reaper"),
            "open_total": open_all, "scoreboard": board, "warnings": warnings,
            "forensics": forensics}


def brief(res: dict) -> str:
    def side_line(tag, s):
        if not s:
            return f"{tag}: no record"
        settled = ", ".join(f"{m['issue']}{'+' if (m['delta'] or 0) >= 0 else ''}{m['delta']}"
                            for m in s["settled"]) or "none"
        return (f"{tag}: opening {s['init']} → ledger_score {s['ledger_score']}"
                f" (Σ settled {'+' if s['settled_sum'] >= 0 else ''}{s['settled_sum']}: {settled})"
                f" · self-reported final round {s['last_reported']}")
    lines = ["[Ledger tally mechanical lines — machine-computed; cite them, do not recompute by hand]",
             side_line("Z", res.get("zealot")), side_line("R", res.get("reaper"))]
    if res.get("open_total"):
        lines.append(f"OPEN claims: {', '.join(res['open_total'])}")
    for tag, key in (("Z", "zealot"), ("R", "reaper")):   # ruler tallies only (the key exists only there)
        req = (res.get(key) or {}).get("deadlock_requested")
        if req:
            lines.append(f"deadlock requested by {tag}: {', '.join(req)} — needs a closing record")
        pend = (res.get(key) or {}).get("self_settled_pending")
        if pend:
            lines.append(f"self-settled by {tag}: {', '.join(pend)} — stays open until the opponent concedes or the "
                         f"arbiter rules on it (a disposition in the round notes)")
    sb = res.get("scoreboard") or {}
    if sb.get("zealot") or sb.get("reaper"):
        z, r = sb.get("zealot") or {}, sb.get("reaper") or {}
        lines.append(f"Scoreboard: Z lost {z.get('lost', 0)}/survived {z.get('survived', 0)}/deadlocked "
                     f"{z.get('deadlocked', 0)} · R lost {r.get('lost', 0)}/survived "
                     f"{r.get('survived', 0)}/deadlocked {r.get('deadlocked', 0)}"
                     f" (stake amounts summed by final claim status; survived does not mean the original weight is fully realized)")
    # 建议焦点: 未结主张按"本方押分+对面指着它的挑战押分"合计排序。brief 是盘中视图,
    # 只供公告议程——警告不进这里(机检发现的问题只归档进全量输出, 供终裁审计与后验归因)。
    all_p = {}
    for key in ("zealot", "reaper"):
        all_p.update(((res.get(key) or {}).get("claims") or {}))
    combined = {}
    for pid, p in all_p.items():
        if p.get("status") in ("open", "deadlocked") and isinstance(p.get("stake"), (int, float)):
            combined[pid] = combined.get(pid, 0) + p["stake"]
        tgt = p.get("target")
        if tgt and tgt in all_p and all_p[tgt].get("status") in ("open", "deadlocked") \
                and isinstance(p.get("stake"), (int, float)):
            combined[tgt] = combined.get(tgt, 0) + p["stake"]
    if combined:
        ranked = sorted(combined.items(), key=lambda kv: -kv[1])
        head = ranked[0]
        rest = " ".join(f"{pid}({s})" for pid, s in ranked[1:4])
        lines.append(f"Suggested focus: {head[0]} (combined stake {head[1]})"
                     + (f" · runners-up: {rest}" if rest else ""))
    mc = [(tag, t) for tag, key in (("Z", "zealot"), ("R", "reaper"))
          for t in ((res.get(key) or {}).get("model_calls") or [])]
    if mc:
        lines.append("Model-call filings: " + " / ".join(
            f"{tag} {t.get('model')}→{t.get('issue') or '?'}" for tag, t in mc)
            + f" · total {len(mc)} (budget and return status filled in by the orchestration)")
    for w in res.get("warnings") or []:
        lines.append(f"⚠ {w}")
    return "\n".join(lines)


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--brief"]
    use_brief = "--brief" in sys.argv[1:]
    if len(args) != 1:
        fail("usage: ledger_tally.py <inflight.jsonl|Analysis.json> [--brief]")
    try:
        entries = load_entries(args[0])
    except OSError as e:
        fail(f"read: {e}")
    except json.JSONDecodeError as e:
        fail(f"parse: {e}")
    if not entries:
        fail("no parseable Zealot/Reaper round records")
    res = tally(entries)
    print(brief(res) if use_brief else json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
