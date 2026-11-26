"""postcheck_agg.py — 后验分数的分段 / 分组 / 配对统计(给 equipoise_atlas 复盘页的接口; 需求稿
D:/front_end/equipoise_atlas/docs/handoff/SCORES-API-REQUEST-2026-10-21.md, 2026-10-23 落地)。

数据: postcheck 表(分、评分员、尺、病灶) ⨝ debates 表(操作类型、ENTRY 派生的市价/限价、报告综合分)。
口径:
- 只统计 score IS NOT NULL; 同一 (symbol, as_of, dt) 多把尺时默认取最新尺(ruler 字典序最大), 传了 ruler 就只取那把。
- 分段按 datalayer.cohorts(唯一出处); 区域按 workflows.symbol_universe 的 <region>_all 清单(缺省 US)。
- execution: v3.2 起终报带 operation_execution.derived(MARKET_ENTRY / LIMIT_ENTRY); 旧场按旧枚举映射; 非入场操作记 n/a。
- 周按 ISO 周一(UTC); 分数桶固定 10 分一桶 [0,10) … [90,100]。
计算全在 Python 里做(表只有几千行), SQL 只负责取行。
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from datalayer.cohorts import COHORTS, RULER_LATEST, cohort_of, era_of

_REGION_CACHE: Optional[Dict[str, str]] = None


def _region_map() -> Dict[str, str]:
    global _REGION_CACHE
    if _REGION_CACHE is None:
        m: Dict[str, str] = {}
        try:
            from workflows.symbol_universe import postcheck_universes
            for k, syms in (postcheck_universes() or {}).items():
                if k.endswith("_all"):
                    for s in syms:
                        m[str(s).upper()] = k[:-4].upper()
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[postcheck_agg] 区域清单不可用, 全部记 US: {e}")
        _REGION_CACHE = m
    return _REGION_CACHE


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(v)


def _dt(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _execution(op_type: Optional[str], derived: Optional[str]) -> str:
    """market / limit / entry_unresolved(ENTRY 没派生出来) / n/a(非入场)。"""
    op = str(op_type or "").upper()
    d = str(derived or "").upper()
    if d == "MARKET_ENTRY" or op == "MARKET_ENTRY":
        return "market"
    if d == "LIMIT_ENTRY" or op == "LIMIT_ENTRY":
        return "limit"
    if op == "ENTRY":
        return "entry_unresolved"
    return "n/a"


def _quant_fields(q) -> Dict[str, Optional[float]]:
    if not isinstance(q, dict):
        return {"ret": None, "mae": None, "mfe": None}
    m = (q.get("members") or [q])[0] if q.get("members") else q
    return {"ret": m.get("ret_pct"), "mae": m.get("mae_pct"), "mfe": m.get("mfe_pct")}


def enrich(raw: Dict[str, Any], region_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """一行 postcheck⨝debates → 统计用的扁平行。raw 键: symbol, as_of, dt, ruler, score, reckoner, lesion, quant,
    operation_type, op_exec(派生名), composite_score, checked_at。"""
    rm = _region_map() if region_map is None else region_map
    sym = str(raw.get("symbol") or "").upper()
    as_of = _dt(raw.get("as_of"))
    week = None
    if as_of:
        monday = (as_of - timedelta(days=as_of.weekday())).date()
        week = monday.isoformat()
    score = raw.get("score")
    lesion = str(raw.get("lesion") or "").strip()
    lesion_key = lesion.split()[0][:3] if lesion else None     # "L2 判断层…" → "L2"
    return {
        "symbol": sym, "as_of": _iso(raw.get("as_of")), "dt": int(raw.get("dt") or 0), "ruler": raw.get("ruler"),
        "score": int(score) if score is not None else None, "reckoner": raw.get("reckoner") or None,
        "operation_type": (raw.get("operation_type") or None),
        "operation_execution": _execution(raw.get("operation_type"), raw.get("op_exec")),
        "composite_score": raw.get("composite_score"),
        "region": rm.get(sym, "US"), "cohort": cohort_of(as_of) if as_of else None,
        "lesion": lesion or None, "lesion_key": lesion_key, "week": week,
        "score_bin": (min(int(score) // 10, 9) * 10) if score is not None else None,
        "checked_at": _iso(raw.get("checked_at")) if raw.get("checked_at") else None,
        **_quant_fields(raw.get("quant")),
    }


# ----------------------------------------------------------------------------- 取行
_SQL = """
SELECT p.symbol, p.as_of, p.dt, p.ruler, p.score, p.reckoner, p.lesion, p.quant, p.checked_at,
       d.operation_type, d.report->'operation_execution'->>'derived' AS op_exec, d.composite_score
FROM postcheck p
LEFT JOIN debates d ON d.symbol = p.symbol AND d.as_of = p.as_of
WHERE p.score IS NOT NULL {extra}
ORDER BY p.symbol, p.as_of, p.dt, p.ruler DESC
"""


def fetch_rows(dts: Optional[Iterable[int]] = None, ruler: Optional[str] = None,
               symbol: Optional[str] = None, months: float = 0) -> List[Dict[str, Any]]:
    """从 PG 取行并扁平化; ruler 为空时每个 (symbol, as_of, dt) 只留最新尺。PG 不通 → []。"""
    from datalayer.pg.postcheck_pg_store import _connect
    con = _connect()
    if con is None:
        return []
    extra, params = [], []
    if dts:
        extra.append("AND p.dt = ANY(%s)"); params.append([int(x) for x in dts])
    if ruler:
        extra.append("AND p.ruler = %s"); params.append(str(ruler).upper())
    if symbol:
        extra.append("AND p.symbol = %s"); params.append(str(symbol).upper())
    if months and months > 0:
        extra.append("AND p.as_of >= %s"); params.append(datetime.now(timezone.utc) - timedelta(days=months * 30.5))
    try:
        from psycopg2.extras import RealDictCursor
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_SQL.format(extra=" ".join(extra)), params)
            raw = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[postcheck_agg] 取行失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    return dedupe_latest_ruler([dict(r) for r in raw]) if not ruler else [enrich(dict(r)) for r in raw]


def dedupe_latest_ruler(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一 (symbol, as_of, dt) 只留尺子最大的那行(输入已按 ruler DESC 排好也行, 这里不依赖顺序)。"""
    best: Dict[tuple, Dict[str, Any]] = {}
    for r in raw_rows:
        k = (str(r.get("symbol") or "").upper(), _iso(r.get("as_of")), int(r.get("dt") or 0))
        if k not in best or str(r.get("ruler") or "") > str(best[k].get("ruler") or ""):
            best[k] = r
    return [enrich(r) for r in best.values()]


# ----------------------------------------------------------------------------- 统计
def summarize(scores: List[int]) -> Dict[str, Any]:
    n = len(scores)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "sd": None, "ge80": 0, "lt50": 0, "ge80_share": None, "lt50_share": None}
    ge80 = sum(1 for s in scores if s >= 80)
    lt50 = sum(1 for s in scores if s < 50)
    return {"n": n, "mean": round(statistics.fmean(scores), 1), "median": statistics.median(scores),
            "sd": round(statistics.pstdev(scores), 1) if n > 1 else 0.0,
            "ge80": ge80, "lt50": lt50, "ge80_share": round(ge80 / n, 3), "lt50_share": round(lt50 / n, 3)}


def _filter(rows, dt=None, region=None, cohort=None, reckoner=None, from_=None, to=None):
    out = []
    for r in rows:
        if dt is not None and r["dt"] != int(dt):
            continue
        if region and r["region"] != str(region).upper():
            continue
        if cohort and r["cohort"] != str(cohort).upper():
            continue
        if reckoner and r["reckoner"] != reckoner:
            continue
        if from_ and r["as_of"] < from_:
            continue
        if to and r["as_of"] >= to:
            continue
        out.append(r)
    return out


_GROUP_KEYS = {"cohort": "cohort", "op": "operation_type", "execution": "operation_execution", "region": "region",
               "reckoner": "reckoner", "week": "week", "score_bin": "score_bin", "lesion": "lesion_key", "ruler": "ruler",
               "symbol": "symbol"}


def aggregate(rows: List[Dict[str, Any]], dt: int, region=None, cohort=None, reckoner=None,
              group_by: Optional[str] = None, from_=None, to=None) -> Dict[str, Any]:
    """S2–S7、S9: 筛选后的总体 + 按 group_by 分组(单选)。"""
    sel = _filter(rows, dt=dt, region=region, cohort=cohort, reckoner=reckoner, from_=from_, to=to)
    out: Dict[str, Any] = {"dt": int(dt), "filters": {k: v for k, v in (("region", region), ("cohort", cohort), ("reckoner", reckoner),
                                                                        ("from", from_), ("to", to)) if v},
                           **summarize([r["score"] for r in sel])}
    if group_by:
        key = _GROUP_KEYS.get(group_by)
        if key is None:
            raise ValueError(f"group_by 只认 {sorted(_GROUP_KEYS)}")
        buckets: Dict[Any, List[int]] = defaultdict(list)
        for r in sel:
            buckets[r.get(key) if r.get(key) is not None else "?"].append(r["score"])
        if group_by == "score_bin":
            groups = [{"key": b, "label": f"[{b},{b + 10}{']' if b == 90 else ')'}", **summarize(buckets.get(b, []))} for b in range(0, 100, 10)]
        else:
            groups = [{"key": k, **summarize(v)} for k, v in buckets.items()]
            groups.sort(key=lambda g: (str(g["key"]) if group_by in ("week", "cohort", "ruler") else -g["n"]))
        out["group_by"] = group_by
        out["groups"] = groups
    return out


def cohorts_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """3.1: 分段定义 + 各段 d3 / d20 汇总、评分员与尺子分布。"""
    by: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["cohort"]:
            by[r["cohort"]][r["dt"]].append(r)
    items = []
    for c in COHORTS:
        cid = c["id"]
        d = by.get(cid, {})
        item = {**c, "d3": summarize([r["score"] for r in d.get(3, [])]), "d20": summarize([r["score"] for r in d.get(20, [])]),
                "reckoners": {"d3": dict(Counter(r["reckoner"] for r in d.get(3, []))), "d20": dict(Counter(r["reckoner"] for r in d.get(20, [])))},
                "rulers": {"d3": dict(Counter(r["ruler"] for r in d.get(3, []))), "d20": dict(Counter(r["ruler"] for r in d.get(20, [])))},
                "flags": [], "note": None}
        s3 = item["d3"]
        if s3["n"] >= 20 and s3["sd"] is not None and s3["sd"] >= 15:
            item["flags"].append("d3 两尾同肥")
        if len(item["reckoners"]["d20"]) > 1:
            item["flags"].append("d20 评分员混评")
        items.append(item)
    return {"generated_at": _iso(datetime.now(timezone.utc)), "ruler_latest": RULER_LATEST, "cohorts": items}


def paired(rows: List[Dict[str, Any]], region=None, cohort=None, reckoner20=None, reckoner3=None,
           min_n: int = 10) -> Dict[str, Any]:
    """3.3: 同一 (symbol, as_of) 两档都有分才配对; reckoner20 只约束 d+20 行(astra 同尺口径), reckoner3 可选。"""
    d3: Dict[tuple, Dict[str, Any]] = {}
    d20: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        if region and r["region"] != str(region).upper():
            continue
        if cohort and r["cohort"] != str(cohort).upper():
            continue
        k = (r["symbol"], r["as_of"])
        if r["dt"] == 3 and (not reckoner3 or r["reckoner"] == reckoner3):
            d3[k] = r
        elif r["dt"] == 20 and (not reckoner20 or r["reckoner"] == reckoner20):
            d20[k] = r
    groups: Dict[str, List[tuple]] = defaultdict(list)
    for k, r20 in d20.items():
        r3 = d3.get(k)
        if r3 is not None and r20["cohort"]:
            groups[r20["cohort"]].append((r3, r20))
    out = []
    for c in COHORTS:
        pairs = groups.get(c["id"], [])
        n = len(pairs)
        if n == 0:
            continue
        m3 = round(statistics.fmean(p[0]["score"] for p in pairs), 1)
        m20 = round(statistics.fmean(p[1]["score"] for p in pairs), 1)
        rk20 = Counter(p[1]["reckoner"] for p in pairs)
        out.append({"cohort": c["id"], "era": c["era"], "n": n, "shown": n >= min_n, "d3_mean": m3, "d20_mean": m20,
                    "diff": round(m20 - m3, 1), "mixed_reckoners": len(rk20) > 1, "reckoners20": dict(rk20),
                    "reckoners3": dict(Counter(p[0]["reckoner"] for p in pairs))})
    return {"filters": {k: v for k, v in (("region", region), ("cohort", cohort), ("reckoner20", reckoner20), ("reckoner3", reckoner3)) if v},
            "min_n": min_n, "groups": out}


def stats_breakdown(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """3.4 给 /api/postcheck/stats 加的分栏: by_dt / by_ruler / by_reckoner(带 dt)。"""
    by_dt = defaultdict(list); by_ruler = defaultdict(list); by_rk = defaultdict(list)
    for r in rows:
        by_dt[r["dt"]].append(r["score"]); by_ruler[r["ruler"]].append(r["score"]); by_rk[(r["reckoner"], r["dt"])].append(r["score"])
    return {"by_dt": {str(k): summarize(v) for k, v in sorted(by_dt.items())},
            "by_ruler": {str(k): summarize(v) for k, v in sorted(by_ruler.items(), key=lambda kv: str(kv[0]))},
            "by_reckoner_dt": [{"reckoner": k[0], "dt": k[1], **summarize(v)} for k, v in sorted(by_rk.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))]}


_SUMMARY_FIELDS = ("symbol", "as_of", "dt", "ruler", "score", "reckoner", "operation_type", "operation_execution",
                   "region", "cohort", "lesion", "ret", "mae", "mfe", "checked_at")


def list_summary(rows: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    """3.4 轻量列表: 只回摘要字段, 按 as_of 倒序、同期按 dt 升序。"""
    rows = sorted(rows, key=lambda r: (r["as_of"], -r["dt"]), reverse=True)
    return [{k: r.get(k) for k in _SUMMARY_FIELDS} for r in rows[: int(limit or 200)]]


def era_label(cohort_id: Optional[str]) -> Optional[str]:
    return era_of(cohort_id)
