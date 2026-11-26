# -*- coding: utf-8 -*-
"""
marketdata_pg_store.py — equipoise 把"技术快照 + 新闻"写进 PG (equipoise.technical / equipoise.news)。

定位: 让前端/复现能从 PG 读到辩论当时的技术面与新闻, 不再依赖散落的 json 文件。
两张表(equipoise 自己写, 同 debates/evidence 走 best-effort, 失败绝不搞挂主流程):
- `technical`: symbol | as_of(=market_data_timestamp_utc) timestamptz | payload jsonb。键 (symbol, as_of)。
- `news`:      symbol | fetched_at timestamptz | items jsonb(那个文章 list)。键 (symbol, fetched_at)。

复现锚: technical 用 market_data_timestamp_utc(与 debates 同一时刻); news 用抓取时刻(文件名时间戳)。
连接复用 lowfreq_reader 的配置, 但**可写**(非只读)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

_CREATE_TECH_SQL = """
CREATE TABLE IF NOT EXISTS technical (
    symbol   text        NOT NULL,
    as_of    timestamptz NOT NULL,
    payload  jsonb        NOT NULL,
    PRIMARY KEY (symbol, as_of)
);
"""

_UPSERT_TECH_SQL = """
INSERT INTO technical (symbol, as_of, payload)
VALUES (%s, %s, %s)
ON CONFLICT (symbol, as_of) DO UPDATE SET payload = EXCLUDED.payload
"""

_CREATE_NEWS_SQL = """
CREATE TABLE IF NOT EXISTS news (
    symbol      text        NOT NULL,
    fetched_at  timestamptz NOT NULL,
    items       jsonb        NOT NULL,
    PRIMARY KEY (symbol, fetched_at)
);
"""

_UPSERT_NEWS_SQL = """
INSERT INTO news (symbol, fetched_at, items)
VALUES (%s, %s, %s)
ON CONFLICT (symbol, fetched_at) DO UPDATE SET items = EXCLUDED.items
"""


def _parse_ts(v) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _connect():
    try:
        import psycopg2
        from datalayer.pg.lowfreq_reader import _load_pg_cfg
    except Exception as e:
        logging.warning(f"[marketdata_pg] 依赖缺失, 跳过 PG 写: {e}")
        return None
    cfg = _load_pg_cfg()
    try:
        con = psycopg2.connect(
            host=cfg["host"], port=int(cfg["port"]), dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"],
            connect_timeout=4, client_encoding="UTF8",
        )
        con.autocommit = True
        return con
    except Exception as e:
        logging.warning(f"[marketdata_pg] PG 连接失败, 跳过写: {type(e).__name__}")
        return None


def _extract_field(payload: Dict, suffix: str):
    """取 payload 里 key==suffix 或 key 以 '_{suffix}' 结尾的值(兼容 {SYMBOL}_ 前缀扁平化格式)。"""
    if suffix in payload:
        return payload[suffix]
    tail = "_" + suffix
    for k, v in payload.items():
        if isinstance(k, str) and k.endswith(tail):
            return v
    return None


def save_technical(payload: Dict, symbol: Optional[str] = None, as_of=None) -> bool:
    """把一份技术快照 upsert 进 technical。best-effort。
    symbol/as_of 不传则从 payload 取; 兼容两种格式: 顶层 symbol/market_data_timestamp_utc,
    或 {SYMBOL}_ 前缀扁平化(symbol→{SYM}_symbol, 时间→{SYM}_market_data_timestamp_utc)。"""
    if not isinstance(payload, dict):
        return False
    sym = (symbol or _extract_field(payload, "symbol") or "").upper()
    ts = _parse_ts(as_of) or _parse_ts(_extract_field(payload, "market_data_timestamp_utc"))
    if not sym or ts is None:
        logging.warning(f"[marketdata_pg] technical 缺 symbol/as_of, 跳过 (sym={sym!r})")
        return False
    con = _connect()
    if con is None:
        return False
    try:
        from psycopg2.extras import Json
        with con.cursor() as cur:
            cur.execute(_CREATE_TECH_SQL)
            cur.execute(_UPSERT_TECH_SQL, (sym, ts, Json(payload)))
        logging.info(f"[marketdata_pg] ✅ technical {sym} @ {ts.isoformat()}")
        return True
    except Exception as e:
        logging.warning(f"[marketdata_pg] 写 technical 失败 ({sym}): {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def save_news(symbol: str, items: List[Dict], fetched_at=None) -> bool:
    """把一批新闻文章(list) upsert 进 news。best-effort。空 list 不写。"""
    if not symbol or not items:
        return False
    con = _connect()
    if con is None:
        return False
    try:
        from psycopg2.extras import Json
        ts = _parse_ts(fetched_at) or datetime.now(timezone.utc)
        with con.cursor() as cur:
            cur.execute(_CREATE_NEWS_SQL)
            cur.execute(_UPSERT_NEWS_SQL, (symbol.upper(), ts, Json(items)))
        logging.info(f"[marketdata_pg] ✅ news {symbol.upper()} @ {ts.isoformat()} ({len(items)} 篇)")
        return True
    except Exception as e:
        logging.warning(f"[marketdata_pg] 写 news 失败 ({symbol}): {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass
