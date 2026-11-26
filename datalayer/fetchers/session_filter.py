#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数之后、落盘之前的时段过滤(不依赖盈透, 可以单测)。

为什么要有(2026-10-02 连盈透核对, 任天堂 7974 与丰田 7203):
  账户没有东证的行情权限, 东证个股只能走通用路由取数; 通用路由会把场外交易系统(Japannext)的日间与夜间时段
  一起带回来, 而盈透的"只取常规时段"开关对这类合约的历史 bar 不起作用(开与关取回的日线、小时线、5 分钟线
  完全一样)。后果有两个:
    1. 盘中文件里混着大量场外时段的 bar(多数成交为零), 技术快照的盘中指标把它们一起算了。
    2. 夜间的场外成交被记到下一个交易日: 在东京夜间取数, 日线末尾会多出一根未来日期的假日线。
  这里按交易所日历把它们去掉。只对核实过的交易所做, 别的市场原样返回。

残留: 当天日线的开盘价与高低点仍可能含少量场外成交; 没有东证的独立数据, 分不开。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pandas as pd

# 已核实会混入场外时段的交易所(按 symbols.yaml 的 primary_exchange 解析出的交易所时钟)
OFF_EXCHANGE_FILTER = {"TSEJ"}


def _bar_minutes(bar_size: str) -> Optional[int]:
    """盈透的 bar 尺寸写法 → 分钟数; 日线及以上 → None。"""
    s = str(bar_size or "").strip().lower()
    try:
        n = int(s.split()[0])
    except (ValueError, IndexError):
        return None
    if "min" in s:
        return n
    if "hour" in s:
        return n * 60
    return None


def drop_off_exchange_bars(df: pd.DataFrame, symbol: str, bar_size: str, now: Optional[datetime] = None,
                           symbols_config_path=None) -> Tuple[pd.DataFrame, dict]:
    """返回 (过滤后的表, 说明)。不在核实名单里的交易所、空表、索引不是带时区的时间 → 原样返回, 说明为空。
    盘中: 只留与正股时段有重叠的 bar(午休整根落在里面的不要; 跨午休或跨收盘的小时线保留)。
    日线: 丢掉所属交易日还没有开盘的那些。"""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        return df, {}
    from analysis.technical_snapshot_builder import get_calendar, resolve_exchange_clock, session_break
    clock = resolve_exchange_clock(symbol, symbols_config_path)
    if clock["exchange"] not in OFF_EXCHANGE_FILTER:
        return df, {}
    now = now or datetime.now(timezone.utc)
    now = pd.Timestamp(now).tz_convert("UTC") if pd.Timestamp(now).tzinfo else pd.Timestamp(now, tz="UTC")
    cal = get_calendar(clock["calendar"])
    minutes = _bar_minutes(bar_size)
    idx = df.index.tz_convert("UTC")

    if minutes is None:  # 日线: 索引是交易日(零点)
        days = [ts.date() for ts in idx]
        sched = cal.schedule(start_date=min(days), end_date=max(days))
        opens = {d.date(): pd.Timestamp(row["market_open"]).tz_convert("UTC") for d, row in sched.iterrows()}
        keep = [not (d in opens and opens[d] > now) for d in days]
        out = df[keep]
        return out, {"exchange": clock["exchange"], "kind": "daily", "dropped": len(df) - len(out)}

    local = idx.tz_convert(clock["tz"])
    sched = cal.schedule(start_date=local.min().date(), end_date=local.max().date())
    segs = {}
    for i, (d, row) in enumerate(sched.iterrows()):
        o = pd.Timestamp(row["market_open"]).tz_convert("UTC")
        c = pd.Timestamp(row["market_close"]).tz_convert("UTC")
        brk = session_break(cal, sched, i)
        segs[d.date()] = [(o, brk[0]), (brk[1], c)] if brk else [(o, c)]
    step = timedelta(minutes=minutes)
    keep = []
    for ts, loc in zip(idx, local):
        # 小时线里不在整点开始的那根(时段开头)到下一个整点就结束, 不满一小时
        end = ts.floor("h") + step if minutes >= 60 and (ts.minute or ts.second) else ts + step
        keep.append(any(ts < e and end > s for s, e in segs.get(loc.date(), [])))
    out = df[keep]
    return out, {"exchange": clock["exchange"], "kind": "intraday", "dropped": len(df) - len(out)}
