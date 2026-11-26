# -*- coding: utf-8 -*-
"""
lowfreq_reader.py — 只读对接 Equipoise 低频数据脑 (PostgreSQL)

分工 (库主 2026-06-14 定):
    单库 equipoise (PG) · 写侧=datalayer/pg/db.py 等(2026-06-30 已并入本工程) · 本模块只读
    本模块 = 读侧唯一接口: 喂三个辩论 AI + 后端 /api/economic

契约表 economic (列以服务器实际为准, 本模块 SELECT * 容错):
    至少含 大区 / 变量名 / 值 / 时间; 可能还有 consensus(预测) + unit,
    或一个"预测 vs 真实"的标志列。SELECT * 后整行透传, 不写死列名。
    region ∈ {US, EZ, UK, JP, KR}

设计要点:
  - 只读, 绝不写。
  - 连接参数: 环境变量 EQUIPOISE_PG_* > data_sources.yaml(lowfreq_pg) > 默认 localhost/postgres
  - 关键列名(大区/变量名/时间)可配, 默认 region/indicator/release_time
  - PG 不可达 / 库表缺失 / 列名不符 / 驱动没装 → 一律优雅返回 None/[] (不崩);
    辩论装配已 PG-only, 拿到空就 macro_data 留空, 不再回退 JSON
  - 低频读, 每次调用开-关连接, 不做连接池
"""

from __future__ import annotations

import os
import re
import json
import logging
import time
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # datalayer/pg/<this> → equipoise root
CONFIG_PATH = PROJECT_ROOT / "config" / "data_sources.yaml"

# 议会引擎 symbol region (US/EU/JP/KR) → 数据脑 region (US/EZ/UK/JP/KR)
REGION_MAP = {
    "US": "US",
    "EU": "EZ",   # 欧元区
    "EZ": "EZ",
    "UK": "UK",
    "JP": "JP",
    "KR": "KR",   # 韩国 (与日本独立)
}

_DEFAULT_CONN = {
    "host": "localhost",
    "port": 5432,
    "dbname": "equipoise",
    "user": "postgres",
    "password": "postgres",
    "economic_table": "economic",
    # 关键列名 (大区/变量名/时间); 服务器实际列名不同就在 yaml 里改这三个
    "region_col": "region",
    "indicator_col": "indicator",
    "time_col": "release_time",
    # 公司基本面两表: company(静态, 每标的1行) + fundamentals(可变长表 point-in-time, 键 symbol+field+span)
    "company_table": "company",
    "fundamentals_table": "fundamentals",
    "fund_time_col": "release_date",
    # 市场情绪表 sentiment(同 economic 结构: region/indicator/value/unit/date)。US=fear_greed/EZ=vstoxx/JP=nikkei_vi/KR=vkospi
    "sentiment_table": "sentiment",
    "sentiment_time_col": "date",
    # 证据表 evidence(证据官 Scout 产出: symbol/generated_at/evidence jsonb)。一行=一批证据 list。equipoise 自己写。
    "evidence_table": "evidence",
    "evidence_time_col": "generated_at",
    # 技术快照表 technical(symbol/as_of/payload jsonb, as_of=market_data_timestamp_utc)。equipoise 自己写。
    "technical_table": "technical",
    "technical_time_col": "as_of",
    # 新闻表 news(symbol/fetched_at/items jsonb, 一行=一批文章 list)。equipoise 自己写。
    "news_table": "news",
    "news_time_col": "fetched_at",
}

# 某区 point-in-time 快照: 每个变量取"截至 as_of 时刻 release_time 最大的那一行"。
# as_of 为空 → 当前最新(实时辩论); 给 UTC 时刻 → 截至该刻最新(复现历史报告/防 lookahead)。
# {asof_outer}/{asof_sub} 仅在有 as_of 时注入 "AND time <= %s"。SELECT * 透传所有列。
_SNAPSHOT_SQL = """
SELECT * FROM {table} e
WHERE e.{region_col} = %s{asof_outer}
  AND e.{time_col} = (
        SELECT MAX({time_col}) FROM {table}
        WHERE {region_col} = e.{region_col} AND {indicator_col} = e.{indicator_col}{asof_sub}
  )
ORDER BY e.{indicator_col}
"""

# 公司可变基本面 point-in-time 快照: 每个 (symbol, field, span) 取"截至 as_of release_date 最大那一行"。
_FUND_SNAPSHOT_SQL = """
SELECT * FROM {table} e
WHERE e.symbol = %s{asof_outer}
  AND e.{time_col} = (
        SELECT MAX({time_col}) FROM {table}
        WHERE symbol = e.symbol AND field = e.field AND span = e.span{asof_sub}
  )
ORDER BY e.field, e.span
"""


def _load_pg_cfg() -> Dict:
    """合并默认 / yaml / env 得到连接配置。"""
    cfg = dict(_DEFAULT_CONN)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
        pg = (y.get("data_sources", {}) or {}).get("lowfreq_pg", {}) or {}
        for k in cfg:
            if k in pg and pg[k] is not None:
                cfg[k] = pg[k]
    except Exception:
        pass
    env_map = {
        "host": "EQUIPOISE_PG_HOST", "port": "EQUIPOISE_PG_PORT", "dbname": "EQUIPOISE_PG_DB",
        "user": "EQUIPOISE_PG_USER", "password": "EQUIPOISE_PG_PASSWORD",
        "economic_table": "EQUIPOISE_PG_ECON_TABLE",
        "region_col": "EQUIPOISE_PG_REGION_COL", "indicator_col": "EQUIPOISE_PG_INDICATOR_COL",
        "time_col": "EQUIPOISE_PG_TIME_COL",
        "company_table": "EQUIPOISE_PG_COMPANY_TABLE", "fundamentals_table": "EQUIPOISE_PG_FUND_TABLE",
        "fund_time_col": "EQUIPOISE_PG_FUND_TIME_COL",
        "sentiment_table": "EQUIPOISE_PG_SENTIMENT_TABLE", "sentiment_time_col": "EQUIPOISE_PG_SENTIMENT_TIME_COL",
        "evidence_table": "EQUIPOISE_PG_EVIDENCE_TABLE", "evidence_time_col": "EQUIPOISE_PG_EVIDENCE_TIME_COL",
        "technical_table": "EQUIPOISE_PG_TECH_TABLE", "technical_time_col": "EQUIPOISE_PG_TECH_TIME_COL",
        "news_table": "EQUIPOISE_PG_NEWS_TABLE", "news_time_col": "EQUIPOISE_PG_NEWS_TIME_COL",
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            cfg[k] = v
    return cfg


def _safe_ident(name: str, default: str) -> str:
    """标识符(表名/列名)不能走参数占位, 白名单校验防注入。"""
    name = str(name or default)
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else default


def _connect_ro(cfg: Dict):
    """只读连接。失败 → None (静默回退)。"""
    try:
        import psycopg2
    except Exception as e:
        logging.debug(f"[lowfreq_reader] psycopg2 未装, 跳过 PG: {e}")
        return None
    last_err = None
    for attempt in range(1, 4):
        try:
            dsn = (
                f"host={cfg['host']} port={int(cfg['port'])} dbname={cfg['dbname']} "
                f"user={cfg['user']} password={cfg['password']} connect_timeout=4 "
                "client_encoding=UTF8 sslmode=disable "
                "options='-c default_transaction_read_only=on'"
            )
            return psycopg2.connect(dsn)
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(0.5)
    tcp_probe = "unknown"
    try:
        with socket.create_connection((cfg["host"], int(cfg["port"])), timeout=2):
            tcp_probe = "open"
    except Exception as probe_err:
        tcp_probe = f"{type(probe_err).__name__}: {probe_err}"
    logging.warning(
        f"[lowfreq_reader] PG 连接失败, 回退旧源: {type(last_err).__name__}: {last_err!r}; "
        f"tcp_probe={cfg['host']}:{cfg['port']} {tcp_probe}"
    )
    return None


def get_economic_snapshot(region: str = "US", as_of=None) -> List[Dict]:
    """取某区 point-in-time 最新快照 (每变量最新一行, 整行透传)。
    as_of=None → 当前最新(实时辩论); 给 UTC 时刻(str/datetime) → 截至该刻最新(复现历史报告)。
    PG 不可用 → []。"""
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        tcol = _safe_ident(cfg["time_col"], "release_time")
        asof_outer = f" AND e.{tcol} <= %s" if as_of else ""
        asof_sub = f" AND {tcol} <= %s" if as_of else ""
        sql = _SNAPSHOT_SQL.format(
            table=_safe_ident(cfg["economic_table"], "economic"),
            region_col=_safe_ident(cfg["region_col"], "region"),
            indicator_col=_safe_ident(cfg["indicator_col"], "indicator"),
            time_col=tcol, asof_outer=asof_outer, asof_sub=asof_sub,
        )
        params = [db_region] + ([as_of, as_of] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 economic({db_region}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_economic_for_prompt(region: str = "US", as_of=None) -> Optional[str]:
    """把某区快照格式化成喂给三个 AI 的 JSON 字符串。

    as_of=None → 当前最新; 给 UTC 时刻 → 截至该刻最新。
    返回 None 表示数据脑不可用 / 该区无数据。
    整行透传 (含 consensus / 预测-真实标志等), 让 AI 自行判断 beat/miss。
    """
    snap = get_economic_snapshot(region, as_of=as_of)
    if not snap:
        return None
    cfg = _load_pg_cfg()
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    tcol = cfg["time_col"]
    latest = max((str(r.get(tcol)) for r in snap if r.get(tcol)), default=None)
    payload = {
        "source": "equipoise.pg.economic",
        "region": db_region,
        "as_of_query": str(as_of) if as_of else None,  # 查询截止时刻 (复现历史时传)
        "latest_release": latest,                       # 结果里最新 release_time (新鲜度参考)
        "indicators": snap,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ───────────────────────── 市场情绪 (sentiment 表, 同 economic 结构) ───────────────────────── #

def get_sentiment_snapshot(region: str = "US", as_of=None) -> List[Dict]:
    """市场情绪 point-in-time 快照 (sentiment 表: US=fear_greed / EZ=vstoxx / JP=nikkei_vi / KR=vkospi)。
    as_of=None → 当前最新; 给 UTC 时刻 → 截至该刻最新。PG 不可用 → []。"""
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        tcol = _safe_ident(cfg["sentiment_time_col"], "date")
        asof_outer = f" AND e.{tcol} <= %s" if as_of else ""
        asof_sub = f" AND {tcol} <= %s" if as_of else ""
        sql = _SNAPSHOT_SQL.format(
            table=_safe_ident(cfg["sentiment_table"], "sentiment"),
            region_col=_safe_ident(cfg["region_col"], "region"),
            indicator_col=_safe_ident(cfg["indicator_col"], "indicator"),
            time_col=tcol, asof_outer=asof_outer, asof_sub=asof_sub,
        )
        params = [db_region] + ([as_of, as_of] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 sentiment({db_region}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_sentiment_for_prompt(region: str = "US", as_of=None) -> Optional[str]:
    """市场情绪 → fear_greed_data JSON 字符串 (按区: US=fear_greed/EZ=vstoxx/JP=nikkei_vi/KR=vkospi)。无 → None。"""
    snap = get_sentiment_snapshot(region, as_of=as_of)
    if not snap:
        return None
    cfg = _load_pg_cfg()
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    tcol = cfg["sentiment_time_col"]
    latest = max((str(r.get(tcol)) for r in snap if r.get(tcol)), default=None)
    payload = {
        "source": "equipoise.pg.sentiment",
        "region": db_region,
        "as_of_query": str(as_of) if as_of else None,
        "latest_date": latest,
        "indicators": snap,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ───────────────────────── 证据 (evidence 表, Scout 产出, 复现用) ───────────────────────── #

def get_evidence_snapshot(symbol: str, as_of=None, max_age_hours: float = 1.0) -> Dict:
    """某标的 point-in-time 证据快照: 取"截至 as_of 最新的那一批"(整批 evidence list)。
    as_of=None → 当前最新; 给 UTC 时刻 → 截至该刻最新(复现历史报告)。
    max_age_hours: 找到的证据若早于 as_of(或当前时间)超过此值则视为过期返回 {}; 0=不限。
    返回 {generated_at, items}; 无 / 过期 / PG 不通 → {}。
    注意: 辩论内不读此表(证据现生成现喂 in-memory), 本接口仅供 /api/evidence 复现/人看。"""
    import datetime
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        tbl = _safe_ident(cfg["evidence_table"], "evidence")
        tcol = _safe_ident(cfg["evidence_time_col"], "generated_at")
        where_asof = f" AND {tcol} <= %s" if as_of else ""
        sql = (f"SELECT * FROM {tbl} WHERE symbol = %s{where_asof} "
               f"ORDER BY {tcol} DESC LIMIT 1")
        params = [str(symbol).upper()] + ([as_of] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            return {}
        generated_at = row.get(cfg["evidence_time_col"])
        if max_age_hours and max_age_hours > 0 and generated_at is not None:
            anchor = (datetime.datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                      if as_of else datetime.datetime.now(datetime.timezone.utc))
            if hasattr(generated_at, "tzinfo") and generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=datetime.timezone.utc)
            age_hours = (anchor - generated_at).total_seconds() / 3600
            if age_hours > max_age_hours:
                return {}
        return {"generated_at": str(generated_at),
                "items": row.get("evidence") or []}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 evidence({symbol}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_evidence_for_prompt(symbol: str, as_of=None) -> Optional[str]:
    """某标的最新证据批 → JSON 字符串 (复现/展示用)。无 → None。"""
    snap = get_evidence_snapshot(symbol, as_of=as_of)
    if not snap or not snap.get("items"):
        return None
    payload = {
        "source": "equipoise.pg.evidence",
        "symbol": str(symbol).upper(),
        "as_of_query": str(as_of) if as_of else None,
        "generated_at": snap.get("generated_at"),
        "items": snap.get("items"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ───────────────────────── 技术快照 + 新闻 (technical / news 表, 前端/复现用) ───────────────────────── #

def _latest_one(table_key: str, time_col_key: str, value_col: str, symbol: str, as_of=None) -> Dict:
    """通用: 取某标的"截至 as_of 最新一行"的 (时间, value_col)。无 / PG 不通 → {}。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        tbl = _safe_ident(cfg[table_key], table_key.replace("_table", ""))
        tcol = _safe_ident(cfg[time_col_key], "as_of")
        where_asof = f" AND {tcol} <= %s" if as_of else ""
        sql = (f"SELECT * FROM {tbl} WHERE symbol = %s{where_asof} "
               f"ORDER BY {tcol} DESC LIMIT 1")
        params = [str(symbol).upper()] + ([as_of] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            return {}
        return {"time": str(row.get(cfg[time_col_key])), "value": row.get(value_col)}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 {table_key}({symbol}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_technical_snapshot(symbol: str, as_of=None) -> Dict:
    """某标的 point-in-time 技术快照: 截至 as_of 最新的那一份(整个 payload)。
    返回 {as_of, payload}; 无 / PG 不通 → {}。equipoise 自己写的 technical 表。"""
    r = _latest_one("technical_table", "technical_time_col", "payload", symbol, as_of)
    return {"as_of": r["time"], "payload": r["value"]} if r else {}


def get_news_snapshot(symbol: str, as_of=None) -> Dict:
    """某标的 point-in-time 新闻: 截至 as_of 最新抓取的那一批(文章 list)。
    返回 {fetched_at, items}; 无 / PG 不通 → {}。equipoise 自己写的 news 表。"""
    r = _latest_one("news_table", "news_time_col", "items", symbol, as_of)
    return {"fetched_at": r["time"], "items": r["value"] or []} if r else {}


# ───────────────────────── 辩论报告 (debates 表, equipoise 自己写, 主页/报告视图读) ───────────────────────── #
# debates(symbol, as_of, report_generated_at, composite_score, operation_type, operation_target, operation_size,
#         next_review jsonb, report jsonb), PK(symbol, as_of)。as_of = market_data_timestamp_utc。
# 2026-10-01 改名: 列 action → composite_score, operation_volume → operation_size。库里的实际列名由 names 模块探测
# (迁移前后都能读); 读出来的行一律带新键, 整行读取时旧键也还在。
def _debate_meta_cols(cur) -> str:
    from datalayer.pg import names as _names
    n = _names.debates(cur)
    meta = (f"as_of, report_generated_at, {n['composite_score']} AS composite_score, operation_type, "
            f"operation_target, {n['operation_size']} AS operation_size")
    if "lang" in _names.columns(cur, "debates"):   # 2026-10-24 多语言列(scripts/migrate_i18n.py)
        meta += ", lang, ARRAY(SELECT jsonb_object_keys(report_i18n)) AS i18n_langs"
    return meta


def _new_keys(row) -> Dict:
    from datalayer.pg import names as _names
    return _names.with_new_keys(dict(row))


def _hydrate_group_decisions(cur, rows):
    """Load shared hearings in one query. Never substitute a nearby hearing."""
    from workflows.group_report_view import group_for_display, project_member_report
    refs = {}
    valid = {}
    for row in rows:
        report = row["report"] or {}
        if "debate_ref" not in report:
            valid[(row["symbol"], row["as_of"])] = row
            continue
        ref = report["debate_ref"]
        if (isinstance(ref, dict) and isinstance(ref.get("symbol"), str)
                and _norm_ts(ref.get("as_of")) == row["as_of"]):
            refs.setdefault((ref["symbol"], row["as_of"]), []).append(row)
    if refs:
        cur.execute("SELECT symbol, as_of, report FROM debate_group_sessions WHERE (symbol, as_of) IN %s",
                    [tuple(refs)])
        for archive in cur.fetchall():
            key = (archive["symbol"], archive["as_of"])
            doc = archive["report"]
            if doc.get("symbol") != key[0] or _norm_ts(doc.get("market_data_timestamp_utc")) != key[1]:
                continue
            doc = group_for_display(doc)
            for row in refs[key]:
                try:
                    view = project_member_report(doc, row["symbol"])
                except ValueError as exc:
                    logging.warning("Invalid group decision %s / %s: %s", key, row["symbol"], exc)
                    continue
                valid[(row["symbol"], row["as_of"])] = dict(row, report=view)
    return [valid[key] for key in ((r["symbol"], r["as_of"]) for r in rows) if key in valid]


def get_debate_latest_batch(symbols=None) -> Dict[str, Dict]:
    """**一条 SQL** 取所有(或给定)标的各自最新一份报告整行。返回 {SYMBOL: row}。
    给 dashboard 用: 一次往返替代 N 次单标的查询(N+1 → 1)。PG 不通 → {}。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        if symbols:
            sql = ("SELECT DISTINCT ON (symbol) * FROM debates "
                   "WHERE symbol = ANY(%s) ORDER BY symbol, as_of DESC")
            params = [[str(s).upper() for s in symbols]]
        else:
            sql = "SELECT DISTINCT ON (symbol) * FROM debates ORDER BY symbol, as_of DESC"
            params = []
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = _hydrate_group_decisions(cur, cur.fetchall())
        return {str(r["symbol"]).upper(): _new_keys(r) for r in rows}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 批量 debates 查询失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_debate_latest(symbol: str, as_of=None) -> Dict:
    """某标的截至 as_of 的最新一份报告整行(含 report jsonb + 决策列)。无 / PG 不通 → {}。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        where = " AND as_of <= %s" if as_of else ""
        sql = f"SELECT * FROM debates WHERE symbol = %s{where} ORDER BY as_of DESC LIMIT 1"
        params = [str(symbol).upper()] + ([_norm_ts(as_of)] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = _hydrate_group_decisions(cur, cur.fetchall())
        return _new_keys(rows[0]) if rows else {}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 debates({symbol}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def list_debates(symbol: str, limit: int = 300, months: float = 0) -> List[Dict]:
    """某标的报告元数据(不含 report jsonb, 轻量), 时间倒序。months>0 时只取最近 N 个月(按 as_of)。
    无 / PG 不通 → []。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        where, params = ["symbol = %s"], [str(symbol).upper()]
        if months and months > 0:
            where.append("as_of >= %s")
            params.append(datetime.now(timezone.utc) - timedelta(days=months * 30.5))
        params.append(int(limit))
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            # 2026-11-06 (frontend): the execution card's action word per row — the primary card's `now` and every
            # lens card's `now` (lens-wise reports), projected in SQL so the list stays light (no report jsonb)
            sql = (f"SELECT {_debate_meta_cols(cur)}, report->'execution_card'->>'now' AS card_now, "
                   f"jsonb_path_query_array(report, '$.by_lens.*.execution_card.now') AS lens_nows, "
                   # Reproject old undecodable cards with the same reader used for report detail.
                   f"CASE WHEN report->'execution_card'->>'kind' = 'undeterminable' OR "
                   f"jsonb_path_exists(report, '$.by_lens.*.execution_card ? (@.kind == \"undeterminable\")') "
                   f"THEN report END AS pending_card_repair, "
                   # 2026-11-10 (frontend): the simple card's action word — stored card's two fields, and whether a
                   # v3.4 report without one has the tree and ruler to project it from
                   f"report->'operation_simple'->>'operation_type' AS simple_op, report->'operation_simple'->>'order' AS simple_order, "
                   f"(report ? 'scenario' AND report ? 'odds') AS has_tree "
                   f"FROM debates WHERE {' AND '.join(where)} ORDER BY as_of DESC LIMIT %s")
            cur.execute(sql, params)
            rows = cur.fetchall()
        for row in rows:
            report = row.pop("pending_card_repair", None)
            if report:
                from workflows.execution_card import ensure_display_fields
                ensure_display_fields(report)
                row["card_now"] = report["execution_card"]["now"]
                row["lens_nows"] = [v["execution_card"]["now"] for v in (report.get("by_lens") or {}).values()
                                    if isinstance(v, dict) and v.get("execution_card")]
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 debates 列表({symbol}) 失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _norm_ts(v):
    """紧凑 YYYYMMDDTHHMMSSZ / ISO 字符串 / datetime → datetime(UTC)。失败 → 原值(交给 PG 解析)。"""
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return v


def get_debate_report_row(symbol: str, as_of, *, member_symbol: Optional[str] = None, exact=False) -> Dict:
    """同 get_debate_report, 但连多语言列一起给: {report, lang, report_i18n}。精确无则取 <= as_of 最新。无 → {}。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    as_of = _norm_ts(as_of)
    try:
        from psycopg2.extras import RealDictCursor
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            owner = str(symbol).upper()
            if member_symbol is None:
                cur.execute("SELECT * FROM debates WHERE symbol = %s AND as_of = %s", [owner, as_of])
            else:
                member = member_symbol.strip().upper()
                cur.execute("SELECT * FROM debates WHERE as_of=%s AND "
                            "(symbol=%s OR (symbol=%s AND report->'debate_ref'->>'symbol'=%s)) "
                            "ORDER BY (symbol=%s) DESC LIMIT 1", [as_of, owner, member, owner, member])
            row = cur.fetchone()
            if not row and member_symbol is None and not exact:
                cur.execute("SELECT * FROM debates WHERE symbol = %s AND as_of <= %s "
                            "ORDER BY as_of DESC LIMIT 1", [str(symbol).upper(), as_of])
                row = cur.fetchone()
            if row and "debate_ref" in (row.get("report") or {}):
                if member_symbol is not None and row["symbol"] != member:
                    return {}
                rows = _hydrate_group_decisions(cur, [row])
                if not rows:
                    return {}
                row = rows[0]
                return {"report": row["report"], "lang": row.get("lang"), "report_i18n": row.get("report_i18n") or {}}
        if not row:
            return {}
        if member_symbol is not None:
            from workflows.group_report_view import group_for_display, project_member_report
            report = row.get("report") or {}
            if (report.get("report_kind") != "sector_group" or report.get("symbol") != symbol.upper()
                    or _norm_ts(report.get("market_data_timestamp_utc")) != as_of):
                return {}
            member = member_symbol.strip().upper()
            view = project_member_report(group_for_display(report), member)
            translations = {}
            for lang, overlay in (row.get("report_i18n") or {}).items():
                scoped = overlay.get("members") if isinstance(overlay, dict) else None
                if isinstance(scoped, dict) and isinstance(scoped.get(member), dict) and scoped[member]:
                    translations[lang] = scoped[member]
            return {"report": view, "lang": row.get("lang"), "report_i18n": translations}
        from workflows.group_report_view import group_for_display
        return {"report": group_for_display(row.get("report") or {}), "lang": row.get("lang"), "report_i18n": row.get("report_i18n") or {}}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 debate row({symbol}, {as_of}) 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_debate_report(symbol: str, as_of) -> Dict:
    """某标的精确 as_of 的 report jsonb(报告查看/复制用); 精确无则取 <= as_of 最新。无 → {}。"""
    return get_debate_report_row(symbol, as_of).get("report", {})


# ───────────────────────── 公司基本面 (company 静态 + fundamentals 可变) ───────────────────────── #

def get_company_profile(symbol: str) -> Dict:
    """静态公司信息 (company 表, 每标的1行)。整行透传。无 / PG 不通 → {}。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        tbl = _safe_ident(cfg["company_table"], "company")
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {tbl} WHERE symbol = %s LIMIT 1", (symbol,))
            row = cur.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 company({symbol}) 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_fundamentals_snapshot(symbol: str, as_of=None) -> List[Dict]:
    """可变基本面 point-in-time 快照 (每 (field,span) 取截至 as_of 最新一行, 整行透传)。
    as_of=None → 当前最新; 给 UTC 时刻 → 截至该刻最新。无 / PG 不通 → []。"""
    cfg = _load_pg_cfg()
    con = _connect_ro(cfg)
    if con is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        tcol = _safe_ident(cfg["fund_time_col"], "release_date")
        asof_outer = f" AND e.{tcol} <= %s" if as_of else ""
        asof_sub = f" AND {tcol} <= %s" if as_of else ""
        sql = _FUND_SNAPSHOT_SQL.format(
            table=_safe_ident(cfg["fundamentals_table"], "fundamentals"),
            time_col=tcol, asof_outer=asof_outer, asof_sub=asof_sub,
        )
        params = [symbol] + ([as_of, as_of] if as_of else [])
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"[lowfreq_reader] 查询 fundamentals({symbol}, as_of={as_of}) 失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_company_for_prompt(symbol: str) -> Optional[str]:
    """静态公司信息 → profile_data JSON 字符串。无 → None。"""
    prof = get_company_profile(symbol)
    if not prof:
        return None
    return json.dumps({"source": "equipoise.pg.company", **prof},
                      ensure_ascii=False, indent=2, default=str)


def format_fundamentals_for_prompt(symbol: str, as_of=None) -> Optional[str]:
    """可变基本面 → fundamentals_data JSON 字符串。无 → None。
    整行透传 (含 consensus 让 AI 看 beat/miss; span 标明口径如 3M/TTM)。"""
    snap = get_fundamentals_snapshot(symbol, as_of=as_of)
    if not snap:
        return None
    cfg = _load_pg_cfg()
    tcol = cfg["fund_time_col"]
    latest = max((str(r.get(tcol)) for r in snap if r.get(tcol)), default=None)
    payload = {
        "source": "equipoise.pg.fundamentals",
        "symbol": symbol,
        "as_of_query": str(as_of) if as_of else None,
        "latest_release": latest,
        "items": snap,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    cfg = _load_pg_cfg()
    print(f"PG: {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']} table={cfg['economic_table']}")
    for reg in ("US", "EU", "JP", "KR"):
        print(f"  region={reg}: {len(get_economic_snapshot(reg))} indicators")
    print("--- US prompt string ---")
    print(format_economic_for_prompt("US") or "(数据脑不可用, 会回退旧 JSON)")
