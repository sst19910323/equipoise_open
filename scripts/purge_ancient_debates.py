#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上古数据清理(2026-08-19, 用户拍板): 分界线 2026-07-06 00:00 UTC(agent 版起点)。

删: debates / postcheck / evidence_items / 随之无人引用的 evidence_entity /
    老 evidence blob 行 / news —— 全部只删 < CUTOFF 的。
留: company / economic / fundamentals / sentiment / technical(基础面, 不占地方)。
用法: purge_ancient.py [--apply]   不带 --apply = dry-run 只报数。
"""
import sys
sys.path.insert(0, "/opt/equipoise")
from datalayer.pg.postcheck_pg_store import _connect

CUTOFF = "2026-07-06 00:00:00+00"
APPLY = "--apply" in sys.argv

con = _connect()
con.autocommit = False
cur = con.cursor()
from datalayer.pg import names as _names
MC = _names.model_calls(cur)["table"]   # 2026-10-01 改名: tasks → model_calls; 按库里实际表名


def count(sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


print(f"分界线: < {CUTOFF} 删除    模式: {'APPLY(真删)' if APPLY else 'DRY-RUN(只报数)'}")
print("=" * 64)

# --- 删前统计 ---
plan = [
    ("debates", "SELECT count(*) FROM debates WHERE as_of < %s"),
    ("postcheck(指向被删辩论)", """SELECT count(*) FROM postcheck p WHERE p.as_of < %s
        OR NOT EXISTS (SELECT 1 FROM debates d WHERE d.symbol=p.symbol AND d.as_of=p.as_of)"""),
    ("evidence_items", "SELECT count(*) FROM evidence_items WHERE as_of < %s"),
    ("evidence(老blob)", "SELECT count(*) FROM evidence WHERE generated_at < %s"),
    ("news", "SELECT count(*) FROM news WHERE fetched_at < %s"),
]
for name, sql in plan:
    print(f"  {name:28s} 将删 {count(sql, (CUTOFF,)):>6d} 行")

# --- 执行 ---
cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
size_before = cur.fetchone()[0]

cur.execute("""DELETE FROM postcheck p WHERE p.as_of < %s
    OR NOT EXISTS (SELECT 1 FROM debates d WHERE d.symbol=p.symbol AND d.as_of=p.as_of)""", (CUTOFF,))
n_pc = cur.rowcount
cur.execute("DELETE FROM evidence_items WHERE as_of < %s", (CUTOFF,))
n_ei = cur.rowcount
cur.execute(f"DELETE FROM {MC} WHERE as_of < %s", (CUTOFF,))
n_tk = cur.rowcount
cur.execute("DELETE FROM debates WHERE as_of < %s", (CUTOFF,))
n_db = cur.rowcount
cur.execute("DELETE FROM evidence WHERE generated_at < %s", (CUTOFF,))
n_ev = cur.rowcount
cur.execute("DELETE FROM news WHERE fetched_at < %s", (CUTOFF,))
n_nw = cur.rowcount
# entity 是全局去重的内容本体, 只回收"删完挂载后再没人引用"的
cur.execute("""DELETE FROM evidence_entity e
    WHERE NOT EXISTS (SELECT 1 FROM evidence_items u WHERE u.eid = e.eid)""")
n_en = cur.rowcount

print("-" * 64)
print(f"  postcheck {n_pc} · evidence_items {n_ei} · tasks {n_tk} · debates {n_db}")
print(f"  evidence(blob) {n_ev} · news {n_nw} · evidence_entity(回收孤儿) {n_en}")

# --- 删后一致性自检 ---
checks = [
    ("debates 残留 < cutoff", "SELECT count(*) FROM debates WHERE as_of < %s", (CUTOFF,)),
    ("evidence_items 孤儿", """SELECT count(*) FROM evidence_items u WHERE NOT EXISTS
        (SELECT 1 FROM debates d WHERE d.symbol=u.symbol AND d.as_of=u.as_of)""", ()),
    ("postcheck 孤儿", """SELECT count(*) FROM postcheck p WHERE NOT EXISTS
        (SELECT 1 FROM debates d WHERE d.symbol=p.symbol AND d.as_of=p.as_of)""", ()),
    ("evidence_entity 无引用", """SELECT count(*) FROM evidence_entity e WHERE NOT EXISTS
        (SELECT 1 FROM evidence_items u WHERE u.eid=e.eid)""", ()),
    ("模型调用孤儿", f"""SELECT count(*) FROM {MC} t WHERE NOT EXISTS
        (SELECT 1 FROM debates d WHERE d.symbol=t.symbol AND d.as_of=t.as_of)""", ()),
    ("保留段无挂载(应=report.evidence空场)", """SELECT count(*) FROM debates d
        WHERE NOT EXISTS (SELECT 1 FROM evidence_items u WHERE u.symbol=d.symbol AND u.as_of=d.as_of)
        AND jsonb_array_length(coalesce(d.report->'evidence','[]'::jsonb)) > 0""", ()),
]
print("-" * 64)
bad = 0
for name, sql, p in checks:
    n = count(sql, p)
    flag = "OK " if n == 0 else "!! "
    bad += (n != 0)
    print(f"  {flag}{name:42s} {n}")

if APPLY and bad == 0:
    con.commit()
    print("\n已提交。")
elif APPLY:
    con.rollback()
    print("\n自检未过, 已回滚, 数据未动。")
    sys.exit(1)
else:
    con.rollback()
    cur.execute("SELECT count(*) FROM debates")
    print(f"\nDRY-RUN 已回滚。删后 debates 将剩 {count('SELECT count(*) FROM debates')} 场"
          f"(当前 {count('SELECT count(*) FROM debates')} 含未提交视图)")
print(f"库大小(删前) {size_before}")
