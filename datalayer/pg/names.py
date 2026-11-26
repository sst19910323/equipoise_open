# -*- coding: utf-8 -*-
"""names.py — PG 改名过渡层(2026-10-01)。

报告字段在 2026-09-30 重建时改了名, PG 的列名与表名随后由迁移脚本
`scripts/migrate_pg_names_2026_10.py` 改。迁移在服务器上择时执行, 读写代码不能卡在同一刻上线,
所以这里按库里的实际形状取名: 迁移前认旧名, 迁移后认新名。全库迁完、确认无旧形状后, 本模块可删,
调用处写死新名。

  debates.action            → composite_score
  debates.operation_volume  → operation_size
  tasks(表)                 → model_calls
  tasks.task_id             → model_call_id
  tasks.playbook            → model
  tasks.tier_eligible       → suggested_evidence_tier
  evidence_items.task_id    → model_call_id
  evidence_entity           + verified_scope(新增列)

标识符只取自下面这几张固定的表, 不接受外部输入, 可以直接拼进 SQL。
"""
from __future__ import annotations

from typing import Dict, Set

_NEW_TO_OLD = {
    "composite_score": "action",
    "operation_size": "operation_volume",
    "model_call_id": "task_id",
    "model": "playbook",
    "suggested_evidence_tier": "tier_eligible",
}
OLD_TO_NEW = {v: k for k, v in _NEW_TO_OLD.items()}


def columns(cur, table: str) -> Set[str]:
    """表的列名集合; 表不存在 → 空集。"""
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s", (table,))
    out = set()
    for r in cur.fetchall():
        out.add(r["column_name"] if isinstance(r, dict) else r[0])
    return out


def _pick(cols: Set[str], new: str) -> str:
    """新名在就用新名, 否则用旧名(表还没建时也给新名: 建表语句用新名)。"""
    old = _NEW_TO_OLD[new]
    return old if (old in cols and new not in cols) else new


def debates(cur) -> Dict[str, str]:
    """{'composite_score': 实际列名, 'operation_size': 实际列名}"""
    cols = columns(cur, "debates")
    return {k: _pick(cols, k) for k in ("composite_score", "operation_size")}


def model_calls(cur) -> Dict[str, str]:
    """{'table': 实际表名, 'model_call_id': …, 'model': …, 'suggested_evidence_tier': …}"""
    cols = columns(cur, "model_calls")
    table = "model_calls"
    if not cols:
        old = columns(cur, "tasks")
        if old:
            cols, table = old, "tasks"
    out = {"table": table}
    for k in ("model_call_id", "model", "suggested_evidence_tier"):
        out[k] = _pick(cols, k)
    return out


def evidence(cur) -> Dict[str, object]:
    """{'model_call_id': evidence_items 里的实际列名, 'verified_scope': evidence_entity 有没有这一列}"""
    return {"model_call_id": _pick(columns(cur, "evidence_items"), "model_call_id"),
            "verified_scope": "verified_scope" in columns(cur, "evidence_entity")}


def with_new_keys(row: dict) -> dict:
    """读出来的一行: 旧列名补成新键(旧键保留, 过渡期两个都在)。"""
    for old, new in OLD_TO_NEW.items():
        if old in row and new not in row:
            row[new] = row[old]
    return row


def with_old_keys(row: dict) -> dict:
    """接口过渡期: 新键的行再带上旧键, 让还没改的前端继续可用。"""
    for old, new in OLD_TO_NEW.items():
        if new in row and old not in row:
            row[old] = row[new]
    return row
