"""
一次性迁移: 日股 symbol 数字代码 → 英文 canonical id (在服务器 PG equipoise 上跑一次)。

背景: 日股原以东证数字代码(7011…)当标的 id, 会被 AI 当成价格。改版后 canonical id = 英文
简称(ADR ticker), 数字降级为 config 里的 ibkr_ticker(只用于询价)。本脚本把**已有 PG 记录**
的数字身份迁移成英文。

范围(严格按"辩论原文不动"):
  - 翻 `symbol` 列 7011→英文, 覆盖 7 张表: debates/postcheck/technical/news/evidence/company/fundamentals
  - technical 表额外重写 payload jsonb 里的 `{数字}_` 量化前缀键 → `{英文}_`(前端展示需要)
  - **不动** debates.report 正文 / postcheck 复盘文字等叙述性字段

⚠️ 先备份再跑!
  pg_dump -t debates -t postcheck -t technical -t news -t evidence -t company -t fundamentals \
          equipoise > /tmp/equipoise_jp_migrate_backup_$(date +%Y%m%dT%H%M%S).sql

用法(工作目录 /opt/equipoise, python 用 ./venv/bin/python):
  ./venv/bin/python -m datalayer.pg.migrate_jp_kr_symbols            # dry-run: 只数每表命中条数, 不改
  ./venv/bin/python -m datalayer.pg.migrate_jp_kr_symbols --apply    # 事务内真正执行

只映射有历史记录的 5 只老日股; 软银/伊藤忠(SFTBY/ITOCY)是新增, 无历史行, 不在映射内。
韩股同为数字代码, 将来入库即用英文 canonical, 无历史需迁, 故本脚本不含韩股。
"""

from __future__ import annotations

import sys

from datalayer.pg.db import connect

# 数字东证代码 → 英文 canonical id (仅含有历史记录的 5 只)
MAP = {
    "7974": "NTDOY",   # 任天堂
    "7011": "MHVYF",   # 三菱重工
    "4063": "SHECY",   # 信越化学
    "7741": "HOCPY",   # HOYA
    "2802": "AJINY",   # 味之素
}

# 只翻 symbol 列即可的表
PLAIN_TABLES = ["debates", "postcheck", "news", "evidence", "company", "fundamentals"]
# 需额外重写 payload 前缀的表
TECH_TABLE = "technical"


def _count(cur, table: str, symbol: str) -> int:
    cur.execute(f"SELECT count(*) FROM {table} WHERE symbol = %s", (symbol,))
    return cur.fetchone()[0]


def dry_run() -> None:
    with connect() as conn, conn.cursor() as cur:
        print("=== DRY-RUN: 各表命中条数 (数字代码 → 英文) ===")
        grand = 0
        for table in PLAIN_TABLES + [TECH_TABLE]:
            hits = [(old, MAP[old], _count(cur, table, old)) for old in MAP]
            n = sum(h[2] for h in hits)
            grand += n
            detail = ", ".join(f"{old}→{new}:{c}" for old, new, c in hits if c)
            print(f"  {table:<14} 共 {n:>4} 行   {detail or '(无)'}")
        print(f"--- 合计 {grand} 行待迁移 ---")
        print("确认无误后加 --apply 执行 (务必已 pg_dump 备份)。")


def _rewrite_tech_payload(payload: dict, old: str, new: str) -> dict:
    """重写 technical payload (递归): `{old}_` 前缀键 → `{new}_`; 内嵌 symbol 值 old→new;
    描述字段里的 `[old]` 标签 → `[new]` (量化描述前端会展示, 一并换掉)。价格里恰好含该数字
    的浮点值 (如 490.2802) 不受影响 —— 只替换整键前缀 / 恰等 symbol 的值 / 方括号标签。"""
    op, np_ = f"{old}_", f"{new}_"

    def walk(o):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                nk = np_ + k[len(op):] if isinstance(k, str) and k.startswith(op) else k
                v = walk(v)
                if isinstance(nk, str) and (nk.endswith("_symbol") or nk == "symbol") and v == old:
                    v = new
                out[nk] = v
            return out
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, str):
            return o.replace(f"[{old}]", f"[{new}]")
        return o

    return walk(payload) if isinstance(payload, dict) else payload


def apply() -> None:
    from psycopg2.extras import Json, RealDictCursor

    conn = connect()
    try:
        with conn.cursor() as cur:
            total = 0
            # 1) 只翻 symbol 列的表
            for table in PLAIN_TABLES:
                for old, new in MAP.items():
                    cur.execute(f"UPDATE {table} SET symbol = %s WHERE symbol = %s", (new, old))
                    if cur.rowcount:
                        total += cur.rowcount
                        print(f"  {table:<14} {old}→{new}: {cur.rowcount} 行")
            # 2) technical: 翻 symbol 列 + 重写 payload 前缀 (逐行)
            with conn.cursor(cursor_factory=RealDictCursor) as rcur:
                for old, new in MAP.items():
                    rcur.execute(
                        f"SELECT symbol, as_of, payload FROM {TECH_TABLE} WHERE symbol = %s", (old,))
                    rows = rcur.fetchall()
                    for r in rows:
                        new_payload = _rewrite_tech_payload(r["payload"], old, new)
                        cur.execute(
                            f"UPDATE {TECH_TABLE} SET symbol = %s, payload = %s "
                            f"WHERE symbol = %s AND as_of = %s",
                            (new, Json(new_payload), old, r["as_of"]))
                    if rows:
                        total += len(rows)
                        print(f"  {TECH_TABLE:<14} {old}→{new}: {len(rows)} 行 (含 payload 前缀重写)")
        conn.commit()
        print(f"✅ 迁移完成, 共 {total} 行已提交。debates.report 正文未动。")
    except Exception as e:
        conn.rollback()
        print(f"❌ 迁移失败, 已回滚: {type(e).__name__}: {e}")
        raise
    finally:
        conn.close()


def main() -> None:
    if "--apply" in sys.argv[1:]:
        apply()
    else:
        dry_run()


if __name__ == "__main__":
    main()
