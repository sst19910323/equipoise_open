#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exchange clocks, trading calendars and session classification shared by the technical snapshot builder,
evidence_quant and the postcheck (moved out of technical_snapshot_builder.py unchanged, V16 2026-10-25).

primary_exchange (config/symbols.yaml) → calendar / local tz / extended hours; session = pre-market / regular /
after-hours / closed (lunch breaks count as closed); the last completed daily bar; prior regular close.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import pandas_market_calendars as mcal
import yaml

log = logging.getLogger(__name__)

# ───────────── Session Classification (V15.1: 按标的交易所时钟, 2026-09-30) ───────────── #
# 旧版所有标的都按 NYSE 日历判时段: 欧洲/日本/韩国本地合约盘中被判成 closed/pre-market,
# 盘中字段清空、日线指标晚一天(flow-cc §4.1 Q3)。现在时钟由 symbols.yaml 的 primary_exchange 决定。

PREMARKET_START_ET_HOUR = 4   # 04:00 ET (美股延长时段起点)
AFTERHOURS_END_ET_HOUR = 20   # 20:00 ET (美股延长时段终点)

_US_CLOCK = {"calendar": "NYSE", "tz": "America/New_York",
             "premarket_start_hour": PREMARKET_START_ET_HOUR,
             "afterhours_end_hour": AFTERHOURS_END_ET_HOUR, "has_extended_hours": True}
# primary_exchange → 交易日历(pandas_market_calendars 名) / 本地时区 / 有无延长时段。
# 非美交易所盈透只给正股时段 bar, 没有盘前盘后: 收盘外一律 closed, 不冒充 after-hours。
EXCHANGE_CLOCKS: Dict[str, Dict[str, Any]] = {
    "NYSE": _US_CLOCK, "NASDAQ": _US_CLOCK, "ARCA": _US_CLOCK, "SMART": _US_CLOCK,
    "IBIS": {"calendar": "XETR", "tz": "Europe/Berlin"},
    "EUREX": {"calendar": "XETR", "tz": "Europe/Berlin"},      # DAX 指数随 Xetra 交易时段计算
    "AEB": {"calendar": "XAMS", "tz": "Europe/Amsterdam"},
    "SBF": {"calendar": "XPAR", "tz": "Europe/Paris"},
    "BVME": {"calendar": "XMIL", "tz": "Europe/Rome"},
    "EBS": {"calendar": "XSWX", "tz": "Europe/Zurich"},
    "CPH": {"calendar": "XCSE", "tz": "Europe/Copenhagen"},
    "TSEJ": {"calendar": "JPX", "tz": "Asia/Tokyo"},
    "OSE.JPN": {"calendar": "JPX", "tz": "Asia/Tokyo"},
    "KSE": {"calendar": "XKRX", "tz": "Asia/Seoul"},
}
_REGION_DEFAULT_EXCHANGE = {"US": "NYSE", "EU": "IBIS", "JP": "TSEJ", "KR": "KSE"}

_CALENDARS: Dict[str, Any] = {}
_SYMBOLS_CFG_CACHE: Dict[str, dict] = {}


def get_calendar(name: str = "NYSE"):
    if name not in _CALENDARS:
        _CALENDARS[name] = mcal.get_calendar(name)
    return _CALENDARS[name]


def get_nyse_calendar():
    return get_calendar("NYSE")


def _symbols_cfg(symbols_config_path: Optional[Path] = None) -> dict:
    """读 config/symbols.yaml (按路径缓存); 读不到 → {}。"""
    if symbols_config_path is None:
        symbols_config_path = Path(__file__).resolve().parents[1] / "config/symbols.yaml"
    key = str(symbols_config_path)
    if key not in _SYMBOLS_CFG_CACHE:
        cfg = {}
        try:
            if Path(symbols_config_path).exists():
                with open(symbols_config_path, encoding='utf-8') as f:
                    cfg = yaml.safe_load(f) or {}
        except Exception as e:
            log.warning(f"symbols.yaml 读取失败 ({symbols_config_path}): {e}")
        _SYMBOLS_CFG_CACHE[key] = cfg
    return _SYMBOLS_CFG_CACHE[key]


def _contract(symbol: str, symbols_config_path: Optional[Path] = None) -> dict:
    cfg = _symbols_cfg(symbols_config_path)
    return ((cfg.get("symbol_contracts") or {}).get(str(symbol).upper())) or {}


def resolve_exchange_clock(symbol: str, symbols_config_path: Optional[Path] = None) -> Dict[str, Any]:
    """标的 → 交易所时钟 {exchange, calendar, tz, premarket_start_hour, afterhours_end_hour, has_extended_hours}。
    先按 primary_exchange, 认不出按 region 退区域默认交易所, 再退美股(不炸)。"""
    info = _contract(symbol, symbols_config_path)
    ex = str(info.get("primary_exchange") or "").upper()
    clock = EXCHANGE_CLOCKS.get(ex)
    if clock is None:
        region = str(info.get("region") or "US").upper()
        ex = _REGION_DEFAULT_EXCHANGE.get(region, "NYSE")
        clock = EXCHANGE_CLOCKS[ex]
    return {
        "exchange": ex,
        "calendar": clock["calendar"],
        "tz": clock["tz"],
        "premarket_start_hour": clock.get("premarket_start_hour"),
        "afterhours_end_hour": clock.get("afterhours_end_hour"),
        "has_extended_hours": bool(clock.get("has_extended_hours", False)),
    }


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


def session_break(calendar, schedule: pd.DataFrame, i: int = 0):
    """交易日历第 i 行那一天的午休 (起, 止), UTC; 没有午休 → None。
    日历库对早已取消的午休仍会给出时间(韩国 2000 年取消), 并在 discontinued_market_times 里记了取消日:
    取消日之后的不算。"""
    if "break_start" not in schedule.columns or "break_end" not in schedule.columns:
        return None
    row = schedule.iloc[i]
    if pd.isna(row["break_start"]) or pd.isna(row["break_end"]):
        return None
    gone = (getattr(calendar, "discontinued_market_times", None) or {}).get("break_start")
    if gone is not None and pd.Timestamp(schedule.index[i]).tz_localize(None) >= pd.Timestamp(gone).tz_localize(None):
        return None
    return _as_utc(row["break_start"]), _as_utc(row["break_end"])


def classify_session(bar_ts_utc: pd.Timestamp, clock: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Classify the market session for a UTC bar timestamp on the given exchange clock
    (default: US). Regular-session bounds come from the exchange calendar (DST + early-close aware).
      pre-market:  premarket_start -> reg_open   (only exchanges with extended hours)
      after-hours: reg_close -> afterhours_end   (only exchanges with extended hours)
      closed:      everything else (non-US exchanges: anything outside the regular session)
    """
    clock = clock or dict(_US_CLOCK, exchange="NYSE")
    bar_ts_utc = _as_utc(bar_ts_utc)
    local_ts = bar_ts_utc.tz_convert(clock["tz"])
    date_local = local_ts.date()

    schedule = get_calendar(clock["calendar"]).schedule(start_date=date_local, end_date=date_local)

    base = {
        "latest_bar_local": local_ts.isoformat(),
        "latest_bar_utc": bar_ts_utc,
        "exchange_tz": clock["tz"],
        "clock": clock,
    }
    if schedule.empty:
        return {**base, "current_session": "closed", "elapsed_minutes": None,
                "reg_open_utc": None, "reg_close_utc": None}

    reg_open_utc = _as_utc(schedule.iloc[0]['market_open'])
    reg_close_utc = _as_utc(schedule.iloc[0]['market_close'])

    if clock.get("has_extended_hours"):
        premarket_start_utc = pd.Timestamp(
            datetime(date_local.year, date_local.month, date_local.day, int(clock["premarket_start_hour"]))
        ).tz_localize(clock["tz"]).tz_convert('UTC')
        afterhours_end_utc = pd.Timestamp(
            datetime(date_local.year, date_local.month, date_local.day, int(clock["afterhours_end_hour"]))
        ).tz_localize(clock["tz"]).tz_convert('UTC')
    else:
        premarket_start_utc, afterhours_end_utc = reg_open_utc, reg_close_utc

    if bar_ts_utc < premarket_start_utc or bar_ts_utc >= afterhours_end_utc:
        session = "closed"
        elapsed = None
    elif bar_ts_utc < reg_open_utc:
        session = "pre-market"
        elapsed = int((bar_ts_utc - premarket_start_utc).total_seconds() / 60)
    elif bar_ts_utc < reg_close_utc:
        session = "regular"
        elapsed = int((bar_ts_utc - reg_open_utc).total_seconds() / 60)
        # 有午休的市场(东京等): 午休里不算正股时段
        brk = session_break(get_calendar(clock["calendar"]), schedule, 0)
        if brk and brk[0] <= bar_ts_utc < brk[1]:
            session, elapsed = "closed", None
    else:
        session = "after-hours"
        elapsed = int((bar_ts_utc - reg_close_utc).total_seconds() / 60)

    return {**base, "current_session": session, "elapsed_minutes": elapsed,
            "reg_open_utc": reg_open_utc, "reg_close_utc": reg_close_utc}


def get_prior_regular_close(bar_ts_utc: pd.Timestamp, clock: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Close timestamp (exchange-local ISO string) of the most recent completed regular session."""
    clock = clock or dict(_US_CLOCK, exchange="NYSE")
    bar_ts_utc = _as_utc(bar_ts_utc)
    end_date = bar_ts_utc.tz_convert(clock["tz"]).date()
    start_date = end_date - pd.Timedelta(days=14)
    schedule = get_calendar(clock["calendar"]).schedule(start_date=start_date, end_date=end_date)
    if schedule.empty:
        return None
    completed = schedule[schedule['market_close'] < bar_ts_utc]
    if completed.empty:
        return None
    return _as_utc(completed.iloc[-1]['market_close']).tz_convert(clock["tz"]).isoformat()


def last_completed_daily_idx(daily_index, latest_ts_utc, clock: Optional[Dict[str, Any]] = None,
                             lookback: int = 5) -> Optional[int]:
    """序列指标(RSI/MACD/ATR/波动档)该用"最后一根已完成的日线": 其交易日在该交易所已收盘
    (market_close ≤ 最新数据时刻)。返回负偏移(-1 = 末根, -2 = 倒数第二根); 全部未完成 → None。
    旧版按"盘中取末根、否则取倒数第二根"猜, 收盘后跑的场会晚一天(flow-cc §4.1 Q2)。"""
    n = len(daily_index)
    if n == 0 or latest_ts_utc is None:
        return None
    clock = clock or dict(_US_CLOCK, exchange="NYSE")
    latest = _as_utc(latest_ts_utc)
    cal = get_calendar(clock["calendar"])
    latest_local_date = latest.tz_convert(clock["tz"]).date()
    for k in range(1, min(n, lookback) + 1):
        bar_date = pd.Timestamp(daily_index[-k]).date()
        sched = cal.schedule(start_date=bar_date, end_date=bar_date)
        if sched.empty:
            # 日历认为不是交易日(节假日口径不一致等): 只要不是"今天", 当作已完成
            if bar_date < latest_local_date:
                return -k
            continue
        if _as_utc(sched.iloc[0]['market_close']) <= latest:
            return -k
    return None


def resolve_benchmark_symbol(symbol: str, symbols_config_path: Optional[Path] = None) -> Optional[str]:
    """标的的基准(symbols.yaml symbol_contracts.<SYM>.benchmark); 基准本身/没配 → None。
    旧版 beta/相对强弱/相关性写死对 SPY(flow-cc §4.1 Q4)。"""
    b = _contract(symbol, symbols_config_path).get("benchmark")
    return str(b).upper() if b else None


def benchmark_is_fresh(df_bench: pd.DataFrame, df_symbol: pd.DataFrame, max_lag_days: int = 5) -> bool:
    """基准日线缓存必须跟标的日线一样新(末根相差 ≤ max_lag_days 个自然日), 否则相对强弱是拿
    旧行情比新行情, 宁可不算。"""
    try:
        lag = (_as_utc(df_symbol.index[-1]) - _as_utc(df_bench.index[-1])).days
    except Exception:
        return False
    return lag <= max_lag_days


def load_instrument_metadata(symbol: str, symbols_config_path: Optional[Path] = None,
                             clock: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Resolve instrument type from symbols.yaml analysis_targets.
      stocks -> "stock" | etfs -> "etf" | benchmarks -> "benchmark" | else -> "unknown"
    has_extended_hours 按交易所时钟(美股才有盘前盘后)。
    """
    if symbols_config_path is None:
        symbols_config_path = Path(__file__).resolve().parents[1] / "config/symbols.yaml"

    inst_type = "unknown"
    try:
        if symbols_config_path.exists():
            with open(symbols_config_path, encoding='utf-8') as f:
                cfg = yaml.safe_load(f)
            targets = cfg.get('analysis_targets', {}) or {}
            sym_upper = symbol.upper()
            # 合约级覆盖(2026-09-30, DAX 案): context_type 优先于列表成员资格
            _ct = ((cfg.get('symbol_contracts') or {}).get(sym_upper) or {}).get('context_type')
            if _ct:
                inst_type = str(_ct)
            elif sym_upper in [s.upper() for s in targets.get('stocks', []) or []]:
                inst_type = "stock"
            elif sym_upper in [s.upper() for s in targets.get('etfs', []) or []]:
                inst_type = "etf"
            elif sym_upper in [s.upper() for s in targets.get('benchmarks', []) or []]:
                inst_type = "benchmark"
    except Exception as e:
        log.warning(f"load_instrument_metadata failed for {symbol}: {e}")

    # Reserved "index" type (e.g. if SPX/NDX are re-added) should set has_volume False.
    if clock is None:
        clock = resolve_exchange_clock(symbol, symbols_config_path)
    return {
        "instrument_type": inst_type,
        "has_volume": True,
        "has_extended_hours": bool(clock.get("has_extended_hours", False)),
    }
