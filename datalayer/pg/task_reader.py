#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_reader.py — 模型调用台账 / evidence_entity / evidence_items(挂载) 读取层(API 用)。

契约: 报告/辩论视图只携带索引(轻量行), 点开按 id 单独加载详情。
v2 模型: evidence 内容本体在 evidence_entity(全局, eid 指纹去重, 可被多场/多次模型调用引用),
evidence_items 是挂载关系(场内号 EV-n, model_call_id NULL=直属)。
- get_model_calls(symbol, as_of)                     → 模型调用索引行(不含 request / detail 全文)
- get_model_calls(symbol, as_of, model_call_id=...)  → 单条全量
- get_evidence_items(symbol, as_of)                  → 挂载 JOIN 实体的索引行
- get_evidence_items(..., evidence_id=...)           → 单条全量(含 detailed / payload_text)
as_of 接受 ISO 字符串, 精确匹配辩论键。

2026-10-01 改名: 表 tasks → model_calls, 列 task_id / playbook / tier_eligible → model_call_id / model /
suggested_evidence_tier, evidence_items.task_id → model_call_id。库里的实际名字由 names 模块探测(迁移前后都能读);
返回的行一律用新键, 同时带上旧键(task_id / playbook / tier_eligible)给还没改的前端, 前端改完后旧键可去掉。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from datalayer.pg import names
from datalayer.pg.postcheck_pg_store import _connect

_MC_INDEX_COLS = ["model_call_id", "round", "issue", "kind", "model", "requested_by",
                  "status", "suggested_evidence_tier", "result_summary", "evidence_ids"]
_MC_ALL_COLS = _MC_INDEX_COLS + ["request", "detail", "created_at"]

_EV_INDEX_COLS = ["evidence_id", "model_call_id", "round", "requested_by", "eid",
                  "kind", "status", "verdict", "question", "simple"]
_EV_DETAIL_COLS = _EV_INDEX_COLS + ["detailed", "payload_text", "source", "generated_at"]


def _iso(d: Dict[str, Any]) -> Dict[str, Any]:
    for k in ("created_at", "generated_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


def get_model_calls(symbol: str, as_of: str, model_call_id: Optional[str] = None) -> List[Dict[str, Any]]:
    con = _connect()
    if con is None:
        raise RuntimeError("PG 不可达")
    try:
        cur = con.cursor()
        mc = names.model_calls(cur)
        cols = _MC_ALL_COLS if model_call_id else _MC_INDEX_COLS
        select = ", ".join(f"{mc[c]} AS {c}" if c in mc else c for c in cols)
        sql = f"SELECT {select} FROM {mc['table']} WHERE symbol=%s AND as_of=%s"
        params: List[Any] = [symbol.upper(), as_of]
        if model_call_id:
            sql += f" AND {mc['model_call_id']}=%s"
            params.append(model_call_id)
        sql += f" ORDER BY {mc['model_call_id']}"
        cur.execute(sql, params)
        return [names.with_old_keys(_iso(dict(zip(cols, r)))) for r in cur.fetchall()]
    finally:
        con.close()


def get_tasks(symbol: str, as_of: str, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """旧名, 等同 get_model_calls(保留给还没改的调用方)。"""
    return get_model_calls(symbol, as_of, model_call_id=task_id)


def get_evidence_items(symbol: str, as_of: str,
                       evidence_id: Optional[str] = None) -> List[Dict[str, Any]]:
    con = _connect()
    if con is None:
        raise RuntimeError("PG 不可达")
    try:
        cur = con.cursor()
        evn = names.evidence(cur)
        cols = list(_EV_DETAIL_COLS if evidence_id else _EV_INDEX_COLS)
        select = (f"u.evidence_id, u.{evn['model_call_id']} AS model_call_id, u.round, u.requested_by, u.eid, "
                  "e.kind, e.status, e.verdict, e.question, e.simple")
        if evidence_id:
            select += ", e.detailed, e.payload_text, e.source, e.generated_at"
        if evn["verified_scope"]:
            select += ", e.verified_scope"
            cols.append("verified_scope")
        sql = (f"SELECT {select} FROM evidence_items u JOIN evidence_entity e ON e.eid = u.eid "
               "WHERE u.symbol=%s AND u.as_of=%s")
        params: List[Any] = [symbol.upper(), as_of]
        if evidence_id:
            sql += " AND u.evidence_id=%s"
            params.append(evidence_id)
        else:
            sql += " ORDER BY u.evidence_id"
        cur.execute(sql, params)
        return [names.with_old_keys(_iso(dict(zip(cols, r)))) for r in cur.fetchall()]
    finally:
        con.close()
