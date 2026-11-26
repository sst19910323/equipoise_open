#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""create_task_evidence_tables.py — task 体系表(v2 实体+挂载模型, 幂等建表)。

设计稿: docs/design/TASK-SYSTEM-EVIDENCE-REFACTOR-2026-08-17.md §六。
关系模型(用户定向 2026-08-17):
- task/evidence 都**绝对归属某一轮**辩论(round; 一轮 0..n 个);
- **evidence 是可复用实体**——同一份证据可被多场辩论、多个 task 引用。
故三张表:
- model_calls:     模型调用台账, (symbol, as_of, model_call_id); 2026-10-01 前叫 tasks(task_id=TK-n)。
- evidence_entity: 证据内容本体(全局唯一, eid=内容指纹 sha1-16, 自动去重);
                   payload_text = 全文铁律落点。
- evidence_items:  挂载关系(某场辩论某轮/某 task 用了哪个 entity), 场内号 EV-n;
                   task_id NULL = 直属辩论(简单取证不触发 task)。
老 evidence blob 表不动, 原地冻结当遗留归档。
本脚本检测到旧单表形状(evidence_items 带 question 列)会 DROP 重建(数据由回填脚本全量重灌)。

用法: ./venv/bin/python scripts/create_task_evidence_tables.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datalayer.pg.postcheck_pg_store import _connect  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS model_calls (
  symbol        text        NOT NULL,
  as_of         timestamptz NOT NULL,
  model_call_id text        NOT NULL,
  round         text,
  issue         text,
  kind          text,
  model         text,
  requested_by  text,
  request       jsonb,
  status        text,
  suggested_evidence_tier text,
  result_summary text,
  detail        text,
  evidence_ids  text[],
  created_at    timestamptz DEFAULT now(),
  PRIMARY KEY (symbol, as_of, model_call_id)
);
CREATE TABLE IF NOT EXISTS evidence_entity (
  eid          text PRIMARY KEY,
  kind         text,
  status       text,
  verdict      text,
  verified_scope text,
  question     text,
  simple       text,
  detailed     text,
  payload_text text,
  source       jsonb,
  generated_at timestamptz,
  created_at   timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS evidence_items (
  symbol       text        NOT NULL,
  as_of        timestamptz NOT NULL,
  evidence_id  text        NOT NULL,
  eid          text        NOT NULL REFERENCES evidence_entity(eid),
  model_call_id text,
  round        text,
  requested_by text,
  created_at   timestamptz DEFAULT now(),
  PRIMARY KEY (symbol, as_of, evidence_id)
);
CREATE INDEX IF NOT EXISTS evidence_items_eid ON evidence_items(eid);
"""


def main() -> int:
    con = _connect()
    if con is None:
        print("PG 不可达")
        return 1
    cur = con.cursor()
    # 2026-10-01 改名(tasks → model_calls 等): 库里还是旧形状时不在这里建新表, 免得新旧两张表并存。
    cur.execute("SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'tasks'")
    if cur.fetchone():
        print("库里还是旧表 tasks: 先跑 scripts/migrate_pg_names_2026_10.py --apply, 再跑本脚本")
        con.close()
        return 1
    # v1 单表形状(内容列内嵌)迁 v2: 直接重建, 数据由 backfill 全量重灌
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name='evidence_items' AND column_name='question'""")
    if cur.fetchone():
        cur.execute("DROP TABLE evidence_items")
        print("evidence_items: v1 单表形状 → DROP, 按 v2 实体+挂载重建(记得重跑 backfill)")
    # v2.1 减冗余(2026-08-18 用户审): 砍 raw(存储×2)/executor(kind 可推导)/settlement(预留)
    for tbl, col in (("evidence_entity", "raw"),
                     ("model_calls", "executor"), ("model_calls", "settlement")):
        cur.execute("ALTER TABLE IF EXISTS %s DROP COLUMN IF EXISTS %s" % (tbl, col))
    # v2.2 task 三件套(2026-08-18 用户定向): 概述(result_summary)+详述(detail=分析过程)+
    # 证据引用(evidence_ids) —— task 详述是"怎么判的", evidence 详述是"挖来的材料", 两码事
    for col, typ in (("detail", "text"), ("evidence_ids", "text[]")):
        cur.execute("ALTER TABLE IF EXISTS model_calls ADD COLUMN IF NOT EXISTS %s %s" % (col, typ))
    cur.execute(DDL)
    con.commit()
    for t in ("model_calls", "evidence_entity", "evidence_items"):
        cur.execute("SELECT count(*) FROM " + t)
        print(f"{t}: OK (rows={cur.fetchone()[0]})")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
