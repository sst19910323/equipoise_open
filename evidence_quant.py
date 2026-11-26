#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence_quant.py — 议会 v2 数据腿统一入口(七固定模式, 机械逻辑, 拒绝 AI 临时编排)。

用法:
    python evidence_quant.py '{"mode":"peer_compare","symbol":"NVDA","window_days":20}'
    python evidence_quant.py --selftest

模式(枚举外一律拒绝):
    peer_compare       同业组同窗口相对表现 (读 config/evidence_map.yaml 的 peers)
    bars               指定标的日线序列
    relative_strength  两标的相对强弱 (banned_benchmarks 循环引用直接拒绝)
    econ_series        区域经济指标 (读 data/economic 水合转运文件)
    event_calendar     窗口内已知二元事件 (读 config/event_calendar.yaml, B5 闸门数据源)
    pair_link          任意两标的联动: 日收益 OLS(beta/alpha/R²/残差) + 滚动相关 vs 历史百分位
    trend_fit          任意单标的趋势质量: 对数价格线性拟合, 长短双窗年化斜率 + R²

数据源: data/market_data/<SYM>/<SYM>_*_1d_*.csv (mtime 最新) — 备餐拉的临时运输产物。
输出: 单个 JSON 到 stdout: {"ok":true,"mode":..,"data":..} 或 {"ok":false,"error":..}。
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
ALLOWED = ["peer_compare", "bars", "relative_strength", "econ_series", "event_calendar",
           "pair_link", "trend_fit"]

# 每模式合法键(schema 固化): 键外一律拒绝并回显 schema; 常见错名先走 ALIASES 归一。
SCHEMAS = {
    "peer_compare":      {"required": ["symbol"], "optional": ["window_days"]},
    "bars":              {"required": ["symbol"], "optional": ["lookback_days", "interval", "date", "as_of"]},
    "relative_strength": {"required": ["symbol", "benchmark"], "optional": ["window_days"]},
    "econ_series":       {"required": ["region", "indicator"], "optional": []},
    "event_calendar":    {"required": [], "optional": ["symbol", "horizon_days"]},
    "pair_link":         {"required": ["symbol", "counterpart"], "optional": ["window_days"]},
    "trend_fit":         {"required": ["symbol"], "optional": ["window_days"]},
}
ALIASES = {
    "peer_compare":      {"lookback_days": "window_days"},
    "bars":              {"window_days": "lookback_days"},
    "relative_strength": {"lookback_days": "window_days", "comparison_symbol": "benchmark",
                          "benchmark_symbol": "benchmark", "vs": "benchmark"},
    "econ_series":       {"country": "region"},
    "event_calendar":    {"window_days": "horizon_days"},
    "pair_link":         {"lookback_days": "window_days", "benchmark": "counterpart",
                          "comparison_symbol": "counterpart", "benchmark_symbol": "counterpart",
                          "vs": "counterpart"},
    "trend_fit":         {"lookback_days": "window_days"},
}
STALE_DAYS = 6  # 最后 bar 距今超过这个天数 → 视为过期(先尝试 live-fetch, 不行就带警告)
# 演习(PTR)时钟: 环境变量 EQUIPOISE_AS_OF=<YYYYMMDDTHHMMSSZ> → 只给那一刻已知的数据(日线只到前一天、"今天"=那天、不现拉、
# 经济文件取不晚于它的、盘中截到它); 不设 = 生产行为不变。备餐文件比开场时刻晚几分钟落盘, 经济文件留这个余量。
PREP_SLACK_MIN = 15


def _drill_as_of():
    """EQUIPOISE_AS_OF → 带 UTC 时区的 datetime; 不设 → None; 设了却解析不了 → 抛错(时钟错了不能静默放行未来数据)。"""
    raw = os.environ.get("EQUIPOISE_AS_OF", "").strip()
    if not raw:
        return None
    d = _parse_utc(raw)
    if d is None:
        raise ValueError(f"EQUIPOISE_AS_OF could not be parsed: {raw!r} (write UTC, e.g. 20261009T133700Z)")
    return d


def _today():
    from datetime import date
    a = _drill_as_of()
    return a.date() if a else date.today()


def _schema_str(mode: str) -> str:
    s = SCHEMAS[mode]
    return (f"mode={mode} valid keys: required {s['required'] or 'none'}, optional {s['optional'] or 'none'}"
            f" (mode/type may also be present; every other key is rejected)")


def _normalize(mode: str, req: dict):
    """别名归一 + 未知键拒绝 + 值归一(symbol 大写)。返回 (norm, error_dict|None)。"""
    s, al = SCHEMAS[mode], ALIASES.get(mode, {})
    norm, changed, unknown = {"mode": mode}, False, []
    for k, v in req.items():
        if k in ("mode", "type"):
            continue
        if k in al and al[k] not in req:
            norm[al[k]] = v
            changed = True
        elif k in s["required"] or k in s["optional"]:
            norm[k] = v
        else:
            unknown.append(k)
    if unknown:
        return None, {"ok": False,
                      "error": f"unknown parameters {unknown}; {_schema_str(mode)}",
                      "allowed_modes": ALLOWED}
    missing = [k for k in s["required"] if k not in norm]
    if missing:
        return None, {"ok": False,
                      "error": f"missing required parameters {missing}; {_schema_str(mode)}",
                      "allowed_modes": ALLOWED}
    for k in ("symbol", "benchmark", "counterpart", "region"):
        if isinstance(norm.get(k), str):
            up = norm[k].strip().upper()
            changed = changed or up != norm[k]
            norm[k] = up
    if changed:
        norm["_normalized"] = True
    return norm, None


# ---------------------------------------------------------------------------
# 底层取数 (机械, 无判断)
# ---------------------------------------------------------------------------

def _latest_csv(sym: str):
    fs = glob.glob(os.path.join(ROOT, "data", "market_data", sym, f"{sym}_*_1d_*.csv"))
    return max(fs, key=os.path.getmtime) if fs else None


def _bars(sym: str, n: int):
    """CSV 尾部 n 行 → [{date, close, high, low[, volume]}]; 无文件/无有效行 → None (gap)。
    volume 只在 CSV 有这一列且解析得出时带上(P05 的量能判据要用; 没有就如实缺, 不补零)。"""
    p = _latest_csv(sym)
    if not p:
        return None
    with open(p, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    a = _drill_as_of()
    if a is not None:  # 截在取尾部之前: as_of 当天及以后的日线一律不给(当天那根开场时还没走完)
        rows = [r for r in rows if (_date_of(r) or a.date()) < a.date()]
    out = []
    for r in rows[-n:]:
        k = {c.lower(): c for c in r}
        try:
            bar = {"date": r[k.get("date", "date")],
                   "close": float(r[k["close"]]),
                   "high": float(r[k.get("high", "close")]),
                   "low": float(r[k.get("low", "close")])}
        except (KeyError, ValueError):
            continue
        try:
            if "volume" in k and str(r[k["volume"]]).strip() != "":
                bar["volume"] = float(r[k["volume"]])
        except ValueError:
            pass
        try:
            if "open" in k and str(r[k["open"]]).strip() != "":
                bar["open"] = float(r[k["open"]])
        except ValueError:
            pass
        out.append(bar)
    return out or None


def _parse_bar_date(value):
    """bar date 字符串 → date (支持 ISO / YYYYMMDD / 带时间), 解析不了返回 None。"""
    from datetime import date
    raw = str(value).strip()[:10].replace(" ", "")
    try:
        if len(raw) == 8 and raw.isdigit():
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _date_of(row: dict):
    k = {c.lower(): c for c in row}
    return _parse_bar_date(row.get(k.get("date", "date"), ""))


def _last_bar_date(bars):
    return _parse_bar_date(bars[-1]["date"])


def _is_stale(bars) -> bool:
    d = _last_bar_date(bars)
    return d is not None and (_today() - d).days > STALE_DAYS


def _live_fetch(sym: str, need_days: int):
    """本地缺/过期时现连盈透拉一次日线, 写 live 转运 CSV, 返回文件路径或 None。

    只在 EQUIPOISE_IBKR_PORT 在环境时尝试(服务器跑盘命令必带); EQUIPOISE_EVIDENCE_LIVE_FETCH=0 强制关。
    Codex 沙箱无网络 → connect 秒挂 → 返回 None, 行为退化为现状(gap)。
    symbol_contracts 有登记按登记合约, 没登记默认 STK/SMART/USD(美股假设); 失败一律 None。
    """
    if os.environ.get("EQUIPOISE_EVIDENCE_LIVE_FETCH", "1") == "0" or _drill_as_of() is not None:
        return None
    port = os.environ.get("EQUIPOISE_IBKR_PORT")
    if not port:
        return None
    try:
        from ib_insync import IB, Contract, Stock
        ib = IB()
        ib.connect("127.0.0.1", int(port), clientId=150 + os.getpid() % 40, timeout=15)
        try:
            cfg_p = os.path.join(ROOT, "config", "symbols.yaml")
            c_info = ((yaml.safe_load(open(cfg_p, encoding="utf-8")) or {})
                      .get("symbol_contracts") or {}).get(sym) if os.path.exists(cfg_p) else None
            if c_info:
                c = Contract(symbol=c_info.get("ibkr_ticker") or c_info["symbol"],
                             secType=c_info["sec_type"], exchange=c_info["exchange"],
                             currency=c_info["currency"])
            else:
                c = Stock(sym, "SMART", "USD")
            q = ib.qualifyContracts(c)
            if not q:
                return None
            days = min(max(need_days * 2, 90), 1825)  # pair_link/trend_fit 要 5 年纵深, 旧模式仍 ≤365
            dur = f"{days} D" if days <= 365 else f"{math.ceil(days / 365)} Y"
            bars = ib.reqHistoricalData(q[0], endDateTime="", durationStr=dur,
                                        barSizeSetting="1 day", whatToShow="TRADES",
                                        useRTH=True, formatDate=1)
            if not bars:
                return None
            import time as _t
            d = os.path.join(ROOT, "data", "market_data", sym)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"{sym}_live_1d_{_t.strftime('%Y%m%dT%H%M%SZ', _t.gmtime())}.csv")
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close", "volume"])
                for b in bars:
                    w.writerow([b.date, b.open, b.high, b.low, b.close, b.volume])
            return p
        finally:
            ib.disconnect()
    except Exception as e:
        # 盈透现拉失败: 行为仍退化为 gap/stale 兜底(返回 None), 但把真实错误落 stderr 留痕,
        # 免得连接失败(拒连/超时/qualify 失败)被彻底吞掉、报告侧看不出是盈透的问题。
        print(f"[evidence_quant._live_fetch] {sym} 盈透现拉失败: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _bars_fresh(sym: str, n: int):
    """_bars + 过期/缺失时 live-fetch 兜底。返回 (bars|None, warning|None)。"""
    b = _bars(sym, n)
    if b is not None and not _is_stale(b):
        return b, None
    if _live_fetch(sym, n):
        nb = _bars(sym, n)
        if nb is not None:
            return nb, None
    if b is None:
        return None, None
    return b, (f"{sym} data is stale (last bar {b[-1]['date']}; live fetch failed or disabled); "
               f"state the as-of time when citing")


def _stats(bars):
    c = [b["close"] for b in bars]
    ret = (c[-1] / c[0] - 1) * 100
    peak, mdd = c[0], 0.0
    for x in c:
        peak = max(peak, x)
        mdd = min(mdd, (x / peak - 1) * 100)
    rets = [(c[i] / c[i - 1] - 1) for i in range(1, len(c))]
    vol = (sum(r * r for r in rets) / max(len(rets), 1)) ** 0.5 * math.sqrt(252) * 100
    return {"ret_pct": round(ret, 2), "max_dd_pct": round(mdd, 2), "ann_vol_pct": round(vol, 1),
            "first_date": str(bars[0]["date"]), "last_date": str(bars[-1]["date"]),
            "n_bars": len(bars)}


def _emap() -> dict:
    p = os.path.join(ROOT, "config", "evidence_map.yaml")
    return yaml.safe_load(open(p, encoding="utf-8")) or {} if os.path.exists(p) else {}


# ---------------------------------------------------------------------------
# 七模式
# ---------------------------------------------------------------------------

def peer_compare(req: dict) -> dict:
    sym, win = req["symbol"], int(req.get("window_days", 20))
    m = _emap().get(sym)
    if not m:
        return {"ok": False,
                "error": (f"{sym} has no evidence-map entry (no peer group registered in config/evidence_map.yaml). "
                          f"Broad-market and broad-based ETFs have no peer group by nature; do not request peer_compare again — for a cross-section use "
                          f"relative_strength against a specific third symbol; qualitative benchmarking goes through the Scout search leg")}
    out, gaps, warns = {}, [], []
    for s in [sym] + list(m.get("peers", [])):
        b, w = _bars_fresh(s, win)
        if b:
            out[s] = _stats(b)
            if w:
                warns.append(w)
        else:
            gaps.append(s)
    data = {"window_days": win, "stats": out, "gaps": gaps, "notes": m.get("notes", "")}
    if warns:
        data["stale_warnings"] = warns
    # 2026-08-12: 新上市标的(SPCX 只有 42 根日线)请求 60 日窗口时, 自己只能给 42 根、同业给足 60 根,
    # 起点日期不同 → ret_pct 不是同一窗口的数, 直接横向比会读成"跑输 N 个点"。窗口不齐就明说。
    nbars = {s: st.get("n_bars") for s, st in out.items()}
    if len(set(nbars.values())) > 1:
        data["window_mismatch_warning"] = (
            "actual windows differ across symbols (short listing history / trading halt): "
            + ", ".join(f"{s}={st.get('n_bars')} bars from {str(st.get('first_date'))[:10]}"
                        for s, st in out.items())
            + " — ret_pct/max_dd over different windows cannot be subtracted directly as 'relative strength'; to compare, shrink window_days "
              "to the shortest one, or use relative_strength instead (it aligns both windows automatically).")
    return {"ok": True, "mode": "peer_compare", "data": data}


# ---------------------------------------------------------------------------
# 盘中档(bars 的 interval=1h / 5m): 只读备餐已经落下的文件, 不现拉; 时段按交易所日历判
# ---------------------------------------------------------------------------

INTRADAY = {"1h": {"minutes": 60, "default_days": 5, "max_days": 10},   # 小时线: 最近几个交易日的日内节奏
            "5m": {"minutes": 5}}                                        # 5 分钟线: 一次一个交易日
INTRADAY_FRESH_HOURS = {"5m": 2, "1h": 24}   # 带 as_of 时, 文件须是本场时点附近备的; 文件名时刻 = 末根 bar 时刻。
# 1h(V16, 2026-10-25): 小时线只拉常规时段, 盘前/盘后开场时末根停在上一常规收盘, 距 as_of 可到十几小时, 所以 1h 的"鲜"放宽到一个
# 交易日; 5m 仍两小时。
OFF_SESSION_QUARANTINE = 0.10  # 非美: 交易时段之外的 bar 占比超过这个参考值 → 来源存疑, 整份不返回
# 盘外 bar 的来源已经核实的市场: 丢掉盘外的并在回执里写明, 不整份拒绝。
# 东证个股(2026-10-02 向盈透核对): 账户没有东证的行情权限, 数据走通用路由, 里面混有场外交易系统 Japannext 的
# 日间与夜间时段; 盈透的"只取常规时段"开关对这类合约的历史 bar 不起作用; 合约与时间戳无误, 盘外成交量极小。
KNOWN_OFF_SESSION = {"TSEJ": "off-exchange trading system (Japannext) sessions, removed"}


def _parse_utc(s):
    """'20261001T115100Z' 或 ISO 字符串 → 带 UTC 时区的 datetime; 解析不了 → None。"""
    from datetime import datetime, timezone
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _intraday_file(sym: str, tag: str, as_of):
    """挑盘中文件: 文件名末段是它的数据时刻。带 as_of 时取不晚于它的最新一份, 且须在它附近; 不带就取最新。
    返回 (路径, 文件时刻, 原因)。原因: '' / 'none'(没有文件) / 'stale'(有文件但不是本场时点附近备的)。"""
    cands = []
    for p in glob.glob(os.path.join(ROOT, "data", "market_data", sym, f"{sym}_*_{tag}_*.csv")):
        ts = _parse_utc(os.path.basename(p).rsplit("_", 1)[-1].replace(".csv", ""))
        if ts is not None:
            cands.append((ts, p))
    if not cands:
        return None, None, "none"
    if as_of is None:
        ts, p = max(cands)
        return p, ts, ""
    ok = [c for c in cands if c[0] <= as_of]
    if not ok:
        return None, None, "stale"
    ts, p = max(ok)
    if (as_of - ts).total_seconds() > INTRADAY_FRESH_HOURS.get(tag, 2) * 3600:
        return None, ts, "stale"
    return p, ts, ""


def _session_table(clock: dict, d0, d1) -> dict:
    """交易所日历 → {本地日期: (开盘, 收盘, 午休起, 午休止)}, 都是 UTC; 没有午休的市场后两项为 None。"""
    from analysis.technical_snapshot_builder import get_calendar, session_break
    cal = get_calendar(clock["calendar"])
    sched = cal.schedule(start_date=d0, end_date=d1)
    out = {}
    for i, (day, row) in enumerate(sched.iterrows()):
        brk = session_break(cal, sched, i)      # 只认还在实行的午休
        bs, be = (brk[0].to_pydatetime(), brk[1].to_pydatetime()) if brk else (None, None)
        out[day.date()] = (row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime(), bs, be)
    return out


def _session_of(ts, local, table: dict, clock: dict, minutes: int = 5) -> str:
    """一根 bar(按起点)落在哪个时段: regular / pre-market / after-hours / lunch(午休) / closed。"""
    day = table.get(local.date())
    if not day:
        return "closed"
    o, c, bs, be = day
    if o <= ts < c:
        if bs and be and bs <= ts < be:
            from datetime import timedelta
            # 整根落在午休里才算午休; 起点在午休、但跨到下午开盘之后的那根小时线含正股时段的成交
            return "lunch" if ts + timedelta(minutes=minutes) <= be else "regular"
        return "regular"
    if clock.get("has_extended_hours"):
        if ts < o and local.hour >= int(clock["premarket_start_hour"]):
            return "pre-market"
        if ts >= c and local.hour < int(clock["afterhours_end_hour"]):
            return "after-hours"
    return "closed"


def _intraday(req: dict, iv: str) -> dict:
    import pandas as pd
    from datetime import timedelta
    from analysis.technical_snapshot_builder import resolve_exchange_clock
    sym, spec = req["symbol"], INTRADAY[iv]
    as_of = None
    if req.get("as_of") not in (None, ""):
        as_of = _parse_utc(req["as_of"])
        if as_of is None:
            return {"ok": False, "error": f"as_of could not be parsed: {req['as_of']!r} (write UTC, e.g. 20261001T115100Z)"}
    clock_as_of = _drill_as_of()
    if clock_as_of is not None and (as_of is None or as_of > clock_as_of):
        as_of = clock_as_of
    path, file_ts, why = _intraday_file(sym, iv, as_of)
    if not path:
        if why == "stale":
            return {"ok": False, "error": f"{sym} has no intraday file near this session's as-of time (gap; intraday data exists only for the symbol prepared for this session; "
                                          f"other symbols have daily bars only: call again without interval)"}
        return {"ok": False, "error": f"{sym} has no intraday data (gap; intraday data exists only for symbols that were prepared; other symbols have daily bars only: "
                                      f"call again without interval)"}
    cutoff = as_of or file_ts
    with open(path, encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    clock = resolve_exchange_clock(sym, os.path.join(ROOT, "config", "symbols.yaml"))
    rows = []
    for r in raw:
        k = {c.lower(): c for c in r}
        ts = _parse_utc(r.get(k.get("date", "date")))
        if ts is None or ts > cutoff:      # 截止之后才开始的 bar 不属于本场已知的数据
            continue
        try:
            bar = {"open": float(r[k["open"]]), "high": float(r[k["high"]]),
                   "low": float(r[k["low"]]), "close": float(r[k["close"]])}
        except (KeyError, ValueError):
            continue
        try:
            if "volume" in k and str(r[k["volume"]]).strip() != "":
                bar["volume"] = float(r[k["volume"]])
        except ValueError:
            pass
        rows.append((ts, pd.Timestamp(ts).tz_convert(clock["tz"]).to_pydatetime(), bar))
    if not rows:
        return {"ok": False, "error": f"{sym} intraday file has no valid bar before the cutoff time (gap)"}
    table = _session_table(clock, rows[0][1].date(), rows[-1][1].date())
    extended = bool(clock.get("has_extended_hours")) and iv != "1h"   # V16: the hourly file is regular-session only
    keep, dropped, lunch = [], 0, 0
    for ts, local, bar in rows:
        s = _session_of(ts, local, table, clock, spec["minutes"])
        if s == "lunch":                   # 午休里的 bar(指数会有平走的报价)直接不要, 不算来源存疑
            lunch += 1
            continue
        if s == "closed":
            dropped += 1
            continue
        keep.append((ts, local, s, bar))
    known = KNOWN_OFF_SESSION.get(clock["exchange"])
    if not extended and not known and dropped > OFF_SESSION_QUARANTINE * len(rows):
        return {"ok": False,
                "error": f"{sym} intraday file has {dropped}/{len(rows)} bars outside {clock['exchange']} trading sessions and "
                         f"the source is unverified (gap; this intraday data is withheld and is not evidence for either side; daily bars are still available: call again without interval)"}
    if not keep:
        return {"ok": False, "error": f"{sym} intraday file has no bars within trading sessions (gap)"}
    days = sorted({local.date() for _, local, _, _ in keep})
    if iv == "5m":
        want = req.get("date")
        if want in (None, ""):
            pick = [days[-1]]
        else:
            try:
                d = pd.Timestamp(str(want)).date()
            except (ValueError, TypeError):
                return {"ok": False, "error": f"date could not be parsed: {want!r} (write the exchange-local trading day, e.g. 2026-10-01)"}
            if d not in days:
                return {"ok": False, "error": f"{sym} has no 5-minute data on {d} (gap); trading days covered by this file: "
                                              + ", ".join(str(x) for x in days)}
            pick = [d]
    else:
        try:
            n = int(req.get("lookback_days", spec["default_days"]))
        except (ValueError, TypeError):
            n = spec["default_days"]
        pick = days[-max(1, min(n, spec["max_days"])):]
    chosen = set(pick)
    step = timedelta(minutes=spec["minutes"])
    data = []
    for ts, local, s, bar in keep:
        if local.date() not in chosen:
            continue
        item = {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "local": local.strftime("%m-%d %H:%M"), "session": s}
        item.update(bar)
        if ts + step > cutoff:
            item["partial"] = True         # 截止时这根还没走完
        data.append(item)
    out = {"ok": True, "mode": "bars", "interval": iv, "symbol": sym,
           "exchange": clock["exchange"], "exchange_tz": clock["tz"],
           "coverage": ("this data covers pre-market, regular and after-hours sessions" if extended
                        else "this data covers the regular session only (the project takes only this market's regular session; it does not mean the market has no off-session trading)"),
           "trading_days": [str(d) for d in pick],
           "data_through_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source_file": os.path.relpath(path, ROOT).replace(os.sep, "/"),
           "note": "a bar's time is its start; the session label follows the start; order within a bar is not visible"
                   + ("; hourly bars are regular-session only and aligned to the open, so the bar before the close is shorter" if iv == "1h" else ""),
           "data": data}
    if dropped:
        out["dropped_off_session"] = dropped
        if known:
            out["off_session_source"] = known
    if lunch:
        out["dropped_lunch_break"] = lunch
    return out


def bars_mode(req: dict) -> dict:
    iv = str(req.get("interval") or "1d").lower()
    if iv in INTRADAY:
        return _intraday(req, iv)
    if iv != "1d":
        return {"ok": False, "error": f"interval must be one of 1d (default) / 1h / 5m; received {iv!r}"}
    b, w = _bars_fresh(req["symbol"], int(req.get("lookback_days", 30)))
    if not b:
        return {"ok": False, "error": f"{req['symbol']} has no local daily bars and the live fetch failed (gap: not found is a neutral result, not evidence for either side)"}
    out = {"ok": True, "mode": "bars", "data": b}
    if w:
        out["stale_warning"] = w
    return out


def relative_strength(req: dict) -> dict:
    sym, bench, win = req["symbol"], req["benchmark"], int(req.get("window_days", 20))
    m = _emap().get(sym) or {}
    if bench in m.get("banned_benchmarks", []):
        return {"ok": False,
                "error": f"{sym} is a heavyweight holding of {bench}; circular reference, not allowed as benchmark; usable peers: {m.get('peers', [])}"}
    a, wa = _bars_fresh(sym, win)
    b, wb = _bars_fresh(bench, win)
    if not a or not b:
        return {"ok": False, "error": f"missing daily bars gap ({'benchmark' if a else 'symbol'} has no data and the live fetch failed)"}
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    series = [{"date": str(a[i]["date"]),
               "rs": round(a[i]["close"] / a[0]["close"] / (b[i]["close"] / b[0]["close"]), 4)}
              for i in range(n)]
    data = {"series": series, "spread_pct": round((series[-1]["rs"] - 1) * 100, 2),
            "symbol": _stats(a), "benchmark": _stats(b)}
    warns = [w for w in (wa, wb) if w]
    if warns:
        data["stale_warnings"] = warns
    return {"ok": True, "mode": "relative_strength", "data": data}


def econ_series(req: dict) -> dict:
    region, ind = req["region"].upper(), req["indicator"]
    fs = glob.glob(os.path.join(ROOT, "data", "economic", f"economic_{region}_*.txt"))
    a = _drill_as_of()
    if a is not None:  # 按文件名时刻挑不晚于 as_of(+备餐余量)的最新一份, 不按 mtime
        from datetime import timedelta
        stamped = [(_parse_utc(os.path.basename(p).rsplit("_", 1)[-1].replace(".txt", "")), p) for p in fs]
        fs = [p for t, p in sorted(x for x in stamped if x[0] is not None)
              if t <= a + timedelta(minutes=PREP_SLACK_MIN)]
    if not fs:
        return {"ok": False, "error": f"economic_{region} transfer file missing (run data preparation / hydration first)"}
    latest = fs[-1] if a is not None else max(fs, key=os.path.getmtime)
    txt = open(latest, encoding="utf-8").read()
    lines = [ln for ln in txt.splitlines() if ind.lower() in ln.lower()]
    if not lines:
        return {"ok": False, "error": f"{region} transfer file has no indicator {ind}"}
    return {"ok": True, "mode": "econ_series",
            "data": {"region": region, "indicator": ind, "lines": lines[:20],
                     "source_file": os.path.basename(latest)}}


def event_calendar(req: dict) -> dict:
    from datetime import date, timedelta
    p = os.path.join(ROOT, "config", "event_calendar.yaml")
    if not os.path.exists(p):
        return {"ok": False, "error": "config/event_calendar.yaml missing"}
    cal = yaml.safe_load(open(p, encoding="utf-8")) or []
    hz = int(req.get("horizon_days", 14))
    today = _today()
    end = today + timedelta(days=hz)
    hits = [{**e, "date": str(e["date"])} for e in cal
            if today <= date.fromisoformat(str(e["date"])) <= end
            and (not e.get("symbols") or req.get("symbol") in e["symbols"])]
    note = ("binary event(s) inside the window → assess under the event gate (§14.2)" if hits else
            "no registered binary event inside the window (a valid answer: stamp verified, not not_found; the calendar covers only macro "
            "policy/inflation dates fomc/cpi/ecb/boj/bok; company events such as earnings go through the search leg)")
    return {"ok": True, "mode": "event_calendar",
            "data": {"horizon_days": hz, "events": hits, "warning": note}}


def _ols(x, y):
    """一元 OLS: 返回 (beta, alpha, r_squared)。x 方差为 0 → None。"""
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0:
        return None
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    beta = sxy / sxx
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 1.0
    return beta, my - beta * mx, r2


def _corr(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / math.sqrt(sxx * syy)


DEPTH_FULL_BARS = 1150  # ≈4.6 年交易日; 低于此值输出如实标注样本深度


def _date_key(v) -> str:
    """日期归一成 'YYYY-MM-DD' 作对齐键。不同缓存源的日线一边带时区后缀
    ('2026-09-26 00:00:00+00:00')一边不带('2026-09-26'), 直接 str 对齐会 0 天重合
    (2026-09-28 DAX×USO pair_link 案)。"""
    s = str(v).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _aligned_returns(a, b):
    """两组 bars 按日期对齐 → (dates, ret_a, ret_b)。"""
    ca = {_date_key(x["date"]): x["close"] for x in a}
    cb = {_date_key(x["date"]): x["close"] for x in b}
    dates = sorted(set(ca) & set(cb))
    ra = [ca[dates[i]] / ca[dates[i - 1]] - 1 for i in range(1, len(dates))]
    rb = [cb[dates[i]] / cb[dates[i - 1]] - 1 for i in range(1, len(dates))]
    return dates[1:], ra, rb


def _corr_drift(old: float, now: float) -> str:
    """联动强度的近期漂移。强度看相关的绝对值: 负相关加深是反向联动变强, 不是脱钩。
    穿过零且两头都不贴零 = 换向, 单独标出(强度可能没变, 方向变了)。"""
    if old * now < 0 and min(abs(old), abs(now)) >= 0.1:
        return "sign flip"
    diff = abs(now) - abs(old)
    return "tightening" if diff > 0.1 else "decoupling" if diff < -0.1 else "stable"


def _corr_direction(r: float) -> str:
    return "same direction" if r >= 0.1 else "opposite direction" if r <= -0.1 else "no clear direction"


def pair_link(req: dict) -> dict:
    sym, cp = req["symbol"], req["counterpart"]
    win = int(req.get("window_days", 60))
    if sym == cp:
        return {"ok": False, "error": "symbol and counterpart are identical; no co-movement to compute against itself"}
    a, wa = _bars_fresh(sym, 1300)
    b, wb = _bars_fresh(cp, 1300)
    if not a or not b:
        miss = sym if not a else cp
        return {"ok": False, "error": f"{miss} has no local daily bars and the live fetch failed (gap: not found is a neutral result, not evidence for either side)"}
    dates, rs, rc = _aligned_returns(a, b)
    n = len(rs)
    if n < 40:
        return {"ok": False, "error": f"only {n} overlapping trading days (<40) between the two symbols; sample too small to regress"}
    win = min(win, n)
    fit = _ols(rc[-win:], rs[-win:])
    if fit is None:
        return {"ok": False, "error": f"{cp} price has no variance within the window; cannot regress"}
    beta, alpha_d, r2 = fit
    m = min(20, n)
    resid = sum(rs[-m + i] - beta * rc[-m + i] for i in range(m)) * 100
    corr_now = _corr(rc[-win:], rs[-win:])
    # 全历史滚动相关 → 当前值百分位(带符号的与按强度的各一个) + 近期漂移(按强度)
    roll = [c for i in range(win, n + 1)
            if (c := _corr(rc[i - win:i], rs[i - win:i])) is not None]
    pctile = round(sum(1 for c in roll if c <= corr_now) / len(roll) * 100, 1) if roll else None
    strength_pctile = (round(sum(1 for c in roll if abs(c) <= abs(corr_now)) / len(roll) * 100, 1)
                       if roll else None)
    drift = "insufficient sample"
    if len(roll) > 21:
        drift = _corr_drift(roll[-21], corr_now)
    data = {"symbol": sym, "counterpart": cp, "window_days": win,
            "beta": round(beta, 3),
            "alpha_annualized_pct": round(((1 + alpha_d) ** 252 - 1) * 100, 2),
            "r_squared": round(r2, 3),
            "residual_return_recent_pct": round(resid, 2),
            "residual_note": f"the part of the last {m} trading days' realized return left after removing beta×{cp}",
            "correlation_now": round(corr_now, 3),
            "correlation_direction": _corr_direction(corr_now),
            "correlation_strength_percentile_in_history": strength_pctile,
            "correlation_percentile_in_history": pctile,
            "correlation_drift": drift,
            "correlation_note": "strength percentile and drift use the absolute correlation (a deepening negative correlation = tightening inverse co-movement); "
                                "the signed percentile only says whether it leans same- or opposite-direction within its own history",
            "sample_depth_bars": n, "last_date": dates[-1]}
    if n < DEPTH_FULL_BARS:
        data["depth_warning"] = (f"overlapping history is only {n} bars (about {n / 252:.1f} years); "
                                 f"percentiles/drift are based on this depth, not a full 5 years")
    warns = [w for w in (wa, wb) if w]
    if warns:
        data["stale_warnings"] = warns
    return {"ok": True, "mode": "pair_link", "data": data}


def _logfit(bars):
    """对数价格线性拟合 → {n_bars, slope_annualized_pct, r_squared} | None。"""
    y = [math.log(b["close"]) for b in bars if b["close"] > 0]
    if len(y) < 10:
        return None
    fit = _ols(list(range(len(y))), y)
    if fit is None:
        return None
    slope, _, r2 = fit
    return {"n_bars": len(y),
            "slope_annualized_pct": round((math.exp(slope * 252) - 1) * 100, 2),
            "r_squared": round(r2, 3)}


def trend_fit(req: dict) -> dict:
    sym, win = req["symbol"], int(req.get("window_days", 60))
    b, w = _bars_fresh(sym, 1300)
    if not b:
        return {"ok": False, "error": f"{sym} has no local daily bars and the live fetch failed (gap: not found is a neutral result, not evidence for either side)"}
    short = _logfit(b[-win:])
    long_ = _logfit(b[-250:])
    if not short or not long_:
        return {"ok": False, "error": f"{sym} has fewer than 10 valid bars; a fit is meaningless"}
    div = (short["slope_annualized_pct"] > 0) != (long_["slope_annualized_pct"] > 0)
    data = {"symbol": sym, "short_window": short, "long_window": long_,
            "divergence": div,
            "divergence_note": "short- and long-window slopes diverge in direction (potential turning point)" if div else "short- and long-window directions agree",
            "sample_depth_bars": len(b), "last_date": str(b[-1]["date"])}
    if w:
        data["stale_warnings"] = [w]
    return {"ok": True, "mode": "trend_fit", "data": data}


DISPATCH = {"peer_compare": peer_compare, "bars": bars_mode,
            "relative_strength": relative_strength, "econ_series": econ_series,
            "event_calendar": event_calendar,
            "pair_link": pair_link, "trend_fit": trend_fit}


def handle(req: dict) -> dict:
    mode = req.get("mode")
    if mode not in ALLOWED:
        return {"ok": False, "error": f"unknown mode: {mode!r}", "allowed_modes": ALLOWED}
    norm, err = _normalize(mode, req)
    if err:
        return err
    try:
        out = DISPATCH[mode](norm)
    except KeyError as e:
        return {"ok": False, "error": f"missing parameter {e}; {_schema_str(mode)}", "allowed_modes": ALLOWED}
    if norm.pop("_normalized", None) and isinstance(out, dict):
        out["request_normalized"] = norm
    return out


# ---------------------------------------------------------------------------
# selftest (合成数据进 temp 目录, 不碰真数据)
# ---------------------------------------------------------------------------

def _selftest():
    import tempfile
    from datetime import date, timedelta
    global ROOT
    old_root, ROOT = ROOT, tempfile.mkdtemp()
    os.environ["EQUIPOISE_EVIDENCE_LIVE_FETCH"] = "0"   # selftest 绝不碰盈透
    try:
        # 两个合成标的: A 线性 +10%, B 平盘
        for sym, closes in (("A", [round(100 + i * 10 / 9, 4) for i in range(10)]),
                            ("B", [50.0] * 10)):
            d = os.path.join(ROOT, "data", "market_data", sym)
            os.makedirs(d)
            with open(os.path.join(d, f"{sym}_7d_1d_1.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "high", "low", "close"])
                for i, c in enumerate(closes):
                    w.writerow([f"2026-07-{i + 1:02d}", c + 1, c - 1, c])
        # pair_link/trend_fit 合成标的(300 bar, 日期滚到今天避免 stale):
        #   X 交替 +1.2%/-0.8%; Y 日收益恒为 X 的 2 倍 → 回归 Y on X: beta=2, alpha=0, R²=1, corr=1
        #   T 纯指数增长 10%/年 → 对数线性拟合 R²=1, 年化斜率 ≈ e^0.1-1 = 10.5%
        n_syn = 300
        xc, yc = [100.0], [100.0]
        for i in range(1, n_syn):
            rx = 0.012 if i % 2 else -0.008
            xc.append(round(xc[-1] * (1 + rx), 6))
            yc.append(round(yc[-1] * (1 + 2 * rx), 6))
        tc = [round(100.0 * math.exp(0.10 / 252 * i), 6) for i in range(n_syn)]
        for sym, closes in (("X", xc), ("Y", yc), ("T", tc)):
            d = os.path.join(ROOT, "data", "market_data", sym)
            os.makedirs(d)
            with open(os.path.join(d, f"{sym}_5y_1d_1.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "high", "low", "close"])
                for i, c in enumerate(closes):
                    w.writerow([(date.today() - timedelta(days=n_syn - 1 - i)).isoformat(),
                                c * 1.01, c * 0.99, c])
        os.makedirs(os.path.join(ROOT, "config"))
        with open(os.path.join(ROOT, "config", "evidence_map.yaml"), "w", encoding="utf-8") as f:
            f.write("A: {peers: [B], banned_benchmarks: [FAKE_ETF], econ: []}\n")
        with open(os.path.join(ROOT, "config", "event_calendar.yaml"), "w", encoding="utf-8") as f:
            f.write(f"- {{date: {(date.today() + timedelta(days=3)).isoformat()}, "
                    f"name: TEST CPI, kind: cpi}}\n")

        r = handle({"mode": "peer_compare", "symbol": "A", "window_days": 10})
        assert r["ok"] and abs(r["data"]["stats"]["A"]["ret_pct"] - 10.0) < 0.2, r
        assert "B" in r["data"]["stats"] and r["data"]["gaps"] == [], r
        r = handle({"mode": "relative_strength", "symbol": "A", "benchmark": "FAKE_ETF"})
        assert not r["ok"] and "circular reference" in r["error"], r
        r = handle({"mode": "relative_strength", "symbol": "A", "benchmark": "B", "window_days": 10})
        assert r["ok"] and r["data"]["spread_pct"] > 5, r
        r = handle({"mode": "bars", "symbol": "A", "lookback_days": 10})
        assert r["ok"] and len(r["data"]) == 10, r
        assert "volume" not in r["data"][0], r  # CSV 没有成交量列就如实缺
        dv = os.path.join(ROOT, "data", "market_data", "V")
        os.makedirs(dv)
        with open(os.path.join(dv, "V_5y_1d_1.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "high", "low", "close", "volume"])
            for i in range(5):
                w.writerow([(date.today() - timedelta(days=4 - i)).isoformat(), 11, 9, 10, 1000 + i])
        r = handle({"mode": "bars", "symbol": "V", "lookback_days": 5})
        assert r["ok"] and r["data"][-1]["volume"] == 1004.0, r
        r = handle({"mode": "bars", "symbol": "NOFILE"})
        assert not r["ok"] and "gap" in r["error"], r
        r = handle({"mode": "econ_series", "region": "US", "indicator": "cpi_yoy"})
        assert not r["ok"], r
        r = handle({"mode": "event_calendar", "symbol": "A", "horizon_days": 14})
        assert r["ok"] and len(r["data"]["events"]) == 1 and r["data"]["warning"], r
        r = handle({"mode": "nonsense"})
        assert not r["ok"] and r.get("allowed_modes") == ALLOWED, r
        # --- schema 归一/拒绝 ---
        r = handle({"mode": "relative_strength", "type": "data", "symbol": "a",
                    "comparison_symbol": "B", "lookback_days": 10})
        assert r["ok"] and r["request_normalized"]["benchmark"] == "B", r
        assert r["request_normalized"]["window_days"] == 10, r
        assert r["data"]["symbol"]["n_bars"] == 10, "别名 lookback_days 必须真生效, 不许静默吃默认窗口"
        r = handle({"mode": "bars", "symbol": "A", "foo": 1})
        assert not r["ok"] and "unknown parameters" in r["error"] and "valid keys" in r["error"], r
        r = handle({"mode": "relative_strength", "symbol": "A"})
        assert not r["ok"] and "missing required parameters" in r["error"], r
        # --- 空日历 = 有效答案 (ok:true + 明确话术), 不是 not_found ---
        r = handle({"mode": "event_calendar", "symbol": "A", "horizon_days": 1})
        assert r["ok"] and r["data"]["events"] == [] and "no registered binary event" in r["data"]["warning"], r
        # --- 过期数据带 stale 警告 (合成 bar 是 7/1-7/10, live-fetch 已关) ---
        r = handle({"mode": "bars", "symbol": "A", "lookback_days": 10})
        assert r["ok"] and ("stale_warning" in r) == _is_stale(r["data"]), r
        # --- pair_link: Y on X 回归, 已知答案 beta=2/alpha≈0/R²=1/corr=1 ---
        r = handle({"mode": "pair_link", "symbol": "Y", "counterpart": "X", "window_days": 60})
        assert r["ok"], r
        d = r["data"]
        assert abs(d["beta"] - 2.0) < 0.05 and d["r_squared"] > 0.99, d
        assert abs(d["alpha_annualized_pct"]) < 2.0, d
        assert abs(d["residual_return_recent_pct"]) < 0.5, d
        assert abs(d["correlation_now"] - 1.0) < 0.01, d
        assert 0 <= d["correlation_percentile_in_history"] <= 100, d
        assert d["sample_depth_bars"] <= n_syn and "depth_warning" in d, \
            "300 bar 远不足 5 年, 必须如实标注样本深度"
        r = handle({"mode": "pair_link", "symbol": "X", "counterpart": "X"})
        assert not r["ok"] and "identical" in r["error"], r
        r = handle({"mode": "pair_link", "symbol": "A", "counterpart": "B"})
        assert not r["ok"] and "40" in r["error"], r  # 重合交易日不足
        r = handle({"mode": "pair_link", "symbol": "y", "vs": "x", "lookback_days": 60})
        assert r["ok"] and r["request_normalized"]["counterpart"] == "X", r
        # --- trend_fit: 纯指数增长, 双窗斜率≈10.5%/R²=1, 无背离 ---
        r = handle({"mode": "trend_fit", "symbol": "T", "window_days": 60})
        assert r["ok"], r
        d = r["data"]
        assert abs(d["short_window"]["slope_annualized_pct"] - 10.5) < 1.5, d
        assert abs(d["long_window"]["slope_annualized_pct"] - 10.5) < 1.5, d
        assert d["short_window"]["r_squared"] > 0.99 and d["long_window"]["r_squared"] > 0.99, d
        assert d["divergence"] is False, d
        r = handle({"mode": "trend_fit", "symbol": "NOFILE2"})
        assert not r["ok"] and "gap" in r["error"], r
    finally:
        ROOT = old_root


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        print("evidence_quant selftest OK")
    else:
        print(json.dumps(handle(json.loads(sys.argv[1])), ensure_ascii=False))
