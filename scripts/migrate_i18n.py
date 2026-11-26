#!/usr/bin/env python3
"""
debates / postcheck 表加多语言列 (2026-10-24; 复盘两列 2026-10-25 主工程加, 说明见 datalayer/pg/i18n.py)。幂等, 可重复跑。

  ./venv/bin/python scripts/migrate_i18n.py            # 空跑: 只报现状与将要做的事
  ./venv/bin/python scripts/migrate_i18n.py --apply    # 真跑: 备份 → 加列 → 按原文回填 lang

做的事:
  1. 备份 debates → debates_bak_i18n_20261024 (已存在就跳过, 不覆盖)
  2. ALTER TABLE debates ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT 'zh'
                                                 report_i18n jsonb NOT NULL DEFAULT '{}'
  3. 修乱码: 少数老报告(8 月前后 6 份)有字符串被 UTF-8→latin1 二次编码(如 'ä¸\\x8dæ\\x8e¥å\\x8f\\x97'),
     逐个字符串还原 —— 只在 latin1→utf8 能解码、结果不同且含汉字时才替换, 其余原样(report 与 next_review 列)
  4. 老行 lang 按 detect_lang(report) 回填(修完乱码后老数据应全是 zh; 检出 en 的单独列出来看)
除第 3 步修乱码外不改 report 内容, 不翻译任何东西。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from psycopg2.extras import Json  # noqa: E402

from datalayer.pg.i18n import detect_lang, has_cjk  # noqa: E402

BACKUP = "debates_bak_i18n_20261024"
MOJIBAKE_RE = r"[ÃÂäåæçèé][\x80-\xbf]"


def unmojibake(o, counter):
    """递归还原 UTF-8→latin1 二次编码的字符串; counter[0] 计替换个数。"""
    if isinstance(o, str):
        try:
            t = o.encode("latin-1").decode("utf-8")
        except UnicodeError:
            return o
        if t != o and has_cjk(t):
            counter[0] += 1
            return t
        return o
    if isinstance(o, dict):
        return {k: unmojibake(v, counter) for k, v in o.items()}
    if isinstance(o, list):
        return [unmojibake(v, counter) for v in o]
    return o


def _connect():
    con = psycopg2.connect(
        host=os.environ.get("EQUIPOISE_PG_HOST", "localhost"),
        port=int(os.environ.get("EQUIPOISE_PG_PORT", "5432")),
        dbname=os.environ.get("EQUIPOISE_PG_DB", "equipoise"),
        user=os.environ.get("EQUIPOISE_PG_USER", "postgres"),
        password=os.environ.get("EQUIPOISE_PG_PASSWORD", "postgres"),
        client_encoding="UTF8",
    )
    return con


def _cols(cur):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='debates'")
    return {r["column_name"] for r in cur.fetchall()}


def _exists(cur, table):
    cur.execute("SELECT to_regclass(%s) AS t", (table,))
    return cur.fetchone()["t"] is not None


def main():
    apply = "--apply" in sys.argv
    con = _connect()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cols = _cols(cur)
            cur.execute("SELECT count(*) AS n FROM debates")
            n = cur.fetchone()["n"]
            print(f"debates: {n} 行; 已有 lang={'lang' in cols}, report_i18n={'report_i18n' in cols}; "
                  f"备份表 {BACKUP} 已存在={_exists(cur, BACKUP)}")

            cur.execute("SELECT symbol, as_of, report, next_review FROM debates WHERE report::text ~ %s",
                        (MOJIBAKE_RE,))
            fixes = []
            for r in cur.fetchall():
                c = [0]
                rep = unmojibake(r["report"], c)
                nr = unmojibake(r["next_review"], c)
                if c[0]:
                    fixes.append((r["symbol"], r["as_of"], rep, nr, c[0]))
            print(f"乱码: {len(fixes)} 份报告待修")
            for sym, ts, rep, _, k in fixes:
                print(f"  {sym} @ {ts.isoformat()}: {k} 个字符串; statement → {(rep.get('statement') or '')[:40]}")
            fixed = {(s, t): rep for s, t, rep, _, _ in fixes}

            cur.execute("SELECT symbol, as_of, report->>'statement' AS st, report->>'debate_summary' AS ds, "
                        "report->>'lang' AS lg FROM debates")
            detected = {}
            for r in cur.fetchall():
                rep = fixed.get((r["symbol"], r["as_of"])) or {"statement": r["st"], "debate_summary": r["ds"], "lang": r["lg"]}
                lg = detect_lang(rep)
                detected[(r["symbol"], r["as_of"])] = lg
            en = [k for k, v in detected.items() if v == "en"]
            print(f"原文检测: zh={len(detected) - len(en)}, en={len(en)}")
            for sym, ts in en[:20]:
                print(f"  en? {sym} @ {ts.isoformat()}")

            if not apply:
                print("空跑结束。加 --apply 真跑。")
                return

            if not _exists(cur, BACKUP):
                cur.execute(f"CREATE TABLE {BACKUP} AS TABLE debates")
                print(f"已备份 → {BACKUP}")
            for sym, ts, rep, nr, _ in fixes:
                cur.execute("UPDATE debates SET report=%s, next_review=%s WHERE symbol=%s AND as_of=%s",
                            (Json(rep), Json(nr) if nr is not None else None, sym, ts))
            cur.execute("ALTER TABLE debates ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT 'zh'")
            cur.execute("ALTER TABLE debates ADD COLUMN IF NOT EXISTS report_i18n jsonb NOT NULL DEFAULT '{}'::jsonb")
            # 2026-10-25 主工程: 复盘表同样两列(只翻 verdict 一句; 写库时按判词有无汉字定 lang, 见 postcheck_store)
            cur.execute("ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT 'zh'")
            cur.execute("ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS report_i18n jsonb NOT NULL DEFAULT '{}'::jsonb")
            print("postcheck: lang / report_i18n 列已就位(幂等)")
            for sym, ts in en:
                cur.execute("UPDATE debates SET lang='en' WHERE symbol=%s AND as_of=%s", (sym, ts))
            con.commit()
            cur.execute("SELECT lang, count(*) AS n FROM debates GROUP BY lang ORDER BY lang")
            print("完成:", {r["lang"]: r["n"] for r in cur.fetchall()})
    finally:
        con.close()


if __name__ == "__main__":
    main()
