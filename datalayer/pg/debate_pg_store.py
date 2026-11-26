# -*- coding: utf-8 -*-
"""
debate_pg_store.py — equipoise 把辩论终报写进 PG (equipoise.debates)。

模式: 胖 jsonb(整份 report 复制进库) + 瘦决策列(composite_score/op_*/as_of, 供沿时间线查评分)。
as_of = market_data_timestamp_utc(分时/量化数据时刻), 也是复现锚 + 主键一部分。

边界:
- 本模块只写 debates 表; 低频表(economic/fundamentals/...)走 datalayer/pg/db.py, 互不越界。
- **best-effort**: PG 写失败绝不搞挂辩论(JSON 仍是 source of truth, horizon 去重也读 JSON)。
- 连接复用 lowfreq_reader 的配置(host/db/user/pwd), 但**可写**(非只读)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS debates (
    symbol               text        NOT NULL,
    as_of                timestamptz NOT NULL,
    report_generated_at  timestamptz,
    composite_score      integer,
    operation_type       text,
    operation_target     text,
    operation_size       text,
    next_review          jsonb,
    report               jsonb        NOT NULL,
    lang                 text         NOT NULL DEFAULT 'zh',
    report_i18n          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (symbol, as_of)
);
"""

def _upsert_sql(n: dict) -> str:
    """n = names.debates(cur): 评分列与规模列在库里的实际名字(迁移前旧名, 迁移后新名)。"""
    score, size = n["composite_score"], n["operation_size"]
    return f"""
INSERT INTO debates (symbol, as_of, report_generated_at, {score}, operation_type,
                     operation_target, {size}, next_review, report, lang)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, as_of) DO UPDATE SET
    report_generated_at = EXCLUDED.report_generated_at,
    {score}             = EXCLUDED.{score},
    operation_type      = EXCLUDED.operation_type,
    operation_target    = EXCLUDED.operation_target,
    {size}              = EXCLUDED.{size},
    next_review         = EXCLUDED.next_review,
    -- 2026-10-24 多语言: 原文变了旧译文作废(清空等翻译任务重翻); 同一份重存不丢译文
    report_i18n         = CASE WHEN debates.report IS DISTINCT FROM EXCLUDED.report
                               THEN '{{}}'::jsonb ELSE debates.report_i18n END,
    report              = EXCLUDED.report,
    lang                = EXCLUDED.lang
WHERE NOT (debates.report ? 'debate_ref')
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


def _safe_int(v) -> Optional[int]:
    try:
        return int(float(str(v)))
    except Exception:
        return None


def _connect():
    try:
        import psycopg2
        from datalayer.pg.lowfreq_reader import _load_pg_cfg
    except Exception as e:
        logging.warning(f"[debate_pg] 依赖缺失, 跳过 PG 写: {e}")
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
        logging.warning(f"[debate_pg] PG 连接失败, 跳过写: {type(e).__name__}")
        return None


def _to_new_names(final_json: Dict) -> Dict:
    """本模块只剩 API 版(已退役, 仅历史回放)在用, 它产出的终报还是旧键名。入库前统一成新名,
    库里只留一种形状(2026-10-01 迁移之后)。转换失败就原样写, 不因此丢报告。"""
    try:
        import copy
        import os
        import sys
        scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from report_names import upgrade_report
        doc, _changes, _skipped = upgrade_report(copy.deepcopy(final_json))
        return doc
    except Exception as e:
        logging.warning(f"[debate_pg] 键名转换失败, 原样入库: {type(e).__name__}: {e}")
        return final_json


def save_debate(symbol: str, final_json: Dict) -> bool:
    """把一份终报 upsert 进 debates。best-effort, 失败返回 False 但绝不抛。"""
    if (not symbol or not isinstance(final_json, dict) or "debate_ref" in final_json or "group_session" in final_json
            or final_json.get("report_kind") == "sector_group"):
        return False
    final_json = _to_new_names(final_json)
    con = _connect()
    if con is None:
        return False
    try:
        from psycopg2.extras import Json
        as_of = (_parse_ts(final_json.get("market_data_timestamp_utc"))
                 or _parse_ts(final_json.get("report_generated_at"))
                 or datetime.now(timezone.utc))
        op_target = final_json.get("operation_target")
        nr = final_json.get("next_review")
        with con.cursor() as cur:
            cur.execute(_CREATE_SQL)
            from datalayer.pg import names as _names
            from datalayer.pg.i18n import detect_lang
            cur.execute(_upsert_sql(_names.debates(cur)), (
                symbol.upper(),
                as_of,
                _parse_ts(final_json.get("report_generated_at")),
                _safe_int(final_json.get("composite_score", final_json.get("action"))),
                final_json.get("operation_type"),
                str(op_target) if op_target is not None else None,
                final_json.get("operation_size", final_json.get("operation_volume")),
                Json(nr) if nr is not None else None,
                Json(final_json),
                detect_lang(final_json),   # 原文语言, 见 datalayer/pg/i18n.py
            ))
            if not cur.rowcount:
                return False
        logging.info(f"[debate_pg] ✅ {symbol.upper()} @ {as_of.isoformat()} 写入 debates")
        return True
    except Exception as e:
        logging.warning(f"[debate_pg] 写 debates 失败 ({symbol}): {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass
