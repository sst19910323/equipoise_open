#!/usr/bin/env python3
"""hydrate_pg_to_files.py — 把 PG 里最新的 technical / news 快照水合成 CC /run 需要的
本地【临时工作文件】(老格式 + 老命名)。

设计约束 (2026-07-01 定):
- PG (equipoise) 仍是【唯一真相】; 前端访问 + 复盘(postcheck) 都走 PG。
- 合并后 fetchers 只写 PG, 本地 data/ 文件早已冻结 → CC /run 的 subagent 只能 Read 文件,
  于是 fire /run 之前由编排层调本脚本, 把目标标的最新 PG 快照 dump 成老格式文件喂 subagent。
- 这些文件性质=临时/可丢弃 (下次水合覆盖出更新的 TS 版本); 不进 git, 不是真相。
- 水合六类: technical + news (STOP 级) + sentiment (fear_greed 老文件) + 父层最新报告
  (记忆继承"可有不强制": 备餐从 PG 判定鲜才水合; 文件只是载体) + economic + fundamentals
  (🔀 2026-07-09 文件为界: codex sandbox 连不上 PG/api, parent 改读运输文件贴 prompt;
  旧"parent 从 PG 查了注入"路线废止)。

用法:
  ./venv/bin/python hydrate_pg_to_files.py NVDA AAPL SPY
  ./venv/bin/python hydrate_pg_to_files.py --symbols NVDA,AAPL
  ./venv/bin/python hydrate_pg_to_files.py --only technical NVDA
  ./venv/bin/python hydrate_pg_to_files.py --as-of 2026-06-10T13:30:00Z NVDA   # 历史回放点

PG 无数据的类型 → 跳过并告警 (不编空文件)。退出码: 0 全部有产出 / 3 有标的 technical+news 都空。
"""
from __future__ import annotations
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from datalayer.pg.lowfreq_reader import (  # noqa: E402
    get_technical_snapshot, get_news_snapshot, get_sentiment_snapshot, get_debate_latest,
)

# 老 fear_greed 文件命名 (run.md Step 0e 期望的 pattern) + PG sentiment 表的 region 值
_SENT_FILE = {"US": "fear_greed_latest_{ts}.json",
              "EU": "fear_greed_eu_latest_{ts}.json",
              "JP": "fear_greed_jp_latest_{ts}.json",
              "KR": "fear_greed_kr_latest_{ts}.json"}
_SENT_PG_REGION = {"US": "US", "EU": "EZ", "JP": "JP", "KR": "KR"}
PARENT_MAX_AGE_S = 3600.0   # 对齐 data_sources.yaml ai_analysis.inheritance_max_age


def _iso_basic(dt) -> str:
    """任意 datetime / ISO 字符串 → 老文件名后缀 YYYYMMDDTHHMMSSZ (UTC)。"""
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # 已经是紧凑格式就直接用
            return dt.strip().replace("-", "").replace(":", "").split(".")[0].rstrip("Z") + "Z"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def hydrate_technical(sym: str, as_of=None) -> Path | None:
    snap = get_technical_snapshot(sym, as_of)
    payload = (snap or {}).get("payload")
    if not payload:
        local = sorted((ROOT / "data" / "technical" / sym.upper()).glob(f"{sym.upper()}_technical_*.json"))
        return local[-1] if local else None
    # 文件名 TS 用 payload 自带的市场数据时刻 (与老 fetcher 命名一致); 兜底用 as_of 时刻
    ts = payload.get(f"{sym.upper()}_market_data_timestamp_utc") if isinstance(payload, dict) else None
    if not ts:
        ts = _iso_basic((snap or {}).get("as_of") or datetime.now(timezone.utc))
    ts = _iso_basic(ts)
    out = ROOT / "data" / "technical" / sym.upper() / f"{sym.upper()}_technical_{ts}.json"
    # V16 (2026-10-25): the debaters read the thin projection (no chart series, confirmed pivots truncated 16/12/10 with
    # earlier_count); PG keeps the full payload. Compact serialization: 2-D arrays would blow past the size target indented.
    try:
        from analysis.snapshot_v16 import debater_view
        thin = debater_view(payload) if isinstance(payload, dict) else payload
    except Exception:
        thin = payload
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(thin, f, ensure_ascii=False, separators=(",", ":"))
    return out


def hydrate_news(sym: str, as_of=None) -> Path | None:
    snap = get_news_snapshot(sym, as_of)
    items = (snap or {}).get("items")
    if not items:
        local = sorted((ROOT / "data" / "news" / sym.upper()).glob(f"{sym.upper()}_news_*.json"))
        if not local and sym.upper() == "GENERAL":
            local = sorted((ROOT / "data" / "news" / "general").glob("*GENERAL_news_*.json"))
        return local[-1] if local else None
    ts = _iso_basic((snap or {}).get("fetched_at") or datetime.now(timezone.utc))
    out = ROOT / "data" / "news" / sym.upper() / f"{sym.upper()}_news_{ts}.json"
    return _write(out, items)  # 老 news 文件就是纯 list


def hydrate_sentiment(region: str = "US", as_of=None) -> Path | None:
    """PG sentiment → 老 fear_greed 文件。US schema 与爬虫一致
    ({"fear_greed": {value, source_date}, "vix": {...}}), sentiment_daily.parse_latest 兼容。"""
    region = region.upper()
    if region not in _SENT_FILE:
        return None
    rows = get_sentiment_snapshot(_SENT_PG_REGION[region], as_of)
    if not rows:
        return None
    payload = {"hydrated_from": "PG sentiment", "region": region}
    latest = ""
    for r in rows:
        ind = str(r.get("indicator") or "").strip()
        if not ind or r.get("value") is None:
            continue
        d = str(r.get("date") or "")[:10]
        payload[ind] = {"value": r.get("value"), "unit": r.get("unit"), "source_date": d}
        latest = max(latest, d)
    if len(payload) <= 2:   # 只有 meta 两个键 = 没有任何指标
        return None
    ts = _iso_basic(latest or datetime.now(timezone.utc))
    out = ROOT / "data" / "fear_greed" / _SENT_FILE[region].format(ts=ts)
    return _write(out, payload)


def _load_symbols_cfg() -> dict:
    try:
        import yaml
        with open(ROOT / "config" / "symbols.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


_REGION_GENERAL = {"EU": "GENERAL_EU", "JP": "GENERAL_JP", "KR": "GENERAL_KR"}


def symbol_region(symbol: str, sy_cfg: dict = None) -> str:
    """标的所属大区 (US/EU/JP/KR): GENERAL_<区> 按名字, 其余按 symbols.yaml region, 缺省 US。"""
    s = symbol.upper()
    for rg, gen in _REGION_GENERAL.items():
        if s == gen:
            return rg
    if s.startswith("GENERAL"):
        return "US"
    sy = sy_cfg if sy_cfg is not None else _load_symbols_cfg()
    info = (sy.get("symbol_contracts") or {}).get(s) or {}
    return str(info.get("region") or "US").upper()


def parent_symbol(symbol: str, sy_cfg: dict = None) -> str | None:
    """父标的路由(两版共用业务规则): EU/JP/KR→GENERAL_区; ETF→GENERAL;
    US 个股 有sector→板块ETF 无→GENERAL; GENERAL*→无父。
    (2026-09-30: 补 KR 分支——此前韩国个股落到 GENERAL, 读到美国大盘语境, topics/INDEX Q6)"""
    s = symbol.upper()
    if s.startswith("GENERAL"):
        return None
    sy = sy_cfg if sy_cfg is not None else _load_symbols_cfg()
    region = symbol_region(s, sy)
    if region in _REGION_GENERAL:
        return _REGION_GENERAL[region]
    info = (sy.get("symbol_contracts") or {}).get(s) or {}
    if s in (((sy.get("analysis_targets") or {}).get("etfs")) or []):
        return "GENERAL"
    return info.get("sector") or "GENERAL"


def sentiment_regions(symbols, sy_cfg: dict = None) -> set:
    """一批标的需要水合哪些区域的恐慌指数: US 永远要(run.md 共用必需), 加上各标的自己的大区。"""
    sy = sy_cfg if sy_cfg is not None else _load_symbols_cfg()
    regions = {"US"}
    for sym in symbols:
        regions.add(symbol_region(sym, sy))
    return regions


def _to_utc_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _with_slow_pressure(text: str, region: str, as_of=None) -> str:
    """Append the slow-pressure page to the US economic hand-off when config/slow_pressure.yaml says `in_material: true`
    (off by default; any failure leaves the economic text untouched)."""
    if (region or "US").upper() != "US":
        return text
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "slow_pressure.yaml").read_text(encoding="utf-8")) or {}
        if not cfg.get("in_material"):
            return text
        from datalayer.pg.slow_pressure import page
        pg = page(as_of)
        if not pg:
            return text
        doc = json.loads(text)
        doc["slow_pressure"] = pg
        return json.dumps(doc, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # the page is an addition; never cost the session its economic data
        print(f"[hydrate][WARN] slow-pressure page skipped: {type(exc).__name__}: {exc}")
        return text


def hydrate_economic(region: str = "US", as_of=None) -> Path | None:
    """PG economic → 临时运输文件(2026-07-09 文件为界: codex sandbox 连不上 PG/api, CC 为对称
    也读文件)。内容 = format_economic_for_prompt 的 prompt-ready 文本。PG 空/不通 → None(警告级)。
    🔴 不进 data_files(复现锚照旧 = market_data_timestamp_utc 查 /api/economic)。"""
    from datalayer.pg.lowfreq_reader import format_economic_for_prompt
    text = format_economic_for_prompt(region, as_of=as_of)
    if not text:
        return None
    text = _with_slow_pressure(text, region, as_of)
    ts = _iso_basic(as_of or datetime.now(timezone.utc))
    out = ROOT / "data" / "economic" / f"economic_{region.upper()}_{ts}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def hydrate_fundamentals(symbol: str, as_of=None) -> Path | None:
    """PG company+fundamentals → 临时运输文件(同上, 文件为界)。stock 场景才有意义。
    内容 = company 简介 + fundamentals prompt 文本拼接。PG 无 → None(警告级, agent 可申报搜索补)。"""
    from datalayer.pg.lowfreq_reader import format_company_for_prompt, format_fundamentals_for_prompt
    parts = [format_company_for_prompt(symbol) or "", format_fundamentals_for_prompt(symbol, as_of) or ""]
    text = "\n\n".join(p for p in parts if p.strip())
    if not text.strip():
        return None
    ts = _iso_basic(as_of or datetime.now(timezone.utc))
    sym = symbol.upper()
    out = ROOT / "data" / "fundamentals" / sym / f"{sym}_fundamentals_{ts}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def hydrate_parent_report(symbol: str, as_of=None, max_age_s: float = PARENT_MAX_AGE_S) -> Path | None:
    """父层最新报告 PG→文件 (CC 版记忆继承是推荐项)。只给鲜的(默认 1h, 对齐
    inheritance_max_age); 超龄不写 —— 背景没对齐的旧记忆不如重跑父层。"""
    parent = parent_symbol(symbol)
    if not parent:
        return None
    row = get_debate_latest(parent, as_of)
    rep, p_as_of = (row or {}).get("report"), (row or {}).get("as_of")
    if not rep or not p_as_of:
        return None
    ref = _to_utc_dt(as_of) or datetime.now(timezone.utc)
    p_dt = _to_utc_dt(p_as_of)
    if p_dt is None or (ref - p_dt).total_seconds() > max_age_s:
        print(f"[{symbol.upper()}] parent {parent} 报告超 {max_age_s/3600:.0f}h 不鲜, 不水合 (要继承先重跑父层)")
        return None
    if isinstance(rep, str):
        try:
            rep = json.loads(rep)
        except Exception:
            pass
    ts = _iso_basic(p_as_of)
    out = ROOT / "data" / "debate" / parent / f"{parent}_Analysis_{ts}.json"
    return _write(out, rep)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hydrate PG snapshots to CC /run 临时文件")
    ap.add_argument("symbols", nargs="*", help="标的 (空格分隔), 也可用 --symbols 逗号分隔")
    ap.add_argument("--symbols", dest="symbols_csv", default="", help="逗号分隔标的")
    ap.add_argument("--only", choices=["technical", "news", "sentiment", "parent"], help="只水合某一类")
    ap.add_argument("--as-of", dest="as_of", default=None, help="历史回放点 UTC (默认取最新)")
    args = ap.parse_args()

    syms = list(args.symbols)
    if args.symbols_csv:
        syms += [s for s in args.symbols_csv.split(",") if s.strip()]
    syms = [s.strip().upper() for s in syms if s.strip()]
    if not syms:
        print("usage: hydrate_pg_to_files.py <SYM> [SYM ...]   (或 --symbols A,B)", file=sys.stderr)
        return 2

    do_tech = args.only in (None, "technical")
    do_news = args.only in (None, "news")
    do_sent = args.only in (None, "sentiment")
    do_parent = args.only in (None, "parent")

    if do_sent:
        for rg in sorted(sentiment_regions(syms)):
            sf = hydrate_sentiment(rg, args.as_of)
            print(f"[sentiment/{rg}] {sf.relative_to(ROOT) if sf else '(PG空)'}")

    empties = []
    for sym in syms:
        tf = hydrate_technical(sym, args.as_of) if do_tech else None
        nf = hydrate_news(sym, args.as_of) if do_news else None
        t_disp = tf.relative_to(ROOT) if tf else ("(PG空)" if do_tech else "-")
        n_disp = nf.relative_to(ROOT) if nf else ("(PG空)" if do_news else "-")
        print(f"[{sym}] technical: {t_disp}")
        print(f"[{sym}] news:      {n_disp}")
        if do_parent:
            pf = hydrate_parent_report(sym, args.as_of)
            print(f"[{sym}] parent:    {pf.relative_to(ROOT) if pf else '(无/不鲜, 记忆继承本就是可选项)'}")
        if do_tech and not tf and do_news and not nf:
            empties.append(sym)

    if empties:
        print(f"\n⚠️ 两类都空 (PG 无数据, 先拉行情/新闻): {', '.join(empties)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
