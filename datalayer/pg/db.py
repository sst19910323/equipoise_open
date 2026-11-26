"""
低频数据写入 (PostgreSQL equipoise) —— 本工程 equipoise 的低频【写侧】。

库主 2026-06-14 定单库 equipoise(PG); 写侧原在 apex_codex, 2026-06-30 整合并入本工程。
本模块是【写侧】。三张表:

  economic(region, indicator, value, consensus, unit, release_time)
      经济指标。region ∈ {US, EZ, UK, JP, KR}。PK (region, indicator, release_time)。

  company(symbol, name, region, sector, industry, currency, description, updated_at)
      公司静态身份(基本不变, 偶尔微调)。symbol = 代号 = id。PK (symbol)。

  fundamentals(symbol, field, span, value, unit, consensus, release_date)
      公司财务(随期变, 长格式)。field 各家按自身口径精确命名(不强制跨公司共享);
      span = 周期长度 3M/6M/9M/12M(不记 FY 年份标签, 用 release_date 当时间轴)。
      同公司同 field 同 span 跨 release_date → 时间序列。PK (symbol, field, release_date, span)。

字段哲学(不变): 只存发布原值 + consensus; 前值/变化/超预期读时算。
复现走 as_of: economic 按 release_time, fundamentals 按 release_date (防 lookahead)。

连接: 默认 localhost:5432/equipoise/postgres/postgres, 可被 EQUIPOISE_PG_* 覆盖。

用法 (服务器上跑, PG 在 localhost, python 用 ./venv/bin/python):
    db.py init                       # 建全部三张表 (IF NOT EXISTS)
    db.py load <staging.json>        # economic upsert
    db.py load-company <file>        # company upsert
    db.py load-fund <file>           # fundamentals upsert
    db.py snapshot <region>          # economic 某区最新快照
    db.py snapshot-fund <symbol>     # fundamentals 某公司最新 (现值/前值/Δ)
    db.py reset                      # TRUNCATE economic (重灌用)

staging JSON:
    economic     : {"points":[{"region","indicator","value","consensus?","unit","release_time"}]}
    company      : {"companies":[{"symbol","name","region","sector","industry","currency","description"}]}
    fundamentals : {"points":[{"symbol","field","span","value","unit","consensus?","release_date"}]}
  consensus 没有就省略该键。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # datalayer/pg/<this> → equipoise root

PG = {
    "host": os.environ.get("EQUIPOISE_PG_HOST", "localhost"),
    "port": int(os.environ.get("EQUIPOISE_PG_PORT", "5432")),
    "dbname": os.environ.get("EQUIPOISE_PG_DB", "equipoise"),
    "user": os.environ.get("EQUIPOISE_PG_USER", "postgres"),
    "password": os.environ.get("EQUIPOISE_PG_PASSWORD", "postgres"),
}
T_ECON = os.environ.get("EQUIPOISE_PG_ECON_TABLE", "economic")
T_COMPANY = os.environ.get("EQUIPOISE_PG_COMPANY_TABLE", "company")
T_FUND = os.environ.get("EQUIPOISE_PG_FUND_TABLE", "fundamentals")
T_SENT = os.environ.get("EQUIPOISE_PG_SENT_TABLE", "sentiment")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {T_ECON} (
    region        TEXT             NOT NULL,
    indicator     TEXT             NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    consensus     DOUBLE PRECISION,
    unit          TEXT,
    release_time  DATE             NOT NULL,
    PRIMARY KEY (region, indicator, release_time)
);
CREATE TABLE IF NOT EXISTS {T_COMPANY} (
    symbol       TEXT PRIMARY KEY,
    name         TEXT,
    region       TEXT,
    sector       TEXT,
    industry     TEXT,
    currency     TEXT,
    description  TEXT,
    updated_at   DATE
);
CREATE TABLE IF NOT EXISTS {T_FUND} (
    symbol        TEXT             NOT NULL,
    field         TEXT             NOT NULL,
    span          TEXT             NOT NULL,   -- 周期长度 3M/6M/9M/12M (不记 FY 年份标签)
    value         DOUBLE PRECISION NOT NULL,
    unit          TEXT,
    consensus     DOUBLE PRECISION,
    release_date  DATE             NOT NULL,   -- 公布日 = 时间轴 + as_of 防 lookahead
    PRIMARY KEY (symbol, field, release_date, span)
);
CREATE TABLE IF NOT EXISTS {T_SENT} (
    region        TEXT             NOT NULL,
    indicator     TEXT             NOT NULL,   -- US: fear_greed/vix ; EZ: vstoxx ; JP: nikkei_vi ; KR: vkospi
    value         DOUBLE PRECISION NOT NULL,
    unit          TEXT,
    date          DATE             NOT NULL,   -- 日频; 前值/周环比从历史算, 不另存
    PRIMARY KEY (region, indicator, date)
);
"""


def connect():
    import psycopg2
    return psycopg2.connect(connect_timeout=6, client_encoding="UTF8", **PG)


def init_db() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
    print(f"tables ready @ {PG['user']}@{PG['host']}/{PG['dbname']}: {T_ECON}, {T_COMPANY}, {T_FUND}, {T_SENT}")


def reset() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {T_ECON}")
    print(f"truncated {T_ECON}")


def _f(v):
    return float(v) if v is not None else None


def load_staging(path: str) -> None:
    pts = json.loads(Path(path).read_text(encoding="utf-8"))["points"]
    rows = [(p["region"], p["indicator"], float(p["value"]), _f(p.get("consensus")),
             p.get("unit"), p["release_time"]) for p in pts]
    sql = f"""INSERT INTO {T_ECON} (region,indicator,value,consensus,unit,release_time)
              VALUES (%s,%s,%s,%s,%s,%s)
              ON CONFLICT (region,indicator,release_time)
              DO UPDATE SET value=EXCLUDED.value, consensus=EXCLUDED.consensus, unit=EXCLUDED.unit"""
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
    print(f"economic: upserted {len(rows)} from {path}")


def load_company(path: str) -> None:
    cos = json.loads(Path(path).read_text(encoding="utf-8"))["companies"]
    rows = [(c["symbol"], c.get("name"), c.get("region"), c.get("sector"),
             c.get("industry"), c.get("currency"), c.get("description"),
             c.get("updated_at")) for c in cos]
    sql = f"""INSERT INTO {T_COMPANY} (symbol,name,region,sector,industry,currency,description,updated_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (symbol) DO UPDATE SET
                name=EXCLUDED.name, region=EXCLUDED.region, sector=EXCLUDED.sector,
                industry=EXCLUDED.industry, currency=EXCLUDED.currency,
                description=EXCLUDED.description, updated_at=EXCLUDED.updated_at"""
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
    print(f"company: upserted {len(rows)} from {path}")


def load_fund(path: str) -> None:
    pts = json.loads(Path(path).read_text(encoding="utf-8"))["points"]
    rows = [(p["symbol"], p["field"], p["span"], float(p["value"]),
             p.get("unit"), _f(p.get("consensus")), p["release_date"]) for p in pts]
    sql = f"""INSERT INTO {T_FUND} (symbol,field,span,value,unit,consensus,release_date)
              VALUES (%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (symbol,field,release_date,span)
              DO UPDATE SET value=EXCLUDED.value, unit=EXCLUDED.unit, consensus=EXCLUDED.consensus"""
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
    print(f"fundamentals: upserted {len(rows)} from {path}")


def load_sentiment(path: str) -> None:
    pts = json.loads(Path(path).read_text(encoding="utf-8"))["points"]
    rows = [(p["region"], p["indicator"], float(p["value"]), p.get("unit"), p["date"]) for p in pts]
    sql = f"""INSERT INTO {T_SENT} (region,indicator,value,unit,date)
              VALUES (%s,%s,%s,%s,%s)
              ON CONFLICT (region,indicator,date)
              DO UPDATE SET value=EXCLUDED.value, unit=EXCLUDED.unit"""
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
    print(f"sentiment: upserted {len(rows)} from {path}")


def snapshot_sent(region: str) -> None:
    from psycopg2.extras import RealDictCursor
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT indicator,value,unit,date FROM {T_SENT} "
                    f"WHERE region=%s ORDER BY indicator,date", (region,))
        rows = cur.fetchall()
    if not rows:
        print(f"(no sentiment for region={region})"); return
    by = {}
    for r in rows:
        by.setdefault(r["indicator"], []).append(r)
    print(f"=== {region} sentiment ({len(by)} indicators, {len(rows)} pts) ===")
    for ind, h in by.items():
        c = h[-1]; p = h[-2] if len(h) >= 2 else None
        wk = next((x for x in reversed(h[:-1]) if (c["date"]-x["date"]).days >= 5), None)
        chg = f"{c['value']-p['value']:+g}" if p else "—"
        wkc = f"{c['value']-wk['value']:+g}" if wk else "—"
        print(f"  {ind:<12} {c['value']} {c['unit'] or ''}  @{c['date']}  前值Δ{chg}  周Δ{wkc}  ({len(h)}天历史)")


def _fmt(v, unit):
    return "—" if v is None else f"{v}{(' ' + unit) if unit else ''}"


def snapshot(region: str) -> None:
    from psycopg2.extras import RealDictCursor
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT indicator,value,consensus,unit,release_time FROM {T_ECON} "
                    f"WHERE region=%s ORDER BY indicator,release_time", (region,))
        rows = cur.fetchall()
    if not rows:
        print(f"(no economic data for region={region})"); return
    by = {}
    for r in rows:
        by.setdefault(r["indicator"], []).append(r)
    print(f"=== latest {region} economic ({len(by)} indicators) ===")
    for ind, h in by.items():
        c = h[-1]; p = h[-2] if len(h) >= 2 else None
        chg = f"{c['value']-p['value']:+g}" if p else "—"
        sur = f"{c['value']-c['consensus']:+g}" if c["consensus"] is not None else "—"
        print(f"  {ind:<24} 现值 {_fmt(c['value'],c['unit']):<12} 预期 {_fmt(c['consensus'],c['unit']):<10} "
              f"较前值Δ {chg}  超预期 {sur}  @{c['release_time']}")


def snapshot_fund(symbol: str) -> None:
    """某公司每个 (field, span) 最新一期 + 同 span 前一期 Δ (按 release_date 排序)。

    只比同 span(3M vs 3M / 12M vs 12M), 不会拿 9M 减 12M。
    """
    from psycopg2.extras import RealDictCursor
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT name,sector,currency FROM {T_COMPANY} WHERE symbol=%s", (symbol,))
        co = cur.fetchone()
        cur.execute(f"SELECT field,span,value,unit,consensus,release_date FROM {T_FUND} "
                    f"WHERE symbol=%s ORDER BY field, span, release_date", (symbol,))
        rows = cur.fetchall()
    head = f"{symbol}" + (f" — {co['name']} [{co['sector']}, {co['currency']}]" if co else " (无 company 记录)")
    print(f"=== {head} ===")
    if not rows:
        print("(no fundamentals)"); return
    by = {}
    for r in rows:
        by.setdefault((r["field"], r["span"]), []).append(r)
    for (field, span), h in by.items():
        c = h[-1]; p = h[-2] if len(h) >= 2 else None
        chg = f"{c['value']-p['value']:+g}" if p else "—"
        sur = f"{c['value']-c['consensus']:+g}" if c["consensus"] is not None else "—"
        prev = f"(前 {_fmt(p['value'],c['unit'])}@{p['release_date']})" if p else ""
        print(f"  {field:<20} [{span:>3}] {_fmt(c['value'],c['unit']):<14} 发布 {c['release_date']}  Δ{chg} 超预期{sur} {prev}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "init":
        init_db()
    elif cmd == "reset":
        reset()
    elif cmd == "load":
        load_staging(sys.argv[2])
    elif cmd == "load-company":
        load_company(sys.argv[2])
    elif cmd == "load-fund":
        load_fund(sys.argv[2])
    elif cmd == "load-sentiment":
        load_sentiment(sys.argv[2])
    elif cmd == "snapshot-sent":
        snapshot_sent(sys.argv[2])
    elif cmd == "snapshot":
        snapshot(sys.argv[2])
    elif cmd == "snapshot-fund":
        snapshot_fund(sys.argv[2])
    else:
        print(f"unknown command: {cmd}"); sys.exit(1)


if __name__ == "__main__":
    main()
