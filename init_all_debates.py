#!/usr/bin/env python3
"""
init_all_debates.py — 全量初始化辩论 (手动一次性跑)

流程：
  Phase 1: Finnhub 新闻更新
            - stocks: 2 线程并行 (key1/key2), 各自 10s 间隔 (~6 req/min per key)
            - ETFs:   同上，stocks 完成后续跑
  Phase 2: IBKR 串行预取
            - 所有 runnable US 标的的行情数据串行拉完，后续辩论跳过 fetch
  Phase 3: 辩论 (DAG 波次, asyncio 并行 subprocess)
            - Wave 0: GENERAL 系列 (全大区, 并行互不依赖)
            - Wave 1: US ETFs + EU/JP/KR 个股 并行 (父层 Wave0 已全完成, 保证时效性)
            - Wave 2a: 无 sector US 个股 (父=GENERAL)
            - Wave 2b: 有 sector US 个股 (全部并行, 各自父 ETF 在 Wave1 已完成)
            全部带 --search --force

标的成员唯一真源 = config/symbols.yaml (经 workflows/symbol_universe 派生),
本文件不再维护任何硬编码清单; 加/删标的只改 symbols.yaml。
"""

import asyncio
import logging
import sys
import time
import threading
from pathlib import Path
from typing import List, Dict

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "logs" / "init_debates.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── 符号定义 (唯一真源 config/symbols.yaml, 经 workflows/symbol_universe 派生;
#     禁止在此写死清单 —— 加/删标的只改 symbols.yaml 一处) ──────────────────

from workflows.symbol_universe import load_symbol_config, symbols_for

_SYCFG = load_symbol_config()
_CONTRACTS = _SYCFG.get("symbol_contracts") or {}

GENERALS: List[str] = list((_SYCFG.get("analysis_targets") or {}).get("generals") or ["GENERAL"])
STOCKS: List[str] = symbols_for("stocks", "US", _SYCFG)
ETFS: List[str] = symbols_for("etfs", "US", _SYCFG)
EU_STOCKS: List[str] = symbols_for("stocks", "EU", _SYCFG)
JP_STOCKS: List[str] = symbols_for("stocks", "JP", _SYCFG)
KR_STOCKS: List[str] = symbols_for("stocks", "KR", _SYCFG)


def _sector(sym: str):
    """个股父 ETF 归属, 读 symbol_contracts.sector (无 = 直接父 GENERAL)。"""
    return (_CONTRACTS.get(sym) or {}).get("sector")

FINN_DELAY = 1.0   # 秒/请求 (fetcher 内部已有 1s, 合计 2s/sym, ~30 req/min per key; 免费档上限 60/min)


def _news_keys() -> tuple:
    """Finnhub key 读 config/data_sources.yaml 的 news.api_key1/2, 不再明文写死在代码里。"""
    from horizon_sentinel import load_helpers
    news = (load_helpers()[0].get("data_sources") or {}).get("news") or {}
    k1 = news.get("api_key1") or news.get("api_key")
    k2 = news.get("api_key2") or k1
    if not k1:
        raise RuntimeError("config/data_sources.yaml 缺 news.api_key1 —— Finnhub key 未配置")
    return k1, k2


# ════════════════════════════════════════════════════════════════════════
# Phase 1: Finnhub 新闻
# ════════════════════════════════════════════════════════════════════════

def _fetch_news_batch(symbols: List[str], api_key: str, key_label: str):
    """单线程: 用指定 key 顺序更新 symbols 的新闻, 每次 sleep FINN_DELAY。"""
    from datalayer.fetchers.finnhub_news_fetcher import FinnhubNewsFetcher
    fetcher = FinnhubNewsFetcher(api_key=api_key)
    for sym in symbols:
        log.info(f"[NEWS/{key_label}] {sym}")
        try:
            fetcher.download_company_news(sym, days=7)
        except Exception as e:
            log.warning(f"[NEWS/{key_label}] {sym} failed: {e}")
        time.sleep(FINN_DELAY)
    log.info(f"[NEWS/{key_label}] batch done ({len(symbols)} symbols)")


def update_news(symbols: List[str]):
    """2 key 各分一半, 两线程并行跑。"""
    key1, key2 = _news_keys()
    half = len(symbols) // 2
    batch1 = symbols[:half]
    batch2 = symbols[half:]
    t1 = threading.Thread(target=_fetch_news_batch, args=(batch1, key1, "key1"), daemon=True)
    t2 = threading.Thread(target=_fetch_news_batch, args=(batch2, key2, "key2"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    log.info(f"[NEWS] All {len(symbols)} symbols updated.")


# ════════════════════════════════════════════════════════════════════════
# Phase 2: IBKR 串行预取
# ════════════════════════════════════════════════════════════════════════

async def prefetch_ibkr():
    from horizon_sentinel import (
        IbkrFetcher, is_symbol_data_fresh,
        DATA_FRESHNESS_SECONDS, BENCHMARK_SYMBOLS, load_helpers,
    )
    from run_one_debate import _fetch_needs

    data_config, symbols_config, _ = load_helpers()
    ta_dir = data_config.get("data_sources", {}).get("technical_analysis", {}).get("output_dir")
    fetcher = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)

    all_targets = GENERALS + ETFS + EU_STOCKS + JP_STOCKS + KR_STOCKS + STOCKS
    # 展开每个标的需要的数据符号, 去重
    needed: List[str] = []
    seen = set()
    for sym in all_targets:
        for fsym in _fetch_needs(sym, symbols_config):
            if fsym not in seen:
                seen.add(fsym)
                needed.append(fsym)

    log.info(f"[IBKR] {len(needed)} data symbols to check: {needed}")
    fetched = skipped = 0
    for fsym in needed:
        if is_symbol_data_fresh(fsym, ta_dir, DATA_FRESHNESS_SECONDS):
            log.info(f"[IBKR] {fsym} fresh, skip")
            skipped += 1
        else:
            log.info(f"[IBKR] Fetching {fsym}...")
            try:
                await fetcher.fetch_symbol(fsym)
                fetched += 1
            except Exception as e:
                log.warning(f"[IBKR] {fsym} fetch error: {e}")

    await fetcher.close()
    log.info(f"[IBKR] Pre-fetch done. fetched={fetched} skipped={skipped}")


# ════════════════════════════════════════════════════════════════════════
# Phase 3: 辩论 (DAG 波次, asyncio 并行 subprocess)
# ════════════════════════════════════════════════════════════════════════

async def run_debate(symbol: str) -> tuple:
    """跑单个 run_one_debate.py --search --force, 返回 (symbol, exit_code)。"""
    cmd = [
        sys.executable, str(ROOT / "run_one_debate.py"),
        symbol, "--search", "--force",
    ]
    log.info(f"[DEBATE] → {symbol}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    rc = proc.returncode
    tail = stdout.decode(errors="replace")[-600:] if stdout else ""
    log.info(f"[DEBATE] ✓ {symbol} exit={rc}")
    if rc != 0:
        log.warning(f"[DEBATE] {symbol} non-zero exit:\n{tail}")
    return symbol, rc


async def run_wave(symbols: List[str], label: str) -> Dict[str, int]:
    if not symbols:
        return {}
    log.info(f"[WAVE] {label}: {symbols}")
    results = await asyncio.gather(*[run_debate(s) for s in symbols], return_exceptions=True)
    summary = {}
    for r in results:
        if isinstance(r, Exception):
            log.error(f"[WAVE] {label} task exception: {r}")
        else:
            sym, rc = r
            summary[sym] = rc
    log.info(f"[WAVE] {label} done: {summary}")
    return summary


async def run_debates():
    # Wave 0: GENERAL 系列 (symbols.yaml generals, 全大区) 并行, 互不依赖
    await run_wave(GENERALS, "Wave-0 GENERAL")

    # Wave 1: US ETFs + EU/JP/KR 个股 并行 (父层 Wave0 已全完成, 保证时效性)
    await run_wave(ETFS + EU_STOCKS + JP_STOCKS + KR_STOCKS, "Wave-1 ETFs+EU+JP+KR")

    # Wave 2a: 无 sector US 个股 (父=GENERAL)
    no_sector = [s for s in STOCKS if not _sector(s)]
    await run_wave(no_sector, "Wave-2a NoSector")

    # Wave 2b: 有 sector US 个股 (父 ETF 已在 Wave-1 完成, 全部并行)
    sector_stocks = [s for s in STOCKS if _sector(s)]
    await run_wave(sector_stocks, "Wave-2b SectorStocks")

    log.info("[DEBATES] All waves complete.")


# ════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════

async def main():
    import os
    start = int(os.getenv("START_PHASE", "1"))

    if start <= 1:
        log.info("═══ Phase 1: Finnhub news (stocks) ═══")
        update_news(STOCKS)
        log.info("═══ Phase 1b: Finnhub news (ETFs) ═══")
        update_news(ETFS)

    if start <= 2:
        log.info("═══ Phase 2: IBKR serial pre-fetch ═══")
        await prefetch_ibkr()

    log.info("═══ Phase 3: Debates (DAG, all --search --force) ═══")
    await run_debates()

    log.info("═══ Init complete ═══")


if __name__ == "__main__":
    asyncio.run(main())
