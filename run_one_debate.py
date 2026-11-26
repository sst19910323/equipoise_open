#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_one_debate.py — 单标的辩论入口 (Hermes 触发用)。

复用 horizon_sentinel 的现成积木 (IbkrFetcher + run_debate_sync + ContextAssembler + DebateEngine)，
只跑**一个**标的、带数据自检、用退出码反馈给 Hermes。

用法:
    python run_one_debate.py <SYMBOL>              # 如 AAPL / GENERAL / SMH
    python run_one_debate.py <SYMBOL> --force      # 重跑: 绕过"报告已存"去重, 强制重辩
    python run_one_debate.py <SYMBOL> --no-search  # 本次关闭证据搜索
    python run_one_debate.py <SYMBOL> --search     # 本次开启证据搜索

开关 (一切为辩论服务, 没有"只爬不辩"的半截入口):
- --force      重跑: 只绕过"报告已存"去重, 强制重新辩论。**仍保留"无行情就 skip"硬闸**
               (价格是硬性前提, 没价格不辩免幻觉; 其余数据缺了能凑合)。也不无脑重砸 IBKR(行情够新则复用)。
- --search / --no-search  本次显式开/关证据搜索(内部即设 EQUIPOISE_EVIDENCE_SEARCH=1/0, 覆盖 config)。
               都不给 → 按 config 默认(data_sources.yaml evidence_search.enabled, 当前默认开)。

退出码 / STATUS (Hermes 据此编排):
    0   STATUS: ran                    跑完辩论, 报告已存(JSON + PG; 量化快照也随之进 PG technical 表)
    10  STATUS: skipped-no-market-data IBKR 挂 / 无新 bar / 技术数据缺 → 没跑(没价格不硬辩)
    12  STATUS: skipped-fresh-exists   已有时间接近的报告, 去重跳过(--force 可强制重辩)
    20  STATUS: error                  异常
    2   用法错误

边界:
- 不做依赖编排(GENERAL→sector→个股的顺序由 Hermes 负责; 本脚本只跑被点名的那个)。
- 数据缺只**自检 + 返回状态码**, 不自己去刷(刷经济/基本面/情绪是隔壁 codex 的活, Hermes 编排)。
"""
import os
import sys
import argparse
import asyncio
import logging

from horizon_sentinel import (
    load_helpers, DebateEngine, ContextAssembler, IbkrFetcher,
    run_debate_sync, is_symbol_data_fresh,
    DATA_FRESHNESS_SECONDS, BENCHMARK_SYMBOLS,
)

_STATUS_EXIT = {
    "ran": 0,
    "skipped-no-market-data": 10,
    "skipped-fresh-exists": 12,
}


def _ctx_type(symbol: str, symbols_map: dict, contracts: dict = None) -> str:
    if symbol.startswith("GENERAL"):
        return "general"
    # 合约级覆盖(2026-09-30, DAX 案): context_type 字段优先, 非美标的借框架不入美股列表
    info = (contracts or {}).get(symbol) or {}
    if info.get("context_type"):
        return str(info["context_type"])
    if symbol in symbols_map.get("etfs", []):
        return "etf"
    return "stock"


def _fetch_needs(symbol: str, symbols_config: dict) -> list:
    """该标的辩论需要哪些 symbol 的 IBKR 行情 (= 自己 + benchmark + sector)。
    镜像 horizon run_analysis_phase 的 fetch_map 逻辑。"""
    if symbol == "GENERAL":
        return list(BENCHMARK_SYMBOLS)   # SPY/QQQ/DIA 三镜头
    if symbol == "GENERAL_EU":
        return ["DAX", "IAEX"]
    if symbol == "GENERAL_JP":
        return ["N225", "TOPX"]           # 2026-11-02: 第二镜头 TOPIX 指数
    if symbol == "GENERAL_KR":
        return ["KODEX200", "KOSDQ150"]   # 2026-09-28: K200 指数 HMDS 断供, 镜头切 KODEX200 ETF; 2026-11-02: 第二镜头 KOSDAQ150 指数
    info = symbols_config.get('symbol_contracts', {}).get(symbol, {})
    needs = [symbol]
    for k in ("benchmark", "sector"):
        v = info.get(k)
        if v and v not in needs:
            needs.append(v)
    return needs


def _fetch_us_news(symbol: str, symbols_config: dict) -> None:
    """美股大区标的跑辩论前必先拉 Finn 新闻 (2026-07-02 规则; region 缺省=US, 含 TSM/ASML)。
    个股: 宏观+自己+sector; ETF: 宏观+自己; GENERAL: 宏观。EU/JP 走各自搜索腿, 不在这。
    best-effort: 拉挂了只警告不拦辩论 (新闻缺失由辩论中证据搜索兜底)。"""
    if symbol in ("GENERAL_EU", "GENERAL_JP", "GENERAL_KR"):
        return
    contracts = symbols_config.get('symbol_contracts', {})
    if symbol != "GENERAL" and contracts.get(symbol, {}).get('region', 'US') != 'US':
        return
    try:
        from datalayer.fetchers.finnhub_news_fetcher import FinnhubNewsFetcher
        nf = FinnhubNewsFetcher()   # 不传 key = 每次请求随机选一个
        nf.download_general_news("GENERAL")
        if symbol != "GENERAL":
            nf.download_company_news(symbol)
            sector = contracts.get(symbol, {}).get('sector')
            if sector:
                nf.download_company_news(sector)
    except Exception as e:
        logging.warning(f"[news] Finn 新闻拉取失败(不拦辩论, 辩论内搜索可兜底): {e}")


async def _run(symbol: str, force: bool = False, as_of=None) -> str:
    data_config, symbols_config, symbols_map = load_helpers()
    engine = DebateEngine()
    assembler = ContextAssembler(data_config, symbols_config)
    ta_dir = data_config.get('data_sources', {}).get('technical_analysis', {}).get('output_dir')
    fetcher = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
    try:
        # 1) live 才拉 IBKR 行情 (自己+benchmark+sector; 鲜则跳, IBKR 挂则 fetch_symbol 内部 skip)。
        #    builder 算完技术快照时会 best-effort 双写进 PG technical 表 (量化数据随辩论自动入库)。
        #    历史回放(as_of)直接用 PG 历史快照, 不碰盈透。
        if as_of is None:
            for fsym in _fetch_needs(symbol, symbols_config):
                if not is_symbol_data_fresh(fsym, ta_dir, DATA_FRESHNESS_SECONDS):
                    await fetcher.fetch_symbol(fsym)
            # 1.5) 美股大区: 跑前必拉 Finn 新闻 (宏观+自己+sector; EU/JP 内部自动跳过)
            _fetch_us_news(symbol, symbols_config)
        # 2) 跑辩论 (run_debate_sync 内部自检 tech_data, 返回状态码; force 穿透去重; as_of 走历史)
        ctx = _ctx_type(symbol, symbols_map, symbols_config.get("symbol_contracts"))
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(
            None, run_debate_sync, engine, assembler, symbol, ctx, data_config, force, as_of
        )
        return status or "ran"
    finally:
        await fetcher.close()


def main():
    ap = argparse.ArgumentParser(
        prog="run_one_debate.py",
        description="单标的辩论入口 (Hermes 触发用): 拉IBKR→算量化→写库→辩论",
    )
    ap.add_argument("symbol", nargs="?", help="标的代码, 如 AAPL / GENERAL / SMH")
    ap.add_argument("--force", action="store_true",
                    help="重跑: 绕过'报告已存'去重强制重辩 (仍保留'无行情就skip'硬闸)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--no-search", dest="search", action="store_false", default=None,
                     help="本次关闭证据搜索 (设 EQUIPOISE_EVIDENCE_SEARCH=0)")
    grp.add_argument("--search", dest="search", action="store_true", default=None,
                     help="本次开启证据搜索 (设 EQUIPOISE_EVIDENCE_SEARCH=1)")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="历史回放: UTC 时刻(如 2026-06-10T13:30:00Z), 给则走PG历史快照、不拉IBKR、强制关搜索")
    args = ap.parse_args()

    if not args.symbol or not args.symbol.strip():
        ap.print_usage()
        sys.exit(2)
    symbol = args.symbol.strip().upper().replace(" ", ".")

    # 搜索开关: 仅在显式给了 --search/--no-search 时覆盖 config (否则按 config 默认)
    if args.search is not None:
        os.environ["EQUIPOISE_EVIDENCE_SEARCH"] = "1" if args.search else "0"
    # 历史回放恒关 grounding (evidence 无 point-in-time 回放)
    if args.as_of:
        os.environ["EQUIPOISE_EVIDENCE_SEARCH"] = "0"

    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    try:
        status = asyncio.run(_run(symbol, force=args.force, as_of=args.as_of))
    except Exception as e:
        logging.error(f"[run_one_debate] {symbol} 异常: {e}", exc_info=True)
        print("STATUS: error")
        sys.exit(20)

    print(f"STATUS: {status}")
    sys.exit(_STATUS_EXIT.get(status, 0))


if __name__ == "__main__":
    main()
