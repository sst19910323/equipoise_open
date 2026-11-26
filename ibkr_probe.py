#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ibkr_probe.py — 盈透健康探针 (确定性, 秒级, 零 LLM, 无副作用)。

为什么分层: Gateway 有两种死法, 处理路径相同(都重启)但报出来要分清, 方便人排障:
  Layer 0  TCP        端口有没有人监听 (3s 超时)
  Layer 1  API 握手   connect_and_start 能不能建会话
  Layer 2  真实数据   请求 SYMBOL 最近 2 个交易日的日线 bar —— Gateway 有一种半死:
                      socket 活着、数据农场断了, 只有真实请求能揪出来。不落 CSV, 无副作用。

退出码:
  0  = 全通 (打印最新 bar 日期+收盘价)
  1  = 端口不通 / 握手失败 (Gateway 进程死/没登录)  → bash ibkr_restart.sh
  2  = 连上了但数据不流 (数据农场断/僵尸)            → bash ibkr_restart.sh
  64 = 用法错误 (symbol 不在 config/symbols.yaml)

用法 (服务器, 必带端口):
  cd /opt/equipoise && EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python ibkr_probe.py [SYMBOL]
  SYMBOL 省略 = SPY; 按 config/symbols.yaml 的 symbol_contracts 路由 (也可探 RHM/N225 等)。
"""
from __future__ import annotations

import os
import random
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else "SPY"
    sym = raw if raw.isdigit() else raw.upper()
    host = os.environ.get("EQUIPOISE_IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("EQUIPOISE_IBKR_PORT", "7496"))
    print(f"[probe] target {host}:{port}  symbol {sym}  (服务器 Gateway 记得 EQUIPOISE_IBKR_PORT=4001)")

    # Layer 0: TCP
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as e:
        print(f"PROBE: PORT-DEAD ({type(e).__name__}: {e}) — Gateway 没监听/没登录 → bash ibkr_restart.sh")
        return 1

    import yaml
    from datalayer.fetchers.interactive_stock_fetcher import (
        MarketDataConfig, MarketDataFetcher, SymbolContract,
    )

    contracts = (yaml.safe_load((ROOT / "config" / "symbols.yaml").read_text(encoding="utf-8"))
                 or {}).get("symbol_contracts", {})
    c_info = contracts.get(sym)
    if not c_info:
        print(f"PROBE: BAD-SYMBOL {sym} 不在 config/symbols.yaml 的 symbol_contracts")
        return 64

    fetcher = MarketDataFetcher(MarketDataConfig.from_yaml(str(ROOT / "config" / "data_sources.yaml")))
    try:
        # Layer 1: API 握手
        ok = fetcher.connect_and_start(host=host, port=port,
                                       client_id=random.randint(20000, 29999))
        if not ok or not fetcher.isConnected():
            print("PROBE: PORT-DEAD (API 握手失败) → bash ibkr_restart.sh")
            return 1

        # Layer 2: 真实数据 (走底层请求, 不经 fetch_bars, 不落 CSV 缓存)
        contract = SymbolContract(symbol=c_info["symbol"], sec_type=c_info["sec_type"],
                                  exchange=c_info["exchange"], currency=c_info["currency"],
                                  ibkr_ticker=c_info.get("ibkr_ticker"))
        req_id = fetcher._next_req_id()
        fetcher._submit_request(
            req_id=req_id, contract=contract.to_ib_contract(),
            duration="2 D", bar_size="1 day", end_datetime="",
            what_to_show="TRADES", use_rth=1, format_date=2,
        )
        if not fetcher._wait_for_completion(req_id, timeout=25.0):
            print("PROBE: DATA-DEAD (请求 25s 超时 — socket 活着但数据不流) → bash ibkr_restart.sh")
            return 2
        bars = fetcher._data_store.get(req_id, [])
        if not bars:
            print("PROBE: DATA-DEAD (返回空 bar — 数据农场断/无权限) → bash ibkr_restart.sh")
            return 2
        last = bars[-1]
        print(f"PROBE: OK {sym} last_bar={getattr(last, 'date', '?')} close={getattr(last, 'close', '?')}")
        return 0
    finally:
        try:
            fetcher.disconnect_and_stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
