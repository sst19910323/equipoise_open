#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_evidence_items.py — 历史证据回填(v2 实体+挂载; 一次性, 可重跑=全量重建)。

源 = debates.report jsonb 的 evidence 数组(每场自带, as_of 天然精确)。
v2 模型: 内容本体 → evidence_entity(eid=内容指纹 sha1-16, 跨场自动去重——同一份证据被
多场辩论引用时只存一份, 挂载各记各的); 使用关系 → evidence_items(EV-n 按数组序,
task_id/round 旧数据无从考据一律 NULL = 直属)。

用法: ./venv/bin/python scripts/backfill_evidence_items.py [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datalayer.pg.postcheck_pg_store import _connect  # noqa: E402


def content_eid(it: dict) -> str:
    """内容指纹: 同问题+同结论+同全文 = 同一份证据实体(跨场复用去重的键)。"""
    key = "\x1f".join([str(it.get("question") or it.get("task") or ""), str(it.get("simple") or ""),
                       str(it.get("detailed") or ""), str(it.get("payload_text") or "")])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def upsert_entity(cur, it: dict, gen_at) -> str:
    eid = content_eid(it)
    src = it.get("source")
    if isinstance(src, str):
        src = [src]
    elif not isinstance(src, list):
        src = []
    cur.execute(
        """INSERT INTO evidence_entity
           (eid, kind, status, question, simple, detailed, payload_text, source, generated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (eid) DO NOTHING""",
        [eid, it.get("kind") or it.get("type"), it.get("status") or it.get("verdict"),
         it.get("question") or it.get("task") or "", it.get("simple") or "", it.get("detailed") or "",
         it.get("payload_text"), json.dumps(src, ensure_ascii=False), gen_at])
    return eid


def main() -> int:
    dry = "--dry-run" in sys.argv
    con = _connect()
    if con is None:
        print("PG 不可达")
        return 1
    cur = con.cursor()
    if not dry:
        cur.execute("TRUNCATE evidence_items")
        cur.execute("DELETE FROM evidence_entity")
    cur.execute("""SELECT symbol, as_of, report->'evidence', report->>'evidence_generated_at'
                   FROM debates WHERE report ? 'evidence' ORDER BY symbol, as_of""")
    rows = cur.fetchall()
    debates_hit = uses = empty = 0
    for sym, as_of, items, gen_at in rows:
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = None
        if not isinstance(items, list) or not items:
            empty += 1
            continue
        debates_hit += 1
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                continue
            if dry:
                uses += 1
                continue
            eid = upsert_entity(cur, it, gen_at)
            cur.execute(
                """INSERT INTO evidence_items
                   (symbol, as_of, evidence_id, eid, round, requested_by)
                   VALUES (%s,%s,%s,%s,NULL,NULL) ON CONFLICT DO NOTHING""",
                [sym, as_of, f"EV{i}", eid])   # 挂载列(task_id / model_call_id)不写, 默认 NULL
            uses += cur.rowcount
    if not dry:
        con.commit()
        cur.execute("SELECT count(*) FROM evidence_entity")
        n_ent = cur.fetchone()[0]
    else:
        n_ent = "?"
    print(f"含 evidence 的辩论 {len(rows)} · 有效 {debates_hit} · 空数组 {empty}"
          f" · 挂载行 {uses} · 实体 {n_ent}(去重后){' (dry-run)' if dry else ''}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
