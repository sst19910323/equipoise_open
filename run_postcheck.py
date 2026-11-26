#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_postcheck.py — BP 后验驱动。捞"成熟"报告 → **新请求结果价(连盈透)** → 算窗口量化摘要(摆动线)
→ Reckoner 清算/归因 → 写 postcheck。

用法 (⚠️ 2026-07-02 起后验要连盈透拉结果价, 服务器上带 EQUIPOISE_IBKR_PORT=4001):
    EQUIPOISE_IBKR_PORT=4001 python run_postcheck.py --dt 14              # 默认起点=10天前, 标的=all
    EQUIPOISE_IBKR_PORT=4001 python run_postcheck.py --dt 14 --latest --symbols AAPL,NVDA,GENERAL

逻辑:
  起点往后所有报告(debates) 中, 已"成熟"(窗口内有 ≥dt 根交易日) 的逐个后验; 覆盖重写 postcheck。
  结果价数据模型(2026-07-02 定): 本次**新请求**该标的日线(IBKR) → 现算摆动线摘要 → 摘要(quant)
  随 postcheck 行**永久入 PG**; 原始 bar/CSV 是临时运输产物不永久保留, 也绝不吃残留旧 CSV
  (盈透挂/没请求到 = 该期无结果价, 跳过并报出来)。
  raw_data_provider 复用 ContextAssembler 的 as_of 历史装配(当时全部数据)。
  search_fn 接 **review_scout(复盘斥候, 后验视角)** —— 与辩论的 evidence_officer 刻意分开。
"""
import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from datalayer.pg.lowfreq_reader import _load_pg_cfg
from workflows.postcheck_quant import compute_window_digest, _GENERAL_MEMBERS
from datalayer.pg.postcheck_store import _prior_timeline
from datalayer.pg.postcheck_pg_store import save_postcheck
from workflows.reckoner import run_reckoning, reckoner_signature
from workflows.llm_client import LLMClient
from horizon_sentinel import (ContextAssembler, load_helpers,
                              EU_BENCHMARK_SYMBOLS, JP_BENCHMARK_SYMBOLS)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("postcheck")


def _fetch_reports(start: datetime, maturity_cutoff: datetime, symbols, latest: bool):
    """从 debates 捞 (symbol, as_of, report)。
    start ≤ as_of ≤ maturity_cutoff(=now-dt天, 必要成熟条件预筛); symbols=None→全部;
    latest→每标的最新的(已预筛成熟的)一期。精确成熟闸(bars≥dt)在主循环里。"""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception as e:
        log.error(f"psycopg2 缺失: {e}")
        return []
    cfg = _load_pg_cfg()
    try:
        con = psycopg2.connect(
            host=cfg["host"], port=int(cfg["port"]), dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"], connect_timeout=5,
            client_encoding="UTF8", options="-c default_transaction_read_only=on")
    except Exception as e:
        log.error(f"PG 连接失败: {e}")
        return []
    try:
        where = ["as_of >= %s", "as_of <= %s"]
        params = [start, maturity_cutoff]
        if symbols:
            where.append("symbol = ANY(%s)")
            params.append([s.upper() for s in symbols])
        wc = " AND ".join(where)
        if latest:
            sql = (f"SELECT DISTINCT ON (symbol) symbol, as_of, report FROM debates "
                   f"WHERE {wc} ORDER BY symbol, as_of DESC")
        else:
            sql = f"SELECT symbol, as_of, report FROM debates WHERE {wc} ORDER BY symbol, as_of"
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [(r["symbol"], r["as_of"], r["report"] or {}) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"查 debates 失败: {e}")
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _make_raw_provider(assembler, symbol: str, as_of):
    """返回一个 () -> 当时全部原始数据文本 的 provider (复用 as_of 历史装配)。"""
    def _provider() -> str:
        try:
            if symbol == "GENERAL":
                raw, _ = assembler.assemble_general(as_of=as_of)
            elif symbol == "GENERAL_EU":
                raw, _ = assembler.assemble_general_region("EU", EU_BENCHMARK_SYMBOLS, "GENERAL_EU", as_of=as_of)
            elif symbol == "GENERAL_JP":
                raw, _ = assembler.assemble_general_region("JP", JP_BENCHMARK_SYMBOLS, "GENERAL_JP", as_of=as_of)
            else:
                raw, _ = assembler.assemble_stock(symbol, as_of=as_of)
        except Exception as e:
            log.warning(f"  [{symbol}] as_of 装配失败: {e}")
            return ""
        # 把当时喂给 AI 的主要数据块拼成文本
        keys = ["tech_data", "benchmark_data", "sector_tech_data", "macro_data",
                "fear_greed_data", "fundamentals_data", "profile_data", "news_data"]
        parts = []
        for k in keys:
            v = raw.get(k)
            if v and str(v).strip() not in ("{}", "[]", ""):
                parts.append(f"### {k}\n{v}")
        return "\n\n".join(parts)
    return _provider


def _make_search_fn(symbol: str, as_of, dt: int, win_start, win_end):
    """Reckoner 的复盘查证 → **复盘斥候 review_scout**(后验视角, 与辩论 Scout 刻意分开)。
    时间约束(软, 写进 context): 框 [win_start, win_end], 硬截止 win_end(后验窗口末端),
    别参考晚于此的"未来"信息。grounding 没法 API 级卡日期, 靠提示。
    review_scout.investigate 直接回一段已蒸馏的证据文本(逐条 结论+原文 + 概述)。"""
    def _search(query: str) -> str:
        if not (query or "").strip():
            return ""
        ctx = (f"这是对 {symbol} 在 {as_of} 那份报告做 {dt} 交易日**后验复盘**的查证(非辩论)。"
               f"窗口 {win_start} ~ {win_end}, 结果已知, 你回头还原窗口内实情。非必要别翻太古老的信息；"
               f"**硬截止 {win_end}——绝不要参考晚于此时间点的信息**(那是后验窗口之后的未来, 用了不公平)。")
        try:
            from workflows.evidence_officer import load_search_cfg
            provider = load_search_cfg().get("provider", "linkup")
            if provider == "linkup":
                from workflows.review_scout_linkup import investigate
                return investigate(query, context=ctx, to_date=win_end) or ""  # to_date 硬截止
            from workflows.review_scout import investigate
            return investigate(query, context=ctx) or ""
        except Exception as e:
            log.warning(f"  [review_scout/{__name__}] 查证异常: {e}")
            return ""
    return _search


async def _prefetch_prices(price_syms, data_config, symbols_config) -> None:
    """结果价新请求 (2026-07-02 规则): 连盈透把本批要用的价格标的日线拉一遍。
    鲜的(1h内刚拉过)跳; 拉挂的标的过不了 min_mtime 闸 → 对应报告本期跳过, 不吃旧 CSV。"""
    from horizon_sentinel import IbkrFetcher, is_symbol_data_fresh, DATA_FRESHNESS_SECONDS
    ta_dir = data_config.get('data_sources', {}).get('technical_analysis', {}).get('output_dir')
    fetcher = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
    try:
        for s in price_syms:
            if not is_symbol_data_fresh(s, ta_dir, DATA_FRESHNESS_SECONDS):
                await fetcher.fetch_symbol(s)
    finally:
        await fetcher.close()


def _maturity_cutoff(now: datetime, dt: int) -> datetime:
    """as_of ≤ 返回值 → 自 as_of 起已过 ≥ dt 个**美股交易日**(窗口能跑满)。
    复用 horizon 的 NYSE 交易日历(pandas_market_calendars); 不可用则退回日历天近似(now-dt天)。
    精确成熟(数据真到位)仍由主循环 bars_in_window≥dt 判。"""
    try:
        from horizon_sentinel import get_trading_calendar
        cal = get_trading_calendar("NYSE")
        sched = cal.schedule(start_date=(now - timedelta(days=dt * 3 + 25)).date(),
                             end_date=now.date())
        sessions = sched.index
        if len(sessions) > dt:
            # 倒数第 dt 个交易日: 报告早于它 → 其后已有 ≥ dt 个交易日
            cut = sessions[-dt]
            cut = cut.tz_localize("UTC") if cut.tzinfo is None else cut.tz_convert("UTC")
            return cut.to_pydatetime()
    except Exception as e:
        log.warning(f"[postcheck] NYSE 交易日历不可用, 退回日历天近似: {e}")
    return now - timedelta(days=dt)


def main():
    ap = argparse.ArgumentParser(prog="run_postcheck.py", description="BP 后验驱动")
    ap.add_argument("--dt", type=int, required=True, help="后验窗口(交易日), 如 14")
    ap.add_argument("--start", default=None, help="起点 YYYY-MM-DD; 默认 10 天前")
    ap.add_argument("--latest", action="store_true", help="每标的只跑最新一期")
    ap.add_argument("--symbols", default="all", help="all 或逗号分隔 list, 如 AAPL,NVDA,GENERAL")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = now - timedelta(days=10)
    symbols = None if args.symbols.strip().lower() == "all" else \
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    # 成熟截止: 用 NYSE 交易日历精确算"已过 dt 个交易日"的 as_of 上界(复用 horizon 日历);
    # 精确成熟(窗口真有 dt 根交易 bar / 数据到位)仍由主循环 bars_in_window 判。
    maturity_cutoff = _maturity_cutoff(now, args.dt)
    reports = _fetch_reports(start, maturity_cutoff, symbols, args.latest)
    log.info(f"起点={start:%Y-%m-%d} dt={args.dt}交易日 截止≤{maturity_cutoff:%Y-%m-%d} "
             f"标的={args.symbols} latest={args.latest} → 候选(已预筛成熟) {len(reports)} 份")
    if not reports:
        log.info("无可后验报告(没有窗口已跑满的报告) — 正常, 太新的报告还没成熟")
        return

    data_config, symbols_config, _ = load_helpers()
    assembler = ContextAssembler(data_config, symbols_config)

    # 结果价新请求: 汇总本批用到的价格标的(GENERAL* 展开成指数镜头), 连盈透拉日线。
    # min_mtime = 预取起点往前推一个鲜度窗 —— 早于它的 CSV 是旧货, digest 直接判无数据。
    from horizon_sentinel import DATA_FRESHNESS_SECONDS
    min_mtime = time.time() - DATA_FRESHNESS_SECONDS
    price_syms = []
    for symbol, _, _ in reports:
        price_syms.extend(_GENERAL_MEMBERS.get(symbol.upper(), [symbol.upper()]))
    price_syms = list(dict.fromkeys(price_syms))
    log.info(f"结果价新请求(IBKR): {', '.join(price_syms)}")
    asyncio.run(_prefetch_prices(price_syms, data_config, symbols_config))

    llm = LLMClient()
    reckoner = reckoner_signature(llm)  # 机械签名: 这批后验是谁(哪个模型)清算的
    log.info(f"回测人(reckoner)={reckoner}")

    ran = skipped = 0
    for symbol, as_of, report in reports:
        digest = compute_window_digest(symbol, as_of, args.dt, report, min_mtime=min_mtime,
                                       prior_timeline=_prior_timeline(symbol, as_of, args.dt))
        if not digest or digest.get("bars_in_window", 0) < args.dt:
            log.info(f"  ⏭️ {symbol} @ {as_of}: 窗口未成熟/本次没请求到结果价 "
                     f"({digest.get('bars_in_window', 0)}/{args.dt}), 跳过")
            skipped += 1
            continue
        win_start = digest.get("win_start", "")
        win_end = digest.get("win_end", "")
        log.info(f"  🔍 后验 {symbol} @ {as_of} | 窗口 {win_start}~{win_end} ret={digest.get('ret_pct')}%")
        result = run_reckoning(
            symbol, as_of, args.dt, win_start, win_end, report, digest, llm,
            raw_data_provider=_make_raw_provider(assembler, symbol, as_of),
            search_fn=_make_search_fn(symbol, as_of, args.dt, win_start, win_end),
        )
        save_postcheck(symbol, as_of, dt=args.dt, score=result.get("score"),
                       verdict=result.get("verdict", ""), review=result.get("review"),
                       diagnosis=result.get("diagnosis", ""),
                       search_request=result.get("search_request", ""), quant=digest,
                       reckoner=reckoner)
        log.info(f"     → score={result.get('score')} review={result.get('review')}: {result.get('verdict','')[:60]}")
        ran += 1

    log.info(f"后验完成: 清算 {ran} 份, 跳过(未成熟) {skipped} 份")


if __name__ == "__main__":
    main()
