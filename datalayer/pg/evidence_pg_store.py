# -*- coding: utf-8 -*-
"""
evidence_pg_store.py — equipoise 把证据官(Scout)产出的证据写进 PG (equipoise.evidence)。

模式: 一批证据(那个 list)整体塞进一行的 jsonb。键 (symbol, generated_at)。
generated_at = 证据生成时刻(UTC), 也是复现锚 + 主键一部分; 报告记这个时间戳, /api/evidence 拿它复现。

边界(同 debate_pg_store):
- 本模块只写 evidence 表; 低频表走 datalayer/pg/db.py, 互不越界。
- **best-effort**: PG 写失败绝不搞挂辩论(证据是现生成现喂, in-memory 才是 source of truth; PG 只管存档/复现)。
- 连接复用 lowfreq_reader 的配置, 但**可写**(非只读)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS evidence (
    symbol        text        NOT NULL,
    generated_at  timestamptz NOT NULL,
    evidence      jsonb        NOT NULL,
    PRIMARY KEY (symbol, generated_at)
);
"""

_UPSERT_SQL = """
INSERT INTO evidence (symbol, generated_at, evidence)
VALUES (%s, %s, %s)
ON CONFLICT (symbol, generated_at) DO UPDATE SET
    evidence = EXCLUDED.evidence
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
    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
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
        logging.warning(f"[evidence_pg] 依赖缺失, 跳过 PG 写: {e}")
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
        logging.warning(f"[evidence_pg] PG 连接失败, 跳过写: {type(e).__name__}")
        return None


def save_evidence(symbol: str, items: List[Dict], generated_at=None) -> bool:
    """把一批证据 upsert 进 evidence。best-effort, 失败返回 False 但绝不抛。
    items = evidence_officer.investigate() 的返回 list。空 list 不写。"""
    if not symbol or not items:
        return False
    con = _connect()
    if con is None:
        return False
    try:
        from psycopg2.extras import Json
        ts = _parse_ts(generated_at) or datetime.now(timezone.utc)
        with con.cursor() as cur:
            cur.execute(_CREATE_SQL)
            cur.execute(_UPSERT_SQL, (symbol.upper(), ts, Json(items)))
        logging.info(f"[evidence_pg] ✅ {symbol.upper()} @ {ts.isoformat()} 写入 {len(items)} 条证据")
        return True
    except Exception as e:
        logging.warning(f"[evidence_pg] 写 evidence 失败 ({symbol}): {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass
