# -*- coding: utf-8 -*-
"""
postcheck_quant.py — BP 后验的"窗口量化摘要"(确定性算好, 喂给 Reckoner 当事实底座)。

给一份报告(标的 + 时刻 T + dt) → 取 [T, T+dt 交易日] 的日线 → 算:
    price_T(入场参考价) / close_end / ret_pct / 窗口 high·low /
    MFE(朝报告方向最大有利幅度) / MAE(最大不利幅度) /
    touched_target(有没有摸到报告目标价) / touched_stop(有没有打穿止损) /
    path(若干采样点, 给 AI 看形状) / swing(长窗才有, 复用 trend_analyzer)
档位: 3d / 20d(2026-09-30 起 50d 退役, 决策契约 §4.7)。dt ≤ 3 撞线排班用 5m 分时, 其余日线。

日线源: 复用现成 data/market_data/{SYM}/{SYM}_*_1d_*.csv (5y 日线, 抓行情时刷新)。不依赖 PG 日线表。
核心数学 window_stats() / short_term_settlement() 吃干净 bars, 可脱离文件/PG 单测。

尺子版本 RULER_VERSION 随 digest 入库: 旧批用新尺重评时另存一行, 不覆盖旧分(FINAL §11.2)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKET_DATA_ROOT = PROJECT_ROOT / "data" / "market_data"   # 测试可指向临时目录

# 尺子版本(评估记录版本化): 改清算口径就升号; 同 (symbol, as_of, dt) 不同 ruler 并存。
RULER_VERSION = "R25"   # R25(2026-10-29, v3.4): + forecast scores on the day-15 close for reports that carry a scenario (CRPS / Brier / interval / skill vs ATR baseline); the d+3 / d+20 operation and overall rulers are unchanged from R24
_R24_NOTE = "R24"   # R24(2026-10-17, D+20 口径三件定案): 老报告起点价退到技术快照最新价; 条件单按撞线数学 + 期限清算;
                        # 事件分段 / 波动门槛 / 判断线接受状态 / 方向无关路径字段 / 可达相邻操作候选; 触线方向按线的角色; 完整日线表
                        # R23(2026-10-10): 盘中报告以报告时点为起点并计入当天余下路径; 短期裁定用同一份精细撞线时序; 日韩代号可解析
# ---- R24 候选阈值(全部待留出样本校准后冻结; 改这里一处) ----
EPS_MIN_PCT = 1.0              # 显著门槛的最小幅(%)
EPS_SIGMA_MULT = 0.5           # eps(H) = max(EPS_MIN_PCT, EPS_SIGMA_MULT * sigma_T * sqrt(H)); sigma_T 冻结于 T
SIGMA_LOOKBACK = 20            # sigma_T: T 前 20 个交易日的日对数收益标准差
ATR_LOOKBACK = 14
ACCEPT_CONSEC_CLOSES = 2       # 判断线"实质接受反向": 连续 2 个收盘越线
ACCEPT_DEPTH_ATR = 0.5         # 且最大越幅 >= 0.5 ATR14
ORDER_VALID_DAYS_DEFAULT = 3   # 限价单未写有效期: 自基准日下一交易日起 3 个交易日(含末日)

                        # 10-11 边界(Codex 复现): 当天余下路径不算一个完整交易日(成熟度/到期/坐标三处同口径); 20d 先继承 3d 精细排班再裁定

PATH_BUDGET = 12  # 路径速写恒定点数预算 (不管窗口多长压到 ~12 个点)

_SELL_OPS = {"TRIM_POSITION", "LIQUIDATE_NOW"}
_BUY_OPS = {"ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY"}   # ENTRY = 2026-10-17 晚起仲裁者写的入场; 终报里由组装器派生成前两者


def _to_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _strip_thousands(text: str) -> str:
    """去掉数字间的千分位逗号("¥2,785" → "¥2785"; R24: 旧版把它解析成 2.0)。"""
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def _floats(v) -> List[float]:
    if v is None:
        return []
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", _strip_thousands(str(v)))]


def _first_float(v) -> Optional[float]:
    """从值里抠第一个数 (容忍 'N/A' / '660' / 'SPY=745.0; QQQ=...' 取首个; 千分位逗号先去掉)。无 → None。"""
    xs = _floats(v)
    return xs[0] if xs else None


def _stop_price(report: Dict, entry: Optional[float] = None, ref: Optional[float] = None) -> Optional[float]:
    """止损价(R24): stop_loss 文本里第一个**像价格**的数——与参考价(挂单价或操作价位)同量级(0.5 到 1.5 倍), 且不等于挂单价;
    "2 日未收回 95" 读 95 不读 2; MP 的 "若44.50守稳低吸成交, 收盘低于39.25即停止" 读 39.25。没有参考价时退回第一个数。
    等于挂单价的数只在没有别的候选时才接受。"""
    ex = report.get('operation_execution') or {}
    if isinstance(ex, dict) and 'stop' in ex:
        s = ex['stop']
        return float(s['level']) if isinstance(s, dict) and isinstance(s.get('level'), (int, float)) and s['level'] > 0 else None
    txt = str((report.get("risk_management") or {}).get("stop_loss") or "")
    from workflows.postcheck_execution import NEGATIVE, REVIEW
    clauses = re.split(r'[。；;]', txt)
    txt = ' ; '.join(c for c in clauses if not re.search(NEGATIVE, c, re.I) and not (
        re.search(REVIEW, c, re.I) and not re.search(r'退出|撤去|卖出|减仓|\bexit\b|\bsell\b', c, re.I)))
    txt = re.sub(r"\d+(?:\.\d+)?\s*(?:个?交易日|日|天|根|个|倍|%|％|小时|分钟"
                 r"|(?:(?:trading\s+)?days?|sessions?|bars?|candles?|hours?|minutes?|times|x)\b)", " ", _strip_thousands(txt),
                 flags=re.IGNORECASE)   # "2 日未收回" / "2 days without reclaim" 的 2 不是价
    xs = _floats(txt)
    if not xs:
        return None
    anchor = entry if entry is not None else ref
    pool = [x for x in xs if 0.4 * anchor <= x <= 2.5 * anchor] if anchor else xs
    if anchor and not pool:
        pool = xs
    for x in pool:
        if entry is None or abs(x - entry) > 1e-9:
            return x
    return pool[0]


_STOP_CLOSE_RE = re.compile(
    r"收盘|收于|收低|收在|(\d+)\s*(?:个?交易日|日|天|根)\s*(?:未|没)\s*(?:收回|收复|回到)"
    # English (reports written from 2026-10-27 on): "close below / closes above / closing basis / close confirmation / daily close",
    # "N consecutive closes", "N days without reclaim", "not reclaimed within N days"
    r"|\bclos(?:e|es|ed|ing)\s+(?:below|above|under|over|beneath|through|outside|back|basis|confirmation)\b"
    r"|\b(?:daily|on\s+a|on\s+the|at\s+the)\s+close\b|\bclos(?:e|ing)-basis\b|\bclosing\s+basis\b"
    r"|\b(\d+)\s+consecutive\s+(?:daily\s+)?closes?\b"
    r"|\b(\d+)\s*(?:consecutive\s+)?(?:trading\s+)?(?:days?|sessions?|closes?|bars?)\s+(?:without|with\s+no|no|not)\s+(?:a\s+)?(?:reclaim|recover|regain|clos(?:e|ing)\s+back)"
    r"|\b(?:fail(?:s|ed|ing)?\s+to|not|without)\s+(?:reclaim|recover|regain)\w*[^.;]{0,20}?\b(?:within|in|after|for)\s+(\d+)\s*(?:trading\s+)?(?:days?|sessions?)\b",
    re.IGNORECASE)


def _stop_rule(report: Dict) -> Tuple[str, int]:
    """止损触发语义(R24): 文本提到收盘确认 → ("close", n); "N 日未收回" → ("close", N); 否则 ("touch", 1)。"""
    txt = str((report.get("risk_management") or {}).get("stop_loss") or "")
    from workflows.postcheck_execution import consecutive
    n = consecutive(txt)
    if n > 1:
        return 'close', n
    m = _STOP_CLOSE_RE.search(txt)
    if not m:
        return "touch", 1
    g = next((x for x in m.groups() if x), None)
    n = int(g) if g else 1
    return "close", max(1, min(n, 10))


_VALID_DAYS_RE = re.compile(
    r"(?:最多等|最长等|最多等待|等待不超过)\s*(\d{1,2})\s*个?\s*交易日"
    r"|(?:挂单|限价单?|订单|条件单)[^。;,，]{0,12}?(?:有效期?|有效)\s*(?:为|是|:|：)?\s*(\d{1,2})\s*个?\s*交易日"
    r"|(?:有效期?|有效)\s*(?:为|是|:|：)?\s*(\d{1,2})\s*个?\s*交易日[^。;,，]{0,12}?(?:撤单|作废|不追|取消)"
    r"|(\d{1,2})\s*个?\s*交易日内?(?:未|没)(?:成交|触及|触价|到价)[^。;,，]{0,8}?(?:撤|作废|取消|不追)"
    # English (reports written from 2026-10-27 on), same four shapes: "wait at most N trading days" /
    # "order valid for N trading days" / "valid for N trading days, then cancel" / "N trading days without a fill → cancel"
    r"|\b(?:wait(?:ing)?|hold)\s+(?:at\s+most|up\s+to|no\s+(?:more|longer)\s+than|a\s+maximum\s+of|max(?:imum)?)\s+(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\b"
    r"|\b(?:order|bid|offer|entry)[^.;,]{0,20}?\b(?:valid|validity|good|live|open|active|stands?|lives?)\b[^.;,]{0,8}?(?:for|of|:|over|through)?\s*(?:the\s+next\s+)?(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\b"
    r"|\b(?:valid|validity|good)\s*(?:for|of|:)?\s*(?:the\s+next\s+)?(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\b[^.;]{0,20}?\b(?:cancel|withdraw|pull|expire|lapse|void|drop|walk)"
    r"|\b(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\s+(?:without|with\s+no|no)\s+(?:a\s+|an\s+)?(?:fill|touch|trigger|execution)[^.;]{0,12}?\b(?:cancel|withdraw|pull|expire|lapse|void|drop|walk)"
    r"|\b(?:not|un)\s*(?:filled|touched|triggered|hit|reached)\s+(?:within|in|after)\s+(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\b[^.;]{0,12}?\b(?:cancel|withdraw|pull|expire|lapse|void|drop|walk)"
    r"|\b(?:cancel|withdraw|pull|expire|lapse|void|drop|walk)\w*[^.;]{0,12}?\b(?:not|un)\s*(?:filled|touched|triggered|hit|reached)\s+(?:within|in|after)\s+(\d{1,2})\s*(?:trading\s+)?(?:days?|sessions?)\b",
    re.IGNORECASE)


_VALID_UNTIL_RE = re.compile(r"(?:挂单|限价单?|订单|条件单)[^。;]{0,20}?(?:有效|到期|撤单|作废|取消)[^。;]{0,12}?(\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日)"
                             r"|(?:有效至|有效到|挂到|等到|最晚)\s*(\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日)"
                             # English (ISO dates only): "order valid through 2026-10-30" / "limit order expires 2026-10-30" /
                             # "valid until 2026-10-30" / "good till 2026-10-30" / "GTD 2026-10-30" / "no later than 2026-10-30"
                             r"|\b(?:order|bid|offer|entry)[^.;]{0,20}?\b(?:valid|good|expir\w*|cancel\w*|withdraw\w*|lapse\w*|stands?|open|live)\b[^.;]{0,12}?(\d{4}-\d{2}-\d{2})"
                             r"|\b(?:valid\s+(?:until|through|till|to|thru)|good\s+(?:till|until|through|thru)|GTD|no\s+later\s+than|at\s+the\s+latest(?:\s+by)?)\s*:?\s*(\d{4}-\d{2}-\d{2})",
                             re.IGNORECASE)


def _order_valid_until(report: Dict, as_of: Optional[datetime] = None) -> Optional[str]:
    """订单明写了到期日期("挂单有效至 2026-09-21" / "最晚 9月21日 撤单")→ ISO 日期; 月日没年份时用报告年份。没有 → None。"""
    from workflows.postcheck_execution import dated_expiry
    explicit = dated_expiry(report, as_of)
    if explicit:
        return explicit
    nr = report.get("next_review") or {}
    rm = report.get("risk_management") or {}
    for t in (report.get("mentality_basis"), report.get("statement"), nr.get("notes"),
              " ".join(str(x) for x in (rm.get("review_triggers") or []))):
        m = _VALID_UNTIL_RE.search(str(t or ""))
        if not m:
            continue
        raw = next(g for g in m.groups() if g)
        if "-" in raw:
            return raw
        mm, dd = re.findall(r"\d+", raw)
        yr = as_of.year if as_of else datetime.now(timezone.utc).year
        return f"{yr:04d}-{int(mm):02d}-{int(dd):02d}"
    return None


def _order_valid_days(report: Dict) -> Tuple[int, str]:
    """限价单有效期(R24, 2026-10-17 定案): 报告明写了按报告("最多等 5 个交易日"); 没写默认 ORDER_VALID_DAYS_DEFAULT。
    不看 short_term_path.horizon_days(订单期限与路径期限解绑)。返回 (天数, 来源)。"""
    nr = report.get("next_review") or {}
    rm = report.get("risk_management") or {}
    texts = [report.get("mentality_basis"), report.get("statement"), nr.get("notes"),
             " ".join(str(x) for x in (rm.get("review_triggers") or []))]
    for t in texts:
        if not t:
            continue
        m = _VALID_DAYS_RE.search(str(t))
        if m:
            n = int(next(g for g in m.groups() if g))
            if 1 <= n <= 30:
                return n, "report"
    return ORDER_VALID_DAYS_DEFAULT, "default"


def _rf(report: Dict, *names):
    """报告字段读取: 新名优先, 旧名兜底(2026-09-30 改名: composite_score/short_term_path/operation_size)。"""
    for n in names:
        if isinstance(report, dict) and report.get(n) is not None:
            return report.get(n)
    return None


def _short_term(report: Dict) -> Dict:
    st = _rf(report, "short_term_path", "short_term")
    return st if isinstance(st, dict) else {}


def _direction(report: Dict) -> int:
    """报告方向(MFE/MAE 的"有利"侧): +1 看多 / -1 看空 / 0 中性。
    后验主要由操作决定(决策契约 §4): 买入族 → +1, 卖出族 → -1; HOLD 时看 short_term 的路径方向;
    都没有才用 action 的粗方向——action 是综合基调, 不再按 55/45 线单独判方向对错。"""
    op = str(report.get("operation_type") or "").upper()
    if op in _BUY_OPS:
        return 1
    if op in _SELL_OPS:
        return -1
    st = _short_term(report)
    d = str(st.get("direction") or "")
    if "上" in d:
        return 1
    if "下" in d:
        return -1
    act = _first_float(_rf(report, "composite_score", "action"))
    if act is not None:
        if act >= 55:
            return 1
        if act <= 45:
            return -1
    return 0


# GENERAL* 不是单标的: 分析的是多个指数 ETF, 后验要每个成员各算一套
_GENERAL_MEMBERS = {
    "GENERAL": ["SPY", "QQQ", "DIA"],
    "GENERAL_EU": ["DAX", "IAEX"],
    # 2026-11-02 起 JP / KR 各加第二镜头(TOPIX 指数 / KOSDAQ150 指数); 之前的单镜头报告仍按自己的 lenses 清算
    "GENERAL_JP": ["N225", "TOPX"],
    # 2026-09-28 起 KR 镜头 = KODEX200 ETF(K200 指数 HMDS 断供退役);
    # 旧 K200 镜头报告仍按 KODEX200 收益率打方向分, K200 点位目标缺键不做撞线判定
    "GENERAL_KR": ["KODEX200", "KOSDQ150"],
}


_TARGET_ENTRY_RE = re.compile(
    r"\s*([A-Za-z][A-Za-z0-9.]*)\s*=\s*(-?\d+(?:\.\d+)?)(?:\s*[-~]\s*(\d+(?:\.\d+)?))?")   # 代号可含数字: N225 / KODEX200


def parse_target_map(s) -> Dict[str, Union[float, Tuple[float, float]]]:
    """GENERAL 多价位串 → {成员: 价位 或 (下沿, 上沿)}。
    'SPY=745.0; QQQ=20660' → {'SPY': 745.0, 'QQQ': 20660.0};
    'SPY=95-105; QQQ=500' → {'SPY': (95.0, 105.0), 'QQQ': 500.0}。
    (旧版把 'SPY=95-105' 读成 95: GENERAL 震荡区间丢上沿, topics/INDEX Q9)"""
    out: Dict[str, Union[float, Tuple[float, float]]] = {}
    for part in re.split(r"[;,]", str(s or "")):
        m = _TARGET_ENTRY_RE.match(part)
        if not m:
            continue
        a = float(m.group(2))
        if m.group(3) is not None:
            b = float(m.group(3))
            if a > 0 and b > 0 and a != b:
                out[m.group(1).upper()] = (min(a, b), max(a, b))
        else:
            out[m.group(1).upper()] = a
    return out


def _parse_price_map(s) -> Dict[str, float]:
    """多价位串里的单价位项 (区间项不在此)。非多价位串 → {}。"""
    return {k: v for k, v in parse_target_map(s).items() if not isinstance(v, tuple)}


def _parse_band_map(s) -> Dict[str, Tuple[float, float]]:
    """多价位串里的区间项 (下沿, 上沿)。"""
    return {k: v for k, v in parse_target_map(s).items() if isinstance(v, tuple)}


def window_stats(bars: List[Dict], price_T: float, direction: int,
                 target: Optional[float], stop: Optional[float]) -> Dict:
    """核心数学(可单测)。bars: [{date, high, low, close}, ...] 已按时间升序、属于窗口内。
    price_T: 入场参考价。direction: +1/-1/0。target/stop: 数或 None。
    标 partial_day 的 bar(报告当天余下路径合成的那根)计入触价、极值与收益, 不计入 bars_in_window:
    它数的是 T 之后的完整交易日, 成熟度检查(postcheck_store.prep)与短期到期都按它。"""
    if not bars or price_T in (None, 0):
        return {}
    highs = [b["high"] for b in bars if b.get("high") is not None]
    lows = [b["low"] for b in bars if b.get("low") is not None]
    closes = [b["close"] for b in bars if b.get("close") is not None]
    if not highs or not lows or not closes:
        return {}
    win_high, win_low, close_end = max(highs), min(lows), closes[-1]
    ret_pct = (close_end - price_T) / price_T * 100.0

    # MFE/MAE 按方向: 看多→有利=涨, 看空→有利=跌。
    # 中性(direction=0)没有"有利方向", mfe/mae 退化为基准上方/下方最大偏移(=up/down 原值)。
    # 旧版 max(up,-down)/min(up,down) 会在下行大于上行时把暴跌读成"有利偏移", 且 mfe/mae
    # 恒成同数反号镜像(2026-09-26 复盘批 d+20 案: 高点仅+3.2% 却报 mfe=17.94)。
    up_pct = (win_high - price_T) / price_T * 100.0
    down_pct = (win_low - price_T) / price_T * 100.0   # 通常为负
    if direction > 0:
        mfe, mae = up_pct, down_pct
    elif direction < 0:
        mfe, mae = -down_pct, -up_pct
    else:
        mfe, mae = up_pct, down_pct

    # B0 修复(2026-07-17): touch 判定由 target/stop 相对 price_T 的位置决定,
    # 不依赖 action 推导的 direction —— 中性带 direction=0 时旧 OR 分支几乎恒真,
    # 是 v3 复盘 25% 错报率的真凶(且旧版 direction=0 时 touched_stop 恒 None)
    touched_target = None
    if target is not None:
        touched_target = (win_high >= target) if target >= price_T else (win_low <= target)
    touched_stop = None
    if stop is not None:
        touched_stop = (win_low <= stop) if stop <= price_T else (win_high >= stop)

    # 路径事实单列(2026-09-06, v7r2 起的尺子病建议): 只看 close_end 会让"穿越 -15% 回撤
    # 又 V 回来"的 HOLD 拿满分——窗口内最大回撤(峰到谷)与现价穿越次数(close 相对 price_T
    # 的换边计数)机械算好, 长窗口判 HOLD/观望时路径质量有据可查。
    run_max, mdd = None, 0.0
    for b in bars:
        h, l = b.get("high"), b.get("low")
        if h is not None:
            run_max = h if run_max is None else max(run_max, h)
        if run_max and l is not None:
            mdd = min(mdd, (l - run_max) / run_max * 100.0)
    crosses, prev_side = 0, 0
    for c in closes:
        side = 1 if c > price_T else (-1 if c < price_T else prev_side)
        if prev_side and side != prev_side:
            crosses += 1
        prev_side = side

    # R24 方向无关的路径字段: 上/下最大偏移(不随报告方向翻转)、收盘在起点上方的时间占比、效率比(净位移 ÷ 总路程)
    travel = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) + abs(closes[0] - price_T)
    eff = (abs(close_end - price_T) / travel) if travel > 0 else None
    return {
        "price_T": round(price_T, 4),
        "close_end": round(close_end, 4),
        "window_high": round(win_high, 4),
        "window_low": round(win_low, 4),
        "ret_pct": round(ret_pct, 2),
        "mfe_pct": round(mfe, 2),
        "mae_pct": round(mae, 2),
        "up_exc_pct": round(up_pct, 2),
        "down_exc_pct": round(down_pct, 2),
        "time_above_T_pct": round(sum(1 for c in closes if c > price_T) / len(closes) * 100, 1),
        "efficiency_ratio": round(eff, 3) if eff is not None else None,
        "max_drawdown_pct": round(mdd, 2),
        "cross_T_count": crosses,
        "touched_target": touched_target,
        "touched_stop": touched_stop,
        "bars_in_window": sum(1 for b in bars if not b.get("partial_day")),
    }


def entry_fill_stats(bars: List[Dict], price_T: float, entry: float,
                     valid_days: Optional[int] = None, stop: Optional[float] = None,
                     long_side: Optional[bool] = None, valid_until: Optional[str] = None,
                     stop_rule: Tuple[str, int] = ("touch", 1)) -> Dict:
    """限价挂单价**到没到** + 到价后的窗口结果 (Task 2.5, 通用看板不预设仓位 → 主操作质量优先)。
    判向: 挂单价低于现价(低吸) → 任一 bar.low <= entry 即到价; 高于现价(突破挂单) → bar.high >= entry。
    日线 bar 的 low/high 是当日真实极值, "摸没摸到"在日线粒度下即机械准确; 当日内先后顺序不可判。

    **触价 ≠ 成交, 更 ≠ 满足报告的全部条件**(2026-09-30 改名): 机器只知道价格到过挂单价;
    报告若写了"回踩后收复 / 缩量企稳再买"之类确认条件, 条件是否成立由评分员读原文判。
    字段: entry_price_reached / reached_date / post_reach_ret_pct
    (旧名 entry_filled / fill_date / post_fill_ret_pct 会让读者把到价当成交)。

    R21 (2026-09-22) 挂深三字段——"挂深"从此有正式机械定义, 评分员不再手工反推:
      entry_depth_pct         (锚价−挂单价)/锚价×100; 正=低吸挂深, 负=突破挂单在上方;
      entry_depth_range_ratio |挂深| ÷ 窗口日均振幅((high−low)/close 均值)——
                              "波幅撑不撑得起这个深度"的读数, 声明单现形器;
      entry_gap_pct           仅未到价: 窗口最近逼近的差口(以锚价计)——
                              复盘"真差点接到"的机械判据。"""
    depth = round((price_T - entry) / price_T * 100, 2)
    ranges = [(b["high"] - b["low"]) / b["close"] * 100 for b in bars if b.get("close")]
    avg_range = sum(ranges) / len(ranges) if ranges else None
    base = {
        "entry_price": entry,
        "entry_depth_pct": depth,
        "entry_depth_range_ratio": round(abs(depth) / avg_range, 2) if avg_range else None,
    }
    if not bars:
        return {**base, "entry_price_reached": False}
    # R24: 有效期内触价才算触价(当天余下那根部分 bar 不计天数); 过期后触价单记 expired_touch_date, 不算成交
    lead = 1 if bars[0].get("partial_day") else 0
    if valid_until:
        base["order_valid_until"] = str(valid_until)[:10]
        live = [b for b in bars if str(b["date"])[:10] <= str(valid_until)[:10]]
    elif valid_days:
        base["order_valid_days"] = int(valid_days)
        live = bars[: lead + int(valid_days)]
    else:
        live = bars
    if not live:
        late = next((b for b in bars if ((b["low"] <= entry) if entry <= price_T else (b["high"] >= entry))), None)
        out = {**base, "entry_price_reached": False, "order_expired_before_window": True}
        if late is not None:
            out["expired_touch_date"] = str(late["date"])[:10]
        return out

    def _hit(b):
        return (b["low"] <= entry) if entry <= price_T else (b["high"] >= entry)

    reached_idx = next((i for i, b in enumerate(live) if _hit(b)), None)
    if reached_idx is None:
        if entry <= price_T:
            gap = (min(b["low"] for b in live) - entry) / price_T * 100
        else:
            gap = (entry - max(b["high"] for b in live)) / price_T * 100
        out = {**base, "entry_price_reached": False, "entry_gap_pct": round(gap, 2)}
        late = next((b for b in bars[len(live):] if _hit(b)), None)
        if late is not None:
            out["expired_touch_date"] = str(late["date"])[:10]
        return out
    out = {
        **base,
        "entry_price_reached": True,
        "reached_date": str(bars[reached_idx]["date"])[:10],
        "post_reach_ret_pct": round((bars[-1]["close"] / entry - 1) * 100, 2),
    }
    # R24: 成交后按报告自写的止损价**触价**模拟退出(多头: 成交当根起任一根 low <= stop 即按 stop 退出; 卖侧镜像), 否则持到窗末。
    # 只模拟触价语义; 报告写的收盘确认 / N 日未收回等按日线表由评分员核。多空由调用方按操作类型给(LIMIT_ENTRY = 多).
    if stop is not None:
        if long_side is None:
            long_side = stop < entry
        out.update(_simulate_exit(bars, reached_idx, entry, stop, long_side, stop_rule))
    return out


def conditional_fill_stats(bars: List[Dict], price_T: float, observe: Any, fill: Any,
                           prev_close: Optional[float] = None, valid_days: Optional[int] = None,
                           valid_until: Optional[str] = None, stop: Optional[float] = None, long_side: bool = True,
                           stop_rule: Tuple[str, int] = ("touch", 1)) -> Dict:
    """G4 (wave 2): a conditional order (operation_execution.conditional {observe, fill}) is never a resting limit at its
    level. The observe rule (odds_model grammar: close / open / touch / gap_open, > >= < <=) is judged on daily bars
    inside the order window (the same window as entry_fill_stats): close / open compare that field; touch uses the high
    (for > / >=) or the low (< / <=); gap_open = the open beyond the level while the previous close was not. Fill:
    `next_open` = the next bar's open; `at_level` = the price of the event that met the condition (the level for a touch,
    the open when the bar opened beyond it, the close for a close rule). Condition never met → no fill:
    entry_price_reached False and `note` says why. Keys: condition_met, condition_met_date, fill_date, fill_price (+ the
    entry_fill_stats keys a reader expects: entry_price = the observe level, entry_price_reached, reached_date = fill_date,
    post_reach_ret_pct from the fill price)."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from odds_model import _parse_observe, _cmp
    fill = str(fill or "next_open")
    base: Dict[str, Any] = {"conditional": True, "observe": observe, "fill": fill,
                            "entry_price_reached": False, "condition_met": False}
    obs = _parse_observe(observe)
    if not obs:
        return {**base, "note": f"conditional observe {observe!r} is unparseable: not settled"}
    kind, op, level = obs
    base["entry_price"] = level
    if not bars:
        return {**base, "note": "no bars after the report"}
    lead = 1 if bars[0].get("partial_day") else 0
    if valid_until:
        base["order_valid_until"] = str(valid_until)[:10]
        live = [b for b in bars if str(b["date"])[:10] <= str(valid_until)[:10]]
    elif valid_days:
        base["order_valid_days"] = int(valid_days)
        live = bars[: lead + int(valid_days)]
    else:
        live = bars
    prev, met_idx, met_px = prev_close, None, None
    for i, b in enumerate(live):
        o = b.get("open")
        if kind in ("open", "gap_open") and b.get("partial_day"):   # the report day's remainder has no session open
            prev = b.get("close", prev)
            continue
        if kind == "close":
            hit, px = _cmp(float(b["close"]), op, level), float(b["close"])
        elif kind == "open":
            hit, px = o is not None and _cmp(float(o), op, level), o
        elif kind == "gap_open":
            hit = o is not None and _cmp(float(o), op, level) and (prev is None or not _cmp(float(prev), op, level))
            px = o
        else:   # touch
            x = b["high"] if op in (">", ">=") else b["low"]
            hit = _cmp(float(x), op, level)
            px = float(o) if o is not None and _cmp(float(o), op, level) else level
        prev = b.get("close", prev)
        if hit:
            met_idx, met_px = i, px
            break
    if met_idx is None:
        return {**base, "note": f"condition {observe} not met in the order window: no fill"}
    out = {**base, "condition_met": True, "condition_met_date": str(live[met_idx]["date"])[:10]}
    if fill == "at_level":
        fill_idx, fill_px = met_idx, met_px
    else:
        fill_idx = met_idx + 1
        nxt = bars[fill_idx] if fill_idx < len(bars) else None
        fill_px = nxt.get("open") if isinstance(nxt, dict) else None
    if fill_px is None:
        out["note"] = f"condition {observe} met on {out['condition_met_date']}; the next open is not in the bars yet (fill pending)"
        return out
    fill_px = float(fill_px)
    out.update({"entry_price_reached": True, "fill_date": str(bars[fill_idx]["date"])[:10], "fill_price": round(fill_px, 6),
                "post_reach_ret_pct": round((bars[-1]["close"] / fill_px - 1) * 100, 2)})
    out["reached_date"] = out["fill_date"]
    if stop is not None:
        out.update(_simulate_exit(bars, fill_idx, fill_px, stop, long_side, stop_rule))
    return out


def _conditional_line(cs: Dict) -> str:
    """One sentence for the alternatives row: what the actual conditional order did."""
    if cs.get("entry_price_reached"):
        return (f"conditional {cs.get('observe')} met on {cs.get('condition_met_date')}; filled {cs.get('fill')} "
                f"@{cs.get('fill_price')} on {cs.get('fill_date')}")
    return str(cs.get("note") or f"conditional {cs.get('observe')} not met: no fill") + \
        " (the actual operation took no position; compare with the alternatives)"


def _simulate_exit(bars: List[Dict], start_idx: int, fill_px: float, stop: float, long_side: bool,
                   stop_rule: Tuple[str, int] = ("touch", 1)) -> Dict:
    """成交后按报告自写的止损模拟退出(R24; 2026-10-17 晚抽出, 条件单与替代策略共用): 触价语义按 low/high(成交当根也查),
    收盘确认 / N 日未收回按连续 n 个收盘在线外、第 n 根收盘价退出; 否则持到窗末。只模拟止损; 止盈分批等由评分员按日线表核。"""
    rule, n_need = stop_rule if stop_rule else ("touch", 1)
    exit_px, exit_reason, exit_date, ambiguous = bars[-1]["close"], "window_end", str(bars[-1]["date"])[:10], False
    run = 0
    for j, b in enumerate(bars[start_idx:], start=start_idx):
        if rule == "close":
            beyond = (b["close"] < stop) if long_side else (b["close"] > stop)
            run = run + 1 if beyond else 0
            if run >= n_need:
                exit_px, exit_reason, exit_date = b["close"], "stop_close", str(b["date"])[:10]
                break
        elif (b["low"] <= stop) if long_side else (b["high"] >= stop):
            exit_px, exit_reason, exit_date, ambiguous = stop, "stop", str(b["date"])[:10], (j == start_idx)
            break
    ret = (exit_px / fill_px - 1) * 100 if long_side else (fill_px / exit_px - 1) * 100
    out = {"simulated_exit_ret_pct": round(ret, 2), "exit_reason": exit_reason, "exit_date": exit_date,
           "exit_rule": rule if rule == "touch" else f"close x{n_need}"}
    if ambiguous:
        out["exit_same_bar_as_fill"] = True
    return out


def immediate_action_stats(bars: List[Dict], price_T: float, stop: Optional[float] = None,
                           stop_rule: Tuple[str, int] = ("touch", 1)) -> Dict:
    """替代策略的机械结果(2026-10-17 晚, 复盘并排清算): T 时按现价进场 / 按现价减仓 / 不动, 同窗口、同退出口径。
    进场按 price_T 成交(不问触价), 止损按报告自写的止损从第一根起模拟; 减仓按 price_T 卖出, 避开的是窗口收益的相反数;
    不动 = 错过了窗口收益。都是事实不是裁决: 评分员拿它与报告实际选的动作并排看, 不把事后最好的那条当成当时必选。"""
    if not bars or not price_T:
        return {}
    ret = round((bars[-1]["close"] / price_T - 1) * 100, 2)
    # 按现价进场永远是多头; 止损只在它位于入场价下方时适用(上方的"止损"是卖侧条件, 不借用), 没有适用的退出条件就持到窗末并标明
    ent: Dict[str, Any] = {"fill_price": price_T, "window_end_ret_pct": ret}
    if stop is not None and stop < price_T:
        ent.update(_simulate_exit(bars, 0, price_T, stop, True, stop_rule))
    else:
        ent["stop_not_applicable"] = True
    ent["realized_ret_pct"] = ent.get("simulated_exit_ret_pct", ret)
    return {"immediate_entry": ent,
            # 按现价减: 与"继续持有到窗末"比, 正 = 减仓避开了下跌
            "immediate_trim": {"exec_price": price_T, "vs_keep_pct": round(-ret, 2), "basis": "vs holding through window end"},
            # 不动: market_ret_pct 是行情本身; opportunity_cost_pct 是相对"按现价进场(含其止损)"的机会成本, 正 = 错过, 负 = 避开
            "no_action": {"ret_pct": 0.0, "market_ret_pct": ret,
                          "opportunity_cost_pct": round(ent["realized_ret_pct"], 2), "basis": "vs realized return of entering at the current price"}}


_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)")


def grace_days(horizon_days: int) -> int:
    """short_term 到期前后的宽限(交易日): **预先定、两侧一致**(目标与失效位同一宽限,
    不看完走势再选延期, FINAL §11.2)。参考值 = 约 horizon 的一半, 最少 1 天、最多 3 天:
    路径判断的时点噪音与窗口同量级, 封顶是不让 3 天主张靠 10 天兑现。数值可调, 改这里一处。"""
    return max(1, min(3, int(round(int(horizon_days) / 2))))


def short_term_settlement(bars: List[Dict], price_T: float, horizon_days: Optional[int],
                          st_target: Optional[float] = None, invalid: Optional[float] = None,
                          st_band: Optional[Tuple[float, float]] = None, timeline: Optional[Dict] = None,
                          sides: Optional[Dict[str, str]] = None) -> Dict:
    """short_term 按 horizon 柔性清算(决策契约 §4.6): 主窗口 = horizon_days 根交易日;
    sides(R24): 各线的角色方向({"st_target": "above", ...}), 没有 5 分钟排班时的日线清算也按角色判触及。
    到期前后 grace_days 内碰线照算(两侧一致); 到期两线都没碰 → 按到期收盘朝目标走了多少给进展;
    宽限过后不再评(过期主张不在 20d 尺上追责)。到期时的原始结果与宽限口径分列保存。
    报告当天余下路径(bars[0] 标 partial_day)计触线, 不算一个完整交易日: 到期日 = T 之后第 horizon_days 个完整交易日。
      outcome_*: target_first / invalid_first / untouched (方向主张)
                 band_held / band_broken (震荡区间主张, 两沿即双侧无效位)
                 pending (窗口还没跑到到期日)
    horizon_days 缺失(老报告) → 整个窗口当主窗口、无宽限, 标 horizon_days=None。"""
    if st_band:
        lines = {"st_low": st_band[0], "st_high": st_band[1]}
    else:
        lines = {k: v for k, v in (("st_target", st_target), ("invalid", invalid)) if v is not None}
    if not lines or not bars:
        return {}
    sided_lines: Dict[str, Any] = {k: ((v, sides[k]) if sides and sides.get(k) else v) for k, v in lines.items()}
    n = len(bars)
    lead = 1 if bars[0].get("partial_day") else 0      # 当天余下那根: 计触线, 不计天数
    n_full = n - lead
    h = int(horizon_days) if horizon_days else None
    h_eff, g = (n_full, 0) if h is None else (h, grace_days(h))
    covered = n_full >= h_eff

    # R23: 有精细撞线时序(5m)就用它裁定, 不再拿日线重算一遍——同日两线都碰到时, 日线版只能按插入顺序取第一条,
    # 会把"先失效后到目标"写成 target_first(2026-10-10 Codex 复现)。线按价位对应(操作锚与短期目标同价时排班只留一个名字)。
    win_dates = []
    for b in bars:
        dd = str(b["date"])[:10]
        if not win_dates or win_dates[-1] != dd:
            win_dates.append(dd)
    by_price = {float(v): k for k, v in lines.items()}
    fine = []
    if timeline and timeline.get("events"):
        for e in timeline["events"]:
            k = by_price.get(float(e.get("price")))
            dd = str(e.get("at"))[:10]
            if k is None:
                continue
            if dd in win_dates:
                day_i = win_dates.index(dd)
            elif dd < win_dates[0]:
                day_i = -1      # 继承的 3d 排班里报告当天的事件, 本窗口没有那根部分 bar(20d 时 5 分钟数据已过期): 记在第一根之前
            else:
                continue
            fine.append({**e, "line": k, "_day": day_i})
        fine.sort(key=lambda e: (e["_day"], e.get("elapsed_td", 0)))

    def _outcome(nfull: int):
        nbars = lead + nfull
        if timeline and timeline.get("events") is not None:
            ev = [e for e in fine if e["_day"] < nbars]
        else:
            ev = touch_timeline(bars[:nbars], price_T, sided_lines)["events"]
        if not ev:
            return ("band_held" if st_band else "untouched"), None
        first = {k: v for k, v in ev[0].items() if k != "_day"}
        if st_band:
            return "band_broken", first
        if first.get("same_bar_ambiguous") and len({e["line"] for e in ev
                                                    if e.get("elapsed_td") == first.get("elapsed_td")}) > 1:
            return "order_unknown", first      # 同一根 bar 内两线都碰到, 先后不可判, 不硬猜
        return ("target_first" if first["line"] == "st_target" else "invalid_first"), first

    at_expiry, first_exp = _outcome(min(h_eff, n_full))
    with_grace, first_grace = _outcome(min(h_eff + g, n_full))
    if not covered and at_expiry in ("untouched", "band_held"):
        at_expiry = "pending"
        if with_grace in ("untouched", "band_held"):
            with_grace = "pending"
    expiry_bar = bars[lead + h_eff - 1] if covered else None
    res = str((timeline or {}).get("resolution") or "")
    out = {
        "settled_by": (res if "5m" in res else "daily"),
        "horizon_days": h,
        "grace_days": g,
        "horizon_covered": covered,
        "grace_covered": n_full >= h_eff + g,
        "expiry_date": str(expiry_bar["date"])[:10] if covered else None,
        "expiry_close": round(float(expiry_bar["close"]), 4) if covered else None,
        "outcome_at_expiry": at_expiry,
        "outcome_with_grace": with_grace,
        "first_touch": first_grace or first_exp,
        "expired_before_window_end": n_full > h_eff + g,
        "progress_at_expiry_pct": None,
    }
    if st_band:
        out["expiry_inside_band"] = bool(st_band[0] <= expiry_bar["close"] <= st_band[1]) if covered else None
    elif covered and st_target is not None and st_target != price_T:
        # 到期收盘朝目标走了几成: 100 = 正好到目标, 负 = 反向走
        out["progress_at_expiry_pct"] = round((expiry_bar["close"] - price_T) / (st_target - price_T) * 100, 1)
    return out


def _dir_op_conflict(st_direction, op_type) -> bool:
    """R21 P4 旗: short_term 路径判断与操作族反向(上行×卖 / 下行×买)。
    不判罪——见强分批减/接飞刀都有户口, 旗只供 V4 机械计数与判词对账(quant 进 PG 可查)。
    用 short_term.direction 而非 action 推的方向: 09-08 锚批病形 = 路径判上行×TRIM。"""
    d = str(st_direction or "").strip()
    op = str(op_type or "").upper()
    return (d == "上行" and op in _SELL_OPS) or (d == "下行" and op in _BUY_OPS)


def _range_pair(v) -> Optional[tuple]:
    """震荡区间 '下沿-上沿' → (low, high); 非区间形 → None (R21 P1 震荡可证伪化)。"""
    if v is None:
        return None
    m = _RANGE_RE.search(str(v))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0 or a == b:
        return None
    return (min(a, b), max(a, b))


def _ny_session(ts) -> Optional[str]:
    """5m bar 时间戳(UTC) → 时段短标签 pre/rth/post/overnight; 判不出 → None。
    简短英文值, 不可能被当数值或编号误读。存储时间戳一律 UTC 不变——
    纽约钟只在此处内部用于判定时段语义(美股时段边界定义在交易所本地钟上), 不落字段。"""
    if not isinstance(ts, datetime):
        return None
    t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        ny = t.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None
    hm = ny.hour * 60 + ny.minute
    if 570 <= hm < 960:
        return "rth"
    if 240 <= hm < 570:
        return "pre"
    if 960 <= hm < 1200:
        return "post"
    return "overnight"


def touch_timeline(bars: List[Dict], price_T: float, lines: Dict[str, Any],
                   day_T_fraction: Optional[float] = None) -> Dict:
    """撞线排班 (Task 2.6): 各条线(entry/stop/target/st_target/invalid)的首次触及事件, 按时间排序。
    判向与 entry/touch 同一规则: 线在现价下方 → bar.low <= 线; 上方 → bar.high >= 线。
    elapsed_td = 自报告时点起经过的交易日数: 之前完整交易日的权重和 + 当日内 bar 序进度(bar_pos/当日bar数)×当日权重;
    日线 bars 下即整数天。报告当天余下路径是第 0 日, 权重 = 剩余时段占整日的比例(day_T_fraction, 或第一根 bar
    自带的 day_fraction), 它的事件落在 0 到该比例之间, 之后的完整交易日接着往上数(R23 边界, 2026-10-11)。
    同一根 bar 内多条线首触 → 顺序不可判, 各事件标 same_bar_ambiguous(不硬猜)。
    时段标签 (2026-09-10): 5m bars 且当日覆盖形状像美股全时段(同日出现 pre+rth+post)时,
    每个触及事件带 `session` 短字段(pre/rth/post/overnight); 覆盖形状不像(欧亚本地时段)
    → 整场不贴, 宁缺毋错。"""
    days: List[str] = []
    for b in bars:
        d = str(b["date"])[:10]
        if not days or days[-1] != d:
            days.append(d)
    per_day = {d: sum(1 for b in bars if str(b["date"])[:10] == d) for d in days}
    w0 = day_T_fraction
    if w0 is None and bars and bars[0].get("partial_day"):
        w0 = bars[0].get("day_fraction")
    weight = {d: 1.0 for d in days}
    if days and w0 is not None:
        weight[days[0]] = float(w0)
    base, day_base = 0.0, {}
    for d in days:
        day_base[d] = base
        base += weight[d]
    # 时段标签自校准闸: 任一天的 bars 同时覆盖 pre+rth+post → 视为美股全时段数据, 开贴
    day_sessions: Dict[str, set] = {}
    for b in bars:
        s = _ny_session(b["date"])
        if s:
            day_sessions.setdefault(str(b["date"])[:10], set()).add(s)
    tag_sessions = any({"pre", "rth", "post"} <= v for v in day_sessions.values())
    # R24: 线的值可以是 价位 或 (价位, "above"|"below"): 按线的角色定触及方向(HYMTF 的失效线 381500 在回退起点 382500 之下,
    # 旧版按"线在起点下方"当成向下触及, 首日就记了伪触及); 没带角色的仍按与起点价的相对位置判
    sided = {k: (v if isinstance(v, tuple) else (v, None)) for k, v in lines.items()}
    events, pending, pos_in_day, cur_day = [], dict(sided), 0, None
    for b in bars:
        d = str(b["date"])[:10]
        if d != cur_day:
            cur_day, pos_in_day = d, 0
        pos_in_day += 1
        hit_now = []
        for name, (px, side) in list(pending.items()):
            if px is None:
                pending.pop(name)
                continue
            below = (side == "below") if side else (px <= price_T)
            if (b["low"] <= px) if below else (b["high"] >= px):
                elapsed = day_base[d] + pos_in_day / per_day[d] * weight[d]
                ev = {"line": name, "price": px, "at": str(b["date"]),
                      "elapsed_td": round(elapsed, 2)}
                if tag_sessions:
                    sess = _ny_session(b["date"])
                    if sess:
                        ev["session"] = sess
                hit_now.append(ev)
                pending.pop(name)
        if len(hit_now) > 1:
            for e in hit_now:
                e["same_bar_ambiguous"] = True
        events.extend(hit_now)
    out = {"events": events, "untouched": sorted(pending.keys())}
    if w0 is not None:
        out["day_T_fraction"] = round(float(w0), 2)
    return out


def merge_timelines(prior: Dict, coarse: Dict) -> Dict:
    """排班拼接 (dt>=20 继承 3d 精细段): 每条线首触 = 3d 精细结果优先(标 inherited_3d),
    3d 里没触到的线用粗分辨率结果补; 两边都没触到 → untouched。"""
    fine = {e["line"]: {**e, "inherited_3d": True} for e in (prior or {}).get("events", [])}
    merged = dict(fine)
    for e in (coarse or {}).get("events", []):
        merged.setdefault(e["line"], e)
    events = sorted(merged.values(), key=lambda e: (str(e.get("at"))[:10], e["elapsed_td"]))
    all_lines = set(merged) | set((prior or {}).get("untouched", [])) | set((coarse or {}).get("untouched", []))
    return {"events": events, "untouched": sorted(all_lines - set(merged)),
            "resolution": f"{(prior or {}).get('resolution', '?')}+{(coarse or {}).get('resolution', '?')}"}


def _sigma_atr(df, day_T) -> Tuple[Optional[float], Optional[float]]:
    """T 前的波动尺度(冻结于 T): sigma = 最近 SIGMA_LOOKBACK 日对数收益标准差(小数); atr = 最近 ATR_LOOKBACK 日 (high-low) 均值(价格)。"""
    import math
    prior = df[df.index < day_T]
    sigma = atr = None
    if len(prior) >= 3:
        c = [float(x) for x in prior["close"].tail(SIGMA_LOOKBACK + 1)]
        rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            sigma = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
        tail = prior.tail(ATR_LOOKBACK + 1)
        if "high" in tail.columns and "low" in tail.columns and len(tail) >= 2:
            hs, ls, cs = [float(x) for x in tail["high"]], [float(x) for x in tail["low"]], [float(x) for x in tail["close"]]
            tr = [max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])) for i in range(1, len(tail))]  # 真实波幅, 含跳空
            if tr:
                atr = sum(tr) / len(tr)
    return sigma, atr


def eps_pct(sigma: Optional[float], horizon_days: int) -> float:
    """显著门槛(%): max(EPS_MIN_PCT, EPS_SIGMA_MULT * sigma * sqrt(H)); sigma 缺失时就是 EPS_MIN_PCT。"""
    if not sigma:
        return EPS_MIN_PCT
    return round(max(EPS_MIN_PCT, EPS_SIGMA_MULT * sigma * (max(int(horizon_days), 1) ** 0.5) * 100), 2)


def line_state(bars: List[Dict], line: float, side: str, atr: Optional[float]) -> Dict:
    """判断线的接受状态(R24): untouched / touched_only / accepted / pending / unknown。
    side = 线被"越过"的方向: "above" = 收盘在线上方算越线(上行目标、失效线在上方时), "below" 反之。
    accepted = 连续 ACCEPT_CONSEC_CLOSES 个收盘越线, 且窗口内最大越幅 >= ACCEPT_DEPTH_ATR * ATR14(atr 缺失时只看连续收盘, 标 depth_unknown);
    pending = 窗末刚越线还没凑够连续根数; touched_only = 盘中触及或单日收盘越线又回来。"""
    if not bars:
        return {"state": "unknown"}
    beyond = [(b["close"] > line) if side == "above" else (b["close"] < line) for b in bars]
    touched = any((b["high"] >= line) if side == "above" else (b["low"] <= line) for b in bars)
    depth = max((abs(b["close"] - line) for b, x in zip(bars, beyond) if x), default=0.0)
    out: Dict[str, Any] = {"line": line, "side": side, "touched": touched,
                           "closes_beyond": sum(beyond), "max_depth_pct": round(depth / line * 100, 2) if line else None}
    # 接受 = 连续 ACCEPT_CONSEC_CLOSES 个收盘越线, 且**到确认那根为止**的最大越幅 >= ACCEPT_DEPTH_ATR * ATR(后面的深穿不倒填确认日)
    run, accepted_at, depth_so_far = 0, None, 0.0
    for b, x in zip(bars, beyond):
        run = run + 1 if x else 0
        if x:
            depth_so_far = max(depth_so_far, abs(b["close"] - line))
        deep_enough = True if atr is None else depth_so_far >= ACCEPT_DEPTH_ATR * atr
        if run >= ACCEPT_CONSEC_CLOSES and deep_enough and accepted_at is None:
            accepted_at = str(b["date"])[:10]
    if accepted_at and beyond[-1]:
        out.update({"state": "accepted", "accepted_at": accepted_at})
    elif accepted_at:
        out.update({"state": "accepted_then_reclaimed", "accepted_at": accepted_at})   # 曾被接受, 评估窗末已收回线内
    elif beyond[-1]:
        out["state"] = "pending"                                                         # 窗末在线外, 但根数或幅度还不够
    elif touched or any(beyond):
        out["state"] = "touched_only"
    else:
        out["state"] = "untouched"
    if atr is None:
        out["depth_unknown"] = True
    return out


def _seg_stats(bars: List[Dict], anchor: float, sigma: Optional[float]) -> Dict:
    if not bars or not anchor:
        return {"n": 0, "insufficient": True}
    closes = [b["close"] for b in bars]
    hi, lo = max(b["high"] for b in bars), min(b["low"] for b in bars)
    run_max, mdd = None, 0.0
    for b in bars:
        run_max = b["high"] if run_max is None else max(run_max, b["high"])
        mdd = min(mdd, (b["low"] - run_max) / run_max * 100)
    n = sum(1 for b in bars if not b.get("partial_day"))
    ret = (closes[-1] - anchor) / anchor * 100
    e = eps_pct(sigma, max(n, 1))
    return {"n": n, "start": str(bars[0]["date"])[:10], "end": str(bars[-1]["date"])[:10], "anchor": round(anchor, 4),
            "ret_pct": round(ret, 2), "up_exc_pct": round((hi - anchor) / anchor * 100, 2),
            "down_exc_pct": round((lo - anchor) / anchor * 100, 2), "max_drawdown_pct": round(mdd, 2),
            "eps_pct": e, "significant": abs(ret) >= e}


def segments(bars: List[Dict], price_T: float, event_date: Optional[str], sigma: Optional[float],
             lines: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """事件分段(R24, 第一件定案): 事件日那根 bar 单列不归两段; 前段锚 = 起点价, 后段锚 = 事件日收盘; 收益链闭合。
    event_date 不是交易日时取窗口内第一个 >= 它的交易日(标 event_bar_is_next_session)。事件不在窗口内 → None。"""
    if not event_date or not bars:
        return None
    if isinstance(event_date, (list, tuple)):   # 11-06: several events → the first one inside the bars window
        lo, hi = str(bars[0]["date"])[:10], str(bars[-1]["date"])[:10]
        event_date = next((str(d)[:10] for d in sorted(str(x)[:10] for x in event_date if x) if lo <= str(d)[:10] <= hi), None)
        if not event_date:
            return None
    ed = str(event_date)[:10]
    if ed < str(bars[0]["date"])[:10]:
        return None            # 事件早于窗口(报告形成于事件之后): 不分段, 不造假事件日
    idx = next((i for i, b in enumerate(bars) if str(b["date"])[:10] >= ed), None)
    if idx is None:
        return None
    eb = bars[idx]
    prev_close = bars[idx - 1]["close"] if idx > 0 else price_T
    touched, beyond = [], []
    for name, v in (lines or {}).items():
        px, side = (v if isinstance(v, tuple) else (v, None))
        if px is None:
            continue
        below = (side == "below") if side else (px <= price_T)
        if (eb["low"] <= px) if below else (eb["high"] >= px):
            touched.append(name)
        if (eb["close"] < px) if below else (eb["close"] > px):
            beyond.append(name)
    return {
        "event_date": ed,
        "event_bar": {"date": str(eb["date"])[:10], "is_next_session": str(eb["date"])[:10] != ed,
                      "day_ret_pct": round((eb["close"] - prev_close) / prev_close * 100, 2),
                      "cum_ret_from_T_pct": round((eb["close"] - price_T) / price_T * 100, 2),
                      "touched": touched, "close_beyond": beyond, "straddles_boundary": True},
        "pre": _seg_stats(bars[:idx], price_T, sigma),
        "post": _seg_stats(bars[idx + 1:], eb["close"], sigma),
    }


def _daily_table(bars: List[Dict]) -> List[Dict]:
    """R24: 完整窗口日线表(给评分员核条件用), 替代 12 点抽稀。"""
    out = []
    for b in bars:
        row = {"date": str(b.get("date"))[:10]}
        for k in ("open", "high", "low", "close"):
            if b.get(k) is not None:
                row[k] = round(float(b[k]), 4)
        if b.get("vol_ratio_20d") is not None:
            row["vol_ratio_20d"] = b["vol_ratio_20d"]
        if b.get("partial_day"):
            row["partial_day"] = True
        out.append(row)
    return out


def _snapshot_prices(report: Dict) -> Dict[str, float]:
    """老报告(没有 reference_prices)的起点价来源(R24): 读该场 data_files 里的技术快照 <SYM>_current_snapshot_last_price。
    文件不在了就空; 路径相对工程根。"""
    out: Dict[str, float] = {}
    paths: List[str] = []

    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                _walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                _walk(v)
        elif isinstance(x, str) and "/technical/" in x and x.endswith(".json"):
            paths.append(x)

    _walk(report.get("data_files") or {})     # symbol / benchmarks / lenses / sector 都走一遍
    for rel in paths:
        try:
            pth = Path(rel) if Path(rel).is_absolute() else PROJECT_ROOT / rel
            if not pth.exists():
                continue
            snap = json.loads(pth.read_text(encoding="utf-8"))
            for k, v in snap.items():
                if str(k).endswith("_current_snapshot_last_price") and v not in (None, "", 0):
                    out[str(k)[: -len("_current_snapshot_last_price")].upper()] = float(v)
        except Exception as e:  # noqa: BLE001
            logging.debug(f"[postcheck_quant] snapshot price read skipped {rel}: {e}")
    return out


def reference_candidates(bars: List[Dict], price_T: float, stats: Dict, levels: Dict[str, float],
                         states: Dict[str, Dict], valid_days: int, stop: Optional[float]) -> Dict:
    """可达相邻操作候选(R24, 第二件定案): 只用 T 时登记的结构位构造, 按执行规则机械清算各自的价格后果;
    单边候选只在某条判断线被实质接受时才"可达"(single_side_available); 差距类别(相同/参数差/缺转态/反向/不可识别)由评分员填。"""
    cands = [{"op": "HOLD", "realized_ret_pct": 0.0, "note": "hold (reference baseline)"}]
    # 单边候选只在某条判断线被实质接受之后才"可达": 从确认那根的收盘起算到窗末(不是从 T 起算, 不用事后视角)
    by_date = {str(b["date"])[:10]: b for b in bars}
    for name, stt in states.items():
        if stt.get("state") not in ("accepted", "accepted_then_reclaimed") or not stt.get("accepted_at"):
            continue
        cb = by_date.get(stt["accepted_at"])
        if not cb:
            continue
        after = (bars[-1]["close"] / cb["close"] - 1) * 100
        op = "TRIM@accepted" if stt.get("side") == "below" else "ENTRY@accepted"
        cands.append({"op": f"{op}:{name}", "level": stt.get("line"), "from": stt["accepted_at"],
                      "realized_ret_pct": round(-after if op.startswith("TRIM") else after, 2),
                      "note": "executed at the close of the bar on which the judgment line was accepted; consequence through window end (trim = decline avoided)"})
    if levels.get("entry") is not None:
        fs = entry_fill_stats(bars, price_T, levels["entry"], valid_days=valid_days, stop=stop)
        cands.append({"op": "LIMIT_ENTRY@registered", "level": levels["entry"],
                      "realized_ret_pct": fs.get("simulated_exit_ret_pct", fs.get("post_reach_ret_pct")) if fs.get("entry_price_reached") else None,
                      "note": "the report's own order level; a consequence only if the level was reached"})
    for name in ("st_target", "st_high", "target"):
        lv = levels.get(name)
        if lv is not None and lv > price_T:
            fs = entry_fill_stats(bars, price_T, lv, valid_days=None)
            cands.append({"op": f"TRIM@{name}", "level": lv,
                          "realized_ret_pct": round((lv / price_T - 1) * 100, 2) if fs.get("entry_price_reached") else None,
                          "note": "trim at the upper level registered at T (fills only if the level was reached)"})
            break
    accepted = [k for k, v in states.items() if v.get("state") in ("accepted", "accepted_then_reclaimed")]
    return {"candidates": cands, "single_side_available": bool(accepted), "accepted_lines": accepted,
            "distance_class": None, "note": "distance_class is filled by the reckoner: equivalent | parameter_gap | missing_switch | opposite | unidentifiable"}


def _sample_path(bars: List[Dict], budget: int = PATH_BUDGET) -> List[Dict]:
    """把窗口日线压成 ~budget 个采样点 (等距取), 每点 {date, close}。给 AI 看形状。"""
    n = len(bars)
    if n <= budget:
        picks = bars
    else:
        step = n / budget
        idx = sorted(set(int(i * step) for i in range(budget)) | {n - 1})
        picks = [bars[i] for i in idx]
    return [{"date": str(b.get("date"))[:10], "close": round(b["close"], 4)}
            for b in picks if b.get("close") is not None]


def _load_daily_df(symbol: str, min_mtime: float = None, res: str = "1d"):
    """读**本次刚请求下来**的 {res} CSV → DataFrame(index=UTC date 升序)。默认 5y_1d 日线;
    res="5m" 时读 7d_5m 分时(撞线排班用, Task 2.6)。
    数据模型(2026-07-02 定): 后验先连盈透新请求该标的日线 → 现算摆动线; **永久保存的是摆动线
    摘要**(quant 随 postcheck 行入 PG), 原始 bar/CSV 是临时运输产物, 不永久保留、不当历史数据源。
    min_mtime 给定时: 最新 CSV 早于它 = 本次没请求到 → 视为无数据(报出来, 不硬算)。"""
    try:
        import pandas as pd
    except Exception:
        return None
    sym = symbol.upper().replace(" ", ".")
    d = MARKET_DATA_ROOT / sym
    files = list(d.glob(f"{sym}_*_{res}_*.csv")) if d.exists() else []
    if not files:
        if res == "1d":
            logging.warning(f"[postcheck_quant] {sym}: no daily bars (none fetched this run) — no result price for this period")
        return None
    latest = max(files, key=lambda f: f.name)
    if min_mtime is not None and latest.stat().st_mtime < min_mtime:
        logging.warning(f"[postcheck_quant] {sym}: {res} is stale ({latest.name}, no new data fetched this run) — "
                        f"leftover CSV is not used")
        return None
    try:
        df = pd.read_csv(latest)
    except Exception as e:
        logging.warning(f"[postcheck_quant] failed to read {latest.name}: {e}")
        return None
    df.columns = [str(c).lower() for c in df.columns]
    dcol = next((c for c in ("date", "datetime", "time", "index") if c in df.columns), df.columns[0])
    df[dcol] = pd.to_datetime(df[dcol], utc=True, errors="coerce")
    df = df.dropna(subset=[dcol]).sort_values(dcol).set_index(dcol)
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]     # R24: 假日重复 bar(GENERAL_JP 9-21/9-22)去重, get_loc 才是整数
    return df


def _one_symbol_digest(price_symbol: str, T: datetime, dt: int, direction: int,
                       target: Optional[float], stop: Optional[float],
                       min_mtime: float = None, entry: Optional[float] = None,
                       st_target: Optional[float] = None,
                       invalid: Optional[float] = None,
                       trim: Optional[float] = None,
                       st_band: Optional[tuple] = None,
                       horizon_days: Optional[int] = None,
                       ref_price: Optional[float] = None,
                       prior_timeline: Optional[Dict] = None,
                       snapshot_price: Optional[float] = None,
                       valid_days: Optional[int] = None,
                       event_date: Optional[str] = None,
                       st_direction: Optional[str] = None,
                       valid_until: Optional[str] = None,
                       stop_rule: Tuple[str, int] = ("touch", 1),
                       conditional: Optional[Dict] = None, execution_report: Optional[Dict] = None) -> Dict:
    """单个价格标的的窗口摘要 (读它自己的日线)。无数据 → {}。
    conditional (G4, ruler reports only): the operation's operation_execution.conditional {observe, fill} — the entry /
    trim is then settled by conditional_fill_stats (observe rule on daily bars, fill at the next open or at level),
    never as a touch of the level.
    prior_timeline: dt>3 时同源 3d 行的撞线排班, 在裁定 short_term 之前合入(20d 继承精细段后才裁定, 不是裁完再拼)。
    horizon_days: short_term 的有效期, 交给 short_term_settlement 柔性清算。
    ref_price: 报告里组装器注入的本场参考价(有就当起点)。
    R23 起点与窗口(2026-10-10, Codex 复现的缺陷): 日线日期标在零点, 旧版把当天收盘当成报告时点的价格(盘中发布时那是
    未来的价), 当天余下的路径也不在窗口里。现在: 起点 = 报告参考价 > 报告时点前最后一根 5 分钟收盘 > 前一交易日收盘;
    窗口 = 当天 T 之后的余下路径(有 5 分钟数据时合成一根部分 bar) + 之后 dt 个交易日。"""
    df = _load_daily_df(price_symbol, min_mtime=min_mtime)
    if df is None or df.empty:
        return {}
    from workflows.postcheck_execution import contract, normalize_prices, apply_settlement
    execution = None
    scale = {'status': 'unverified'}
    if execution_report is not None:
        execution = contract(execution_report, T, stop, stop_rule)
        stop = execution['stop']['level'] if execution['stop'] else None
        if execution['stop']:
            stop_rule = execution['stop']['semantics'], execution['stop']['count']
        valid_until = execution['valid_until'] or valid_until
        valid_days = execution['valid_days'] or valid_days
        if execution.get('expiry_instruction_present') and not valid_until and _order_valid_days(execution_report)[1] == 'default':
            execution['issues'].append('explicit order expiry instruction could not be parsed; default validity forbidden')
        df, scale = normalize_prices(df, execution_report, str(price_symbol).upper(), PROJECT_ROOT)
        if scale['status'] == 'unassessable':
            return {'error': scale['reason'], 'settlement_status': 'unassessable', 'price_normalization': scale}
    import pandas as pd
    day_T = pd.Timestamp(T).tz_convert("UTC").normalize() if pd.Timestamp(T).tzinfo else pd.Timestamp(T, tz="UTC").normalize()
    prior = df[df.index < day_T]
    if prior.empty:
        return {}
    idf = _load_daily_df(price_symbol, min_mtime=min_mtime, res="5m")
    if scale['status'] == 'normalized':
        # Intraday adjustment can differ from daily adjustment. Do not guess its scale.
        idf = None
        prior_timeline = None
    have_5m = idf is not None and not idf.empty
    anchor_source, anchor_note = "prior_close", None
    price_T = float(prior.iloc[-1]["close"])
    reference = ref_price or snapshot_price
    if execution is not None and scale['status'] == 'unverified' and reference and abs(reference / price_T - 1) > 0.4:
        return {'error': 'reference and market price scales cannot be reconciled from frozen evidence',
                'settlement_status': 'unassessable', 'price_normalization': scale}
    rest_T = None
    if have_5m:
        upto = idf[idf.index <= T]
        if not upto.empty and str(upto.index[-1])[:10] == str(day_T)[:10]:
            price_T, anchor_source = float(upto.iloc[-1]["close"]), "5m_close_at_T"
        rest = idf[(idf.index > T) & (idf.index < day_T + pd.Timedelta(days=1))]
        if not rest.empty:
            # 当天余下占整日的比例: 余下 bar 数 ÷ 其他日子里最多的 bar 数(没有别的日子就记半天)
            per_day = idf.groupby(idf.index.strftime("%Y-%m-%d")).size()
            others = per_day[per_day.index != str(day_T)[:10]]
            frac = min(1.0, len(rest) / int(others.max())) if len(others) and int(others.max()) > 0 else 0.5
            rest_T = {"date": day_T, "high": float(rest["high"].max()), "low": float(rest["low"].min()),
                      "close": float(rest.iloc[-1]["close"]), "partial_day": True, "day_fraction": round(frac, 2)}
    if ref_price is not None and ref_price > 0:
        price_T, anchor_source = float(ref_price), "report_reference_price"
    elif anchor_source == "prior_close" and snapshot_price is not None and snapshot_price > 0:
        # R24: 老报告没有 reference_prices 且 5 分钟线已过期 → 用该场技术快照的最新价(SFTBY 6540→5823, HYMTF 382500→370500)
        price_T, anchor_source = float(snapshot_price), "technical_snapshot"
    if rest_T is None and str(day_T)[:10] in {str(i)[:10] for i in df.index} and anchor_source != "5m_close_at_T":
        anchor_note = "report issued intraday with no intraday data: anchor = prior session close; the remainder of the report day is not counted"
    after = df[df.index > day_T].head(int(dt))  # 当天之后的前 dt 个交易日 (不够就用现有)
    if after.empty and rest_T is None:
        return {}
    bars = ([rest_T] if rest_T else []) + [
        {"date": idx, "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]),
         **({"open": float(r["open"])} if "open" in after.columns and r.get("open") == r.get("open") else {})}
        for idx, r in after.iterrows()]
    # R24: 量比(当日量 ÷ 前 20 个交易日均量), 给"放量 / 缩量"类硬条件用
    if "volume" in df.columns:
        vol = df["volume"].astype(float)
        for b in bars:
            if b.get("partial_day"):
                continue
            pos = df.index.get_loc(b["date"]) if b["date"] in df.index else None
            if not isinstance(pos, int):
                continue
            if pos >= 1:
                base_v = vol.iloc[max(0, pos - 20):pos]
                if len(base_v) and base_v.mean() > 0:
                    b["vol_ratio_20d"] = round(float(vol.iloc[pos]) / float(base_v.mean()), 2)
    sigma_T, atr_T = _sigma_atr(df, day_T)
    d = window_stats(bars, price_T, direction, target, stop)
    if not d:
        return {}
    d["resolution"] = "daily"
    d["anchor_source"] = anchor_source
    if anchor_note:
        d["anchor_note"] = anchor_note
    if rest_T:
        d["day_T_partial"] = True
        d["day_T_fraction"] = rest_T["day_fraction"]
    d["symbol"] = str(price_symbol).upper()   # 机械绑定, 防串标的(上古 GENERAL 成员互换旧案)
    d["direction_call"] = {1: "看多", -1: "看空", 0: "中性"}[direction]
    d["target"] = target
    d["stop"] = stop
    if st_target is not None:
        d["st_target"] = st_target
    if invalid is not None:
        d["invalid"] = invalid
    if st_band is not None:
        # R21 P1: 震荡=区间主张, 两沿即双侧无效位——排班机械裁定破沿时刻
        d["st_band"] = {"low": st_band[0], "high": st_band[1]}
    cond = conditional if isinstance(conditional, dict) and conditional.get("observe") else None
    prev_close = float(prior.iloc[-1]["close"])
    if entry is not None and cond:
        d["entry"] = conditional_fill_stats(bars, price_T, cond.get("observe"), cond.get("fill"), prev_close=prev_close,
                                            valid_days=valid_days, valid_until=valid_until, stop=stop, long_side=True,
                                            stop_rule=stop_rule)
    elif entry is not None:
        d["entry"] = entry_fill_stats(bars, price_T, entry, valid_days=valid_days, stop=stop, long_side=True,
                                      valid_until=valid_until, stop_rule=stop_rule)
    if trim is not None and cond:
        d["trim_exec"] = {k.replace("entry_", "exec_"): v for k, v in conditional_fill_stats(
            bars, price_T, cond.get("observe"), cond.get("fill"), prev_close=prev_close, valid_days=valid_days,
            valid_until=valid_until, long_side=False).items()}
    elif trim is not None:
        # R21 卖侧镜像: TRIM 执行价卖没卖到/挂多高/差口——机制与买侧同一把尺
        # (entry_fill_stats 本就方向对称), 字段前缀改 exec_ 防误读
        d["trim_exec"] = {k.replace("entry_", "exec_"): v
                          for k, v in entry_fill_stats(bars, price_T, trim).items()}
    # 2026-10-17 晚(并排清算): 替代策略的机械结果——T 时按现价进场 / 按现价减 / 不动; 评分员与实际动作并排, 不作免责门槛
    d["alternatives"] = immediate_action_stats(bars, price_T, stop=stop, stop_rule=stop_rule)
    if d["alternatives"]:
        d["alternatives"]["price_basis"] = d.get("anchor_source")   # price_T 来自哪(reference_prices / 5m 收盘 / 快照 / 前收 = 代理成交假设)
    if cond and (entry is not None or trim is not None):   # G4: the actual conditional order next to the alternatives
        cs = d.get("entry") if entry is not None else {k.replace("exec_", "entry_"): v for k, v in d["trim_exec"].items()}
        d.setdefault("alternatives", {})["actual_conditional"] = _conditional_line(cs)
    # 撞线排班 (Task 2.6): dt<=3 优先 5m 分时(判同日先后), 覆盖不全/没请求到 → 日线凑合。
    # 2026-09-06 分列修复: target 位非 LIMIT_ENTRY 时装的是操作锚(TRIM 执行价/HOLD 观察锚),
    # short_term 的真实获利目标(st_target)与失效线(invalid)此前从未进排班——评分员每场手动
    # 纠偏、invalid 首触时刻永缺数据。现在与操作锚分列同排; 同价去重(先到先得)。
    lines: Dict[str, Any] = {}
    band_lines = (("st_low", st_band[0]), ("st_high", st_band[1])) if st_band else ()
    # R24: 线带角色方向。st_target 按短期方向(上行 → 在上方), invalid 与之相反; 区间两沿固定; 执行线按与起点价的相对位置
    st_up = "上" in str(st_direction or "")
    st_down = "下" in str(st_direction or "")
    sides = {"st_target": "above" if st_up else ("below" if st_down else None),
             "invalid": "below" if st_up else ("above" if st_down else None),
             "st_low": "below", "st_high": "above"}
    seen: Dict[float, str] = {}
    for k, v in (("entry", entry), ("stop", stop), ("target", target),
                 ("st_target", st_target), ("invalid", invalid)) + band_lines:
        if v is None:
            continue
        if v in seen:
            kept = seen[v]
            if sides.get(k) and not isinstance(lines[kept], tuple):
                lines[kept] = (v, sides[k])       # 同价去重先到先得, 但角色方向以带角色的那条为准
            continue
        lines[k] = (v, sides.get(k)) if sides.get(k) else v
        seen[v] = k
    if lines:
        tl_bars, tl_res = bars, "daily"
        if int(dt) <= 3 and have_5m:
            win_dates = {str(b["date"])[:10] for b in bars}
            ibars = [{"date": idx, "high": float(r["high"]), "low": float(r["low"]),
                      "close": float(r["close"])}
                     for idx, r in idf[idf.index > T].iterrows() if str(idx)[:10] in win_dates]
            if {str(b["date"])[:10] for b in ibars} >= win_dates:
                tl_bars, tl_res = ibars, "5m"
        d["touch_timeline"] = touch_timeline(tl_bars, price_T, lines,
                                             day_T_fraction=rest_T["day_fraction"] if rest_T else None)
        d["touch_timeline"]["resolution"] = tl_res
        if prior_timeline:
            d["touch_timeline"] = merge_timelines(prior_timeline, d["touch_timeline"])
    # short_term 按 horizon 柔性清算 (排班仍是全窗口事实; 这里是有效期内的裁定); 排班里有 5m 精细段(本场的或 3d 继承的)就用同一份时序
    tl = d.get("touch_timeline") or {}
    st_settle = short_term_settlement(bars, price_T, horizon_days,
                                      st_target=st_target, invalid=invalid, st_band=st_band,
                                      timeline=tl if "5m" in str(tl.get("resolution") or "") else None,
                                      sides={k: v for k, v in sides.items() if v})
    if st_settle:
        d["short_term"] = st_settle
    d["win_start"] = str(bars[0]["date"])[:10]
    d["win_end"] = str(bars[-1]["date"])[:10]
    d["path"] = _sample_path(bars)
    # ---- R24 ----
    d["daily_bars"] = _daily_table(bars)
    d["sigma_T_pct"] = round(sigma_T * 100, 2) if sigma_T else None
    d["atr_T"] = round(atr_T, 4) if atr_T else None
    d["eps_window_pct"] = eps_pct(sigma_T, d.get("bars_in_window") or len(bars))
    if valid_days is not None:
        d["order_valid_days"] = valid_days
    # 判断线接受状态: 用原始线值(不受同价去重影响), 只在短期路径的有效期(含宽限)内评——过期的短线不参与中期参照
    states: Dict[str, Dict] = {}
    lead = 1 if bars and bars[0].get("partial_day") else 0
    if horizon_days:
        h = int(horizon_days)
        st_bars = bars[: lead + h + grace_days(h)]
    else:
        st_bars = bars
    raw_lines = {"st_target": st_target, "invalid": invalid,
                 "st_low": st_band[0] if st_band else None, "st_high": st_band[1] if st_band else None}
    for name, px in raw_lines.items():
        if px is None:
            continue
        side = sides.get(name) or ("above" if px > price_T else "below")
        states[name] = line_state(st_bars, px, side, atr_T)
        states[name]["eval_bars"] = len(st_bars)
    if states:
        d["line_state"] = states
    seg = segments(bars, price_T, event_date, sigma_T, lines)
    if seg:
        d["segments"] = seg
    levels = {k: (v[0] if isinstance(v, tuple) else v) for k, v in lines.items()}
    d["reference"] = reference_candidates(bars, price_T, d, levels, states, valid_days or ORDER_VALID_DAYS_DEFAULT, stop)
    try:
        from analysis.trend_analyzer import calculate_swing_points
        sw = calculate_swing_points(after) if len(after) >= 40 else None
        if sw:
            d["swing"] = sw
    except Exception as e:
        logging.debug(f"[postcheck_quant] swing skipped: {e}")
    if execution is not None:
        apply_settlement(d, execution_report, execution)
        d['price_normalization'] = scale
    return d


def forecast_scores(symbol: str, T: datetime, report: Dict, min_mtime: float = None, T_days: int = 15) -> Dict:
    """v3.4 (R25): score the report's scenario distributions against the realized close of the T_days-th local trading day after T.
    Returns {} when the report has no scenario, the window is immature, or bars are missing. Scores: CRPS (lower is better),
    Brier of P(up), the nominal-coverage interval hit, and skill = (CRPS_baseline − CRPS) / CRPS_baseline with the versioned
    baseline (three points at ±1σ, σ = ATR%14·√T) from the report's odds snapshot. Each of arbiter-final / Zealot / Reaper
    is scored (each side's endorsed scenario, else its last; plus delta_crps, C8). A report that carries a scenario but no
    odds snapshot is not scored: {"error": "no odds snapshot"}. Pre-ruler reports (no scenario) → {}."""
    sc = report.get("scenario")
    if not isinstance(sc, dict):
        return {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from odds_model import (config_from_snapshot, validate_scenario, expand_leaves, crps_discrete,
                                quantile_interval, score as _score)
    except Exception as exc:
        return {"error": f"odds_model unavailable: {exc}"}
    # the report carries its session's ruler (C4); a scenario without it is never scored on config/odds.yaml
    if not (isinstance(report.get("odds"), dict) and report["odds"]):
        return {"error": "no odds snapshot"}
    cfg = config_from_snapshot(report["odds"])
    T_days = int(sc.get("T_days") or T_days)
    df = _load_daily_df(symbol, min_mtime=min_mtime)
    if df is None or df.empty:
        return {}
    from workflows.postcheck_execution import normalize_prices
    df, normalization = normalize_prices(df, report, str(symbol).upper(), PROJECT_ROOT)
    if normalization['status'] == 'unassessable':
        return {'error': normalization['reason'], 'price_normalization': normalization}
    import pandas as pd
    day_T = pd.Timestamp(T).tz_convert("UTC").normalize() if pd.Timestamp(T).tzinfo else pd.Timestamp(T, tz="UTC").normalize()
    after = df[df.index > day_T]
    if len(after) < T_days:
        return {"status": "immature", "bars_after_T": int(len(after)), "T_days": T_days}
    close_T = float(after.iloc[T_days - 1]["close"])
    date_T = str(after.index[T_days - 1])[:10]
    P0 = float(sc.get("P0") or 0) or None
    if not P0:
        for r in (report.get("reference_prices") or []):
            if isinstance(r, dict) and str(r.get("symbol") or "").upper() == str(symbol).upper() and float(r.get("price") or 0) > 0:
                P0 = float(r["price"])
    if not P0:
        return {"status": "no_P0"}
    y = close_T / P0 - 1.0
    out = {"status": "scored", "ruler": RULER_VERSION, "T_days": T_days, "valuation_date": date_T, "P0": P0, "close_T": close_T,
           "realized_return": round(y, 5), "nominal_coverage": float(cfg.get("coverage_nominal", 0.8))}

    def one(label: str, scen: Dict) -> Dict:
        fatal, _w, norm = validate_scenario(scen, cfg)
        if fatal:
            return {"label": label, "status": "invalid", "fatal": fatal[:2]}
        leaves = [(l["p"], l["P_T"] / P0 - 1.0) for l in expand_leaves(norm)]
        sco = _score(norm)
        lo, hi, cov = quantile_interval(leaves, float(cfg.get("coverage_nominal", 0.8)))
        return {"label": label, "status": "scored", "S_ref": sco["S_ref"], "p_up": round(sco["p_up"], 4),
                "crps": round(crps_discrete(leaves, y), 6),
                "brier_up": round((sco["p_up"] - (1.0 if y > 0 else 0.0)) ** 2, 5),
                "interval": [round(lo, 5), round(hi, 5)], "interval_hit": bool(lo <= y <= hi), "interval_mass": round(cov, 3),
                "upside_weight_realized": bool((sco["S_ref"] >= 50) == (y >= 0))}

    out["arbiter"] = one("arbiter_final", sc)
    if isinstance(sc.get("mixture"), dict) and out["arbiter"].get("status") == "scored":
        # C8: delta_crps = CRPS(A) − CRPS(A at w = ½), A at ½ rebuilt from mixture.z_prime / r_prime
        def _crps(scen: Dict) -> float:
            fatal, _w, norm = validate_scenario(scen, cfg)
            if fatal:
                raise ValueError("; ".join(fatal[:2]))
            return crps_discrete([(l["p"], l["P_T"] / P0 - 1.0) for l in expand_leaves(norm)], y)
        try:
            from scenario_replay import rebuild_at_half
            out["delta_crps"] = round(_crps(sc) - _crps(rebuild_at_half(sc)), 6)
        except Exception as exc:   # noqa: BLE001
            out["delta_crps_error"] = f"{type(exc).__name__}: {exc}"[:200]
    dissent = report.get("dissent") if isinstance(report.get("dissent"), dict) else {}
    for role, key in (("Zealot", "zealot_last"), ("Reaper", "reaper_last")):
        endorsed = dissent.get(f"{role.lower()}_endorsed_scenario")
        if isinstance(endorsed, dict):   # each side is scored on the scenario it endorsed (C8)
            out[key] = one(f"{role.lower()}_endorsed", endorsed)
            continue
        last = None
        for e in report.get("debate_history") or []:
            if e.get("role") == role:
                c = e.get("content")
                if isinstance(c, str):
                    try:
                        c = json.loads(c)
                    except Exception:
                        c = None
                if isinstance(c, dict) and isinstance(c.get("scenario"), dict):
                    last = c["scenario"]
        if last is not None:
            out[key] = one(key, last)
    # versioned baseline: three points at −σ / 0 / +σ, σ = ATR%14·√T from the report's opening snapshot
    atr = report.get("atr_pct_14")
    base = cfg.get("baseline") or {}
    if isinstance(atr, (int, float)) and atr > 0:
        sigma = float(atr) / 100.0 * (T_days ** 0.5)
        bl = [(float(pb), float(ns) * sigma) for ns, pb in zip(base.get("nodes_sigma") or [-1, 0, 1], base.get("probs") or [.25, .5, .25])]
        crps_b = crps_discrete(bl, y)
        out["baseline"] = {"sigma": round(sigma, 5), "crps": round(crps_b, 6)}
        if crps_b > 0 and out["arbiter"].get("status") == "scored":
            out["arbiter"]["skill"] = round((crps_b - out["arbiter"]["crps"]) / crps_b, 4)
            for key in ("zealot_last", "reaper_last"):
                if out.get(key, {}).get("status") == "scored":
                    out[key]["skill"] = round((crps_b - out[key]["crps"]) / crps_b, 4)
    else:
        out["baseline"] = {"status": str(base.get("zero_scale") or "skip"), "reason": "atr_pct_14 missing or zero on the report"}
    return out


def path_fit_block(symbol: str, T: datetime, report: Dict, dt: int, min_mtime: float = None,
                   event_dates: Optional[list] = None) -> Dict:
    """PF1 (2026-11-06, diagnostic only — never a score): how the realised daily closes sat inside the report's scenario
    tree, day by day up to min(dt, T_days) — directional PIT, central-90 / 50 coverage, breach persistence, normalised path
    CRPS and skill against a zero-drift Gaussian on the same ATR scale, authored open / gap_open / touch events checked
    against the realised open / high / low. scripts/path_fit.py; the duty loop's divergence.py is not reused. Reports
    without a scenario → {}; missing bars or ATR → a status block. Pre / post segments on the first event date inside
    the window (P0 / probabilities / ATR / t never reset)."""
    sc = report.get("scenario")
    if not isinstance(sc, dict):
        return {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import path_fit as _pf
    except Exception as exc:
        return {"status": "error", "error": f"path_fit unavailable: {exc}"[:200]}
    df = _load_daily_df(symbol, min_mtime=min_mtime)
    if df is None or df.empty:
        return {"status": "no_bars", "method_version": _pf.METHOD_VERSION, "diagnostic_only": True}
    from workflows.postcheck_execution import normalize_prices
    df, normalization = normalize_prices(df, report, str(symbol).upper(), PROJECT_ROOT)
    if normalization['status'] == 'unassessable':
        return {'status': 'unassessable', 'error': normalization['reason'], 'price_normalization': normalization}
    import pandas as pd
    day_T = pd.Timestamp(T).tz_convert("UTC").normalize() if pd.Timestamp(T).tzinfo else pd.Timestamp(T, tz="UTC").normalize()
    after = df[df.index > day_T]
    bars = []
    for idx, row in after.iterrows():
        bars.append({"day": str(idx)[:10], "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close")})
    atr = report.get("atr_pct_14")
    if not (isinstance(atr, (int, float)) and atr > 0):
        atr = (((sc.get("derived") or {}).get("unit_basis") or {}).get("atr_pct_14")) if isinstance(sc.get("derived"), dict) else None
    event_day = None
    for d in (event_dates or []):
        ds = str(d)[:10]
        hit = next((i + 1 for i, b in enumerate(bars) if str(b["day"]) >= ds), None)
        if hit is not None:
            event_day = hit
            break
    try:
        return _pf.path_fit(sc, atr, bars, int(dt), event_day=event_day)
    except Exception as exc:   # the reference block never blocks the settlement
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200], "method_version": _pf.METHOD_VERSION, "diagnostic_only": True}


def _primary_lens(report: Dict, tmap: Dict) -> Optional[str]:
    """3.4 GENERAL: the lens operation_target names (`<lens>=<level>`), else the odds snapshot's
    unit_basis.data_symbol (the lens whose price is the scenario's P0)."""
    if len(tmap) == 1:
        return next(iter(tmap)).upper()
    ub = ((report.get("odds") or {}).get("unit_basis") or {}) if isinstance(report.get("odds"), dict) else {}
    lens = ub.get("data_symbol") if isinstance(ub, dict) else None
    return str(lens).upper() if lens else None


def _lens_member_digest(m, report, entry, lw, T, dt, horizon, refs, snaps, valid_days, event_date, st_dir,
                        valid_until, stop_rule, st_targets, st_invalids, min_mtime):
    """v3.4.1 lens-wise report: settle lens `m` exactly as a GENERAL member, but from its own `by_lens[m]` entry
    (own level, own operation family, own scenario). No ruler bump, no per-lens score; weight = frozen config weight."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from lens_views import project_report
    lenses = [str(x).upper() for x in (report.get("lenses") or [])] or list(report["by_lens"].keys())
    try:
        proj = project_report(report, m, lenses)
        direction = _direction(proj)
        g_op = str(entry.get("operation_type") or "").upper()
        op_t = _first_float(entry.get("operation_target"))
        g_st = _short_term(report)
        tv, iv = st_targets.get(m), st_invalids.get(m)
        band = tv if isinstance(tv, tuple) else (iv if isinstance(iv, tuple) else None)
        st_t = None if band or not isinstance(tv, float) else tv
        inv = None if band or not isinstance(iv, float) else iv
        ex_m = entry.get("operation_execution") if isinstance(entry.get("operation_execution"), dict) else {}
        entries = ("LIMIT_ENTRY", "ENTRY") + (("MARKET_ENTRY",) if isinstance(ex_m.get("conditional"), dict) else ())
        entry_m = op_t if g_op in entries and op_t is not None else None
        trim_m = op_t if g_op == "TRIM_POSITION" and op_t is not None else None
        dm = _one_symbol_digest(m, T, dt, direction, (st_t if entry_m is not None else op_t), None,
                                min_mtime=min_mtime, entry=entry_m, trim=trim_m,
                                st_target=st_t, invalid=inv, st_band=band, horizon_days=horizon,
                                ref_price=refs.get(m), snapshot_price=snaps.get(m), valid_days=valid_days,
                                event_date=event_date, st_direction=st_dir, valid_until=valid_until,
                                stop_rule=stop_rule, conditional=ex_m.get("conditional"), execution_report=proj)
        if not dm:   # `_no_bars`: the caller decides immature (an operation lens) or an error member (a HOLD lens)
            return {"symbol": m, "error": "no bars for lens %s (not borrowed from another lens)" % m, "_no_bars": True}
        dm["symbol"] = m
        dm["role"] = "operation"
        dm["weight"] = lw.get(m)
        if int(dt) >= 15 and isinstance(proj.get("scenario"), dict):
            try:
                dm["forecast"] = forecast_scores(m, T, proj, min_mtime=min_mtime)
            except Exception as exc:   # scoring never blocks the settlement itself
                dm["forecast"] = {"error": str(exc)[:200]}
        if isinstance(proj.get("scenario"), dict):   # PF1 reference block on this lens's own bars (every tier)
            dm["path_fit"] = path_fit_block(m, T, proj, dt, min_mtime=min_mtime, event_dates=[event_date] if event_date else None)
        return dm
    except Exception as exc:   # G5: flagged, so the caller makes an operation lens's period immature
        return {"symbol": m, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "_error": True}


def _lens_has_operation(entry: Dict) -> bool:
    """A lens carries an operation when its by_lens entry is not HOLD_NEUTRAL, or it rests a stop / conditional."""
    if str(entry.get("operation_type") or "").upper() != "HOLD_NEUTRAL":
        return True
    ex = entry.get("operation_execution")
    return isinstance(ex, dict) and bool(ex.get("stop") or ex.get("conditional"))


def _event_dates(report: Dict, known_events: Optional[List[str]] = None) -> Tuple[List[str], str]:
    """(dates, source) for the event segmentation (2026-11-06, owner): a report with `next_review.events` (the new
    format) segments only by those events' dates ('events'; a window uses its start); an older report is projected —
    the issuer's actual release dates the caller found in PG ('projected') — and only failing that falls back to its
    `recommend_after` ('recommend_after_legacy', the pre-11-06 behaviour). Never recommend_after for a new report:
    it is the call time, written early, not an event."""
    nr = report.get("next_review") or {}
    evs = nr.get("events")
    if isinstance(evs, list):
        ds = sorted({str(e.get("date"))[:10] for e in evs if isinstance(e, dict) and e.get("date")
                     and re.match(r"^\d{4}-\d{2}-\d{2}", str(e.get("date")))})
        return ds, "events"
    ra = str(nr.get("recommend_after") or "")[:10]
    if known_events:   # projected release dates plus the legacy call time: segments() keeps the first inside the window
        return sorted({str(d)[:10] for d in known_events if d} | ({ra} if ra else set())), "projected+legacy"
    return ([ra] if ra else []), "recommend_after_legacy"


def compute_window_digest(symbol: str, as_of, dt: int, report: Dict,
                          min_mtime: float = None, prior_timeline: Dict = None,
                          known_events: Optional[List[str]] = None) -> Dict:
    """组装窗口量化摘要。普通标的=自己一套; GENERAL*=每个指数 ETF 各一套, members 列表喂 LLM。
    min_mtime: 结果价必须是本次新请求的(CSV mtime ≥ 它); 见 _load_daily_df。失败/无数据 → {}。
    prior_timeline: dt>3 时传入同源 3d 行的 touch_timeline → 在 short_term 裁定之前合入(Task 2.6 继承; R23 边界)。"""
    T = _to_dt(as_of)
    if T is None:
        return {}
    direction = _direction(report)  # GENERAL 的市场方向对各指数共享

    members = _GENERAL_MEMBERS.get(str(symbol).upper())
    g_st = _short_term(report)
    refs = {}
    for r in (report.get("reference_prices") or []):
        try:
            if isinstance(r, dict) and r.get("symbol") and float(r.get("price") or 0) > 0:
                refs[str(r["symbol"]).upper()] = float(r["price"])
        except (TypeError, ValueError):
            pass
    horizon = None
    try:
        horizon = int(float(g_st.get("horizon_days"))) if g_st.get("horizon_days") not in (None, "") else None
    except Exception:
        horizon = None
    # R24: 老报告起点价来源、限价单有效期(与 horizon 解绑)、事件日(next_review.recommend_after 落窗口内才分段)
    snaps = _snapshot_prices(report) if not refs else {}
    valid_days, _valid_src = _order_valid_days(report)
    valid_until = _order_valid_until(report, T)
    if valid_until:
        _valid_src = "report_date"
    stop_rule = _stop_rule(report)
    event_dates, event_source = _event_dates(report, known_events)
    event_date = [d for d in event_dates if d >= T.date().isoformat()] or None   # segments() picks the first inside the bars window
    st_dir = str(g_st.get("direction") or "")
    by_lens = report.get("by_lens") if isinstance(report.get("by_lens"), dict) else None
    if by_lens is not None:   # lens-wise (v3.4.1): the members are the report's frozen lenses, never _GENERAL_MEMBERS
        members = [str(x) for x in (report.get("lenses") or [])] or [str(x) for x in by_lens]
    if members:
        # GENERAL*: operation_target 是 "SPY=X; QQQ=Y; DIA=Z" / "DAX=X; IAEX=Y" 这类多价位串,
        # 给每个成员配各自目标; short_term 震荡时 "SPY=95-105" 是区间, 两沿都要 (R21 P1 同尺)。
        tmap = _parse_price_map(report.get("operation_target"))
        g_op = str(report.get("operation_type") or "").upper()
        st_targets = parse_target_map(g_st.get("target"))
        st_invalids = parse_target_map(g_st.get("invalid"))
        # 3.4: operation_target is "<lens>=<level>" on the primary lens only (the scenario's P0 is that lens's price);
        # the operation clears on that lens alone, the other lenses keep their path analysis (no entry / trim)
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from ruler_era import is_ruler_report
        ruler_era = is_ruler_report(report)   # a ruler report; pre-ruler GENERAL reports clear every lens
        op_lens = _primary_lens(report, tmap) if ruler_era else None
        out_members, counts = [], []
        lw = report.get("lens_weights_default") if isinstance(report.get("lens_weights_default"), dict) else {}
        for m in members:
            if by_lens is not None:
                if not isinstance(by_lens.get(m), dict):
                    out_members.append({"symbol": m, "error": "no by_lens entry for lens %s" % m})
                    continue
                if lw.get(m) is None:
                    out_members.append({"symbol": m, "error": "no lens_weights_default weight for lens %s" % m})
                    continue
                dm = _lens_member_digest(m, report, by_lens[m], lw, T, dt, horizon, refs, snaps, valid_days,
                                         event_date, st_dir, valid_until, stop_rule, st_targets, st_invalids,
                                         min_mtime)
                no_bars, err = dm.pop("_no_bars", False), dm.pop("_error", False)
                if (no_bars or err) and _lens_has_operation(by_lens[m]):
                    # G5: an operation lens never leaves the period scored without it. no_bars = no fresh result price
                    # (retried next run); error = a program error on that lens (named with its exception)
                    if no_bars:
                        logging.warning(f"[postcheck_quant] {symbol}: lens {m} carries {by_lens[m].get('operation_type')} "
                                        f"but has no fresh bars (no_bars) — digest immature, period skipped (retry)")
                    else:
                        logging.warning(f"[postcheck_quant] {symbol}: lens {m} carries {by_lens[m].get('operation_type')} "
                                        f"and failed with a program error (error: {dm.get('error')}) — digest immature, "
                                        f"period skipped")
                    return {}
                out_members.append(dm)
                if "error" not in dm:
                    counts.append(dm.get("bars_in_window", 0))
                continue
            tv, iv = st_targets.get(m), st_invalids.get(m)
            band = tv if isinstance(tv, tuple) else (iv if isinstance(iv, tuple) else None)
            st_t = None if band or not isinstance(tv, float) else tv
            inv = None if band or not isinstance(iv, float) else iv
            if ruler_era:
                op_t = tmap.get(m) if m == op_lens else None
            else:
                op_t = tmap.get(m) if tmap.get(m) is not None else st_t
            # 2026-10-17 晚: 大盘场的条件动作也按镜头并排清算——挂单价 / 减仓价来自 operation_target 的该镜头价位(区间不算挂单价)
            entry_m = op_t if g_op in ("LIMIT_ENTRY", "ENTRY") and isinstance(op_t, float) else None
            trim_m = op_t if g_op == "TRIM_POSITION" and isinstance(op_t, float) else None
            analysis_only = ruler_era and m != op_lens   # 3.4: path analysis against the short-term target, no operation
            g_ex = report.get("operation_execution") if isinstance(report.get("operation_execution"), dict) else {}
            dm = _one_symbol_digest(m, T, dt, direction, (st_t if entry_m is not None or analysis_only else op_t), None,
                                    min_mtime=min_mtime,
                                    entry=entry_m, trim=trim_m,
                                    st_target=st_t, invalid=inv, st_band=band, horizon_days=horizon,
                                    ref_price=refs.get(m), snapshot_price=snaps.get(m), valid_days=valid_days,
                                    event_date=event_date, st_direction=st_dir, valid_until=valid_until, stop_rule=stop_rule,
                                    conditional=g_ex.get("conditional") if ruler_era and m == op_lens else None,
                                    execution_report=report if m == op_lens else None)
            if dm:
                dm["symbol"] = m
                if ruler_era:
                    dm["role"] = "operation" if m == op_lens else "analysis"
                out_members.append(dm)
                counts.append(dm.get("bars_in_window", 0))
        if not out_members or (by_lens is not None and not counts):
            return {}
        g_flag = ({"dir_op_conflict": {"direction": str(g_st.get("direction")),
                                       "operation": str(report.get("operation_type"))}}
                  if _dir_op_conflict(g_st.get("direction"), report.get("operation_type")) else {})
        if by_lens is not None:   # lens-wise (v3.4.1): every lens settled from its own entry; forecasts sit on the members
            g_flag["primary_lens"] = op_lens
            g_flag["lenswise"] = True
            g_flag["session_fields_scope"] = "primary lens (direction_call, dir_op_conflict); each member carries its own"
            g_flag["session_score_weights"] = dict(lw)
        elif ruler_era:
            g_flag["primary_lens"] = op_lens
            if isinstance(report.get("scenario"), dict) and int(dt) >= 15:
                try:   # the scenario's P0 is the primary lens's price: score the forecast on that lens's bars
                    g_flag["forecast"] = (forecast_scores(op_lens, T, report, min_mtime=min_mtime) if op_lens
                                          else {"error": "GENERAL 3.4 report without a primary lens"})
                except Exception as exc:   # scoring never blocks the settlement itself
                    g_flag["forecast"] = {"error": str(exc)[:200]}
            if isinstance(report.get("scenario"), dict) and op_lens:   # PF1 reference block (every tier)
                g_flag["path_fit"] = path_fit_block(op_lens, T, report, dt, min_mtime=min_mtime, event_dates=event_dates)
        return {
            **g_flag,
            "ruler": RULER_VERSION,
            "event_dates": event_dates, "event_source": event_source,
            "order_valid_source": _valid_src,
            "stop_rule": stop_rule[0] if stop_rule[0] == "touch" else f"close x{stop_rule[1]}",
            "general": True,
            "symbol": str(symbol).upper(),
            "direction_call": {1: "看多", -1: "看空", 0: "中性"}[direction],
            "members": out_members,                 # 三套(或两/一套)拼成 list, 喂给 LLM
            "win_start": next((x for x in out_members if "error" not in x), {}).get("win_start", ""),
            "win_end": next((x for x in out_members if "error" not in x), {}).get("win_end", ""),
            "bars_in_window": min(counts) if counts else 0,
        }

    # 普通标的: 自己的价格 + 单一目标/止损
    # LIMIT_ENTRY 语义拆分 (Task 2.5): operation_target 是挂单价 → 判"接没接到"(entry),
    # 方向目标退回 short_term.target; 其余操作类型维持原目标语义
    op_type = str(report.get("operation_type") or "").upper()
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from ruler_era import is_ruler_report
    s_ex = report.get("operation_execution") if isinstance(report.get("operation_execution"), dict) else {}
    s_cond = s_ex.get("conditional") if is_ruler_report(report) and isinstance(s_ex.get("conditional"), dict) else None
    # G4: a ruler report's conditional is settled by its observe rule (pre-ruler untouched); MARKET_ENTRY only with one
    entries = ("LIMIT_ENTRY", "ENTRY") + (("MARKET_ENTRY",) if s_cond else ())
    entry = _first_float(report.get("operation_target")) if op_type in entries else None
    trim = _first_float(report.get("operation_target")) if op_type == "TRIM_POSITION" else None
    if entry is not None:
        target = _first_float(_short_term(report).get("target"))
    else:
        target = _first_float(report.get("operation_target")) or _first_float(
            _short_term(report).get("target"))
    st = _short_term(report)
    stop = _stop_price(report, entry, ref=_first_float(report.get("operation_target")) or refs.get(str(symbol).upper()))
    st_band = _range_pair(st.get("target")) or _range_pair(st.get("invalid"))
    st_target = None if st_band else _first_float(st.get("target"))
    invalid = None if (st_band and _range_pair(st.get("invalid"))) else _first_float(st.get("invalid"))
    dg = _one_symbol_digest(symbol, T, dt, direction, target, stop,
                            min_mtime=min_mtime, entry=entry,
                            st_target=st_target, invalid=invalid, trim=trim,
                            st_band=st_band, horizon_days=horizon,
                            ref_price=refs.get(str(symbol).upper()),
                            prior_timeline=prior_timeline,
                            snapshot_price=snaps.get(str(symbol).upper()), valid_days=valid_days,
                            event_date=event_date, st_direction=st.get("direction"),
                            valid_until=valid_until, stop_rule=stop_rule, conditional=s_cond, execution_report=report)
    if dg:
        dg["ruler"] = RULER_VERSION
        dg["order_valid_source"] = _valid_src
        if valid_until:
            dg["order_valid_until"] = valid_until
        dg.setdefault("stop_rule", stop_rule[0] if stop_rule[0] == "touch" else f"close x{stop_rule[1]}")
    if dg and _dir_op_conflict(st.get("direction"), report.get("operation_type")):
        dg["dir_op_conflict"] = {"direction": str(st.get("direction")),
                                 "operation": str(report.get("operation_type"))}
    if dg and isinstance(report.get("scenario"), dict) and int(dt) >= 15:
        try:
            dg["forecast"] = forecast_scores(symbol, T, report, min_mtime=min_mtime)
        except Exception as exc:   # scoring never blocks the settlement itself
            dg["forecast"] = {"error": str(exc)[:200]}
    if dg and isinstance(report.get("scenario"), dict):   # PF1 path-fit reference block (every tier; diagnostic only)
        dg["path_fit"] = path_fit_block(symbol, T, report, dt, min_mtime=min_mtime, event_dates=event_dates)
    if dg:
        dg["event_dates"], dg["event_source"] = event_dates, event_source
    return dg


if __name__ == "__main__":
    # 核心数学单测 (合成 bars, 不碰文件/PG)
    bars = [
        {"date": "2026-06-19", "high": 105, "low": 99, "close": 104},
        {"date": "2026-06-20", "high": 110, "low": 103, "close": 108},
        {"date": "2026-06-23", "high": 112, "low": 96, "close": 98},
    ]
    # 看多, 入场100, 目标110, 止损95
    s = window_stats(bars, price_T=100.0, direction=1, target=110.0, stop=95.0)
    assert s["window_high"] == 112 and s["window_low"] == 96, s
    assert s["close_end"] == 98 and s["ret_pct"] == -2.0, s
    assert s["mfe_pct"] == 12.0 and s["mae_pct"] == -4.0, s   # 看多: 最高+12%, 最低-4%
    assert s["touched_target"] is True, s                     # 摸到110
    assert s["touched_stop"] is False, s                      # 最低96 没破95
    # B0 回归: 中性 direction=0 时旧版 OR 分支恒 True 的错报
    s2 = window_stats(bars, price_T=100.0, direction=0, target=115.0, stop=90.0)
    assert s2["touched_target"] is False, s2   # 高点112没到115; 旧版因 win_low 96<=115 错报 True
    assert s2["touched_stop"] is False, s2     # 低点96没破90
    # 中性 mfe/mae 反镜像回归 (2026-09-26): 下行(-4%)大于上行才暴露, 用重心下移的窗口验
    sb = window_stats([{"date": "d1", "high": 103, "low": 82, "close": 85}],
                      price_T=100.0, direction=0, target=None, stop=None)
    assert sb["mfe_pct"] == 3.0 and sb["mae_pct"] == -18.0, sb  # 旧版会报 mfe=18/mae=-18 镜像
    s3 = window_stats(bars, price_T=100.0, direction=0, target=97.0, stop=None)
    assert s3["touched_target"] is True, s3    # 下方目标97, win_low 96 触及
    s4 = window_stats(bars, price_T=100.0, direction=-1, target=95.0, stop=113.0)
    assert s4["touched_target"] is False, s4   # 看空目标95, 低点96未到
    assert s4["touched_stop"] is False, s4     # 上方止损113, 高点112未到
    # 限价接单判定 (Task 2.5): 低吸挂 97, 窗口低点 96 → 6/23 接到, 收 98 → +1.03%
    ef = entry_fill_stats(bars, price_T=100.0, entry=97.0)
    assert ef["entry_price_reached"] is True and ef["reached_date"] == "2026-06-23", ef
    assert ef["post_reach_ret_pct"] == 1.03, ef
    # R21 挂深三字段: 挂97=挂深3%; 窗口日均振幅 (6/104+7/108+16/98)/3 ≈ 9.53% → 比值0.31
    assert ef["entry_depth_pct"] == 3.0 and ef["entry_depth_range_ratio"] == 0.31, ef
    assert "entry_gap_pct" not in ef, ef       # 成交单无差口
    ef2 = entry_fill_stats(bars, price_T=100.0, entry=95.0)
    assert ef2["entry_price_reached"] is False, ef2   # 挂 95 没等到 (低点96)
    assert ef2["entry_depth_pct"] == 5.0 and ef2["entry_gap_pct"] == 1.0, ef2  # 差口1个点
    assert ef2["entry_depth_range_ratio"] == 0.52, ef2
    ef3 = entry_fill_stats(bars, price_T=100.0, entry=111.0)
    assert ef3["entry_price_reached"] is True and ef3["reached_date"] == "2026-06-23", ef3  # 突破挂单 111, 高点112触发
    assert ef3["entry_depth_pct"] == -11.0, ef3  # 负=挂在上方(突破单)
    # R21 卖侧镜像: TRIM 执行价挂 115 没卖到(窗口高点 112), 差口 3 个点
    tx = {k.replace("entry_", "exec_"): v
          for k, v in entry_fill_stats(bars, price_T=100.0, entry=115.0).items()}
    assert tx["exec_price_reached"] is False and tx["exec_gap_pct"] == 3.0, tx
    assert tx["exec_depth_pct"] == -15.0, tx
    # 撞线排班 (Task 2.6): 5m 级 bars, 先接单后打止损, 目标没到
    ibars = [
        {"date": "2026-07-08 09:35", "high": 101.0, "low": 96.5, "close": 100.5},   # 触 entry 97
        {"date": "2026-07-08 12:00", "high": 103.0, "low": 98.0, "close": 102.0},
        {"date": "2026-07-09 09:35", "high": 99.0,  "low": 89.0, "close": 91.0},    # 触 stop 90
    ]
    # R21 P4 旗: 方向×操作拧着
    assert _dir_op_conflict("上行", "TRIM_POSITION") is True
    assert _dir_op_conflict("下行", "LIMIT_ENTRY") is True
    assert _dir_op_conflict("上行", "LIMIT_ENTRY") is False
    assert _dir_op_conflict("震荡", "TRIM_POSITION") is False
    assert _dir_op_conflict(None, "HOLD_NEUTRAL") is False
    # R21 P1: 区间解析 + 两沿排班
    assert _range_pair("393-405") == (393.0, 405.0)
    assert _range_pair("405-393") == (393.0, 405.0)
    assert _range_pair("247.86") is None and _range_pair(None) is None
    tlb = touch_timeline(bars, price_T=100.0, lines={"st_low": 97.0, "st_high": 111.0})
    assert {e["line"] for e in tlb["events"]} == {"st_low", "st_high"}, tlb
    tl = touch_timeline(ibars, price_T=100.0, lines={"entry": 97.0, "stop": 90.0, "target": 115.0})
    assert [e["line"] for e in tl["events"]] == ["entry", "stop"], tl
    assert tl["events"][0]["elapsed_td"] == 0.5, tl      # 第1天2根bar的第1根
    assert tl["events"][1]["elapsed_td"] == 2.0, tl      # 第2天唯一一根
    assert tl["untouched"] == ["target"], tl
    assert not tl["events"][0].get("same_bar_ambiguous"), tl
    # 同 bar 双线首触 → 顺序不可判
    tl2 = touch_timeline([{"date": "2026-07-08", "high": 112.0, "low": 89.0, "close": 100.0}],
                         price_T=100.0, lines={"stop": 90.0, "target": 110.0})
    assert all(e.get("same_bar_ambiguous") for e in tl2["events"]), tl2
    # 排班拼接: 3d 精细段优先, 粗分辨率补余
    prior = {"events": [{"line": "entry", "price": 97.0, "at": "2026-07-08 09:35", "elapsed_td": 0.5}],
             "untouched": ["stop", "target"], "resolution": "5m"}
    coarse = {"events": [{"line": "entry", "price": 97.0, "at": "2026-07-09", "elapsed_td": 1.0},
                         {"line": "target", "price": 115.0, "at": "2026-07-15", "elapsed_td": 5.0}],
              "untouched": ["stop"], "resolution": "daily"}
    mg = merge_timelines(prior, coarse)
    assert [(e["line"], e["elapsed_td"]) for e in mg["events"]] == [("entry", 0.5), ("target", 5.0)], mg
    assert mg["events"][0].get("inherited_3d") is True and not mg["events"][1].get("inherited_3d"), mg
    assert mg["untouched"] == ["stop"], mg
    # 路径事实单列 (2026-09-06): 最大回撤=滚动峰到谷, 穿越=close 相对 price_T 换边计数
    assert s["max_drawdown_pct"] == -14.29, s   # run_max 112, low 96 → (96-112)/112
    assert s["cross_T_count"] == 1, s           # closes 104,108,98 vs 100: 上→上→下 换边一次
    # 撞线分列 (2026-09-06): st_target/invalid 是独立线名, 与操作锚 target/stop 并排
    tl3 = touch_timeline(ibars, price_T=100.0,
                         lines={"target": 103.5, "st_target": 115.0, "invalid": 90.0})
    assert [e["line"] for e in tl3["events"]] == ["invalid"], tl3   # 90 被 7/09 low 89 触及
    assert tl3["untouched"] == ["st_target", "target"], tl3        # 103.5/115 都没到 (高点103)
    assert _first_float("SPY=745.0; QQQ=20660") == 745.0
    assert _first_float("N/A") is None
    assert len(_sample_path(bars)) == 3
    pm = _parse_price_map("SPY=745.0; QQQ=20660; DIA=445.5")
    assert pm == {"SPY": 745.0, "QQQ": 20660.0, "DIA": 445.5}, pm
    assert _parse_price_map("N/A") == {}
    # 时段标签 (2026-09-10): 美股全时段形状 → 事件带 session 短词; 欧亚本地形状 → 整场不贴
    def _utc(h, mi):
        return datetime(2026, 9, 2, h, mi, tzinfo=timezone.utc)
    eth_bars = [  # 9 月=EDT: 08:00Z=pre 4:00 / 14:00Z=rth 10:00 / 20:30Z=post 16:30
        {"date": _utc(8, 0), "high": 99.0, "low": 96.0, "close": 98.0},
        {"date": _utc(14, 0), "high": 103.0, "low": 98.0, "close": 102.0},
        {"date": _utc(20, 30), "high": 106.0, "low": 101.0, "close": 105.0},
    ]
    tl4 = touch_timeline(eth_bars, price_T=100.0, lines={"entry": 97.0, "target": 105.5})
    s4 = {e["line"]: e.get("session") for e in tl4["events"]}
    assert s4 == {"entry": "pre", "target": "post"}, tl4
    xetra_bars = [  # 欧洲形状: 07:30Z=overnight 3:30 / 10:00Z=pre 6:00 / 15:00Z=rth 11:00, 无 post
        {"date": _utc(7, 30), "high": 99.0, "low": 96.0, "close": 98.0},
        {"date": _utc(10, 0), "high": 103.0, "low": 98.0, "close": 102.0},
        {"date": _utc(15, 0), "high": 106.0, "low": 101.0, "close": 105.0},
    ]
    tl5 = touch_timeline(xetra_bars, price_T=100.0, lines={"entry": 97.0, "target": 105.5})
    assert all("session" not in e for e in tl5["events"]), tl5
    # 字符串日期(日线)兼容: 判不出时段, 不贴不炸
    assert "session" not in touch_timeline([{"date": "2026-07-08", "high": 112.0, "low": 89.0,
                                             "close": 100.0}], 100.0, {"stop": 90.0})["events"][0]
    print("postcheck_quant self-check passed (window_stats + multi-level parsing + session tags)")
