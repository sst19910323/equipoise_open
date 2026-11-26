#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_pg_names_2026_10.py — 把 PG 里的旧名改成新名(2026-09-30 重建的改名)。

对照表与步骤说明: docs/handoff/FIELD-RENAMES-2026-10.md 第 8 节。读写代码已经新旧两套都认
(datalayer/pg/names.py), 所以本脚本可以在任何没有辩论在跑的时刻执行, 不需要和代码上线卡在同一刻。

做三件事:
  1. 结构: debates.action → composite_score, debates.operation_volume → operation_size;
           tasks 表 → model_calls, 列 task_id / playbook / tier_eligible → model_call_id / model /
           suggested_evidence_tier; evidence_items.task_id → model_call_id;
           evidence_entity 补 verdict、verified_scope 两列(没有才加)。
  2. 取值: evidence_entity.verdict 的 supported / refuted / verified → supports / refutes / supports;
           发起方 Q-PARENT / PARENT / ARBITER → Arbiter。
  3. 报告: debates.report 里的旧键改成新键(scripts/report_names.py; 编号的值与正文不动)。
           改之前把原报告存进 debates_report_bak_20261001(同一行只存第一次的原件)。

用法(工作目录 /opt/equipoise, 用 ./venv/bin/python):
    python scripts/migrate_pg_names_2026_10.py                 # 演练: 不改任何东西, 只报告会改什么
    python scripts/migrate_pg_names_2026_10.py --apply         # 真改
    python scripts/migrate_pg_names_2026_10.py --verify        # 只读检查(迁移前后都能跑)
    选项: --limit N   报告改写只处理前 N 行(先小样试跑)
          --no-backup 不存原件(不建议)
演练时结构那一步会真的执行再回滚, 用来确认语句在这个库上跑得通; 回滚前有极短的表锁。
全部步骤可重复执行: 已经改过的不会再改。
"""
from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datalayer.pg import names  # noqa: E402
from datalayer.pg.postcheck_pg_store import _connect  # noqa: E402
from report_names import LEGACY_ARBITER, VERDICT, upgrade_report  # noqa: E402

BACKUP = "debates_report_bak_20261001"
PAGE = 40


def _tables(cur) -> set:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {r[0] for r in cur.fetchall()}


def schema_plan(cur) -> list:
    """按库里的实际形状列出要执行的语句(已经改过的不再列)。"""
    plan = []
    tabs = _tables(cur)
    if "debates" in tabs:
        cols = names.columns(cur, "debates")
        for old, new in (("action", "composite_score"), ("operation_volume", "operation_size")):
            if old in cols and new not in cols:
                plan.append(f"ALTER TABLE debates RENAME COLUMN {old} TO {new}")
    if "tasks" in tabs and "model_calls" in tabs:
        raise SystemExit("tasks 与 model_calls 两张表同时存在: 先人工确认哪张是对的, 再跑本脚本")
    if "tasks" in tabs:
        cols = names.columns(cur, "tasks")
        for old, new in (("task_id", "model_call_id"), ("playbook", "model"),
                         ("tier_eligible", "suggested_evidence_tier")):
            if old in cols and new not in cols:
                plan.append(f"ALTER TABLE tasks RENAME COLUMN {old} TO {new}")
        plan.append("ALTER TABLE tasks RENAME TO model_calls")
    if "evidence_items" in tabs:
        cols = names.columns(cur, "evidence_items")
        if "task_id" in cols and "model_call_id" not in cols:
            plan.append("ALTER TABLE evidence_items RENAME COLUMN task_id TO model_call_id")
    if "evidence_entity" in tabs:
        cols = names.columns(cur, "evidence_entity")
        for col in ("verdict", "verified_scope"):
            if col not in cols:
                plan.append(f"ALTER TABLE evidence_entity ADD COLUMN {col} text")
    return plan


def value_plan(cur) -> list:
    """(说明, 计数语句, 更新语句, 参数)。表名按当前实际形状取。"""
    out = []
    tabs = _tables(cur)
    if "evidence_entity" in tabs and "verdict" in names.columns(cur, "evidence_entity"):
        for old, new in VERDICT.items():
            out.append((f"evidence_entity.verdict {old} → {new}",
                        "SELECT count(*) FROM evidence_entity WHERE verdict = %s",
                        "UPDATE evidence_entity SET verdict = %s WHERE verdict = %s", (old, new)))
    legacy = sorted(LEGACY_ARBITER)
    mc = "model_calls" if "model_calls" in tabs else ("tasks" if "tasks" in tabs else None)
    for tab in ("evidence_items", mc):
        if tab and tab in tabs:
            out.append((f"{tab}.requested_by {'/'.join(legacy)} → Arbiter",
                        f"SELECT count(*) FROM {tab} WHERE requested_by = ANY(%s)",
                        f"UPDATE {tab} SET requested_by = 'Arbiter' WHERE requested_by = ANY(%s)", (legacy,)))
    return out


def run_values(cur, apply: bool) -> None:
    for label, count_sql, update_sql, params in value_plan(cur):
        if len(params) == 2:      # verdict: (旧, 新)
            cur.execute(count_sql, (params[0],))
            n = cur.fetchone()[0]
            if apply and n:
                cur.execute(update_sql, (params[1], params[0]))
        else:
            cur.execute(count_sql, params)
            n = cur.fetchone()[0]
            if apply and n:
                cur.execute(update_sql, params)
        print(f"  {label}: {n} 行" + ("" if apply or not n else " (演练, 未改)"))


def run_reports(con, apply: bool, limit: int, backup: bool) -> None:
    from psycopg2.extras import Json
    cur = con.cursor()
    if apply and backup:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {BACKUP} (symbol text NOT NULL, as_of timestamptz NOT NULL, "
                    f"report jsonb NOT NULL, saved_at timestamptz DEFAULT now(), PRIMARY KEY (symbol, as_of))")
        con.commit()
    stats, kinds = Counter(), Counter()
    errors: list = []
    last = None
    seen = 0
    while True:
        if last is None:
            cur.execute("SELECT symbol, as_of, report FROM debates ORDER BY symbol, as_of LIMIT %s", (PAGE,))
        else:
            cur.execute("SELECT symbol, as_of, report FROM debates WHERE (symbol, as_of) > (%s, %s) "
                        "ORDER BY symbol, as_of LIMIT %s", (last[0], last[1], PAGE))
        rows = cur.fetchall()
        if not rows:
            break
        for sym, as_of, report in rows:
            last = (sym, as_of)
            seen += 1
            if isinstance(report, str):
                try:
                    report = json.loads(report)
                except json.JSONDecodeError:
                    stats["跳过: report 不是合法 JSON"] += 1
                    continue
            try:
                new_doc, changes, skipped = upgrade_report(copy.deepcopy(report))
            except Exception as exc:  # noqa: BLE001  一份形状意外的报告不该中断整批; 记下来, 原样留着
                stats[f"转换出错(未改): {type(exc).__name__}"] += 1
                if len(errors) < 10:
                    errors.append(f"{sym} @ {as_of}: {type(exc).__name__}: {exc}")
                continue
            if skipped:
                stats[f"跳过: {skipped}"] += 1
                continue
            if not changes:
                stats["已是新名"] += 1
                continue
            stats["改写"] += 1
            kinds.update(changes)
            if apply:
                if backup:
                    cur.execute(f"INSERT INTO {BACKUP} (symbol, as_of, report) VALUES (%s, %s, %s) "
                                f"ON CONFLICT (symbol, as_of) DO NOTHING",
                                (sym, as_of, Json(report, dumps=lambda o: json.dumps(o, ensure_ascii=False))))
                cur.execute("UPDATE debates SET report = %s WHERE symbol = %s AND as_of = %s",
                            (Json(new_doc, dumps=lambda o: json.dumps(o, ensure_ascii=False)), sym, as_of))
            if limit and stats["改写"] >= limit:
                break
        if apply:
            con.commit()
        if limit and stats["改写"] >= limit:
            break
    print(f"  扫了 {seen} 份报告" + (f"(到 --limit {limit} 为止)" if limit else ""))
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v}" + (" (演练, 未改)" if k == "改写" and not apply else ""))
    for e in errors:
        print(f"    出错: {e}")
    if kinds:
        print("  各类改动出现在多少份报告里(前 15 类):")
        for k, v in kinds.most_common(15):
            print(f"    {v:5d}  {k}")


def verify(con) -> int:
    cur = con.cursor()
    tabs = _tables(cur)
    d = names.debates(cur)
    mc = names.model_calls(cur)
    ev = names.evidence(cur)
    print("结构:")
    print(f"  debates 评分列 = {d['composite_score']}, 规模列 = {d['operation_size']}")
    print(f"  模型调用表 = {mc['table']}({mc['model_call_id']}, {mc['model']}, {mc['suggested_evidence_tier']})"
          + ("; 旧表 tasks 还在" if "tasks" in tabs and mc["table"] != "tasks" else ""))
    print(f"  evidence_items 挂载列 = {ev['model_call_id']}; evidence_entity.verified_scope "
          f"{'有' if ev['verified_scope'] else '没有'}")
    pending = schema_plan(cur)
    print(f"  结构上还没做的: {len(pending)} 条" + ("" if not pending else " → " + "; ".join(pending)))
    print("报告:")
    checks = [
        ("总数", "SELECT count(*) FROM debates"),
        ("顶层有 composite_score", "SELECT count(*) FROM debates WHERE report ? 'composite_score'"),
        ("顶层还有 action", "SELECT count(*) FROM debates WHERE report ? 'action'"),
        ("顶层还有 operation_volume", "SELECT count(*) FROM debates WHERE report ? 'operation_volume'"),
        ("顶层还有 short_term", "SELECT count(*) FROM debates WHERE report ? 'short_term'"),
        ("顶层还有 tasks", "SELECT count(*) FROM debates WHERE report ? 'tasks'"),
        ("顶层还有 arbiter_shadow_log", "SELECT count(*) FROM debates WHERE report ? 'arbiter_shadow_log'"),
        ("带 final_report 包装(不处理的更早形状)", "SELECT count(*) FROM debates WHERE report ? 'final_report'"),
        ("评分列与报告里的评分不一致",
         f"SELECT count(*) FROM debates WHERE report ? 'composite_score' AND "
         f"{d['composite_score']}::text IS DISTINCT FROM split_part(report->>'composite_score', '.', 1)"),
    ]
    bad = 0
    for label, sql in checks:
        cur.execute(sql)
        n = cur.fetchone()[0]
        print(f"  {label}: {n}")
        if label.startswith("评分列") and n:
            bad += 1
    if BACKUP in tabs:
        cur.execute(f"SELECT count(*) FROM {BACKUP}")
        print(f"  原件备份表 {BACKUP}: {cur.fetchone()[0]} 行")
    print("取值:")
    for label, count_sql, _u, params in value_plan(cur):
        cur.execute(count_sql, (params[0],) if len(params) == 2 else params)
        print(f"  还没改的 {label}: {cur.fetchone()[0]}")
    return 1 if bad else 0


def main(argv: list) -> int:
    apply = "--apply" in argv
    backup = "--no-backup" not in argv
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 0
    con = _connect()
    if con is None:
        print("PG 不可达")
        return 1
    try:
        if "--verify" in argv:
            con.autocommit = True
            return verify(con)
        con.autocommit = False
        cur = con.cursor()
        print(f"模式: {'APPLY(真改)' if apply else '演练(不改)'}")

        plan = schema_plan(cur)
        con.rollback()
        if not apply:
            # 演练: 报告与取值只读不写; 结构语句放在最后执行再立刻回滚, 表锁只持续这一下
            print("3. 报告里的旧键:")
            run_reports(con, False, limit, backup)
            print("2. 取值:")
            run_values(cur, False)
            con.rollback()
            print(f"1. 结构({len(plan)} 条, 执行后回滚):")
            for sql in plan:
                cur.execute(sql)
                print(f"  可执行: {sql}")
            con.rollback()
            print("演练结束, 什么都没改。确认无误后加 --apply。")
            return 0

        print(f"1. 结构({len(plan)} 条):")
        for sql in plan:
            cur.execute(sql)
            print(f"  已执行: {sql}")
        con.commit()
        print("2. 取值:")
        run_values(cur, True)
        con.commit()
        print("3. 报告里的旧键:")
        run_reports(con, True, limit, backup)
        con.commit()
        print("完成。接着跑 --verify 看结果。")
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
