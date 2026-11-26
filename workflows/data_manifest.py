#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_manifest.py — RAW DATA FILES 清单(重建批 3, 2026-09-30)。

一份代码, 两处使用: 数据准备(prepare_debate_data)在 READY 回执里打印它; 编排(run_step open)把它
原样拼进辩手的 spawn 材料。清单每行 = 路径 + 一句内容说明 + 缺料警告; 编排层与仲裁者都不读这些
文件的内容(语境最小化), 辩手用 Read 自取。

同时给出前端契约的 data_files 字典(key 固定, 只填真实存在的路径)、数据时戳锚(DATA_TIMESTAMP,
取技术快照文件名后缀, 不用墙钟)、标的语境手册路径、触发信号(trend_context / dealer_gamma 的
trigger_signals, 旧名 beacons 兼容读)。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Must equal config/symbols.yaml symbol_contracts.GENERAL_*.lenses in order (run_step open refuses otherwise);
# guarded by scripts/test_general_lens_config.py. JP / KR second lenses from 2026-11-02 (TOPIX index, KOSDAQ 150 index).
GENERAL_LENSES = {"GENERAL": ["SPY", "QQQ", "DIA"], "GENERAL_EU": ["DAX", "IAEX"],
                  "GENERAL_JP": ["N225", "TOPX"], "GENERAL_KR": ["KODEX200", "KOSDQ150"]}
CONTEXT_MANUAL = {"GENERAL": "prompts/agent/manuals/evidence/context_general.md",
                  "GENERAL_EU": "prompts/agent/manuals/evidence/context_general_eu.md",
                  "GENERAL_JP": "prompts/agent/manuals/evidence/context_general_jp.md",
                  "GENERAL_KR": "prompts/agent/manuals/evidence/context_general_kr.md",
                  "etf": "prompts/agent/manuals/evidence/context_etf.md",
                  "stock": "prompts/agent/manuals/evidence/context_stock.md"}
_REGION_OF_GENERAL = {"GENERAL": "US", "GENERAL_EU": "EU", "GENERAL_JP": "JP", "GENERAL_KR": "KR"}


def _latest(pattern: str, root: Path) -> Path | None:
    hits = sorted(root.glob(pattern))
    return hits[-1] if hits else None


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def _ts_of(p: Path) -> str:
    return p.name.rsplit("_", 1)[1][:-5]


def load_symbols_cfg(root: Path = ROOT) -> dict:
    import yaml
    return yaml.safe_load((root / "config" / "symbols.yaml").read_text(encoding="utf-8")) or {}


def classify(symbol: str, cfg: dict) -> tuple[str, str, dict]:
    """→ (context_type, region, contract)。合约级 context_type 覆盖优先(非美借框架不入列表)。"""
    contracts = cfg.get("symbol_contracts") or {}
    contract = contracts.get(symbol) or {}
    if symbol in GENERAL_LENSES:
        return "general", _REGION_OF_GENERAL[symbol], contract
    if contract.get("context_type"):
        ctx = str(contract["context_type"])
    elif symbol in ((cfg.get("analysis_targets") or {}).get("etfs") or []):
        ctx = "etf"
    else:
        ctx = "stock"
    return ctx, str(contract.get("region") or "US").upper(), contract


def _read_signals(path: Path, key_new: str, key_old: str) -> list[str]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    v = obj.get(key_new)
    if v is None:
        v = obj.get(key_old)
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in (v or [])]


def build_manifest(symbol: str, root: Path = ROOT, cfg: dict | None = None) -> dict:
    symbol = symbol.upper()
    cfg = cfg or load_symbols_cfg(root)
    ctx, region, contract = classify(symbol, cfg)
    benchmark, sector = contract.get("benchmark"), contract.get("sector")
    files: list[dict] = []
    missing: list[str] = []
    warnings: list[str] = []
    data_files: dict = {"macro": {}, "benchmarks": {}, "sector": {}, "config": "config/symbols.yaml"}

    def add(path: Path, desc: str, key: tuple | None = None, required: bool = True) -> None:
        rel = _rel(path, root)
        files.append({"path": rel, "desc": desc})
        if key:
            section, name = key
            if section == "symbol":
                data_files.setdefault("symbol", {})[name] = rel
            elif section == "lenses":
                data_files.setdefault("lenses", {})[name] = rel
            else:
                data_files[section][name] = rel

    def tech(sym: str) -> Path | None:
        return _latest(f"data/technical/{sym}/{sym}_technical_*.json", root)

    ts = None
    if ctx == "general":
        lens_files = {l: tech(l) for l in GENERAL_LENSES[symbol]}
        if any(v is None for v in lens_files.values()):
            missing.append(f"lens technical snapshots missing ({[l for l, f in lens_files.items() if f is None]})")
        else:
            ts = max(_ts_of(f) for f in lens_files.values())
            for l, f in lens_files.items():
                add(f, f"{l} lens technical snapshot (quantitative indicators, prefix {l}_)", ("lenses", l))
    else:
        t = tech(symbol)
        if t is None:
            missing.append(f"data/technical/{symbol}/ own technical snapshot (needs IBKR market data)")
        else:
            ts = _ts_of(t)
            add(t, "own technical snapshot (sole truth for quantitative indicators; includes session_context and relative strength vs benchmark)", ("symbol", "technical"))
        for name, sym_ in (("benchmark", benchmark), ("sector", sector)):
            if not sym_:
                continue
            f = tech(sym_)
            if f is None:
                missing.append(f"data/technical/{sym_}/ ({name} technical snapshot)")
            else:
                add(f, f"{name} {sym_} technical snapshot (relative position before absolute indicators)",
                    ("benchmarks" if name == "benchmark" else "sector", sym_))
    # 情绪 / 宏观新闻 / 经济(US 全场景)
    fg = _latest("data/fear_greed/fear_greed_latest_*.json", root)
    if fg is None:
        missing.append("data/fear_greed/ (US sentiment)")
    else:
        add(fg, "US sentiment (fear & greed and volatility readings)", ("macro", "fear_greed_us"))
    gn = _latest("data/news/GENERAL/GENERAL_news_*.json", root)
    if gn is None:
        missing.append("data/news/GENERAL/ (macro news)")
    else:
        add(gn, "macro news (list; grade before citing, see principles section 1)", ("macro", "news_general_us"))
    econ = _latest("data/economic/economic_US_*.txt", root)
    if econ:
        add(econ, "US macro economy (prompt-ready text, one line per region/indicator/value/consensus/release_time; derive prior values and surprises yourself from the multi-line history)")
    else:
        warnings.append("US economic data missing (may file an evidence search as fallback)")
    if region != "US":
        r = region.lower()
        for pat, key, label in ((f"data/economic/economic_{region}_*.txt", None, f"{region} regional economy"),
                                (f"data/fear_greed/fear_greed_{r}_latest_*.json", f"fear_greed_{r}", f"{region} regional sentiment"),
                                (f"data/news/GENERAL_{region}/GENERAL_{region}_news_*.json", f"news_general_{r}",
                                 f"{region} regional macro news")):
            f = _latest(pat, root)
            if f is None:
                warnings.append(f"{label} missing (may file an outcall in the first round)")
            else:
                add(f, label + (" (prompt-ready text)" if key is None else ""), ("macro", key) if key else None)
    # 个股/ETF 新闻、基本面、板块新闻
    if ctx in ("stock", "etf"):
        n = _latest(f"data/news/{symbol}/{symbol}_news_*.json", root)
        if n is None:
            if ctx == "stock" and region == "US":
                missing.append(f"data/news/{symbol}/ (US single-name news)")
            else:
                warnings.append("own news missing (non-US single name: data preparation runs the Linkup leg; if still missing, the orchestration can run `/refresh-news <SYM>`; may file an evidence search in the first round)")
        else:
            add(n, "own news (list; an item may also appear in sector or macro news; count the same article once)", ("symbol", "news"))
        if sector:
            sn = _latest(f"data/news/{sector}/{sector}_news_*.json", root)
            if sn:
                add(sn, f"news fetched under the {sector} code (not the full set of industry news; grade before citing)", ("sector", f"{sector}_news"))
    if ctx == "stock":
        fu = _latest(f"data/fundamentals/{symbol}/{symbol}_fundamentals_*.txt", root)
        if fu:
            add(fu, "fundamentals (TTM basis; judge freshness by time since the latest quarter; do not pretend fields absent from the snapshot exist)")
        else:
            warnings.append("no fundamentals file (may file an evidence search in request_evidence)")
    files.append({"path": "config/symbols.yaml", "desc": "routing info (benchmark, sector, region, currency)"})
    pc = _latest(f"data/evidence_prep/{symbol}/{symbol}_peer_compare_*.json", root)
    if pc:
        add(pc, "peer comparison (peer-group relative performance over the same window; ok:false means no evidence map, which is itself a fact)")
    trigger_signals: list[str] = []
    # V16 (2026-10-25): the trend-structure numbers live in the technical snapshot (trend_signal_inputs, ladders); the
    # trigger signals are derived from the same file the debaters read, at the same information time. GENERAL has no
    # single own snapshot; its lenses are not signalled (as before).
    own_snap = _latest(f"data/technical/{symbol}/{symbol}_technical_*.json", root)
    if own_snap and ctx != "general":
        try:
            import json as _json
            from workflows.trend_context import trigger_signals_from_snapshot
            trigger_signals += trigger_signals_from_snapshot(_json.loads(Path(own_snap).read_text(encoding="utf-8")))
        except Exception as exc:
            warnings.append(f"trigger signals could not be derived from the technical snapshot ({exc})")
    latest_dg: dict[str, Path] = {}
    for dg in sorted(root.glob(f"data/evidence_prep/{symbol}/{symbol}_dealer_gamma_*.json")):
        und = dg.name.split("_dealer_gamma_")[1].rsplit("_", 1)[0]
        latest_dg[und] = dg
    for und, dg in latest_dg.items():
        add(dg, f"{und} dealer gamma positioning profile (GEX, zero-gamma level, the two walls, ATM implied move; a reference, not a switch)")
        trigger_signals += _read_signals(dg, "trigger_signal", "beacon")
    return {"ok": not missing, "symbol": symbol, "context_type": ctx, "region": region,
            "benchmark": benchmark, "sector": sector, "ts": ts, "currency": contract.get("currency"),
            "context_manual": CONTEXT_MANUAL.get(symbol) or CONTEXT_MANUAL[ctx],
            "lenses": GENERAL_LENSES.get(symbol),
            "files": files, "missing": missing, "warnings": warnings,
            "data_files": data_files, "trigger_signals": trigger_signals}


def render_raw_data_files(m: dict) -> str:
    lines = [f"- {f['path']}   ({f['desc']})" for f in m["files"]]
    if m["warnings"]:
        lines.append("Missing-material warnings: " + "; ".join(m["warnings"]))
    return "\n".join(lines)


def render_trigger_signals(m: dict) -> str:
    return "\n".join(m["trigger_signals"]) if m["trigger_signals"] else "(no trigger signals)"


if __name__ == "__main__":
    import sys
    for sym in sys.argv[1:] or ["GENERAL"]:
        m = build_manifest(sym)
        print(json.dumps({k: v for k, v in m.items() if k != "files"}, ensure_ascii=False, indent=1))
        print(render_raw_data_files(m))
