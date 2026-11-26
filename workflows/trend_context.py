#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trend_context.py — 趋势结构机器数 + 触发信号表 (R16.3; 2026-09-30 改名 beacons → trigger_signals)。

为什么存在: 复盘坐实"阴涨失明"——§13.1 后视性铁律 × §13.2 幅度先于符号 × 突破确认唯一
通道, 三条合围把慢速单边行情定义成"零证据事件"。而 trend_fit 作为可选模式长期零调用
——可选机制≈没写。

修法(保真度定律配方): ①**入料化**——备餐对每个个股/ETF 强制算本模块, 结构事实(斜率/R²/
期间回撤/关键位站稳天数)直接进辩论开局材料, 不再依赖谁"想起来去查"; ②**触发信号**——机器判
"何时", 数据命中模式时把相关铁律的一句话热加载到现场(数据触发的条款点名), 模型只判"何意"。

用法(V16, 2026-10-25): 结构数字全部由技术快照算好(analysis/snapshot_v16: trend_signal_inputs / positioning 的双梯);
本模块只剩"何时点名"——workflows/data_manifest.build_manifest 调 trigger_signals_from_snapshot(payload) 生成
trigger_signals[], 由编排(run_step open)原样贴进每轮 spawn 材料的 TRIGGER SIGNALS 段。不再读 bar, 不再落
_trend_context_*.json(GENERAL 与镜头标的因此也有同一组数字)。阈值不变, 版本化在此, 哨兵盯效果。

触发信号阈值(v1, 版本化在此, 哨兵盯效果):
  B1 结构事实: |20日累计| ≥ 5% 且 R² ≥ 0.55 且 期间逆向回撤 ≤ 涨跌幅一半 → 点名 §13.1 边界
  B2 关键位站稳: 连续 ≥2 日收于前 20 日高点上方(或下方, 对称) → 点名站稳/失守事实
  B3 延伸末端: 20日累计 ≥ ±8% → 确认信号先过 §2 定价状态
  B4 动能衰竭(对称双向): 前10日 |≥4%| 有力、后10日 |≤1%| 熄火、且贴近 60 日高/低位
     → 上行衰竭 = 合法看空结构证据(高位滞涨, §13.1 豁免); 下行衰竭 = P08 质量赔率曲线
     "够了"档的入场证据(跌不动了)
"""
from __future__ import annotations

import math


def _sym_prefix(payload: dict) -> str:
    for k, v in payload.items():
        if k.endswith("_symbol") and isinstance(v, str) and k[:-len("_symbol")] == v:
            return v
    k = next((k for k in payload if k.endswith("_feature_version")), None)
    return k[:-len("_feature_version")] if k else ""


def trigger_signals_from_snapshot(payload: dict) -> list:
    """B1-B4 + 双梯信号, 读 V16 快照的 <SYM>_trend_signal_inputs_* 与 <SYM>_positioning_*_ladder_90d_*。
    文案与阈值与 R16.3/R21 时代逐字相同; 输入全部基于完成日收盘(快照保证)。旧快照(无这些键)→ []。"""
    sym = _sym_prefix(payload)
    if not sym:
        return []
    g = lambda k: payload.get(f"{sym}_trend_signal_inputs_{k}")
    ret20, r2, adv = g("ret_20d_close_pct"), g("fit_20d_r2"), g("adverse_20d_pct")
    if not isinstance(ret20, (int, float)):
        return []
    hold_above = g("hold_days_above_prior20d_high") or 0
    hold_below = g("hold_days_below_prior20d_low") or 0
    first10, last10 = g("ret_first10d_pct"), g("ret_last10d_pct")
    d_hi, d_lo = g("dist_60d_close_high_pct"), g("dist_60d_close_low_pct")
    lower_highs, higher_lows = bool(g("lower_highs_5v5")), bool(g("higher_lows_5v5"))
    bcn = []
    if abs(ret20) >= 5 and isinstance(r2, (int, float)) and r2 >= 0.55 and isinstance(adv, (int, float)) and adv <= abs(ret20) * 0.5:
        kind = "quiet grind up" if ret20 > 0 else "quiet grind down"
        bcn.append(f"⚑ Structural fact: 20-day {ret20:+.1f}% · R²={r2} · adverse move within the window only {adv:.1f}% = a {kind} structure "
                   f"— this is a machine-verifiable description of the current structure and counts as legitimate pricing input (it can support the cycle-phase call and claims that the structure is intact or deteriorating); "
                   f"what is banned is the naive extrapolation 'it moved, so it will keep moving'. A {kind} is a move with evidence. Named: trend-structure manual (P07).")
    if hold_above >= 2:
        bcn.append(f"⚑ Key level held: {hold_above} consecutive closes above the prior 20-day high (machine fact from bars, "
                   f"not single-day crossing noise) — a bearish structural judgment that predates this fact must be reconciled with it before being repeated. Named: trend-structure manual (P07), "
                   f"the five price-action questions (P05).")
    if hold_below >= 2:
        bcn.append(f"⚑ Key level lost: {hold_below} consecutive closes below the prior 20-day low (machine fact from bars)"
                   f" — a bullish structural judgment that predates this fact must be reconciled with it before being repeated. Named: trend-structure manual (P07), the five price-action questions (P05).")
    if abs(ret20) >= 8:
        side = "chasing longs" if ret20 > 0 else "chasing shorts"
        bcn.append(f"⚑ Extended end: 20-day already {ret20:+.1f}% — a breakout/breakdown confirmation appearing here belongs to the end of an extended move; "
                   f"before {side}, first answer 'how much is already priced in' (mid-move structural confirmation and end-of-move chasing differ completely in risk/reward). "
                   f"Named: trend-structure manual (P07), event pricing topic (P01, if an event falls in the window).")
    if isinstance(first10, (int, float)) and isinstance(last10, (int, float)):
        if first10 >= 4 and last10 <= 1 and isinstance(d_hi, (int, float)) and d_hi >= -3:
            extra = " · highs no longer making new highs" if lower_highs else ""
            bcn.append(f"⚑ Upside momentum exhaustion: first 10 days {first10:+.1f}% · last 10 days {last10:+.1f}% · {d_hi:+.1f}% from the 60-day "
                       f"closing high{extra} — stalling at the highs is legitimate bearish structural evidence (a structural description, same standing as a quiet grind up); the side claiming 'trend intact'"
                       f" reconciles with this first; a light trim is the symmetric exit. Named: trend-structure manual (P07).")
        if first10 <= -4 and last10 >= -1 and isinstance(d_lo, (int, float)) and d_lo <= 5:
            extra = " · lows no longer making new lows" if higher_lows else ""
            bcn.append(f"⚑ Downside momentum exhaustion: first 10 days {first10:+.1f}% · last 10 days {last10:+.1f}% · {d_lo:+.1f}% from the 60-day "
                       f"closing low{extra} — the decline has stopped making progress; if size/quality tiering rules this a quality name's false alarm, this is the entry evidence of odds turning favorable"
                       f" (no full upside confirmation required). Named: trend-structure manual (P07), size/quality tiering (P08).")
    depths = (">=2%", ">=3%", ">=5%", ">=8%")
    ladder = {d: payload.get(f"{sym}_positioning_pullback_ladder_90d_{d}") for d in depths}
    bounce = {d: payload.get(f"{sym}_positioning_bounce_ladder_90d_{d}") for d in depths}
    if all(isinstance(v, int) for v in ladder.values()):
        n = g("ladder_bars") or 90
        lad_txt = " · ".join(f"{k}×{v}" for k, v in ladder.items())
        bnc_txt = " · ".join(f"{k}×{v}" for k, v in bounce.items())
        bcn.append(f"⚑ Pullback ladder (lookback {n} days): {lad_txt} times; bounce ladder: {bnc_txt} times — "
                   f"how many rounds each depth has occurred in this history (only a count, not this order's fill probability): read the pullback ladder for a buy-the-dip level, and the bounce ladder for how high"
                   f" to place a trim; a level this history never reached needs a separate reason why it would be reached this time. "
                   f"Named: execution lines and entry levels manual (P06) — before giving a level.")
    return bcn
