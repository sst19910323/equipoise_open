# -*- coding: utf-8 -*-
"""
postcheck_pg_store.py — BP 后验结果写入 PG (equipoise.postcheck)。Reckoner(清算者) 的产出落这。

一份报告(debates 的一行, 键 symbol+as_of) 对应一行后验。键也用 (symbol, as_of) → 和 debates 1:1, 直接 join。
**覆盖重写**: 重跑后验同 (symbol, as_of) 覆盖, 不记"复没复过"。dt 当普通列(只留最新那次)。

字段:
    symbol text | as_of timestamptz(=报告 market_data_timestamp_utc) | dt int(后验窗口交易日)
    score int(0踩空/50错过/100踩中, 中间自取) | verdict text(一句话清算结论)
    review bool(是否触发复盘) | diagnosis text(复盘归因+教训; 无则空)
    search_request text(Reckoner 让 Scout 查的; grounding 结果本身不存) | quant jsonb(窗口量化摘要)
    checked_at timestamptz(本次后验时刻)
连接复用 lowfreq_reader 的 PG 配置, 但可写。读侧(统计/展示)后续加到 lowfreq_reader。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS postcheck (
    symbol          text        NOT NULL,
    as_of           timestamptz NOT NULL,
    dt              integer     NOT NULL,
    score           integer,
    verdict         text,
    review          boolean,
    diagnosis       text,
    search_request  text,
    quant           jsonb,
    reckoner        text,
    checked_at      timestamptz,
    ruler           text        NOT NULL DEFAULT 'R21',
    PRIMARY KEY (symbol, as_of, dt, ruler)
);
"""

# 老表迁移: 主键 (symbol, as_of) → (symbol, as_of, dt) → (symbol, as_of, dt, ruler) + 补列。
# ruler = 尺子版本 (2026-09-30 评估记录版本化, 与 datalayer/pg/postcheck_store.py 同一 schema):
# 旧行回填 'R21', 换尺重评另存一行, 旧分不覆盖。
# CREATE TABLE IF NOT EXISTS 不改已存在的表, 故显式 ALTER (幂等; 单条失败不阻断)。
_MIGRATE_SQL = [
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS reckoner text",
    "ALTER TABLE postcheck ALTER COLUMN dt SET NOT NULL",
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS ruler text NOT NULL DEFAULT 'R21'",
    "ALTER TABLE postcheck DROP CONSTRAINT IF EXISTS postcheck_pkey",
    "ALTER TABLE postcheck ADD PRIMARY KEY (symbol, as_of, dt, ruler)",
]

_UPSERT_SQL = """
INSERT INTO postcheck (symbol, as_of, dt, ruler, score, verdict, review, diagnosis,
                       search_request, quant, reckoner, checked_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, as_of, dt, ruler) DO UPDATE SET
    score = EXCLUDED.score, verdict = EXCLUDED.verdict,
    review = EXCLUDED.review, diagnosis = EXCLUDED.diagnosis,
    search_request = EXCLUDED.search_request, quant = EXCLUDED.quant,
    reckoner = EXCLUDED.reckoner, checked_at = EXCLUDED.checked_at
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
        logging.warning(f"[postcheck_pg] 依赖缺失, 跳过 PG 写: {e}")
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
        logging.warning(f"[postcheck_pg] PG 连接失败, 跳过写: {type(e).__name__}")
        return None


_SCHEMA_READY = False


def _ensure_schema(cur) -> None:
    """建表(新库) + 迁移(老库: 主键升级到含 dt + 补 reckoner 列)。每进程跑一次即可。
    con.autocommit=True → 单条 ALTER 失败(如约束已是目标态)不污染后续, debug 吞掉。"""
    cur.execute(_CREATE_SQL)
    for stmt in _MIGRATE_SQL:
        try:
            cur.execute(stmt)
        except Exception as e:
            logging.debug(f"[postcheck_pg] 迁移跳过 ({stmt[:38]}…): {type(e).__name__}")


def ensure_table() -> bool:
    """显式建表+迁移 (服务器 CC 一行命令建空表/升级老表用; 首次 save 也会自动跑)。"""
    global _SCHEMA_READY
    con = _connect()
    if con is None:
        return False
    try:
        with con.cursor() as cur:
            _ensure_schema(cur)
        _SCHEMA_READY = True
        logging.info("[postcheck_pg] ✅ postcheck 表已就绪 (PK=symbol,as_of,dt)")
        return True
    except Exception as e:
        logging.warning(f"[postcheck_pg] 建表失败: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def save_postcheck(symbol: str, as_of, dt: Optional[int] = None, *,
                   score=None, verdict: str = "", review: Optional[bool] = None,
                   diagnosis: str = "", search_request: str = "",
                   quant: Optional[Dict] = None, reckoner: str = "",
                   checked_at=None) -> bool:
    """一份后验结果 upsert 进 postcheck。best-effort, 失败返 False 但绝不抛。
    键 (symbol, as_of, dt) 覆盖重写 → 同一报告的不同档位(dt)各存一行, 可按档位查。
    reckoner: 机械记录写这份后验的模型名(签名), 见 reckoner.reckoner_signature。"""
    global _SCHEMA_READY
    sym = str(symbol or "").upper()
    ts = _parse_ts(as_of)
    if not sym or ts is None:
        logging.warning(f"[postcheck_pg] 缺 symbol/as_of, 跳过 (sym={sym!r}, as_of={as_of!r})")
        return False
    con = _connect()
    if con is None:
        return False
    try:
        from psycopg2.extras import Json
        with con.cursor() as cur:
            if not _SCHEMA_READY:
                _ensure_schema(cur)
                _SCHEMA_READY = True
            ruler = (quant or {}).get("ruler") or "R21"   # digest 自带尺子版本; 没有=改尺前的老口径
            cur.execute(_UPSERT_SQL, (
                sym, ts, _safe_int(dt), ruler, _safe_int(score),
                verdict or "", review, diagnosis or "", search_request or "",
                Json(quant) if quant is not None else None, reckoner or None,
                _parse_ts(checked_at) or datetime.now(timezone.utc),
            ))
        logging.info(f"[postcheck_pg] ✅ postcheck {sym} @ {ts.isoformat()} dt={_safe_int(dt)} "
                     f"(score={score}, by={reckoner or '?'})")
        return True
    except Exception as e:
        logging.warning(f"[postcheck_pg] 写 postcheck 失败 ({sym}): {type(e).__name__}: {e}")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


# =====================================================================
# 读侧 (前端/统计) — 回测是"单期报告的衍生品": (标的 + as_of 定哪期) + (dt 定档位)
# =====================================================================

# 档位登记: 前端用 label, 库里存 dt(美股交易日数)。单一来源, 加/改档位只动这里 + 用 --dt 重跑。
# 3d 贴短线战术 + 20d(对应 MA20 月线尺)。50d 2026-09-30 退役(决策契约 §4.7); 库里旧的 dt=50 行
# 仍可用数字 dts=50 读到, 只是不再是登记档位。
POSTCHECK_TIERS = {"3d": 3, "20d": 20}

_READ_COLS = ("symbol, as_of, dt, ruler, score, verdict, review, diagnosis, "
              "search_request, quant, reckoner, checked_at")
_HAS_I18N: Optional[bool] = None   # postcheck.lang / report_i18n 列(2026-10-25 多语言)在不在; 第一次用时查一次


def _read_cols(cur) -> str:
    """带多语言列的读列表; 列还没迁移时(scripts/migrate_i18n.py --apply 之前)退回旧列表。"""
    global _HAS_I18N
    if _HAS_I18N is None:
        try:
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name = 'postcheck' AND column_name = 'report_i18n'")
            _HAS_I18N = cur.fetchone() is not None
        except Exception:
            _HAS_I18N = False
    return _READ_COLS + (", lang, report_i18n" if _HAS_I18N else "")


def tier_to_dt(tier) -> Optional[int]:
    """档位 → dt(交易日数)。接受 '3d'/'2week'/'2month' 或直接整数/数字串。无法解析 → None。"""
    if tier is None:
        return None
    s = str(tier).strip().lower()
    if s in POSTCHECK_TIERS:
        return POSTCHECK_TIERS[s]
    return _safe_int(s)


def _dt_to_tier(dt: Optional[int]) -> Optional[str]:
    """dt → 档位 label (反查; 非登记档位则返回 '<dt>d')。"""
    for k, v in POSTCHECK_TIERS.items():
        if v == dt:
            return k
    return f"{dt}d" if dt is not None else None


def parse_tiers(spec) -> Optional[List[int]]:
    """档位多选解析: '3d,2week' / '3 10' / ['3d',10] → [3,10]; 空/None → None(=不限,全部档位)。
    无法解析的项静默忽略; 全无效也回 None。"""
    if spec is None:
        return None
    items = spec if isinstance(spec, (list, tuple)) else re.split(r"[,\s]+", str(spec).strip())
    out = []
    for it in items:
        d = tier_to_dt(it)
        if d is not None and d not in out:
            out.append(d)
    return out or None


def _iso(v):
    """datetime → ISO 'Z' 串; 其它原样。给前端用。"""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return v


def _row_to_dict(row) -> Dict:
    d = dict(row)
    d["as_of"] = _iso(d.get("as_of"))
    d["checked_at"] = _iso(d.get("checked_at"))
    d["tier"] = _dt_to_tier(d.get("dt"))
    return d


def localize_items(items: list, lang: Optional[str]) -> list:
    """复盘行按语言套译文(2026-10-25; 同 debates 的 i18n.localize_report, 覆盖层只有 verdict); 每项带 lang_served / lang_source /
    lang_available, report_i18n 本身不回给前端。列还没迁移(行里没有 lang 键)时按判词有无汉字判原文语言。"""
    from datalayer.pg import i18n
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            out.append(it)
            continue
        d = dict(it)
        ov = d.pop("report_i18n", None) or {}
        src = d.get("lang") or ("zh" if i18n.has_cjk(d.get("verdict") or "") else "en")
        doc, served = i18n.localize_report(d, src, ov, lang, fields=i18n.POSTCHECK_TRANSLATABLE_FIELDS)
        doc = dict(doc)
        doc.update(i18n.lang_meta(src, ov, served))
        out.append(doc)
    return out


def get_postcheck(symbol: str, as_of=None, dts=None) -> Dict:
    """【前端核心】单期后验: (标的 + as_of 定哪一期) 下的一个/几个/全部档位。
    as_of = 报告时刻(**精确对应那一期**, 不就近回退): 后验是单期报告的衍生品, 错配别期的复盘是错的;
            该期没后验就返回 items=[](别拿 ≤ 的最近一期顶替)。省略 as_of = 最新一期。
    dts = 档位多选: None/[]/'' → 该期全部档位; '3d,2week' / [3,10] → 指定档位。
    返回 {symbol, as_of(定位到的那一期), items:[每档位一份后验, 按 dt 升序]}。该期无后验 → items=[]。"""
    sym = str(symbol or "").upper()
    if not sym:
        return {"symbol": sym, "as_of": None, "items": []}
    tiers = parse_tiers(dts)
    con = _connect()
    if con is None:
        return {"symbol": sym, "as_of": None, "items": []}
    try:
        from psycopg2.extras import RealDictCursor
        ts = _parse_ts(as_of) if as_of else None
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. 定位"那一期": 给了 as_of → **精确等于**那一期(后验严格对应报告时刻, 不就近回退到 ≤ 的最近一期,
            #    否则会把别期报告的复盘错配过来); 省略 as_of → 最新一期
            if ts is not None:
                cur.execute("SELECT max(as_of) AS m FROM postcheck WHERE symbol=%s AND as_of=%s", (sym, ts))
            else:
                cur.execute("SELECT max(as_of) AS m FROM postcheck WHERE symbol=%s", (sym,))
            row = cur.fetchone()
            edition = row.get("m") if row else None
            if edition is None:
                return {"symbol": sym, "as_of": None, "items": []}
            # 2. 取该期的档位行 (空 tiers → 全部)
            sql = f"SELECT {_read_cols(cur)} FROM postcheck WHERE symbol=%s AND as_of=%s"
            params = [sym, edition]
            if tiers:
                sql += " AND dt = ANY(%s)"
                params.append(tiers)
            # 同一档位可能有多把尺子的评估(评估记录版本化), 全部返回, 新尺在前
            sql += " ORDER BY dt, ruler DESC"
            cur.execute(sql, params)
            items = [_row_to_dict(r) for r in cur.fetchall()]
        return {"symbol": sym, "as_of": _iso(edition), "items": items}
    except Exception as e:
        logging.warning(f"[postcheck_pg] 读 postcheck 失败 ({sym}): {type(e).__name__}: {e}")
        return {"symbol": sym, "as_of": None, "items": []}
    finally:
        try:
            con.close()
        except Exception:
            pass


def list_postcheck(symbol: Optional[str] = None, dts=None, limit: int = 200,
                   months: float = 0) -> list:
    """列表(前端表格/历史): 可按标的、档位(多选/空=不限)过滤; months>0 只取最近 N 个月(按 as_of);
    按 as_of 倒序、同期按 dt 升序。"""
    con = _connect()
    if con is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        where, params = [], []
        if symbol:
            where.append("symbol=%s")
            params.append(str(symbol).upper())
        tiers = parse_tiers(dts)
        if tiers:
            where.append("dt = ANY(%s)")
            params.append(tiers)
        if months and months > 0:
            where.append("as_of >= %s")
            params.append(datetime.now(timezone.utc) - timedelta(days=months * 30.5))
        wc = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit or 200))
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            sql = f"SELECT {_read_cols(cur)} FROM postcheck{wc} ORDER BY as_of DESC, dt, ruler DESC LIMIT %s"
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"[postcheck_pg] list 失败: {type(e).__name__}: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def postcheck_stats(symbol: Optional[str] = None, dts=None, months: float = 0) -> Dict:
    """统计聚合(前端总览): 数量/均分/极值/复盘率/三档分布(踩空≤33·错过34-66·踩中≥67)/按回测人。
    档位 dts 多选/空=不限; months>0 只统计最近 N 个月(按 as_of, 默认不限保历史口径)。"""
    con = _connect()
    if con is None:
        return {}
    try:
        where, params = [], []
        if symbol:
            where.append("symbol=%s")
            params.append(str(symbol).upper())
        tiers = parse_tiers(dts)
        if tiers:
            where.append("dt = ANY(%s)")
            params.append(tiers)
        if months and months > 0:
            where.append("as_of >= %s")
            params.append(datetime.now(timezone.utc) - timedelta(days=months * 30.5))
        wc = (" WHERE " + " AND ".join(where)) if where else ""
        with con.cursor() as cur:
            cur.execute(f"""SELECT count(*), avg(score), min(score), max(score),
                avg(CASE WHEN review THEN 1.0 ELSE 0.0 END),
                count(*) FILTER (WHERE score <= 33),
                count(*) FILTER (WHERE score > 33 AND score < 67),
                count(*) FILTER (WHERE score >= 67)
                FROM postcheck{wc}""", params)
            n, avg_s, mn, mx, rev, miss, fair, hit = cur.fetchone()
            cur.execute(f"SELECT reckoner, count(*), avg(score) FROM postcheck{wc} "
                        f"GROUP BY reckoner ORDER BY count(*) DESC", params)
            by = [{"reckoner": r[0], "n": r[1],
                   "avg_score": round(float(r[2]), 1) if r[2] is not None else None}
                  for r in cur.fetchall()]
        return {
            "symbol": (symbol or "").upper() or None,
            "tiers": [_dt_to_tier(t) for t in tiers] if tiers else None,
            "dts": tiers,
            "n": n,
            "avg_score": round(float(avg_s), 1) if avg_s is not None else None,
            "min_score": mn, "max_score": mx,
            "review_rate": round(float(rev), 3) if rev is not None else None,
            "dist": {"miss_0": miss, "fair_50": fair, "hit_100": hit},
            "by_reckoner": by,
        }
    except Exception as e:
        logging.warning(f"[postcheck_pg] stats 失败: {type(e).__name__}: {e}")
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 不连 PG 的轻自检 (校验 SQL 字段数对齐 + 解析工具)
    assert _UPSERT_SQL.count("%s") == 12, "VALUES 占位符应为 12 (含 ruler)"
    assert _parse_ts("2026-06-18T13:30:00Z") is not None
    assert _safe_int("70") == 70 and _safe_int("x") is None
    assert tier_to_dt("3d") == 3 and tier_to_dt("20d") == 20 and tier_to_dt("50d") is None  # 50d 退役
    assert tier_to_dt("14") == 14 and tier_to_dt("bad") is None
    assert _dt_to_tier(20) == "20d" and _dt_to_tier(7) == "7d"
    assert parse_tiers("3d,20d") == [3, 20] and parse_tiers("") is None and parse_tiers(None) is None
    assert parse_tiers(["3d", 50, "bad"]) == [3, 50]
    print("postcheck_pg_store 自检通过 (占位符/解析/档位/多选)")
