# -*- coding: utf-8 -*-
"""
pre_round_brief.py — 跑前简报拉取(给对话版 CC 做"重要性排序 → 选本轮跑哪些"用)。

思路(用户 2026-06-29 定): 美股不再每轮全量跑 30 个。GENERAL 必跑且第一;之后对话版 CC
读"综合简报 + 下一轮简评"给子标的排重要性, 挑 ~15 个(参考值, 可临时指定), 其余错开退居二线。
本工具只负责**把排序要看的信息从 PG 拉成一张紧凑表**, 判断/排序由 CC(我)来做, 不在这里写死规则。

每个标的取 debates 表里**最新一行**(按 report_generated_at, 缺则 as_of), 抽:
  - 上轮 as_of / report_generated_at(陈旧度)
  - composite_score / operation_type / operation_target(有没有在场上的仓位: ENTRY/TRIM/PILOT)
  - next_review.recommend_after(下次建议时刻) → 算逾期天数(正=已过该跑, 负=还没到)
  - next_review.notes 或 reason(下一轮简评, 截断)
  - debate_summary(上轮一句话, 截断)
  - 引擎(signatures.zealot: cc=claude-* / deepseek)
无历史 → 标 NO_HISTORY(视为该跑, 建基线)。

**三种到期(2026-10-24 council 定, 治 X7 转态无人执行 / X8 过期空转)**: 一份报告只在三种事件上"到期该重评",
全由本工具机械判, 列 `due`:
  (i)  重评时刻: recommend_after 已过;
  (ii) 有效线触发: next_review.structured_triggers(以后 lines)对着**报告基准日以来的全部日线**判——price_* 看日内高低触价,
       close_* 看连续收盘根数, date 看日期; 板块线没有板块行情, 列"需人工";
  (iii) 未成交订单到期: 派生为限价(LIMIT_ENTRY)的入场单, 按复盘同一套规则(workflows.postcheck_quant.entry_fill_stats,
       有效期默认 3 个交易日或报告明写)判: 有效期内触价 = 模拟成交(列状态, 不算到期); 过了有效期没触价 = 到期未成交。
到期 = 进重评队列; 撤不撤单、续不续单由调仓方案决定, 本工具不碰订单。
日线: 带 --fetch 时先按复盘结果价预取同一条路连盈透刷新(要 EQUIPOISE_IBKR_PORT, 1h 内鲜则跳); 不带就读 data/market_data 残留的最新 CSV,
没有就标"无行情"。

用法:
  python pre_round_brief.py                 # 默认美股全集(WAVES['us'])
  python pre_round_brief.py --market us|eu|jp|kr
  python pre_round_brief.py --symbols NVDA,AMD,SPY
  python pre_round_brief.py --json          # 输出 JSON(默认输出可读表)
  EQUIPOISE_IBKR_PORT=4001 python pre_round_brief.py --market us --fetch   # 先刷日线再判撞线 / 订单到期
输出按"优先级粗排"(NO_HISTORY 与逾期多的在前)排序, 仅作起点; 真正排序 CC 结合 GENERAL 当轮基调再定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from workflows.symbol_universe import market_waves  # 市场成员唯一真源 = config/symbols.yaml
    WAVES = market_waves()
except Exception:
    WAVES = {}


def _pg_cfg():
    return {
        "host": os.environ.get("EQUIPOISE_PG_HOST", "localhost"),
        "port": int(os.environ.get("EQUIPOISE_PG_PORT", "5432")),
        "dbname": os.environ.get("EQUIPOISE_PG_DB", "equipoise"),
        "user": os.environ.get("EQUIPOISE_PG_USER", "postgres"),
        "password": os.environ.get("EQUIPOISE_PG_PASSWORD", "postgres"),
    }


def _universe(market, symbols):
    if symbols:
        return [s.strip() if s.strip().isdigit() else s.strip().upper()
                for s in symbols.split(",") if s.strip()]
    out = []
    for _label, ss in WAVES.get(market, []):
        out.extend(ss)
    return out


def _parse_ts(v):
    if not v:
        return None
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # 只写了日期或没带时区的(报告里 recommend_after 常是纯日期)一律按 UTC, 否则与当前时刻相减会报错
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _snip(s, n=70):
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except Exception:
        return None


_DV = None


def _dv():
    """scripts/divergence.py (the duty loop's routing measure, 2026-11-06), loaded lazily by path: this file lives under
    skills/ and is itself loaded by path, so neither side is an importable package."""
    global _DV
    if _DV is None:
        import importlib.util
        from pathlib import Path
        p = Path(__file__).resolve().parents[3] / "scripts" / "divergence.py"
        spec = importlib.util.spec_from_file_location("equipoise_divergence", p)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        _DV = m
    return _DV


def _nre():
    """scripts/next_review_enrich.py (the assembler's next_review enrichment: expires_at, known calendar), loaded lazily
    by path like _dv — the read-time projection below reuses its pure builders."""
    import importlib.util
    from pathlib import Path
    if not hasattr(_nre, "_m"):
        p = Path(__file__).resolve().parents[3] / "scripts" / "next_review_enrich.py"
        spec = importlib.util.spec_from_file_location("equipoise_next_review_enrich", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _nre._m = m
    return _nre._m


def _regions():
    """symbol → region from config/symbols.yaml (unknown → US); cached per process."""
    if not hasattr(_regions, "_m"):
        try:
            from workflows.symbol_universe import load_symbol_config
            contracts = (load_symbol_config() or {}).get("symbol_contracts") or {}
            _regions._m = {str(k).upper(): str((v or {}).get("region") or "US").upper() for k, v in contracts.items()}
        except Exception:
            _regions._m = {}
    return _regions._m


PROJECTED_SOURCE = "projected from the known calendar (the report predates the 2026-11-06 events format)"


def _tree_scenario(report):
    dv = _dv()
    sc = report.get("scenario") if isinstance(report.get("scenario"), dict) else None
    if sc and dv.tree_nodes(sc):
        return sc
    by = report.get("by_lens") if isinstance(report.get("by_lens"), dict) else {}
    for e in by.values():
        if isinstance(e, dict) and isinstance(e.get("scenario"), dict) and dv.tree_nodes(e["scenario"]):
            return e["scenario"]
    return None


def project_next_review(report, as_of, last_release=None, macro_rows=None):
    """Pure (2026-11-06, owner): a report with a v3.4 tree whose next_review predates the events format (price trigger
    lines, no events[]) is *read* as if written in the current one — `expires_at` from the tree's horizon, `events[]`
    projected from the known calendar (the issuer's expected earnings by cadence, scheduled macro releases), each marked
    `projected: true` with coverage 'none' and no tests (the arbiter never judged them). The stored report is not
    rewritten; the projection lives in the pulled row. None when the report already carries events[] or has no tree."""
    nr = report.get("next_review") if isinstance(report.get("next_review"), dict) else {}
    if isinstance(nr.get("events"), list):
        return None
    sc = _tree_scenario(report)
    if not sc:
        return None
    nre = _nre()
    built = nre.build(nr, as_of, sc.get("T_days"), last_release, macro_rows or [])
    events = []
    for k in built.get("known_events") or []:
        if k.get("kind") == "earnings_expected":
            win = k.get("window") or [None, None]
            ev = {"name": "earnings (expected by quarterly cadence)", "who": "issuer", "date": win[0] or k.get("date"),
                  "date_precision": "window", "window_end": win[1], "expected_date": k.get("date")}
        else:
            ev = {"name": f"{k.get('region') or ''} {k.get('name')}".strip(), "who": "macro", "date": k.get("date"),
                  "date_precision": "date"}
        ev.update({"source": PROJECTED_SOURCE, "basis": k.get("basis"), "tests": [], "coverage": "none",
                   "check": "calendar projection, not the arbiter's: judge it against the tree and the measured divergence",
                   "projected": True})
        events.append(ev)
    return {"expires_at": built.get("expires_at"), "events": events, "projected": True}


def _all_bars(df):
    """Every daily bar of the frame as dicts (ascending), for the ATR of a lens whose report carries none."""
    if df is None or len(df) == 0:
        return []
    out = []
    for ts, row in df.iterrows():
        out.append({"date": ts.date().isoformat(), "open": float(row.get("open", row.get("close"))), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"])})
    return out


def _bars_after(df, as_of, include_base_day=False):
    """日线 DataFrame(postcheck_quant._load_daily_df 的形状) → (基准日之后的 bar 列表, 基准日(含)前最后收盘, 行情最后一天)。
    缺省不要基准日当天那根(余下的行情不算整日, 复盘同口径)。include_base_day=True(值班循环的撞线报警用): 基准日那根也算——
    报告多在开盘前写, 当天的走势全发生在报告之后, 而最新价就在当天那根(盘中为止的部分 bar)里; 盘中写的报告可能多报一次, 代价只是
    多叫醒一次值班 codex(11-04: GOOGL / AMZN 开盘前写的报告, 当天冲过触发线, 报警却始终为零)。last_close 仍是基准日前一日的收盘。"""
    if df is None or len(df) == 0:
        return [], None, None
    day0 = as_of.date() if as_of else None
    bars, last_close, last_day = [], None, None
    for ts, row in df.iterrows():
        d = ts.date()
        rec = {"date": d.isoformat(), "open": float(row.get("open", row.get("close"))), "high": float(row["high"]),
               "low": float(row["low"]), "close": float(row["close"])}
        last_day = rec["date"]
        if day0 is None or d > day0 or (include_base_day and d == day0):
            bars.append(rec)
        else:
            last_close = rec["close"]
    return bars, last_close, last_day


def _trigger_hits(triggers, bars, today, bars_for=None, own_symbol=None, base_day=None):
    """结构化触发线对着基准日以来的全部日线判。返回 (命中句列表, 需人工句列表)。
    price_below/above: 任一根日内触价; close_below/above: 连续 n_closes 根收盘越线(任一处); date_reached/date: 日期已到——但不晚于
    基准日(base_day = 报告当天)的日期当作报告写时已满足, 不算(11-04: ITOCY 报告当天写 date_reached 当天, 每 10 分钟空叫一次 codex)。
    线上写了别的标的(大盘场的 SPY/QQQ 镜头线)且给了 bars_for(symbol)→bars 时, 用那只标的的日线判。"""
    hits, manual = [], []
    own_bars = bars
    for t in triggers or []:
        if not isinstance(t, dict):
            continue
        typ, val = str(t.get("type") or ""), _num(t.get("value"))
        sym = t.get("symbol")
        tag = f"{sym} " if sym else ""
        bars = own_bars
        if sym and own_symbol and str(sym).upper() != str(own_symbol).upper() and bars_for is not None:
            bars = bars_for(str(sym).upper()) or []
        if typ in ("price_below", "price_above") and val and bars:
            hit = next((b for b in bars if (b["low"] <= val if typ == "price_below" else b["high"] >= val)), None)
            if hit:
                hits.append(f"{tag}{'broke below' if typ == 'price_below' else 'broke above'} {val:g}@{hit['date'][5:]}")
        elif typ in ("close_below", "close_above") and val and bars:
            n = max(1, int(_num(t.get("n_closes")) or 1))
            run = 0
            for b in bars:
                run = run + 1 if (b["close"] < val if typ == "close_below" else b["close"] > val) else 0
                if run >= n:
                    hits.append(f"{tag}{'close' if n == 1 else f'{n} consecutive closes'} {'below' if typ == 'close_below' else 'above'} {val:g}@{b['date'][5:]}")
                    break
        elif typ in ("date_reached", "date"):
            d = str(t.get("date") or "")[:10]
            if d and d <= today and not (base_day and d <= base_day):
                hits.append(f"date reached {d[5:]}")
        elif typ.startswith("sector"):
            manual.append(f"sector line ({typ} {t.get('sector') or ''} {t.get('pct') or ''})".replace("  ", " "))
    return hits, manual


def _conditional_state(report, cond, bars, ref_price, today):
    """G4 (wave 2): a conditional order (operation_execution.conditional) is judged by its observe rule on the daily bars
    (postcheck_quant.conditional_fill_stats: close / open compare that field, touch the high / low, gap_open the open
    against the previous close) — "met on <date>" / "not met", never a "simulated fill" at the level."""
    head = f"conditional: {cond.get('observe')}, fill {cond.get('fill') or 'next_open'}"
    try:
        from workflows.postcheck_quant import conditional_fill_stats, _order_valid_days, _order_valid_until
    except Exception as e:
        return f"{head}; postcheck module unavailable ({e})", False
    vd, _src = _order_valid_days(report)
    vu = _order_valid_until(report, None)
    if not bars:
        return f"{head}; not met yet (no full-day bars after the base date; valid {vu or f'{vd} days'})", False
    cs = conditional_fill_stats(bars, ref_price or 0.0, cond.get("observe"), cond.get("fill"), prev_close=ref_price,
                                valid_days=None if vu else vd, valid_until=vu)
    if cs.get("condition_met"):
        if cs.get("entry_price_reached"):
            return f"{head}; met on {cs['condition_met_date'][5:]}, filled @{cs['fill_price']:g} {cs['fill_date'][5:]}", False
        return f"{head}; met on {cs['condition_met_date'][5:]}, fill at the next open (pending)", False
    if "unparseable" in str(cs.get("note") or ""):
        return f"{head}; {cs['note']}", False
    expired = (vu and today > vu) or (not vu and len(bars) >= vd)
    if expired:
        return f"{head}; not met (expired, valid {vu or f'{vd} days'})", True
    left = vd - len(bars) if not vu else None
    return f"{head}; not met" + (f" ({left} days left)" if left is not None else f" (until {vu})"), False


def _order_state(report, bars, ref_price, today):
    """派生为限价的入场单: 按复盘同一套规则判 模拟成交 / 挂单中 / 到期未成交。返回 (状态句, 是否到期)。
    只判单价位的单(多镜头大盘场不判); 不派生为限价的操作返回 (None, False)。
    G4: operation_execution.conditional 存在 = 条件单(入场或减仓), 按 observe 规则判, 不是挂在价位上的限价单。"""
    cond = (report.get("operation_execution") or {}).get("conditional") if isinstance(report.get("operation_execution"), dict) else None
    if isinstance(cond, dict) and cond.get("observe"):
        return _conditional_state(report, cond, bars, ref_price, today)
    op = str(report.get("operation_type") or "").upper()
    derived = str(((report.get("operation_execution") or {}).get("derived")) or "").upper()
    if op != "LIMIT_ENTRY" and derived != "LIMIT_ENTRY":
        return None, False
    tgt = str(report.get("operation_target") or "")
    if "=" in tgt or "-" in tgt.strip("-"):
        return "limit: multi-lens / range, not judged", False
    entry = _num(tgt)
    if not entry:
        return None, False
    refs = {str(r.get("symbol") or "").upper(): r.get("price") for r in (report.get("reference_prices") or []) if isinstance(r, dict)}
    p0 = _num(refs.get(str(report.get("symbol") or "").upper())) or ref_price
    if not p0:
        return "limit order: no reference price, not judged", False
    try:
        from workflows.postcheck_quant import entry_fill_stats, _order_valid_days, _order_valid_until
    except Exception as e:
        return f"limit order: postcheck module unavailable ({e})", False
    vd, _src = _order_valid_days(report)
    vu = _order_valid_until(report, None)
    if not bars:
        return f"limit {entry:g} open (no full-day bars after the base date; valid {vu or f'{vd} days'})", False
    fs = entry_fill_stats(bars, p0, entry, valid_days=None if vu else vd, valid_until=vu)
    if fs.get("entry_price_reached"):
        return f"limit {entry:g} simulated fill @{fs['reached_date'][5:]}", False
    expired = (vu and today > vu) or (not vu and len(bars) >= vd)
    if expired:
        return f"limit {entry:g} expired unfilled (valid {vu or f'{vd} days'})", True
    left = vd - len(bars) if not vu else None
    return f"limit {entry:g} open" + (f" ({left} days left)" if left is not None else f" (until {vu})"), False


def _fmt_level(v):
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _lens_rows(report, as_of, df_lookup, today, include_base_day=False):
    """v3.4.1 lens-wise report (by_lens): one row per lens in the frozen order, its own operation and its order state
    judged from its own level, reference price and bars (df_lookup(lens)); None for every other report."""
    by = report.get("by_lens")
    if not isinstance(by, dict):
        return None
    rows = []
    for L in [str(x) for x in (report.get("lenses") or [])] or [str(x) for x in by]:
        e = by.get(L)
        if not isinstance(e, dict):
            rows.append({"lens": L, "op_type": None, "op_target": None, "op_size": None,
                         "order": "no by_lens entry", "expired": False})
            continue
        sub = {**report, "symbol": L, "operation_type": e.get("operation_type"),
               "operation_target": _fmt_level(e.get("operation_target")),
               "operation_execution": e.get("operation_execution") or {}}
        bars, last_close, _ = _bars_after(df_lookup(L) if df_lookup else None, as_of, include_base_day)
        order, expired = _order_state(sub, bars, last_close, today)
        rows.append({"lens": L, "op_type": e.get("operation_type"), "op_target": sub["operation_target"],
                     "op_size": e.get("operation_size"), "order": order, "expired": expired})
    return rows


def due_reasons(report, as_of, df, now_utc, df_lookup=None, include_base_day=False, div_cfg=None):
    """一份报告的到期原因 + 订单状态。返回 dict: due(到期原因句列表) / order(状态句) / manual(需人工句) /
    bars_through(行情到哪天, None=没有行情) / n_bars(基准日之后的整日根数, 0=行情还没过基准日) / divergence(背离度, 见 scripts/divergence.py)。
    2026-11-06(库主): 有 v3.4 情景树的报告不再判撞线——树对最新价的背离度(`tree dead:` 硬 / `drift:` 软)替代它, 结构化触发线与 lines 只对
    没有树的老报告按旧法判(`line hit:`)。div_cfg: {dead_tail, drift_tail} 阈值(config/duty.yaml `divergence`), 缺省用模块里的。
    df_lookup(symbol)→DataFrame 可选, 给大盘场的镜头取别的标的的日线。纯函数, 不连库不连盈透, 便于单测。"""
    today = now_utc.date().isoformat()
    bars, last_close, last_day = _bars_after(df, as_of, include_base_day)
    nr = report.get("next_review") or {}
    triggers = (nr.get("structured_triggers") if isinstance(nr, dict) else None) or []
    lines = report.get("lines") or []
    base_day = as_of.date().isoformat() if as_of is not None else None
    bars_for = (lambda sym: _bars_after(df_lookup(sym), as_of, include_base_day)[0]) if df_lookup else None
    dv = _dv()
    own = str(report.get("symbol") or "").upper()
    has_tree = bool(dv.tree_nodes(report.get("scenario") or {})) or any(
        isinstance(e, dict) and dv.tree_nodes(e.get("scenario") or {}) for e in (report.get("by_lens") or {}).values()
        if isinstance(report.get("by_lens"), dict))
    div, hits, manual = None, [], []
    if has_tree:
        def _bf(sym):
            if str(sym).upper() == own or not df_lookup:
                d2 = df
            else:
                d2 = df_lookup(sym)
            if d2 is None or len(d2) == 0:
                return None
            return _bars_after(d2, as_of, include_base_day)[0], _all_bars(d2)
        kw = {k: float(v) for k, v in (div_cfg or {}).items() if k in ("dead_tail", "drift_tail")}
        div = dv.report_divergence(report, _bf, base_day, today, **kw)
        _h, manual = _trigger_hits([t for t in triggers if isinstance(t, dict) and str(t.get("type", "")).startswith("sector")],
                                   bars, today, own_symbol=report.get("symbol"), base_day=base_day)   # sector lines stay informational
    else:
        hits, manual = _trigger_hits(list(triggers) + [l for l in lines if isinstance(l, dict)], bars, today,
                                     bars_for=bars_for, own_symbol=report.get("symbol"), base_day=base_day)
    lens_rows = _lens_rows(report, as_of, df_lookup, today, include_base_day)
    if lens_rows is None:
        order, expired = _order_state(report, bars, last_close, today)
    else:   # lens-wise: the top-level op_type is the primary lens's; each lens's order is judged from its own level
        order = "lens-wise, see per-lens lines"
        expired = False
    due = []
    ra = _parse_ts(nr.get("recommend_after")) if isinstance(nr, dict) else None
    if ra and now_utc >= ra:
        due.append(f"review time passed {(now_utc - ra).days}d ago")
    if has_tree:
        # 11-06 (GPT review): the horizon end is a date, not a price fact — expire the report even when no bars came
        sc_T = ((report.get("scenario") or {}).get("T_days")) or next((((e or {}).get("scenario") or {}).get("T_days")
                 for e in ((report.get("by_lens") or {}).values() if isinstance(report.get("by_lens"), dict) else []) if isinstance(e, dict)), None)
        exp = (nr.get("expires_at") if isinstance(nr, dict) else None) or dv.expires_on(base_day, sc_T)
        if exp and today > str(exp)[:10]:
            if div and div.get("level") != "expired":
                div = dict(div, level="expired", severity=1.0, T_days=div.get("T_days") or sc_T, expires_at=str(exp)[:10])
            elif div is None:
                div = {"level": "expired", "severity": 1.0, "T_days": sc_T, "expires_at": str(exp)[:10], "t": None, "move_atr": None,
                       "tail": None, "kl_bits": None, "dominant": None, "note": "no bars this breath; expired by the horizon date"}
    if div:
        rt = (f"tree expired: horizon {div.get('T_days')}d passed (expires_at {div.get('expires_at')}); divergence unavailable (no bars)"
              if div.get("t") is None and div.get("level") == "expired" else dv.reason_text(div, div.get("lens")))
        if rt:
            due.append(rt)
    due += [f"line hit: {h}" for h in hits]
    if expired:
        due.append("order expired unfilled")
    for x in lens_rows or []:
        if x["expired"]:
            due.append(f"order expired unfilled ({x['lens']})")
    out = {"due": due, "has_scenario_baseline": has_tree, "order": order, "manual": manual, "bars_through": last_day, "n_bars": len(bars), "divergence": div}
    if lens_rows is not None:
        out["lenses"] = [{k: v for k, v in x.items() if k != "expired"} for x in lens_rows]
    return out


def _refresh_bars(symbols):
    """--fetch: 连盈透把这些标的的日线刷一遍(与复盘结果价预取同一条路: horizon_sentinel.IbkrFetcher, 1h 内鲜则跳),
    一次连接跑完所有标的。需要 EQUIPOISE_IBKR_PORT。拉挂不抛, 该标的照旧按"无行情 / 旧行情"判。GENERAL* 拉它的指数镜头。"""
    try:
        import asyncio
        from horizon_sentinel import IbkrFetcher, is_symbol_data_fresh, DATA_FRESHNESS_SECONDS, load_helpers
        from workflows.postcheck_quant import _GENERAL_MEMBERS
        data_config, symbols_config, _ = load_helpers()
        ta_dir = data_config["data_sources"]["technical_analysis"]["output_dir"]
        todo = []
        for s in symbols:
            for m in _GENERAL_MEMBERS.get(s.upper(), [s.upper()]):
                if m not in todo and not is_symbol_data_fresh(m, ta_dir, DATA_FRESHNESS_SECONDS):
                    todo.append(m)
        if not todo:
            return

        async def _run():
            f = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
            try:
                for m in todo:
                    try:
                        await f.fetch_symbol(m)
                    except Exception as e:
                        print(f"[pre_round_brief] {m}: 日线刷新失败({type(e).__name__}: {e})", file=sys.stderr)
            finally:
                await f.close()
        asyncio.run(_run())
    except Exception as e:
        print(f"[pre_round_brief] 日线刷新整体失败({type(e).__name__}: {e}) — 按缓存里的行情判", file=sys.stderr)


def _daily_df(symbol):
    """最新日线 CSV → DataFrame(复盘同一读法); 没有返回 None。pandas 缺失也返回 None。"""
    try:
        from workflows.postcheck_quant import _load_daily_df
        return _load_daily_df(symbol)
    except Exception:
        return None


_LATEST_SQL = """
SELECT DISTINCT ON (symbol)
       symbol, as_of, report_generated_at, {score} AS composite_score, operation_type, operation_target,
       next_review, report, report->>'debate_summary' AS summary,
       report->'signatures'->>'zealot' AS eng
FROM debates
WHERE symbol = ANY(%s)
ORDER BY symbol, report_generated_at DESC NULLS LAST, as_of DESC
"""


def pull(symbols, now_utc, with_bars=True, fetch=False, df_source=None, include_base_day=False, div_cfg=None):
    """df_source(symbol)→DataFrame 可选: 替代读缓存 CSV 的日线来源(值班循环传它的"盈透新鲜日线 + CSV 合并")。
    include_base_day: 背离度 / 撞线与订单状态把报告当天那根也算进去(值班循环的报警口径; 简报与复盘不传, 保持老口径)。
    div_cfg: 背离度阈值(值班循环从 config/duty.yaml 传; 简报用缺省)。"""
    import psycopg2
    src = df_source or _daily_df
    if with_bars and fetch and df_source is None:
        _refresh_bars(symbols)
    from psycopg2.extras import RealDictCursor
    cfg = _pg_cfg()
    con = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
                           user=cfg["user"], password=cfg["password"],
                           connect_timeout=4, client_encoding="UTF8")
    rows_by_sym = {}
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            # 2026-10-01 改名: 评分列 action → composite_score; 库里的实际列名现查(迁移前后都能读)
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'debates'")
            cols = {r["column_name"] for r in cur.fetchall()}
            score = "action" if ("action" in cols and "composite_score" not in cols) else "composite_score"
            cur.execute(_LATEST_SQL.format(score=score), ([s.upper() for s in symbols],))
            for r in cur.fetchall():
                rows_by_sym[r["symbol"]] = r
            # 11-06: release dates for the read-time projection of pre-events next_review blocks (one query, last)
            try:
                cur.execute("SELECT symbol, array_agg(DISTINCT release_date) AS rds FROM fundamentals "
                            "WHERE symbol = ANY(%s) AND release_date IS NOT NULL GROUP BY symbol", ([s.upper() for s in symbols],))
                releases = {r["symbol"]: list(r["rds"] or []) for r in cur.fetchall()}
            except Exception:
                releases = {}
    finally:
        con.close()

    macro_cache = {}

    def _macro_rows(region):
        if region not in macro_cache:
            try:
                import yaml
                sched = yaml.safe_load(open(os.path.join(_ROOT, "config", "update_schedule.yaml"), encoding="utf-8")) or {}
                macro_cache[region] = _nre().macro_rows_from_schedule(sched, region)
            except Exception:
                macro_cache[region] = []
        return macro_cache[region]

    out = []
    for s in symbols:
        r = rows_by_sym.get(s.upper())
        if not r:
            out.append({"symbol": s, "history": False, "overdue_days": 9999,
                        "composite_score": None, "op_type": None, "recommend_after": None,
                        "notes": "", "summary": "", "eng": None, "last_gen": None})
            continue
        nr = r.get("next_review") or {}
        ra = _parse_ts(nr.get("recommend_after")) if isinstance(nr, dict) else None
        overdue = round((now_utc - ra).total_seconds() / 86400, 1) if ra else 9999
        notes = (nr.get("notes") or nr.get("reason") or "") if isinstance(nr, dict) else ""
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        rep = {**rep, "symbol": rep.get("symbol") or s, "next_review": rep.get("next_review") or nr,
               "operation_type": rep.get("operation_type") or r.get("operation_type"),
               "operation_target": rep.get("operation_target") or r.get("operation_target")}
        as_of = r.get("as_of") if isinstance(r.get("as_of"), datetime) else _parse_ts(r.get("as_of"))
        if as_of is not None and as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        # 11-06 (owner): a tree report written before the events format is read in the current one — expires_at from the
        # horizon, events[] projected from the known calendar (marked projected); the stored report stays as written.
        proj = None
        try:
            rds = [d for d in (releases.get(s.upper()) or []) if as_of is None or d <= as_of.date()]
            proj = project_next_review(rep, as_of, max(rds) if rds else None, _macro_rows(_regions().get(s.upper(), "US")))
        except Exception:
            proj = None
        if proj:
            rep["next_review"] = {**(rep.get("next_review") or {}), "expires_at": proj["expires_at"]}
        dr = due_reasons(rep, as_of, src(s) if with_bars else None, now_utc,
                         df_lookup=src if with_bars else None, include_base_day=include_base_day, div_cfg=div_cfg)
        out.append({
            "symbol": s, "history": True, "as_of": (as_of.isoformat() if as_of else None),   # ISO text: --json must stay serialisable
            "due": dr["due"], "order": dr["order"], "manual": dr["manual"], "bars_through": dr["bars_through"], "n_bars": dr["n_bars"],
            "divergence": dr.get("divergence"), "has_scenario_baseline": dr["has_scenario_baseline"],
            "events": (nr.get("events") if isinstance(nr, dict) and isinstance(nr.get("events"), list)
                       else (proj["events"] if proj else None)),
            "events_source": ("report" if isinstance(nr, dict) and isinstance(nr.get("events"), list)
                              else ("projected" if proj else None)),
            "expires_at": ((nr.get("expires_at") if isinstance(nr, dict) else None) or (proj["expires_at"] if proj else None)),
            "last_gen": (r["report_generated_at"] or r["as_of"]).isoformat() if (r["report_generated_at"] or r["as_of"]) else None,
            "composite_score": r["composite_score"], "op_type": r["operation_type"],
            "op_target": r["operation_target"],
            **({"lenses": dr["lenses"]} if "lenses" in dr else {}),
            "recommend_after": nr.get("recommend_after") if isinstance(nr, dict) else None,
            "overdue_days": overdue, "notes": _snip(notes, 90),
            "summary": _snip(r.get("summary"), 80),
            "eng": ("cc" if (r.get("eng") or "").startswith("claude") else (r.get("eng") or "")),
        })
    # 粗排: 到期原因多 / 逾期多 / 无历史在前(仅起点, CC 再据 GENERAL 基调微调)
    out.sort(key=lambda x: (-len(x.get("due") or []) if x["history"] else -99, -x["overdue_days"]))
    return out


def _fmt_div(d):
    """One cell for the reference value every dispatcher (a person's session or the duty Codex) reads before choosing:
    the report's own scenario tree against the latest price — level, move in ATR, trading days since the report."""
    if not isinstance(d, dict) or not d.get("level"):
        return "-"
    lens = f"{d['lens']} " if d.get("lens") else ""
    return f"{lens}{d['level']} {d.get('move_atr', 0):+.1f}σ/{d.get('t', 0):g}d"


def _print_table(rows, now_utc):
    print(f"# Pre-round brief  symbols={len(rows)}  now={now_utc.isoformat()}  (rough order = number of due reasons, overdue desc; the final order is yours, with the GENERAL tone)")
    print(f"{'SYM':8} {'eng':3} {'score':>5} {'op_type':14} {'overdue(d)':>10} {'tree (divergence)':18} {'due':28} order / notes")
    print("-" * 140)
    for x in rows:
        if not x["history"]:
            print(f"{x['symbol']:8} {'-':3} {'-':>5} {'NO_HISTORY':14} {'∞':>10} {'-':18} {'(never debated → build baseline)':28}")
            continue
        od = f"{x['overdue_days']:+.1f}" if x["overdue_days"] != 9999 else "no nr"
        due = "; ".join(x.get("due") or []) or ("-(no bars)" if not x.get("bars_through") else
                                                 ("-(bars not past base date)" if not x.get("n_bars") else "-"))
        tail = " | ".join(p for p in (x.get("order"), "; ".join(x.get("manual") or []), x["notes"] or x["summary"]) if p)
        print(f"{x['symbol']:8} {x['eng']:3} {str(x['composite_score']):>5} {str(x['op_type'] or '-'):14} {od:>10} {_fmt_div(x.get('divergence')):18} {_snip(due, 28):28} {tail}")
        for ln in x.get("lenses") or []:   # lens-wise report: one line per lens, its own operation and order
            print(f"  └ {ln['lens']:6} {str(ln['op_type'] or '-'):14} {str(ln['op_target'] or '-'):>10} "
                  f"{str(ln['op_size'] or '-'):16} {ln['order'] or '-'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us", choices=list(WAVES) or ["us", "eu", "jp", "kr"])
    ap.add_argument("--symbols", help="逗号分隔, 覆盖 --market")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="ISO 时刻(默认系统当前 UTC)")
    ap.add_argument("--no-bars", action="store_true", help="不读日线(只算重评时刻那一种到期)")
    ap.add_argument("--fetch", action="store_true", help="先连盈透刷新这些标的的日线(要 EQUIPOISE_IBKR_PORT), 再判撞线与订单到期")
    a = ap.parse_args()
    now_utc = _parse_ts(a.now) or datetime.now(timezone.utc)
    syms = _universe(a.market, a.symbols)
    if not syms:
        print("no symbols (check --market/--symbols)", file=sys.stderr)
        return 1
    rows = pull(syms, now_utc, with_bars=not a.no_bars, fetch=a.fetch)
    if a.json:
        print(json.dumps({"now": now_utc.isoformat(), "rows": rows}, ensure_ascii=False, indent=1))
    else:
        _print_table(rows, now_utc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
