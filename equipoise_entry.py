#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equipoise_entry.py - Equipoise Backend (V13.3 - Strict V13 Only)
核心修正:
1. [Strict Mode] _extract_core_signal: 仅支持最新的扁平化 JSON 结构。
   不再尝试读取 final_report 嵌套字段，强制要求前端/AI生成符合新标准。
2. Dashboard: 聚合所有标的最新状态。
"""

import re
import json
import yaml
import uvicorn
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from datalayer.pg import i18n   # 2026-10-24 报告多语言: ?lang=, 默认 EQUIPOISE_DEFAULT_LANG(缺省 zh)
from workflows.execution_card import ensure_display_fields   # 2026-11-03 执行卡: 读时对老报告现算(与组装器同一函数)

LANG_DOC = (f"报告语言: {' / '.join(i18n.SUPPORTED_LANGS)}; 省略=服务器默认({i18n.DEFAULT_LANG})。"
            "没有该语言译文时回退原文, 实际给的语言看 lang_served")

# --- 初始化 ---
app = FastAPI(title="Equipoise API V13", version="13.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 路径配置 ---
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_CACHE_DIR = PROJECT_ROOT / "data"
CONFIG_SYMBOLS = PROJECT_ROOT / "config" / "symbols.yaml"
CONFIG_DATA = PROJECT_ROOT / "config" / "data_sources.yaml"

def get_results_dir() -> Path:
    """动态获取结果目录，失败则回退到默认"""
    default_path = PROJECT_ROOT / "results" / "debate"
    if not CONFIG_DATA.exists(): return default_path
    try:
        with open(CONFIG_DATA, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
            rel = cfg.get("data_sources", {}).get("ai_analysis", {}).get("output_dir")
            if rel: return (PROJECT_ROOT / rel).resolve()
    except: pass
    return default_path

RESULTS_DIR = get_results_dir()
print(f"📂 API Serving Results from: {RESULTS_DIR}")

# --- 宏观标的列表 (按 symbols.yaml `analysis_targets.generals` 配置) ---
# GENERAL 系列以 symbols.yaml 为 SoT, 三个内置: GENERAL (US) / GENERAL_EU / GENERAL_JP
# 每个虚拟标的在 symbol_contracts 里有 region + currency + runnable 声明。

def list_general_symbols(cfg=None) -> List[Dict[str, Any]]:
    """读 symbols.yaml 的 GENERAL 系列, 返回前端 item 列表 (含 region/currency/manual_only)."""
    fallback = [{"symbol": "GENERAL", "type": "general", "currency": "USD", "region": "US"}]
    if cfg is None:
        if not CONFIG_SYMBOLS.exists():
            return fallback
        with CONFIG_SYMBOLS.open(encoding="utf-8") as source:
            cfg = yaml.safe_load(source) or {}

    targets = cfg.get('analysis_targets', {}) or {}
    contracts = cfg.get('symbol_contracts', {}) or {}
    generals = targets.get('generals') or ["GENERAL"]

    region_curr_default = {"US": "USD", "EU": "EUR", "JP": "JPY"}
    out: List[Dict[str, Any]] = []
    for sym in generals:
        sym_u = str(sym).upper()
        info = contracts.get(sym_u, {}) or {}
        region = (info.get('region') or "US").upper()
        item: Dict[str, Any] = {
            "symbol": sym_u,
            "type": "general",
            "currency": info.get('currency') or region_curr_default.get(region, "USD"),
            "region": region,
        }
        if info.get('runnable') is False:
            item["manual_only"] = True
        out.append(item)
    return out or fallback

# --- 辅助: 路径安全检查 ---
def safe_path(rel_path: str) -> Path:
    try:
        clean_path = Path(rel_path.lstrip("/\\"))
        abs_path = (PROJECT_ROOT / clean_path).resolve()
        # 严格限制在项目根目录下
        if not str(abs_path).startswith(str(PROJECT_ROOT)):
            raise ValueError
        return abs_path
    except:
        raise HTTPException(status_code=403, detail="Invalid path or access denied")

# =================================================================
# 核心业务接口
# =================================================================

@app.get("/api/symbol-tree", tags=["Core"])
def get_symbol_tree() -> List[Dict[str, Any]]:
    """Dashboard navigation by region, active research group and standalone instrument."""
    from workflows.symbol_universe import research_group

    if not CONFIG_SYMBOLS.exists():
        return []
    with CONFIG_SYMBOLS.open(encoding="utf-8") as source:
        cfg = yaml.safe_load(source) or {}
    targets = cfg.get("analysis_targets", {})
    contracts = cfg.get("symbol_contracts", {})
    etfs = set(targets.get("etfs", []))
    regions = {
        region: dict(region=region, region_label=label, groups=[], research_groups=[])
        for region, label in (("US", "美国"), ("EU", "欧洲"), ("JP", "日本"), ("KR", "韩国"))
    }

    def item(symbol, kind):
        info = contracts.get(symbol, {})
        region = info.get("region", "US")
        row = dict(symbol=symbol, type=kind, region=region, currency=info.get("currency") or "USD")
        if info.get("runnable") is False:
            row["manual_only"] = True
        return row

    for entry in list_general_symbols(cfg):
        regions[entry["region"]]["groups"].append(dict(group="宏观", group_en="Macro", items=[entry]))

    assigned = set()
    for key in cfg.get("research_groups", {}):
        group = research_group(key, cfg)
        region = regions[group["region"]]
        members = group["members"]
        if assigned.intersection(members):
            raise ValueError(f"Duplicate actionable member in {key}")
        assigned.update(members)
        label = group.get("label", key)
        label_en = group.get("label_en", key)
        region["research_groups"].append(dict(symbol=key, region=group["region"], label=label,
            label_en=label_en, members=members, benchmark=group["benchmark"], references=group.get("references", [])))
        region["groups"].append(dict(group=label, group_en=label_en, research_group=key,
            items=[item(s, "etf" if s in etfs else "stock") for s in members]))

    standalone = {region: [] for region in regions}
    for kind, field in (("stock", "stocks"), ("etf", "etfs")):
        for symbol in targets.get(field, []):
            if symbol in assigned:
                continue
            entry = item(symbol, kind)
            standalone[entry["region"]].append(entry)
    for region, items in standalone.items():
        remaining = {entry["symbol"]: entry for entry in items}
        for section in cfg.get("standalone_sections", {}).get(region, []):
            rows = [remaining.pop(s) for s in section["symbols"] if s in remaining]
            if rows:
                regions[region]["groups"].append(dict(group="独立研究 · " + section["label"],
                    group_en="Standalone · " + section["label_en"], items=rows))
        if remaining:
            regions[region]["groups"].append(dict(group="独立研究", group_en="Standalone", items=list(remaining.values())))
    return [region for region in regions.values() if region["groups"]]


@app.get("/api/symbol-groups", tags=["Core"])
def get_symbol_groups() -> List[Dict[str, Any]]:
    """Flat projection of the same navigation tree."""
    groups = [group for region in get_symbol_tree() for group in region["groups"]]
    return groups or [{"group": "宏观", "group_en": "Macro", "items": list_general_symbols()}]


@app.get("/api/symbols", tags=["Core"])
def get_symbols() -> List[Dict[str, str]]:
    """Visible instruments in dashboard order."""
    return [{"symbol": row["symbol"], "type": row["type"]}
            for group in get_symbol_groups() for row in group["items"]]

# --- API 2: 获取历史报告列表 ---
@app.get("/api/reports/{symbol}", tags=["Core"])
def get_symbol_reports(
    symbol: str,
    months: float = Query(2, description="只返回最近 N 个月的报告(按 as_of); 0=不限(全历史)"),
    lang: str = Query(None, description=LANG_DOC),
) -> List[Dict[str, Any]]:
    """
    返回该标的历史报告元数据，按时间倒序。默认只给最近 2 个月, 要全历史传 months=0。
    数据源: PG debates 表。`path` 是 PG 句柄 (pg/debates/<SYM>/<ts>), 传给 /api/file 取整份 jsonb。
    """
    try:
        from datalayer.pg.lowfreq_reader import list_debates
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    sym = symbol.upper()
    rows = list_debates(sym, months=months)
    want = i18n.norm_lang(lang)
    report_list = []
    for r in rows:
        as_of = r.get("as_of")
        ts_compact = _fmt_compact_ts(as_of)
        try:
            unix_ts = int(as_of.timestamp()) if isinstance(as_of, datetime) else 0
        except Exception:
            unix_ts = 0
        report_list.append({
            "filename": f"{sym}_Analysis_{ts_compact}.json",
            "timestamp": unix_ts,
            "date": _fmt_pg_date(r.get("report_generated_at") or as_of),
            "path": f"pg/debates/{sym}/{ts_compact}",
            "as_of": ts_compact,
            "composite_score": r.get("composite_score"),
            "action": r.get("composite_score"),   # 旧名, 前端改完后去掉
            "op_type": r.get("operation_type"),
            # 多语言: 这一期原文语言 / 已有语言 / 按请求 lang 打开时实际会给的语言
            "lang_source": r.get("lang") or "zh",
            "lang_available": [r.get("lang") or "zh"] + [l for l in (r.get("i18n_langs") or []) if l != (r.get("lang") or "zh")],
            "lang_served": want if want in ([r.get("lang") or "zh"] + list(r.get("i18n_langs") or [])) else (r.get("lang") or "zh"),
            **_now_fields(r, sym),   # `now` (primary card's action word), `lens_split` (lens cards disagree), `now_simple` (simple card's); each omitted when absent
        })
    report_list.sort(key=lambda x: x['timestamp'], reverse=True)
    return report_list


def _now_fields(r: Dict[str, Any], symbol: Optional[str] = None) -> Dict[str, Any]:
    """`now` = the stored primary execution card's action word (old reports without a card: omitted — the list does not
    recompute cards); `lens_split` = True only for a lens-wise report whose lens cards do not all say the same `now`;
    `now_simple` = the simple card's action word in the same vocabulary (2026-11-10; a v3.4 report's card is projected
    once and remembered; reports before v3.4 have no simple card and no field)."""
    out: Dict[str, Any] = {}
    try:
        from workflows.simple_projection import now_simple_for_row
        from datalayer.pg.lowfreq_reader import get_debate_report
        word = now_simple_for_row(r, symbol or "", get_debate_report)
        if word:
            out["now_simple"] = word
    except Exception:
        pass
    now = r.get("card_now")
    if isinstance(now, str) and now:
        out["now"] = now
    lens_nows = r.get("lens_nows")
    if isinstance(lens_nows, list):
        words = {str(x) for x in lens_nows if isinstance(x, str) and x}
        if len(lens_nows) > 1 and len(words) > 1:
            out["lens_split"] = True
    return out

# --- API 3: 万能文件读取 ---
@app.get("/api/file", tags=["Core"])
def get_file_content(
    path: str = Query(..., description="相对路径, 或 PG 句柄 pg/debates/<SYM>/<ts>"),
    lang: str = Query(None, description=LANG_DOC + "。只对 PG 报告句柄生效"),
) -> Any:
    """
    读取报告内容。
    - PG 句柄 `pg/debates/<SYM>/<ts>` → 返回 debates 表对应 as_of 的整份 report jsonb (主用)。
      ?lang= 选语言: 正文字段换成译文(辩论过程永远原文), 顶层多带 lang_served / lang_source / lang_available。
    - 普通相对路径 → 读 data 缓存 / results 下的 JSON 文件 (技术/新闻原始文件等仍走文件)。
    """
    # PG 句柄: pg/debates/<SYMBOL>/<YYYYMMDDTHHMMSSZ>
    if path.startswith("pg/debates/"):
        parts = path.split("/")
        if len(parts) != 4 or not parts[2] or not parts[3]:
            raise HTTPException(status_code=400, detail="Bad PG handle, expect pg/debates/<SYM>/<ts>")
        sym, ts = parts[2].upper(), parts[3]
        try:
            from datalayer.pg.lowfreq_reader import get_debate_report_row
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")
        row = get_debate_report_row(sym, ts, exact=True)
        if not row or not row.get("report"):
            raise HTTPException(status_code=404, detail=f"No debate report for {sym} @ {ts}")
        ensure_display_fields(row["report"])   # 2026-11-03 执行卡: 老报告读时现算(与组装器同一函数), 前端不分新旧
        doc, served = i18n.localize_report(row["report"], row.get("lang"), row.get("report_i18n"), lang)
        doc = dict(doc)
        doc.update(i18n.lang_meta(row.get("lang") or i18n.detect_lang(row["report"]), row.get("report_i18n"), served))
        return doc

    abs_path = safe_path(path)

    is_data = str(abs_path).startswith(str(DATA_CACHE_DIR))
    is_result = str(abs_path).startswith(str(RESULTS_DIR))

    if not (is_data or is_result):
        raise HTTPException(status_code=403, detail="Access denied: Path outside allowed data dirs")

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    with open(abs_path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            f.seek(0)
            return {"raw_content": f.read()}

# --- API 4: 宏观经济快照 (只读隔壁 apex_codex 数据脑) ---
@app.get("/api/economic", tags=["Core"])
def get_economic(
    region: str = Query("US", description="区域: US / EU(→EZ) / EZ / UK / JP"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻的最新数据(复现历史报告, 传报告的 market_data_timestamp_utc); 省略=当前最新"),
) -> Dict[str, Any]:
    """
    某区 point-in-time 宏观快照 (每指标取截至 as_of 的 release_time 最新一行)。
    as_of 省略=当前最新(实时); 给时间戳=复现那一刻。
    报告不再存 economic 文件路径, 用报告统一时间戳重查此接口即可复现。
    数据源: equipoise(PG) economic 表, 隔壁 apex_codex 写本工程只读。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_economic_snapshot, REGION_MAP
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    snap = get_economic_snapshot(region, as_of=as_of)
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    latest = max((str(r.get("release_time")) for r in snap if r.get("release_time")), default=None)
    return {
        "region": db_region,
        "as_of_query": as_of,
        "latest_release": latest,
        "available": bool(snap),
        "indicators": snap,
    }

# --- API 6: 市场情绪 (sentiment 表 point-in-time; US=fear_greed/EZ=vstoxx/JP=nikkei_vi) ---
@app.get("/api/sentiment", tags=["Core"])
def get_sentiment(
    region: str = Query("US", description="区域: US(fear_greed) / EU(→EZ, vstoxx) / JP(nikkei_vi)"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻最新(复现历史报告); 省略=当前最新"),
) -> Dict[str, Any]:
    """
    市场情绪 point-in-time 快照。按区: US=fear_greed(0-100) / EZ=vstoxx / JP=nikkei_vi。
    报告不再存 fear&greed 路径, 用报告统一时间戳重查此接口即可复现。
    数据源: equipoise(PG) sentiment 表, 隔壁 apex_codex 写本工程只读。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_sentiment_snapshot, REGION_MAP
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    snap = get_sentiment_snapshot(region, as_of=as_of)
    db_region = REGION_MAP.get((region or "US").upper(), (region or "US").upper())
    latest = max((str(r.get("date")) for r in snap if r.get("date")), default=None)
    return {
        "region": db_region,
        "as_of_query": as_of,
        "latest_date": latest,
        "available": bool(snap),
        "indicators": snap,
    }

# --- API 5: 公司基本面 (company 静态 + fundamentals 可变 point-in-time) ---
@app.get("/api/fundamentals", tags=["Core"])
def get_fundamentals(
    symbol: str = Query(..., description="标的, 如 AAPL"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻的最新基本面(复现历史报告, 传报告的 market_data_timestamp_utc); 省略=当前最新"),
) -> Dict[str, Any]:
    """
    公司基本面: company(静态信息, 每标的1行) + fundamentals(可变, 每 field/span 取截至 as_of 最新一行)。
    报告不再存基本面文件路径, 用报告统一时间戳重查此接口即可复现。
    数据源: equipoise(PG), 隔壁 apex_codex 写本工程只读。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_company_profile, get_fundamentals_snapshot
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    prof = get_company_profile(symbol)
    snap = get_fundamentals_snapshot(symbol, as_of=as_of)
    latest = max((str(r.get("release_date")) for r in snap if r.get("release_date")), default=None)
    return {
        "symbol": symbol,
        "as_of_query": as_of,
        "latest_release": latest,
        "available": bool(prof or snap),
        "profile": prof,
        "fundamentals": snap,
    }

# --- API 7: 证据 (evidence 表 point-in-time; 证据官 Scout 产出, 复现辩论当时看到的搜索证据) ---
@app.get("/api/evidence", tags=["Core"])
def get_evidence(
    symbol: str = Query("GENERAL", description="标的; v1 只 GENERAL 自搜, 其它标的继承 GENERAL"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻的最新一批证据(复现历史报告, 传报告的 evidence 时间戳); 省略=当前最新"),
    max_age_hours: float = Query(1.0, description="证据最大时效(小时); 找到的证据若早于 as_of(或当前时间) 超过此值则视为过期, 返回 available=false; 0=不限"),
) -> Dict[str, Any]:
    """
    证据 point-in-time 快照: 某标的截至 as_of 最新的那一批 Scout 证据(整批 list)。
    辩论内不读此表(证据现生成现喂); 此接口仅供复现/前端展示。
    数据源: equipoise(PG) evidence 表, equipoise 自己写(best-effort)。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_evidence_snapshot
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    snap = get_evidence_snapshot(symbol, as_of=as_of, max_age_hours=max_age_hours)
    # PG jsonb does not preserve object key order. Keep the legacy four fields
    # first for old renderers, but pass through the v2 data-leg envelope and any
    # future fields. In particular payload_text is the archived source content;
    # source_file is lineage only and must never replace it.
    items = []
    for item in (snap.get("items") or []):
        if not isinstance(item, dict):
            continue
        source = item.get("source") or []
        if isinstance(source, str):
            source = [source]
        elif not isinstance(source, list):
            source = []
        normalized = {
            "question": item.get("question") or item.get("task") or "",
            "task": item.get("task") or item.get("question") or "",   # 旧名
            "simple": item.get("simple") or "",
            "detailed": item.get("detailed") or "",
            "source": source,
        }
        normalized.update({k: v for k, v in item.items() if k not in normalized})
        items.append(normalized)
    return {
        "symbol": (symbol or "GENERAL").upper(),
        "as_of_query": as_of,
        "max_age_hours": max_age_hours,
        "generated_at": snap.get("generated_at"),
        "available": bool(items),
        "items": items,
    }

# --- API 7b: task 体系(独立表, 索引+懒加载详情; 契约见 docs/handoff/FRONTEND-TASK-EVIDENCE-API.md) ---
@app.get("/api/model-calls", tags=["Core"])
@app.get("/api/tasks", tags=["Core"])   # 旧路径, 前端改完后去掉
def get_model_calls_api(
    symbol: str = Query(..., description="标的"),
    as_of: str = Query(..., description="辩论 as_of(ISO, 与 /api/reports 行一致, 精确匹配)"),
    id: str = Query(None, description="模型调用场内 id(如 Z.MC1; 历史场次是 TK1); 省略=索引行列表, 给定=单条全量"),
) -> Dict[str, Any]:
    """模型调用台账: 省略 id 返回轻量索引行(折叠区直接渲染), 点开传 id 加载全量(含 request 申报原文与 detail)。
    空列表=该场零模型调用(合法)。行里用新键 model_call_id / model / suggested_evidence_tier, 同时带旧键
    task_id / playbook / tier_eligible; 响应里 model_calls 与 tasks 是同一个列表(旧键与 tasks 前端改完后去掉)。"""
    try:
        from datalayer.pg.task_reader import get_model_calls
        rows = get_model_calls(symbol, as_of, model_call_id=id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"task_reader: {e}")
    if id and not rows:
        raise HTTPException(status_code=404, detail=f"model call {id} not found @ {symbol} {as_of}")
    return {"symbol": symbol.upper(), "as_of": as_of, "count": len(rows),
            "detail": bool(id), "model_calls": rows, "tasks": rows}


@app.get("/api/evidence/items", tags=["Core"])
def get_evidence_items_api(
    symbol: str = Query(..., description="标的"),
    as_of: str = Query(..., description="辩论 as_of(ISO, 精确匹配)"),
    id: str = Query(None, description="证据场内 id(如 EV2); 省略=索引行, 给定=单条全量"),
) -> Dict[str, Any]:
    """证据逐条行(evidence_items): 索引行含 evidence_id/model_call_id/kind/status/verdict/question/simple,
    库里有 verified_scope 列时一并返回(仲裁者核定为事实的范围); 详情另含 detailed/payload_text(数据调用全文)/
    source/generated_at。verdict=执行者的结论(supports/refutes/mixed/not_found/unrelated), status=仲裁者的核定
    (verified/unverified/not_found/failed), 两者语义不同; 老行 verdict 可为 null。
    model_call_id=null = 正文直属的搜索或数据调用。行里同时带旧键 task_id(前端改完后去掉)。
    老接口 /api/evidence 保留为整批 point-in-time 快照。"""
    try:
        from datalayer.pg.task_reader import get_evidence_items
        rows = get_evidence_items(symbol, as_of, evidence_id=id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"task_reader: {e}")
    if id and not rows:
        raise HTTPException(status_code=404, detail=f"evidence {id} not found @ {symbol} {as_of}")
    return {"symbol": symbol.upper(), "as_of": as_of, "count": len(rows),
            "detail": bool(id), "items": rows}


# --- API 8: 技术快照 (technical 表 point-in-time; 复现/前端展示辩论当时的技术面) ---
@app.get("/api/technical", tags=["Core"])
def get_technical(
    symbol: str = Query(..., description="标的, 如 AAPL / SPY / GENERAL"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻的最新技术快照(复现, 传报告 market_data_timestamp_utc); 省略=当前最新"),
) -> Dict[str, Any]:
    """
    某标的 point-in-time 技术快照 (整份计算好的技术指标 payload)。
    数据源: equipoise(PG) technical 表, equipoise 自己写(best-effort, 与辩论同时双写)。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_technical_snapshot
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    snap = get_technical_snapshot(symbol, as_of=as_of)
    return {
        "symbol": (symbol or "").upper(),
        "as_of_query": as_of,
        "as_of": snap.get("as_of"),
        "available": bool(snap.get("payload")),
        "payload": snap.get("payload"),
    }

# --- API 9: 新闻 (news 表 point-in-time; 复现/前端展示辩论当时的新闻批) ---
@app.get("/api/news", tags=["Core"])
def get_news(
    symbol: str = Query(..., description="标的, 如 AAPL / SPY / GENERAL"),
    as_of: str = Query(None, description="UTC时刻(ISO); 返回截至该刻最新抓取的那批新闻(复现); 省略=当前最新"),
) -> Dict[str, Any]:
    """
    某标的 point-in-time 新闻批 (一次抓取的文章 list)。
    数据源: equipoise(PG) news 表, equipoise 自己写(best-effort, 抓取时双写)。
    """
    try:
        from datalayer.pg.lowfreq_reader import get_news_snapshot
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")

    snap = get_news_snapshot(symbol, as_of=as_of)
    items = snap.get("items") or []
    return {
        "symbol": (symbol or "").upper(),
        "as_of_query": as_of,
        "fetched_at": snap.get("fetched_at"),
        "available": bool(items),
        "count": len(items),
        "items": items,
    }

# --- API 10: 后验/回测 (postcheck 表) — 回测是"单期报告的衍生品" ---
#   单期: (标的 + as_of 定哪一期) + (档位 dt 定 3d/2week/2month)
@app.get("/api/postcheck", tags=["Core"])
def get_postcheck_api(
    symbol: str = Query(..., description="标的, 如 GENERAL / AAPL"),
    as_of: str = Query(None, description="UTC时刻(ISO); 定位那一期报告(取截至该刻最新一期); 省略=最新一期"),
    dt: str = Query(None, description="档位多选: 3d/20d/50d 或交易日数, 逗号分隔(如 '3d,20d'); 空=该期全部档位"),
    lang: str = Query(None, description=LANG_DOC + "。复盘只有 verdict 一句有译文, diagnosis 永远原文"),
) -> Dict[str, Any]:
    """某标的某期报告的后验(回测)。回测是单期报告的衍生品: (标的+as_of)定哪期, dt 定哪个/哪些档位(空=全部)。"""
    try:
        from datalayer.pg.postcheck_pg_store import get_postcheck, localize_items
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"postcheck_pg_store 不可用: {e}")
    res = get_postcheck(symbol, as_of=as_of, dts=dt)
    items = localize_items(res.get("items") or [], lang)
    return {
        "symbol": (symbol or "").upper(), "as_of_query": as_of,
        "as_of": res.get("as_of"), "requested_tiers": dt or None,
        "available": bool(items), "count": len(items), "items": items,
    }

# 列表: 前端表格/历史 (可按标的、档位多选/空=不限 过滤)
@app.get("/api/postcheck/list", tags=["Core"])
def list_postcheck_api(
    symbol: str = Query(None, description="标的过滤; 省略=全部"),
    dt: str = Query(None, description="档位多选 3d/20d/50d 或整数, 逗号分隔; 空=全部。前端分表: 3d 表传 '3d', 20d 表传 '20d'"),
    limit: int = Query(200, description="最多返回行数"),
    months: float = Query(2, description="只返回最近 N 个月(按报告 as_of); 0=不限(全历史)"),
    fields: str = Query(None, description="'summary' = 轻量模式: 只回 symbol/as_of/dt/ruler/score/reckoner/operation_type/operation_execution/region/cohort/lesion/ret/mae/mfe, 不带正文(2026-10-23 给 atlas)"),
    lang: str = Query(None, description=LANG_DOC + "。全量模式下 verdict 按语言; summary 模式不带正文不受影响"),
) -> Dict[str, Any]:
    """后验列表 (前端表格/历史)。默认只给最近 2 个月, 要全历史传 months=0。fields=summary 走轻量模式(同期多把尺只回最新尺)。"""
    if (fields or "").strip().lower() == "summary":
        from datalayer.pg import postcheck_agg
        from datalayer.pg.postcheck_pg_store import parse_tiers
        rows = postcheck_agg.list_summary(postcheck_agg.fetch_rows(dts=parse_tiers(dt), symbol=symbol, months=months), limit=limit)
        return {"symbol": (symbol or "").upper() or None, "requested_tiers": dt or None, "months": months or None,
                "fields": "summary", "count": len(rows), "items": rows}
    try:
        from datalayer.pg.postcheck_pg_store import list_postcheck, localize_items
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"postcheck_pg_store 不可用: {e}")
    rows = localize_items(list_postcheck(symbol=symbol, dts=dt, limit=limit, months=months), lang)
    return {"symbol": (symbol or "").upper() or None, "requested_tiers": dt or None,
            "months": months or None, "count": len(rows), "items": rows}

# 统计: 前端总览 (数量/均分/复盘率/三档分布/按回测人)
@app.get("/api/postcheck/stats", tags=["Core"])
def postcheck_stats_api(
    symbol: str = Query(None, description="标的过滤; 省略=全部"),
    dt: str = Query(None, description="档位多选 3d/20d/50d 或整数, 逗号分隔; 空=全部"),
    months: float = Query(0, description="只统计最近 N 个月(按 as_of); 默认 0=不限, 保持历史统计口径"),
) -> Dict[str, Any]:
    """后验统计聚合 (前端总览)。统计默认全历史(口径不悄悄变), 要近期口径显式传 months。"""
    try:
        from datalayer.pg.postcheck_pg_store import postcheck_stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"postcheck_pg_store 不可用: {e}")
    res = postcheck_stats(symbol=symbol, dts=dt, months=months) or {}
    try:   # 2026-10-23 atlas 需求 3.4: 按 dt / 尺子 / 评分员(带 dt)分栏, 每场取最新尺
        from datalayer.pg import postcheck_agg
        from datalayer.pg.postcheck_pg_store import parse_tiers
        res.update(postcheck_agg.stats_breakdown(postcheck_agg.fetch_rows(dts=parse_tiers(dt), symbol=symbol, months=months)))
    except Exception as e:
        res["breakdown_error"] = f"{type(e).__name__}: {e}"
    return res


# --- 后验分段 / 分组 / 配对统计(2026-10-23, 给 equipoise_atlas 复盘页; 需求稿见 atlas docs/handoff/SCORES-API-REQUEST-2026-10-21.md) ---
def _agg_rows(dts=None, ruler=None):
    try:
        from datalayer.pg import postcheck_agg
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"postcheck_agg 不可用: {e}")
    return postcheck_agg, postcheck_agg.fetch_rows(dts=dts, ruler=ruler)


@app.get("/api/postcheck/cohorts", tags=["Core"])
def postcheck_cohorts_api() -> Dict[str, Any]:
    """分段(cohort)定义与各段 d3 / d20 汇总(n/均值/中位/sd/≥80/<50)、评分员与尺子分布。边界精确到分钟, to 为不含上界, 最新段 to=null。"""
    agg, rows = _agg_rows()
    return agg.cohorts_payload(rows)


@app.get("/api/postcheck/agg", tags=["Core"])
def postcheck_agg_api(
    dt: int = Query(..., description="档位 3 | 20"),
    region: str = Query(None, description="US / EU / JP / KR"),
    cohort: str = Query(None, description="C0 … C10"),
    reckoner: str = Query(None, description="评分员"),
    ruler: str = Query(None, description="尺子; 省略 = 每场取最新尺"),
    group_by: str = Query(None, description="cohort | op | execution | region | reckoner | week | score_bin | lesion | ruler | symbol(单选)"),
    from_: str = Query(None, alias="from", description="as_of 下限(ISO, 含)"),
    to: str = Query(None, description="as_of 上限(ISO, 不含)"),
) -> Dict[str, Any]:
    """通用分组汇总: 只统计 score 非空; 同一 (symbol, as_of, dt) 多把尺默认取最新尺; week 按 ISO 周一(UTC); score_bin 固定 10 分一桶;
    execution 按 ENTRY 派生后的市价 / 限价分(v3.2 起), 旧场按旧枚举映射。"""
    agg, rows = _agg_rows(dts=[dt], ruler=ruler)
    try:
        out = agg.aggregate(rows, dt=dt, region=region, cohort=cohort, reckoner=reckoner, group_by=group_by, from_=from_, to=to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if ruler:
        out["filters"]["ruler"] = ruler
    return out


@app.get("/api/postcheck/paired", tags=["Core"])
def postcheck_paired_api(
    region: str = Query(None), cohort: str = Query(None),
    reckoner20: str = Query(None, description="只约束 d+20 行的评分员(母工程'astra 同尺'口径)"),
    reckoner3: str = Query(None, description="可选, 约束 d+3 行的评分员"),
    ruler: str = Query(None, description="尺子; 省略 = 每场取最新尺"),
    min_n: int = Query(10, description="n 小于它的段 shown=false, 仍返回 n 让前端判断"),
) -> Dict[str, Any]:
    """d+20 对 d+3 配对表: 配对键 (symbol, as_of), 两档都有分才配对; mixed_reckoners 为 true 时前端打星号。"""
    agg, rows = _agg_rows(dts=[3, 20], ruler=ruler)
    return agg.paired(rows, region=region, cohort=cohort, reckoner20=reckoner20, reckoner3=reckoner3, min_n=min_n)

# =================================================================
# Dashboard 聚合接口 (Strict V13 Mode)
# =================================================================

def _fmt_compact_ts(v) -> str:
    """PG datetime/str → 紧凑 YYYYMMDDTHHMMSSZ (UTC, URL 安全)。失败 → 原样字符串。"""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        m = re.search(r"\d{8}T\d{6}Z", s)
        return m.group(0) if m else s


def _fmt_pg_date(v) -> Optional[str]:
    """PG datetime/str → 'YYYY-MM-DD HH:MM:SS UTC' 展示串。失败 → 原样。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return s


def _protection_summary(view: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the machine phase beside localized copy for compact dashboard views."""
    if not view.get("hedge_summary"):
        return {}
    result = {"hedge_summary": view["hedge_summary"]}
    protection = view.get("downside_protection")
    if isinstance(protection, dict) and isinstance(protection.get("phase"), str):
        result["downside_protection"] = {"phase": protection["phase"]}
    return result


def _lens_summary(report: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
    """v3.4.1 lens-wise report (GENERAL = SPY/QQQ/DIA, GENERAL_EU = DAX/IAEX): each lens's own operation, in the frozen
    lens order. The top-level op_type is the primary (first) lens's and op_target the joined "L=level; ..." string, so a
    reader pairs the two only through this per-lens view. None for every other report."""
    by = report.get("by_lens")
    if not isinstance(by, dict):
        return None
    order = [str(x) for x in (report.get("lenses") or [])] or [str(x) for x in by]
    result = {}
    for lens in order:
        card = by.get(lens)
        if isinstance(card, dict):
            result[lens] = {k: card.get(k) for k in ("operation_type", "operation_target", "operation_size", "execution_card", "operation_simple")}
            result[lens].update(_protection_summary(card))
    return result


def _signal_from_debate(row: Dict[str, Any], lang: Optional[str] = None) -> Dict[str, Any]:
    """从 debates 行(决策列 + report jsonb)提取 Dashboard 核心字段。决策列优先, 缺则回退 report。
    lang: 正文(目前只有 short_term_path.rationale)按语言取, 没译文回退原文; 实际语言回在 lang_served。"""
    ensure_display_fields(row.get("report") or {})   # 2026-11-03 执行卡(老报告现算; 覆盖层的 _leaves 在 localize 里套上)
    report, served = i18n.localize_report(row.get("report") or {}, row.get("lang"), row.get("report_i18n"), lang)
    st = (report.get("short_term_path") or report.get("short_term") or {})   # 2026-09-30 改名, 旧报告兜底
    def pick(col, jkey, default):
        v = row.get(col)
        if v is None:
            for k in ((jkey,) if isinstance(jkey, str) else jkey):   # 新名优先, 旧名兜底
                v = report.get(k)
                if v is not None:
                    break
        return v if v is not None else default
    score = pick("composite_score", ("composite_score", "action"), 0)
    size = pick("operation_size", ("operation_size", "operation_volume"), "-")
    path = {
        "direction":    st.get("direction") or "-",
        "target":       st.get("target") or "-",
        "horizon_days": st.get("horizon_days") or 0,
        "rationale":    st.get("rationale") or "",
    }
    out = {
        "composite_score": score,
        "op_type":      pick("operation_type", "operation_type", "WAITING"),
        "op_target":    pick("operation_target", "operation_target", "-"),
        "execution_card": report.get("execution_card"),   # 2026-11-03: kind / now / trigger / stop / one_line(按 ?lang=)
        "operation_simple": report.get("operation_simple"),   # 2026-11-10: the simple card, same structure as /api/file (price; source = projected when computed at read time)
        "unresolved_claims": report.get("unresolved_claims"),
        "operation_size": size,
        "short_term_path": path,
        # 本场参考价(脚本从技术快照机械注入, 每条带自己的行情时刻; 旧报告没有这个字段 → 空列表)
        "reference_prices": report.get("reference_prices") or [],
        "lang_served": served,
        # 旧名, 前端改完后去掉
        "action_score": score, "op_volume": size, "short_term": path,
    }
    out.update(_protection_summary(report))
    lens_view = _lens_summary(report)
    if lens_view is not None:   # lens-wise: op_type is the primary lens's; each lens's own operation is here
        out["by_lens"] = lens_view
    return out

@app.get("/api/dashboard", tags=["Dashboard"])
def get_dashboard_summary(lang: str = Query(None, description=LANG_DOC)):
    """
    看板接口: 返回所有标的最新状态。?lang= 选正文语言, 每条带 lang_served。
    """
    dashboard_data = []

    targets = get_symbols()

    # 3. 一次 SQL 取所有标的最新决策 (N+1 → 1; 数据源 PG debates 表)
    try:
        from datalayer.pg.lowfreq_reader import get_debate_latest_batch
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lowfreq_reader 不可用: {e}")
    latest_map = get_debate_latest_batch()   # 一条 SQL 取全部标的最新; 非展示的老标的 targets 里没有自然忽略

    for item in targets:
        symbol = item['symbol']

        entry = {
            "symbol": symbol,
            "type": item['type'],
            "last_updated": None,
            "composite_score": None,
            "op_type": "WAITING",
            "op_target": "-",
            "operation_size": "-",
            "short_term_path": None,
            "reference_prices": [],
            "lang_served": None,
            "action_score": None, "op_volume": "-", "short_term": None,   # 旧名, 前端改完后去掉
            "filename": None,
            "path": None,
        }

        row = latest_map.get(symbol)
        if row:
            signal = _signal_from_debate(row, lang)
            ts_compact = _fmt_compact_ts(row.get("as_of"))
            entry["last_updated"] = _fmt_pg_date(row.get("report_generated_at") or row.get("as_of"))
            for k in ("composite_score", "op_type", "op_target", "operation_size", "short_term_path", "execution_card", "operation_simple",
                      "reference_prices", "lang_served", "action_score", "op_volume", "short_term"):
                entry[k] = signal.get(k)
            for key in ("hedge_summary", "downside_protection"):
                if key in signal:
                    entry[key] = signal[key]
            if "by_lens" in signal:   # lens-wise report only (additive: single-lens entries keep their shape)
                entry["by_lens"] = signal["by_lens"]
            entry["filename"]     = f"{symbol}_Analysis_{ts_compact}.json"
            entry["path"]         = f"pg/debates/{symbol}/{ts_compact}"

        dashboard_data.append(entry)

    # 4. 排序: 等待中的沉底，有动作的置顶，然后按 Symbol 排序
    dashboard_data.sort(key=lambda x: (x['op_type'] == 'WAITING', x['symbol']))

    return dashboard_data

# --- 启动入口 ---
# --- 值班循环(2026-11-04): 哪些可能要更新、哪些正在跑、最近的决定。来源 PG duty_events(datalayer/pg/duty_store.py);
#     写入方只有 scripts/duty_status.py(breath)与 scripts/duty.sh → duty_record.py(decision / launch); Hermes 与前端只读 ---
@app.get("/api/duty", tags=["Core"])
def duty_api(
    decisions: int = Query(10, description="最近几条值班决定"),
    live: bool = Query(True, description="正在跑的单元现查 systemd; False = 用最近一次呼吸快照里的"),
) -> Dict[str, Any]:
    """值班状态页。`breath` = 最近一次呼吸(每 ~10 分钟一次)的快照: `markets.<m>.due[]` 可能要更新的标的(symbol / region / reasons /
    skips / last_run / overdue_days / score / op_type / order), `waiting_for_window[]` 窗外等待, `deferred[]` 被值班 Codex 推迟的(until /
    why / deferred_by), `next_due`, `capacity`, `usage`(额度摘要), `recommended_harness`, `exit`(0 可以叫人 1 没事 2 盈透挂 3 没额度 4 池满);
    `running[]` 正在跑的单元(unit / harness / symbols / since; `running_source` 说明现查还是快照); `decisions[]` 最近的决定(launch /
    defer / dropped / skipped / note / harness / refused); `launches[]` 最近起的单元。"""
    import importlib.util as _ilu
    from datetime import datetime as _dt, timezone as _tz
    try:
        from datalayer.pg import duty_store
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"duty_store 不可用: {e}")
    try:
        b = duty_store.latest_breath()
        decs = duty_store.recent("decision", max(1, min(int(decisions), 100)))
        launches = duty_store.recent("launch", 20)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"PG 不可用: {e}")
    breath = None
    if b:
        p = b.get("payload") or {}
        keep = ("used_percent", "stop_used_pct", "available", "debates_left", "today_left", "per_day_budget", "burn",
                "resets_at", "sampled_at", "stale_minutes", "cost_per_debate_pct",
                "weekly_units_at_85", "units_left", "reserved_units", "units_per_trading_day", "units_launched_today")
        usage = {h: {k: v for k, v in u.items() if k in keep} for h, u in (p.get("usage") or {}).items() if isinstance(u, dict)}
        breath = {"at": b["at"].isoformat(), "wake": b["wake"], "exit": p.get("exit"), "exit_reason": p.get("exit_reason"), "ibkr": p.get("ibkr"),
                  "capacity": p.get("capacity"), "recommended_harness": p.get("recommended_harness"), "usage": usage,
                  "markets": p.get("markets"), "total_due": p.get("total_due"), "reset_window": p.get("reset_window"),
                  "dispatch_jobs": p.get("dispatch_jobs"),
                  "memory": p.get("memory")}
    running, source = None, "breath"
    if live:
        try:
            spec = _ilu.spec_from_file_location("duty_status", PROJECT_ROOT / "scripts" / "duty_status.py")
            m = _ilu.module_from_spec(spec); spec.loader.exec_module(m)
            running, source = m.running_units(m.dispatch_jobs(m.load_symbol_config()), strict=True), "systemd"
        except Exception:
            running = None
    if running is None and b:
        running = (b.get("payload") or {}).get("running")

    capacity = dict((breath or {}).get("capacity") or {})
    capacity.pop("launch_cap", None)
    if source == "systemd":
        capacity = m.pool_capacity(running, capacity.get("max_parallel", int(m._cfg().get("max_parallel", 8))))

    # The weekly reading is sampled once per breath. Recompute headroom against
    # that same sample using the jobs returned by this request.
    usage = {}
    for harness, sample in ((breath or {}).get("usage") or {}).items():
        reserved = sum(job["cost_units"] for job in running or []
                       if job.get("harness") == harness and job.get("cost_units") is not None)
        used, stop, budget = (sample.get(k) for k in ("used_percent", "stop_used_pct", "weekly_units_at_85"))
        left = None if None in (used, stop, budget) else round(max(0, (stop - used) * budget / 85 - reserved), 2)
        usage[harness] = dict(units_left=left, reserved_units=reserved,
                              units_per_trading_day=sample.get("units_per_trading_day"),
                              weekly_units_at_85=budget, sampled_at=sample.get("sampled_at"))
    def _dec(d):
        p = d.get("payload") or {}; acc = p.get("accepted") or {}; raw = p.get("decision") or {}
        return {"at": d["at"].isoformat(), "wake": d["wake"], "harness": d["harness"], "refused": p.get("refused"),
                "launch": acc.get("launch"), "defer": acc.get("defer"), "dropped": p.get("dropped"),
                "skipped": raw.get("skipped"), "note": raw.get("note"), "skips": p.get("skips")}

    from workflows.duty_completions import recent_completions
    completions = recent_completions() if live else None
    return {"at": _dt.now(_tz.utc).isoformat(), "breath": breath, "running": running or [], "running_source": source,
            "completions": completions,
            "capacity": capacity or None, "usage": usage,
            "decisions": [_dec(d) for d in decs],
            "launches": [{"at": l["at"].isoformat(), "wake": l["wake"], "harness": l["harness"], "market": l["market"],
                          "unit": (l.get("payload") or {}).get("unit"), "symbols": (l.get("payload") or {}).get("symbols"),
                          "key": (l.get("payload") or {}).get("key"), "cost_units": (l.get("payload") or {}).get("cost_units")}
                         for l in launches]}

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Equipoise API Server (V13.3 Strict New Only)")
    print(f"📁 Root: {PROJECT_ROOT}")
    print(f"📊 Results: {RESULTS_DIR}")
    print(f"🌐 URL: http://0.0.0.0:8001")
    print("=" * 60)

    # 生产: reload=False (热重载只在开发用; 关掉省一个文件监视进程 + 少抖动)
    uvicorn.run("equipoise_entry:app", host="0.0.0.0", port=8001, reload=False)
