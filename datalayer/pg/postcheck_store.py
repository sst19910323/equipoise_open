# -*- coding: utf-8 -*-
"""
postcheck_store.py — BP 后验(复盘)的 PG 读写 + parent 编排的 CLI 入口。

定位: CC 版后验 —— reckoner(判断脑子)由代码版的 deepseek 换成 parent(Claude Code)。
本模块只管【确定性的读 + 写】, 判断/归因由主会话照 orchestration/postcheck-protocol.md 做。

  读: debates 表 (辩论侧已写, 本模块只读这张) —— 捞"成熟"的旧报告
  算: 复用 analysis.postcheck_quant.compute_window_digest (窗口量化摘要)
  写: postcheck 表 (代码版已建, 同表同 schema, CC 版复用)

表 postcheck (PK = symbol, as_of, dt, ruler):
    symbol text | as_of timestamptz(=报告 market_data_timestamp_utc) | dt int(后验窗口交易日)
    ruler text(尺子版本, = postcheck_quant.RULER_VERSION; 旧行回填 'R21'——同一份报告换尺重评另存一行, 不覆盖)
    score int(0踩空/50错过/100踩中) | verdict text | review bool | diagnosis text | lesion text(病灶坐标 R21)
    search_request text | quant jsonb(窗口摘要) | reckoner text(签名) | checked_at timestamptz

CLI (服务器 /opt/equipoise 跑, PG 在 localhost, python 用 ./venv/bin/python):
    postcheck_store.py init
        建表/迁移 (equipoise 已建则幂等跳过)
    postcheck_store.py prep --symbol NVDA --dt 20 [--as_of <TS>] [--start YYYY-MM-DD]
        捞 1 份成熟报告(指定 as_of 或最新成熟一期) + 算窗口 digest → 写 staging JSON → 打印回执
        digest 未成熟(bars_in_window<dt) → status=immature
    postcheck_store.py save <judgment.json>
        judgment.json = {staging_path, score, verdict, review, diagnosis, lesion?, search_request?}
        从 staging 取 symbol/as_of/dt/quant, 合并 parent 的判断, upsert 进 postcheck
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]   # datalayer/pg/<this> → equipoise root
sys.path.insert(0, str(ROOT))

from datalayer.pg.db import PG, connect  # 复用低频写侧 PG 配置/连接  # noqa: E402
from workflows.postcheck_quant import compute_window_digest, RULER_VERSION  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("postcheck_store")

RECKONER_SIGNATURE = "claude-opus-4-8"  # CC reckoner 默认签名(回退值)。真值优先级:
# judgment JSON 的 reckoner 字段(模型自报 / cc_loop 从 modelUsage 回写的真实 id) > 此常量。
# 判"是不是 agent 干的"按前缀族 claude*(CC)/gpt*|codex*(GPT)(见 skills/equipoise-postcheck/scripts/ 的 worklist 与两个 loop), 不比版本号。
# ⚠️ codex 引擎必须自报 reckoner(真实 slug), 省略会被此回退值记成 claude → 归属错乱。
STAGING_DIR = ROOT / "data" / "postcheck_staging"

# ---------------------------------------------------------------------------
# schema (移植自 equipoise postcheck_pg_store; 同表同结构, equipoise 已建则幂等)
# ---------------------------------------------------------------------------
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS postcheck (
    symbol          text        NOT NULL,
    as_of           timestamptz NOT NULL,
    dt              integer     NOT NULL,
    score           integer,
    verdict         text,
    review          boolean,
    diagnosis       text,
    lesion          text,
    search_request  text,
    quant           jsonb,
    reckoner        text,
    checked_at      timestamptz,
    ruler           text        NOT NULL DEFAULT 'R21',
    PRIMARY KEY (symbol, as_of, dt, ruler)
);
"""
# 评估记录版本化 (2026-09-30, FINAL §11.2): ruler 进主键, 旧行回填 'R21'(改尺前最后一版),
# 新尺重评同一份报告另存一行; 旧分永不被覆盖。
_MIGRATE_SQL = [
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS reckoner text",
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS lesion text",  # R21 病灶坐标
    "ALTER TABLE postcheck ALTER COLUMN dt SET NOT NULL",
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS ruler text NOT NULL DEFAULT 'R21'",
    "ALTER TABLE postcheck DROP CONSTRAINT IF EXISTS postcheck_pkey",
    "ALTER TABLE postcheck ADD PRIMARY KEY (symbol, as_of, dt, ruler)",
    # 2026-10-25 多语言(同 debates): 原文语言 + 译文覆盖层 {"zh": {"verdict": "…", "_meta": {…}}}
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT 'zh'",
    "ALTER TABLE postcheck ADD COLUMN IF NOT EXISTS report_i18n jsonb NOT NULL DEFAULT '{}'::jsonb",
]
_UPSERT_SQL = """
INSERT INTO postcheck (symbol, as_of, dt, ruler, score, verdict, review, diagnosis,
                       lesion, search_request, quant, reckoner, checked_at, lang)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, as_of, dt, ruler) DO UPDATE SET
    score = EXCLUDED.score, verdict = EXCLUDED.verdict,
    review = EXCLUDED.review, diagnosis = EXCLUDED.diagnosis,
    lesion = EXCLUDED.lesion,
    search_request = EXCLUDED.search_request, quant = EXCLUDED.quant,
    reckoner = EXCLUDED.reckoner, checked_at = EXCLUDED.checked_at,
    lang = EXCLUDED.lang,
    report_i18n = CASE WHEN postcheck.verdict IS DISTINCT FROM EXCLUDED.verdict THEN '{}'::jsonb ELSE postcheck.report_i18n END
"""

# 档位登记: 3d / 20d (2026-09-30 起 50d 退役——没有多少有效信息能撑到 50 个交易日, 决策契约 §4.7)
POSTCHECK_TIERS = {"3d": 3, "20d": 20}


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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# schema 就绪
# ---------------------------------------------------------------------------
def ensure_table() -> bool:
    """建表 + 迁移 (equipoise 已建则全幂等跳过)。"""
    try:
        with connect() as con, con.cursor() as cur:
            cur.execute(_CREATE_SQL)
            for stmt in _MIGRATE_SQL:
                try:
                    cur.execute(stmt)
                    con.commit()
                except Exception as e:
                    con.rollback()
                    log.debug(f"migration skipped ({stmt[:36]}…): {type(e).__name__}")
        log.info("✅ postcheck table ready (PK=symbol,as_of,dt,ruler)")
        return True
    except Exception as e:
        log.warning(f"table creation failed: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# 读侧: 从 debates 捞成熟报告
# ---------------------------------------------------------------------------
def _maturity_cutoff(now: datetime, dt: int) -> datetime:
    """as_of ≤ 返回值 → 自 as_of 起已过 ≥ dt 个**美股交易日**(窗口能跑满)。
    有 pandas_market_calendars 用 NYSE 日历精确; 否则退日历天近似(偏宽松, 精确成熟由 bars 闸把)。"""
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        sched = cal.schedule(start_date=(now - timedelta(days=dt * 3 + 25)).date(),
                             end_date=now.date())
        # 只认**已收盘**的 session: 当天盘中/盘前跑时, 今天这根还没走完, 算进来会把
        # 成熟线整体放宽一天(表现为 worklist 说成熟、prep 却判 immature)。
        sched = sched[sched["market_close"] <= now]
        sessions = sched.index
        if len(sessions) > dt:
            cut = sessions[-dt]
            cut = cut.tz_localize("UTC") if cut.tzinfo is None else cut.tz_convert("UTC")
            return cut.to_pydatetime()
    except Exception as e:
        log.debug(f"NYSE calendar unavailable, falling back to calendar-day approximation: {type(e).__name__}")
    return now - timedelta(days=dt)


def fetch_report(symbol: str, dt: int, as_of: Optional[str] = None,
                 start: Optional[str] = None) -> Optional[Tuple[str, datetime, Dict]]:
    """捞 1 份待复盘报告 (symbol, as_of, report)。
    as_of 给了 → 精确取那一期; 否则取 ≤ 成熟截止 的最新一期(可选 start 下界)。无 → None。
    debates 表只读 (equipoise 写)。"""
    from psycopg2.extras import RealDictCursor
    sym = str(symbol or "").upper()
    if not sym:
        return None
    now = datetime.now(timezone.utc)
    try:
        with connect() as con, con.cursor(cursor_factory=RealDictCursor) as cur:
            if as_of:
                ts = _parse_ts(as_of)
                cur.execute("SELECT symbol, as_of, report FROM debates "
                            "WHERE symbol=%s AND as_of=%s", (sym, ts))
            else:
                cutoff = _maturity_cutoff(now, dt)
                where = ["symbol=%s", "as_of<=%s"]
                params: List = [sym, cutoff]
                if start:
                    where.append("as_of>=%s")
                    params.append(_parse_ts(start))
                cur.execute(f"SELECT symbol, as_of, report FROM debates "
                            f"WHERE {' AND '.join(where)} ORDER BY as_of DESC LIMIT 1", params)
            row = cur.fetchone()
    except Exception as e:
        log.error(f"debates query failed ({sym}): {type(e).__name__}: {e}")
        return None
    if not row:
        return None
    report = row["report"]
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except Exception:
            report = {}
    return row["symbol"], row["as_of"], (report or {})


# ---------------------------------------------------------------------------
# prep: 捞报告 + 新请求结果价 + 算 digest → staging
# ---------------------------------------------------------------------------
def _prefetch_prices(symbol: str) -> float:
    """结果价新请求(2026-07-02 铁律): 连盈透把该标的(GENERAL*→指数镜头)的日线拉一遍;
    1h 内鲜则跳(批内共享镜头自然去重)。返回 min_mtime —— 早于它的 CSV 是残留旧货,
    digest 直接判无数据(**绝不吃旧 CSV 硬算**)。拉挂/盈透不通不抛, 该期自然 immature。
    环境要求: EQUIPOISE_IBKR_PORT(服务器=4001)。"""
    import time as _time
    min_mtime = _time.time() - 3600.0   # 对齐 DATA_FRESHNESS_SECONDS
    try:
        import asyncio
        from horizon_sentinel import (IbkrFetcher, is_symbol_data_fresh,
                                      DATA_FRESHNESS_SECONDS, load_helpers)
        from workflows.postcheck_quant import _GENERAL_MEMBERS
        data_config, symbols_config, _ = load_helpers()
        ta_dir = data_config['data_sources']['technical_analysis']['output_dir']
        syms = _GENERAL_MEMBERS.get(symbol.upper(), [symbol.upper()])

        async def _run():
            f = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
            try:
                for s in syms:
                    if not is_symbol_data_fresh(s, ta_dir, DATA_FRESHNESS_SECONDS):
                        await f.fetch_symbol(s)
            finally:
                await f.close()
        asyncio.run(_run())
    except Exception as e:
        print(f"[postcheck_store] result-price prefetch failed ({type(e).__name__}: {e}) — this period will be treated as no data",
              file=sys.stderr)
    return min_mtime


def _prior_timeline(sym: str, ts, dt: int) -> Optional[Dict]:
    """dt>3 时查同源 3d 后验行的撞线排班 (Task 2.6 继承: 20d 前 3 天用 5m 精细段)。
    只继承**同尺**的(R24 起): 旧尺的排班可能带着已修掉的缺陷(R23 的起点回退让 HYMTF 的失效线首日"触及"), 继承回来等于把缺陷带回来。"""
    if dt <= 3:
        return None
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT quant FROM postcheck WHERE symbol=%s AND as_of=%s AND dt=3 AND ruler=%s "
                        "ORDER BY checked_at DESC LIMIT 1",
                        (sym, ts, RULER_VERSION))
            row = cur.fetchone()
        q = row[0] if row else None
        if isinstance(q, str):
            q = json.loads(q)
        return (q or {}).get("touch_timeline")
    except Exception as e:
        log.warning(f"3d timeline inheritance query failed (non-blocking, using this tier's coarse timeline): {e}")
        return None


def _known_event_dates(sym: str, ts, dt: int) -> list:
    """Projection for reports without `next_review.events` (owner, 11-06: "they did not know to write it; we know now"):
    the issuer's actual release dates inside the window, from PG fundamentals. GENERAL* has no issuer → []."""
    if str(sym).upper().startswith("GENERAL"):
        return []
    try:
        from datetime import timedelta
        from workflows.postcheck_quant import _to_dt
        T = _to_dt(ts)
        if T is None:
            return []
        end = T + timedelta(days=int(int(dt) * 1.6) + 3)
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT DISTINCT release_date FROM fundamentals WHERE symbol = %s AND release_date > %s AND release_date <= %s",
                        (str(sym).upper(), T.date(), end.date()))
            return sorted(r[0].isoformat() for r in cur.fetchall() if r and r[0])
    except Exception as e:  # noqa: BLE001
        log.warning(f"known-event projection skipped for {sym}: {type(e).__name__}: {e}")
        return []


def prep(symbol: str, dt: int, as_of: Optional[str] = None,
         start: Optional[str] = None) -> Dict:
    """捞 1 份成熟报告 + **新请求结果价(连盈透)** + 算窗口 digest, 写 staging JSON。返回回执 dict。"""
    fetched = fetch_report(symbol, dt, as_of=as_of, start=start)
    if fetched is None:
        return {"status": "no_report", "symbol": symbol.upper(), "dt": dt,
                "why": "no matching report in debates (too recent / not found / PG unreachable)"}
    sym, ts, report = fetched
    min_mtime = _prefetch_prices(sym)
    digest = compute_window_digest(sym, ts, dt, report, min_mtime=min_mtime,
                                   prior_timeline=_prior_timeline(sym, ts, dt),
                                   known_events=_known_event_dates(sym, ts, dt))
    bars = digest.get("bars_in_window", 0) if digest else 0
    if digest and digest.get('error') and digest.get('settlement_status') == 'unassessable':
        return {'status': 'immature', 'symbol': sym, 'as_of': _iso(ts), 'dt': dt,
                'bars_in_window': bars, 'why': 'settlement unassessable: ' + digest['error'],
                'price_normalization': digest.get('price_normalization')}
    if not digest or bars < dt:
        return {"status": "immature", "symbol": sym, "as_of": _iso(ts), "dt": dt,
                "bars_in_window": bars,
                "why": f"window not fully covered ({bars}/{dt}) or no result price fetched this run (IBKR down / no new data; stale CSV is never used)"}

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts_compact = _iso(ts).replace("-", "").replace(":", "")
    ruler = digest.get("ruler") or RULER_VERSION
    staging_path = STAGING_DIR / f"{sym}_prep_{ts_compact}_dt{dt}_{ruler}.json"
    staging = {"symbol": sym, "as_of": _iso(ts), "dt": dt, "ruler": ruler,
               "win_start": digest.get("win_start", ""), "win_end": digest.get("win_end", ""),
               "digest": digest, "report": report}
    staging_path.write_text(json.dumps(staging, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
    # 回执: 给 parent 一眼能看的事实摘要 (不含整份 report/path, 那些读 staging)
    receipt = {"status": "ok", "staging_path": str(staging_path),
               "symbol": sym, "as_of": _iso(ts), "dt": dt, "ruler": ruler,
               "win_start": digest.get("win_start"), "win_end": digest.get("win_end"),
               "direction_call": digest.get("direction_call"),
               "bars_in_window": bars}

    def _st_brief(d: Dict) -> Dict:
        st = d.get("short_term") or {}
        return {"short_term_outcome": st.get("outcome_with_grace"),
                "short_term_horizon_days": st.get("horizon_days")} if st else {}

    if digest.get("general"):
        receipt["members"] = [{"symbol": m.get("symbol"), "ret_pct": m.get("ret_pct"),
                               "touched_target": m.get("touched_target"),
                               "touched_stop": m.get("touched_stop"), **_st_brief(m)}
                              for m in digest.get("members", [])]
    else:
        receipt.update({"ret_pct": digest.get("ret_pct"), "mfe_pct": digest.get("mfe_pct"),
                        "mae_pct": digest.get("mae_pct"),
                        "touched_target": digest.get("touched_target"),
                        "touched_stop": digest.get("touched_stop"), **_st_brief(digest)})
    return receipt


# ---------------------------------------------------------------------------
# save: 合并 parent 判断 → upsert postcheck
# ---------------------------------------------------------------------------
def save_postcheck(symbol: str, as_of, dt: int, *, score=None, verdict: str = "",
                   review: Optional[bool] = None, diagnosis: str = "",
                   lesion: str = "", search_request: str = "", quant: Optional[Dict] = None,
                   reckoner: str = RECKONER_SIGNATURE, checked_at=None,
                   ruler: Optional[str] = None) -> bool:
    """一份后验结果 upsert 进 postcheck (键含 ruler: 同报告换尺另存, 同尺重跑覆盖)。失败返 False 不抛。"""
    sym = str(symbol or "").upper()
    ts = _parse_ts(as_of)
    if not sym or ts is None or _safe_int(dt) is None:
        log.warning(f"missing symbol/as_of/dt, skipped (sym={sym!r}, as_of={as_of!r}, dt={dt!r})")
        return False
    ruler = ruler or (quant or {}).get("ruler") or RULER_VERSION
    try:
        with connect() as con, con.cursor() as cur:
            _write_result(cur, sym, ts, _safe_int(dt), ruler, score, verdict, review, diagnosis,
                          lesion, search_request, quant, reckoner, _parse_ts(checked_at) or datetime.now(timezone.utc))
        log.info(f"✅ postcheck {sym} @ {_iso(ts)} dt={_safe_int(dt)} ruler={ruler} score={score} by={reckoner}")
        return True
    except Exception as e:
        log.warning(f"postcheck write failed ({sym}): {type(e).__name__}: {e}")
        return False


def _write_result(cur, symbol, ts, dt, ruler, score, verdict, review, diagnosis, lesion, search_request,
                  quant, reckoner, checked_at):
    from datalayer.pg.i18n import has_cjk
    from psycopg2.extras import Json
    lang = "zh" if has_cjk(verdict or diagnosis or "") else "en"
    cur.execute(_UPSERT_SQL, (symbol, ts, dt, ruler, _safe_int(score), verdict or "", review,
                diagnosis or "", lesion or None, search_request or "", Json(quant) if quant is not None else None,
                reckoner or None, checked_at, lang))


def _save_group_judgment(j, directory):
    """Consume one judged hearing/window. Member digests come from their frozen staging files."""
    group = j["symbol"]
    cutoff = _parse_ts(j["as_of"])
    dt = j["dt"]
    if not cutoff or cutoff.utcoffset() is None or type(dt) is not int or dt not in POSTCHECK_TIERS.values() or not j.get("reckoner"):
        raise ValueError("group judgment needs a frozen cutoff, registered window and reckoner")
    members = {}
    for result in j["members"]:
        stage = json.loads((directory / result["staging_path"]).read_text(encoding="utf-8"))
        sym = stage["symbol"]
        ref = stage["report"].get("debate_ref") or {}
        if (sym in members or sym != stage["report"].get("symbol") or ref.get("symbol") != group
                or _parse_ts(ref.get("as_of")) != cutoff or _parse_ts(stage["as_of"]) != cutoff or stage["dt"] != dt):
            raise ValueError(f"conflicting member staging identity for {sym}")
        digest = stage["digest"]
        score = result["score"]
        if not isinstance(digest, dict) or not stage.get("ruler") or digest.get("ruler") != stage["ruler"] or not isinstance(result.get("verdict"), str) or not result["verdict"].strip():
            raise ValueError(f"missing ruler or verdict for {sym}")
        if score is not None and (type(score) is not int or not 0 <= score <= 100 or digest.get("error")
                                 or digest.get("settlement_status") == "unassessable" or digest.get("bars_in_window", 0) < dt):
            raise ValueError(f"invalid or unsupported score for {sym}")
        members[sym] = (result, stage)
    if len({st["ruler"] for _, st in members.values()}) != 1:
        raise ValueError("group members must use the same ruler")
    checked = datetime.now(timezone.utc)
    with connect() as con, con.cursor() as cur:
        cur.execute("SELECT report->'members' FROM debate_group_sessions WHERE symbol=%s AND as_of=%s",
                    (group, cutoff))
        row = cur.fetchone()
        if not row or set(members) != {m["symbol"] for m in row[0]}:
            raise ValueError("judgment must cover exactly the frozen group roster")
        for sym in sorted(members):
            result, stage = members[sym]
            _write_result(cur, sym, cutoff, dt, stage["ruler"], result["score"], result["verdict"],
                          result.get("review"), result.get("diagnosis"), result.get("lesion"),
                          result.get("search_request"), stage["digest"], j["reckoner"], checked)
    return dict(status="saved", symbol=group, as_of=_iso(cutoff), dt=dt,
                members=[dict(symbol=s, score=r["score"]) for s, (r, _) in members.items()])


def save_from_judgment(judgment_path: str) -> Dict:
    """读 parent 给的 judgment JSON (引 staging 取 symbol/as_of/dt/quant) → upsert。"""
    j = json.loads(Path(judgment_path).read_text(encoding="utf-8"))
    if "members" in j:
        from psycopg2 import Error as DatabaseError
        try:
            return _save_group_judgment(j, Path(judgment_path).parent)
        except (ValueError, KeyError, TypeError, OSError, DatabaseError) as exc:
            return {"status": "error", "why": str(exc)}
    staging_path = j.get("staging_path")
    if not staging_path or not Path(staging_path).exists():
        return {"status": "error", "why": f"staging file not found: {staging_path}"}
    st = json.loads(Path(staging_path).read_text(encoding="utf-8"))
    ok = save_postcheck(
        st["symbol"], st["as_of"], st["dt"],
        score=j.get("score"), verdict=j.get("verdict", ""),
        review=j.get("review"), diagnosis=j.get("diagnosis", ""),
        lesion=j.get("lesion", ""), search_request=j.get("search_request", ""),
        quant=st.get("digest"),
        reckoner=j.get("reckoner") or RECKONER_SIGNATURE,
        ruler=st.get("ruler"),
    )
    return {"status": "saved" if ok else "error", "symbol": st["symbol"],
            "as_of": st["as_of"], "dt": st["dt"], "ruler": st.get("ruler") or RULER_VERSION,
            "score": j.get("score"), "review": j.get("review")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(prog="postcheck_store.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("prep")
    p.add_argument("--symbol", required=True)
    p.add_argument("--dt", type=int, required=True)
    p.add_argument("--as_of", default=None, help="pick exactly this period (omit = latest mature period)")
    p.add_argument("--start", default=None, help="lower bound YYYY-MM-DD for the latest mature period (optional)")
    s = sub.add_parser("save")
    s.add_argument("judgment", help="path to the judgment JSON")
    args = ap.parse_args()

    if args.cmd == "init":
        ensure_table()
        return
    if args.cmd == "prep":
        ensure_table()
        out = prep(args.symbol, args.dt, as_of=args.as_of, start=args.start)
    else:  # save
        out = save_from_judgment(args.judgment)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
