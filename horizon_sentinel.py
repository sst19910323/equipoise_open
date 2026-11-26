#!/usr/bin/env python3
"""
Horizon Sentinel - AI Parliament Scheduler (V9.0 - DAG-Driven Concurrent)

核心逻辑:
1. [DAG 调度] 根据 symbols.yaml 的 sector 字段构依赖图：
     GENERAL → 板块 ETF / 宽基 ETF / 无板块个股 → 有板块个股(等其 sector 完成)
2. [LLM 并发] 全局 Semaphore 控辩论并发上限 (MAX_DEBATE_CONCURRENT=20)。
3. [IBKR 串行节流] 单 fetcher 协程，所有 IBKR 请求串行 + 强制 ≥1s 间隔。
4. [JIT 数据] 每个标的开始辩论前检查其依赖数据鲜度 (≤1h)，过期则入 fetch 队列。
5. [子等父结束] child 必须等 parent 的辩论 analysis 结果完整产出才入 pool。
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import json
import random
import yaml
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Tuple, Optional

# --- 引入市场日历库 ---
import pandas_market_calendars as mcal

# -----------------------------------------------------------------
# 路径设置
# -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# --- 导入组件 ---
try:
    from analysis.technical_snapshot_builder import process_symbol_technical_analysis
    from datalayer.fetchers.interactive_stock_fetcher import MarketDataFetcher, MarketDataConfig, SymbolContract
    from run_debate import DebateEngine
except ImportError as e:
    print(f"CRITICAL ERROR: Failed to import required modules. {e}")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# 配置常量 & 交易日辅助函数
# ──────────────────────────────────────────────────────────────────────────────
EASTERN = ZoneInfo("America/New_York")
RUN_TIMES = [dtime(9, 0)]
CHECK_INTERVAL = 60

# 调度并发参数
MAX_DEBATE_CONCURRENT = 20        # LLM 辩论同时进行的最大数量 (DeepSeek 默认 500 并发, 此处实际瓶颈是 IBKR 串行拉数据)
IBKR_REQUEST_INTERVAL = 1.0       # IBKR 两次请求之间最小间隔(秒)
DATA_FRESHNESS_SECONDS = 3600     # 技术数据鲜度阈值(1小时)，超过则重拉

# 宏观基准ETF（GENERAL 三镜头分析所需，DIA 不在 etfs 列表所以必须显式拉）
BENCHMARK_SYMBOLS = ["SPY", "QQQ", "DIA"]
EU_BENCHMARK_SYMBOLS = ["DAX", "IAEX"]
JP_BENCHMARK_SYMBOLS = ["N225"]
KR_BENCHMARK_SYMBOLS = ["KODEX200"]  # 2026-09-28: K200 指数 HMDS 断供, 切 KODEX200 ETF

def get_trading_calendar(exchange: str = 'NYSE') -> mcal.MarketCalendar:
    try:
        return mcal.get_calendar(exchange)
    except Exception as e:
        logging.critical(f"Failed to get trading calendar: {e}")
        raise

def is_trading_day(calendar: mcal.MarketCalendar, check_date: datetime) -> bool:
    date_to_check = check_date.date()
    try:
        schedule = calendar.schedule(start_date=date_to_check, end_date=date_to_check)
        return not schedule.empty
    except Exception as e:
        logging.warning(f"Failed to check trading day for {date_to_check}: {e}. Defaulting to True.")
        return True

def get_now_et():
    return datetime.now(EASTERN)

# ──────────────────────────────────────────────────────────────────────────────
# Context Assembler (数据组装工)
# ──────────────────────────────────────────────────────────────────────────────
def _combine_deduped_news(deduped: Dict[str, str], priority_order: List[str]) -> str:
    """把多层 dedup 后的新闻合并为单一 JSON list，priority_order 决定保留顺序。"""
    combined = []
    for key in priority_order:
        s = deduped.get(key, "")
        if not s:
            continue
        try:
            items = json.loads(s)
            if isinstance(items, list):
                combined.extend(items)
        except json.JSONDecodeError:
            continue
    return json.dumps(combined, ensure_ascii=False)


def _dedup_news_by_id(news_layers: List[Tuple[str, str]]) -> Dict[str, str]:
    """跨层去重 Finnhub 新闻 (按 id)。同 id 保留高优先级层，低层删除。

    Args:
        news_layers: [(key_name, json_string), ...]，按优先级降序排列
                     (个股 > 板块 > 宏观)。空 / 缺失层用 "" 或 None 占位。

    Returns:
        {key_name: deduped_json_string}。原本为空的层保持 "[]"。
    """
    seen_ids = set()
    out: Dict[str, str] = {}
    for key, json_str in news_layers:
        if not json_str:
            out[key] = "[]"
            continue
        try:
            items = json.loads(json_str)
        except json.JSONDecodeError:
            out[key] = json_str
            continue
        if not isinstance(items, list):
            out[key] = json_str
            continue
        kept = []
        for item in items:
            iid = item.get('id') if isinstance(item, dict) else None
            if iid is None:
                kept.append(item)
                continue
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            kept.append(item)
        out[key] = json.dumps(kept, ensure_ascii=False)
    return out


class ContextAssembler:
    def __init__(self, data_config: Dict, symbols_config: Optional[Dict] = None,
                 inheritance_enabled: bool = True):
        self.full_cfg = data_config.get('data_sources', {})
        self.root = PROJECT_ROOT
        self.contracts = (symbols_config or {}).get('symbol_contracts', {})
        self.inheritance_enabled = inheritance_enabled

    def _get_path_str(self, path: Path) -> str:
        if not path: return None
        try:
            return str(path.relative_to(self.root)).replace("\\", "/")
        except:
            return str(path).replace("\\", "/")

    def _tech_text_for(self, symbol: str, as_of=None) -> Tuple[Optional[str], Optional[str]]:
        """技术快照文本 + 路径串, 统一读 PG technical 表 (不再读 JSON 文件)。
        as_of=None → 最新一份; 给时刻 → 截至该刻最新。返回 (text_or_None, path_str_or_None)。"""
        clean = symbol.upper().replace(" ", ".")
        from datalayer.pg.lowfreq_reader import get_technical_snapshot
        snap = get_technical_snapshot(clean, as_of)
        if not snap or not snap.get("payload"):
            return (None, None)
        return (json.dumps(snap["payload"], ensure_ascii=False, default=str),
                f"pg:technical@{snap.get('as_of')}")

    def _news_text_for(self, symbol: str, as_of=None) -> Tuple[Optional[str], Optional[str]]:
        """新闻文本 + 路径串, 统一读 PG news 表 (不再读 JSON 文件)。
        as_of=None → 最新一批; 给时刻 → 截至该刻最新。返回 (text_or_None, path_str_or_None)。"""
        clean = symbol.upper().replace(" ", ".")
        from datalayer.pg.lowfreq_reader import get_news_snapshot
        snap = get_news_snapshot(clean, as_of)
        if not snap or not snap.get("items"):
            return (None, None)
        return (json.dumps(snap["items"], ensure_ascii=False, default=str),
                f"pg:news@{snap.get('fetched_at')}")

    def _build_benchmark_data(self, symbols: List[str], as_of=None) -> Tuple[str, Dict[str, str]]:
        """拼接给定 symbols 的技术分析文本为单个 benchmark_data 字符串。
        symbols 由调用方决定 (GENERAL=三镜头, 个股/板块=自己的主基准, 宽基=其他宽基横向对比)。
        as_of 给定时走 PG 历史快照 (经 _tech_text_for)。
        """
        sections: List[str] = []
        used_paths: Dict[str, str] = {}

        for sym in symbols:
            content, path = self._tech_text_for(sym, as_of)
            if content is None:
                continue
            sections.append(f"#### {sym} 技术分析\n{content}")
            if path:
                used_paths[sym] = path

        return "\n\n".join(sections), used_paths

    def _load_sector_layer(self, sector_symbol: str, as_of=None) -> Tuple[str, str, Dict[str, str]]:
        """加载板块 ETF 的技术分析 + 新闻。返回 (tech_json, news_json, paths)。
        缺失部分用空字符串占位，dedup 阶段会处理。as_of 给定时走 PG 历史快照。"""
        paths: Dict[str, str] = {}
        tech_json, tech_path = self._tech_text_for(sector_symbol, as_of)
        tech_json = tech_json or ""
        if tech_path:
            paths["technical"] = tech_path

        news_json, news_path = self._news_text_for(sector_symbol, as_of)
        news_json = news_json or ""
        if news_path:
            paths["news"] = news_path

        return tech_json, news_json, paths

    def _render_general_evidence(self, as_of=None) -> str:
        """取最新 fresh 的 GENERAL 报告里的证据官产出(读 PG debates), 只渲染简单结论(task+simple)。
        供任何非 GENERAL 标的辩论继承; detailed 太长不下传。无 fresh / 无证据 → ''。
        历史回放(as_of 给定)时不下传证据(evidence 无 point-in-time 回放) → ''。"""
        if as_of is not None:
            return ""
        from datalayer.pg.lowfreq_reader import get_debate_latest
        ai_cfg = self.full_cfg.get('ai_analysis', {})
        max_age = float(ai_cfg.get('inheritance_max_age', 3600))
        row = get_debate_latest("GENERAL")
        if not row:
            return ""
        last = _to_dt(row.get("as_of"))
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > max_age:
            return ""   # 没有够新的 GENERAL 报告 → 不下传证据
        gj = row.get("report") or {}
        evidence = gj.get('evidence', []) or []
        lines = []
        for it in evidence:
            task   = (it.get('task') or '').strip()
            simple = (it.get('simple') or '').strip()
            if task and simple:
                lines.append(f"- {task}：{simple}")
            elif simple:
                lines.append(f"- {simple}")
        if not lines:
            return ""
        return (
            "## 大盘证据官查证结论（继承自 GENERAL，仅结论）\n"
            "以下是大盘辩论中证据官外部搜索得到的关键结论（精简），作为背景参考，非 ground truth：\n"
            + "\n".join(lines)
        )

    def _load_inheritance(self, symbol: str, as_of=None) -> Tuple[str, Optional[str]]:
        """读取上层报告的辩论过程，渲染成"前层语境"段落字符串。

        路由规则 (按 symbols.yaml 的 sector 字段)：
            个股有 sector → parent = sector ETF       (layer_name = '板块')
            板块/宽基/无 sector 个股 → parent = GENERAL (layer_name = '大盘')
            GENERAL → 无 parent

        鲜度阈值：data_sources.yaml 的 ai_analysis.inheritance_max_age (默认 1h)。
        过期或缺失 → 返回 ("", None)，不阻塞分析。
        as_of 给定(历史回放) → 父报告改从 PG get_debate_latest(parent, as_of) 取(截至该刻最新)。

        Returns:
            (rendered_text, parent_file_path_str)
        """
        if not self.inheritance_enabled:
            return "", None
        if symbol == "GENERAL":
            return "", None

        # 大盘证据官结论: 任何非 GENERAL 标的都直接继承 GENERAL 的证据(仅简单结论)。
        # 个股多经板块 ETF 路由, 而板块 ETF v1 不自搜, 故不走 parent 链, 直接取 GENERAL。
        gen_ev = self._render_general_evidence(as_of)

        info = self.contracts.get(symbol, {})
        sector = info.get('sector')
        if sector:
            parent_symbol = sector
            layer_name = "板块"
        else:
            parent_symbol = "GENERAL"
            layer_name = "大盘"

        # 父报告统一从 PG debates 取 (report jsonb 含 debate_history)。
        # live(as_of=None): 取最新, 并要求够新(market_data_timestamp 在 inheritance_max_age 内);
        # 历史回放(as_of 给定): 取截至该刻最新, 不卡鲜度(point-in-time)。
        from datalayer.pg.lowfreq_reader import get_debate_latest
        try:
            row = get_debate_latest(parent_symbol, as_of)
        except Exception as e:
            logging.warning(f"  [{symbol}] PG 取父({parent_symbol})报告异常: {e}")
            row = {}
        parent_json = (row or {}).get('report') or {}
        if not parent_json:
            logging.info(f"  [{symbol}] No PG parent ({parent_symbol}) report; skip inheritance.")
            return gen_ev, None
        if as_of is None:
            ai_cfg = self.full_cfg.get('ai_analysis', {})
            max_age = float(ai_cfg.get('inheritance_max_age', 3600))
            last = _to_dt(row.get('as_of'))
            if last is None or (datetime.now(timezone.utc) - last).total_seconds() > max_age:
                logging.info(f"  [{symbol}] Parent ({parent_symbol}) report not fresh (≤{max_age:.0f}s); skip inheritance.")
                return gen_ev, None
        parent_path_str = f"pg:debates@{(row or {}).get('as_of')}"

        polished_history = parent_json.get('debate_history', []) or []
        if not polished_history:
            return gen_ev, None

        # 渲染辩论过程：用 polished_text，跳过原始 XML
        parts: List[str] = []
        for entry in polished_history:
            role  = entry.get('role', '?')
            rd    = entry.get('round', '?')
            text  = entry.get('polished_text', '')
            if not text:
                content = entry.get('content', {}) or {}
                text = content.get('reasoning', '') or content.get('statement', '')
            if text:
                parts.append(f"[{role}] {rd}: {text}")

        if not parts:
            return gen_ev, None

        history_text = "\n\n".join(parts)
        rendered = (
            f"## 前层语境（重要参考，非 ground truth）\n"
            f"这是 {symbol} 所在的{layer_name}（{parent_symbol}）刚结束的辩论过程。\n"
            f"{layer_name}聚焦在单一议题，三方 attention 比本层集中，过程值得参考；\n"
            f"但本层目标是 {symbol}，证据冲突时以本层为准。\n\n"
            f"{history_text}"
        )
        if gen_ev:
            rendered = f"{rendered}\n\n{gen_ev}"
        return rendered, parent_path_str

    def _load_macro_layer(self, raw: Dict, paths: Dict, region: str = "US", as_of=None, news_symbol: str = "GENERAL") -> None:
        """加载第 1 层宏观数据：general 新闻 + 经济指标 + 情绪指数。原地修改 raw / paths。

        经济指标只读隔壁 apex_codex 的低频数据脑 (PG, 库 equipoise)，
        不再回退 JSON；PG 无数据时 macro_data 留空 (由调用方 setdefault 兜底)。
        as_of 给定(历史回放)时各源按时刻 point-in-time 读。
        """
        news_text, news_path = self._news_text_for(news_symbol, as_of)
        if news_text is not None:
            raw["general_news_data"] = news_text
            if news_path:
                paths["macro"]["general_news"] = news_path

        # 经济指标：只读 PG 数据脑 (equipoise)，不再回退 JSON
        try:
            from datalayer.pg.lowfreq_reader import format_economic_for_prompt
            econ_from_brain = format_economic_for_prompt(region, as_of=as_of)
        except Exception as e:
            logging.warning(f"[macro] PG 数据脑读取异常: {e}")
            econ_from_brain = None

        if econ_from_brain:
            raw["macro_data"] = econ_from_brain
            # economic 不再记进 data_files。复现时拿报告统一时间戳 market_data_timestamp_utc
            # 调 /api/economic?region=&as_of=<时间戳> 即可重现, 不存路径/文件名。
        else:
            logging.warning(f"[macro] PG economic({region}) 无数据，macro_data 留空")

        # 市场情绪: 读 PG 数据脑 sentiment 表 (US=fear_greed / EZ=vstoxx / JP=nikkei_vi)。
        # 不再读 JSON、不再记 data_files 路径; 复现用报告统一时间戳 as_of 调 /api/sentiment。
        try:
            from datalayer.pg.lowfreq_reader import format_sentiment_for_prompt
            sent_data = format_sentiment_for_prompt(region, as_of=as_of)   # as_of=None → 当前最新(实时辩论)
        except Exception as e:
            logging.warning(f"[sentiment] PG 数据脑读取异常: {e}")
            sent_data = None
        if sent_data:
            raw["fear_greed_data"] = sent_data

    def assemble_general(self, as_of=None) -> Tuple[Dict, Dict]:
        """GENERAL 宏观三镜头分析：SPY/QQQ/DIA 并列。顶层无继承。as_of 给定→历史回放。"""
        raw = {}
        paths = {"macro": {}, "benchmarks": {}, "symbol": {}}

        self._load_macro_layer(raw, paths, as_of=as_of)

        bench_text, bench_paths = self._build_benchmark_data(BENCHMARK_SYMBOLS, as_of=as_of)
        raw["benchmark_data"] = bench_text
        paths["benchmarks"] = bench_paths

        # GENERAL 新闻只一层，归一化到 news_data (模板用统一的 {news_data})
        raw["news_data"] = raw.pop("general_news_data", "[]")

        # 顶层无继承
        raw["inheritance_block"] = ""

        # 兜底默认值
        for k in ("macro_data", "fear_greed_data"):
            raw.setdefault(k, "{}")

        return raw, paths

    def assemble_general_region(self, region: str, benchmarks: list, news_symbol: str, as_of=None) -> Tuple[Dict, Dict]:
        """GENERAL_EU / GENERAL_JP 区域宏观分析。region 决定经济/情绪数据源，benchmarks 为区域指数列表。"""
        raw = {}
        paths = {"macro": {}, "benchmarks": {}, "symbol": {}}

        # 经济指标 region 映射 (EU → EZ, JP → JP)
        econ_region = {"EU": "EZ"}.get(region, region)
        self._load_macro_layer(raw, paths, region=econ_region, as_of=as_of, news_symbol=news_symbol)

        bench_text, bench_paths = self._build_benchmark_data(benchmarks, as_of=as_of)
        raw["benchmark_data"] = bench_text
        paths["benchmarks"] = bench_paths

        raw["news_data"] = raw.pop("general_news_data", "[]")
        raw["inheritance_block"] = ""

        for k in ("macro_data", "fear_greed_data"):
            raw.setdefault(k, "{}")

        return raw, paths

    def assemble_stock(self, symbol: str, as_of=None) -> Tuple[Dict, Dict]:
        """ETF 与个股共用的三层 (或两层) 装配。

        按 symbols.yaml 的 benchmark / sector 字段动态决定：
          第 1 层 (宏观)：general 新闻 + 经济 + 情绪 + 主基准 ETF 技术 (1~2 个)
          第 2 层 (板块)：sector ETF 的技术 + 新闻 (无 sector 则空段)
          第 3 层 (自身)：profile + 技术 + 基本面 + 新闻
        as_of 给定(历史回放)时, 技术/新闻/经济/情绪/基本面/父语境全按时刻 point-in-time 读 PG。
        """
        raw: Dict = {}
        paths: Dict = {"macro": {}, "benchmarks": {}, "sector": {}, "symbol": {}}

        clean_sym = symbol.upper().replace(" ", ".")
        raw["symbol"] = clean_sym

        info = self.contracts.get(clean_sym, {})

        # === 第 1 层 / 宏观背景 (按标的 region 取对应区宏观) ===
        region = (info.get('region') or "US").upper()
        self._load_macro_layer(raw, paths, region=region, as_of=as_of)
        benchmark = info.get('benchmark')
        sector = info.get('sector')

        if benchmark:
            bench_symbols = [benchmark]
            raw["benchmark_symbol"] = benchmark
        else:
            # 宽基 ETF (SPY/QQQ/GLD)：拿其他 BENCHMARK_SYMBOLS 做横向对比
            bench_symbols = [s for s in BENCHMARK_SYMBOLS if s != clean_sym]
            raw["benchmark_symbol"] = " / ".join(bench_symbols) if bench_symbols else "N/A"

        bench_text, bench_paths = self._build_benchmark_data(bench_symbols, as_of=as_of)
        raw["benchmark_data"] = bench_text
        paths["benchmarks"] = bench_paths

        # === 第 2 层 / 板块 ===
        sector_news_json = ""
        if sector:
            sector_tech, sector_news_json, sector_paths = self._load_sector_layer(sector, as_of=as_of)
            paths["sector"] = sector_paths
            raw["sector_symbol"] = sector
            raw["sector_tech_data"] = sector_tech if sector_tech else "(无板块技术数据)"
            # sector_news 占位；真正内容在 dedup 之后填进 sector_block
        else:
            raw["sector_symbol"] = "N/A"
            raw["sector_tech_data"] = ""

        # === 第 3 层 / 分析对象自身 ===
        tech_text, tech_path = self._tech_text_for(clean_sym, as_of)
        if tech_text is not None:
            raw["tech_data"] = tech_text
            if tech_path:
                paths["symbol"]["technical"] = tech_path

        # 公司基本面: 读 PG 数据脑 (company 静态 + fundamentals 可变 point-in-time)。
        # 不再读 JSON 文件、不再记 data_files 路径; 复现用报告统一时间戳 market_data_timestamp_utc
        # 调 /api/fundamentals?symbol=&as_of= 重查即可。
        try:
            from datalayer.pg.lowfreq_reader import format_company_for_prompt, format_fundamentals_for_prompt
            prof = format_company_for_prompt(clean_sym)
            fund = format_fundamentals_for_prompt(clean_sym, as_of=as_of)   # as_of=None → 当前最新(实时辩论)
        except Exception as e:
            logging.warning(f"[fundamentals] PG 数据脑读取异常: {e}")
            prof = fund = None
        if prof:
            raw["profile_data"] = prof
        if fund:
            raw["fundamentals_data"] = fund

        if "profile_data" not in raw: raw["profile_data"] = "{}"
        if "fundamentals_data" not in raw: raw["fundamentals_data"] = "{}"

        news_text, news_path = self._news_text_for(clean_sym, as_of)
        if news_text is not None:
            raw["news_data"] = news_text
            if news_path:
                paths["symbol"]["news"] = news_path

        # === 跨层新闻去重 (Finnhub id) + 合并为单一 news_data ===
        # 优先级 个股 > 板块 > 宏观；同 id 高优先级层保留
        deduped = _dedup_news_by_id([
            ("news_data", raw.get("news_data", "")),
            ("sector_news_data", sector_news_json),
            ("general_news_data", raw.get("general_news_data", "")),
        ])
        raw["news_data"] = _combine_deduped_news(
            deduped, ["news_data", "sector_news_data", "general_news_data"]
        )
        raw.pop("general_news_data", None)

        # === 兜底默认值 ===
        for k in ("tech_data", "fundamentals_data", "profile_data", "macro_data", "fear_greed_data"):
            raw.setdefault(k, "{}")

        # === 渲染 sector_block：有 sector 时含标题；无 sector 时整段消失 ===
        if sector:
            raw["sector_block"] = (
                f"### 板块 ({sector})\n"
                f"- 技术指标: {raw['sector_tech_data']}\n"
                f"- 强弱判读义务：显式比较 {clean_sym} vs {sector} vs {raw['benchmark_symbol']}，"
                f"是跟涨、跟跌、还是逆势。"
            )
        else:
            raw["sector_block"] = ""  # 整段消失，不留空标题

        # === 加载前层语境（GENERAL 不继承；其它按 yaml parent 路由） ===
        inh_text, parent_path = self._load_inheritance(clean_sym, as_of=as_of)
        raw["inheritance_block"] = inh_text  # 空字符串则模板里整块消失
        if parent_path:
            paths["parent_analysis_file"] = parent_path

        return raw, paths


# ──────────────────────────────────────────────────────────────────────────────
# 去重逻辑
# ──────────────────────────────────────────────────────────────────────────────

def _extract_ts_from_path(path_str: str) -> str:
    """Extract ISO 8601 timestamp (YYYYMMDDTHHMMSSZ) from a filename.

    Only the current ISO format is recognized. Any other shape returns "" so
    the dedup check treats it as "unknown / not comparable" and re-runs.
    Legacy pre-migration filenames have been purged from the data dirs.
    """
    if not path_str: return ""
    try:
        stem = Path(path_str).stem
        last = stem.split('_')[-1]
        if "T" in last and last.endswith("Z"):
            return last
        return ""
    except Exception:
        return ""

# GENERAL* 取数据时间锚用的"固定指数 ETF"(不再 max 多个 benchmark)
_GENERAL_TS_ANCHOR = {"GENERAL": "SPY", "GENERAL_EU": "DAX", "GENERAL_JP": "N225", "GENERAL_KR": "KODEX200"}


def _to_dt(v):
    """各种时间表示 → aware UTC datetime; 失败 → None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pg_data_ts(symbol: str, ctx_type: str, as_of=None):
    """数据时间锚 = PG technical 的 as_of(=market_data_timestamp_utc)。
    GENERAL* 取固定指数 ETF 的; 个股/ETF 取自己的。返回 aware datetime 或 None。"""
    from datalayer.pg.lowfreq_reader import get_technical_snapshot
    anchor = _GENERAL_TS_ANCHOR.get(symbol, symbol) if ctx_type == 'general' else symbol
    snap = get_technical_snapshot(anchor.upper().replace(" ", "."), as_of)
    return _to_dt(snap.get("as_of")) if snap else None


def _check_analysis_exists(symbol: str, data_ts, data_config: Dict, as_of=None) -> bool:
    """是否已存在 >= 该数据版本(data_ts)的辩论报告 —— 读 PG debates 表(不再 glob JSON)。
    debates.as_of = market_data_timestamp_utc; 已有报告的 as_of >= data_ts 即视为已分析过。"""
    data_ts = _to_dt(data_ts)
    if data_ts is None:
        return False
    from datalayer.pg.lowfreq_reader import get_debate_latest
    row = get_debate_latest(symbol.upper(), as_of)
    last = _to_dt((row or {}).get("as_of"))
    return last is not None and last >= data_ts


# ──────────────────────────────────────────────────────────────────────────────
# LLM辩论任务
# ──────────────────────────────────────────────────────────────────────────────

def run_debate_sync(engine, assembler, symbol: str, ctx_type: str, data_config: Dict,
                    force: bool = False, as_of=None):
    """
    同步执行单个标的的辩论任务。
    包含数据组装、完整性检查、去重检查、引擎调用。

    force=True (重跑): 绕过"报告已存"去重, 强制重新辩论。
        注意: **不**绕过"无行情就 skip"安全闸 —— 没价格仍不辩 (免 AI 幻觉价位)。
    as_of 给定 (历史回放): 装配按时刻 point-in-time 读 PG; 不回写 schedule (回放不改未来排期)。
    """
    # 1. 组装数据
    if symbol == "GENERAL":
        raw_data, paths = assembler.assemble_general(as_of=as_of)
    elif symbol == "GENERAL_EU":
        raw_data, paths = assembler.assemble_general_region("EU", EU_BENCHMARK_SYMBOLS, "GENERAL_EU", as_of=as_of)
    elif symbol == "GENERAL_JP":
        raw_data, paths = assembler.assemble_general_region("JP", JP_BENCHMARK_SYMBOLS, "GENERAL_JP", as_of=as_of)
    elif symbol == "GENERAL_KR":
        raw_data, paths = assembler.assemble_general_region("KR", KR_BENCHMARK_SYMBOLS, "GENERAL_KR", as_of=as_of)
    else:
        raw_data, paths = assembler.assemble_stock(symbol, as_of=as_of)

    # 2. 完整性检查 - 看 value 真有内容, 不只看 key 是否存在
    # 背景: assemble_stock L451-453 有个 setdefault 兜底, 会把 tech_data 默认填成 "{}"
    # (为了模板渲染不 KeyError)。旧版只检查 "tech_data" not in raw_data → 永远 False,
    # technical 文件过期时 sanity check 失效, AI 在没价格数据情况下硬辩论, 幻觉 target 价位
    # (案例: META 6/9 technical 25h 老 被剔除, AI 给 target=660, 真实 last_price=589.85)。
    if not symbol.startswith("GENERAL"):
        tech_val = (raw_data.get("tech_data") or "").strip()
        if not tech_val or tech_val in ("{}", "[]"):
            logging.warning(
                f"⚠️ [Skip Debate] {symbol}: Tech Data missing or expired "
                f"(tech_data 兜底为空, 不辩论免 AI 幻觉价格)"
            )
            return "skipped-no-market-data"
    else:
        # GENERAL*: benchmark technical 至少有一个不能为空
        bench_val = (raw_data.get("benchmark_data") or "").strip()
        if not bench_val:
            logging.warning(
                f"⚠️ [Skip Debate] {symbol}: benchmark technical 全部过期或缺失 "
                f"(benchmark_data 为空, 不辩论免 AI 幻觉价位)"
            )
            return "skipped-no-market-data"

    # 3. 提取数据时间锚 = PG technical 的 as_of(=market_data_timestamp_utc)
    #    GENERAL* 取固定指数 ETF(SPY/DAX/N225), 个股/ETF 取自己。
    data_ts = _pg_data_ts(symbol, ctx_type, as_of)

    # 4. 去重检查 (读 PG debates; force=True 重跑时跳过, 强制重辩)
    if force and data_ts and _check_analysis_exists(symbol, data_ts, data_config, as_of):
        logging.info(f"♻️ [Force Rerun] {symbol}: Fresh analysis exists ({data_ts}), but --force → 重辩")
    elif data_ts and _check_analysis_exists(symbol, data_ts, data_config, as_of):
        logging.info(f"⏭️ [Skip Debate] {symbol}: Fresh analysis exists ({data_ts})")
        return "skipped-fresh-exists"

    # 5. 执行辩论
    logging.info(f"🚀 [Debate Start] {symbol} ({ctx_type}) | DataTS: {data_ts} | as_of={as_of}")
    final_state = engine.run(symbol, ctx_type, raw_data, paths, 10)
    logging.info(f"✅ [Debate Done] {symbol}")

    # 6. live 跑完回写 schedule 的 next (历史回放 as_of 不回写)。best-effort。
    if as_of is None and final_state:
        try:
            from workflows.schedule_writeback import write_next
            fr = final_state.get("final_report") or {}
            # next_review 可能在 final_report 顶层, 或再嵌一层 final_report
            nr = fr.get("next_review") or (fr.get("final_report") or {}).get("next_review")
            if nr:
                sched_path = PROJECT_ROOT / "config" / "debate_schedule.yaml"
                write_next(symbol, nr, sched_path)
            else:
                logging.info(f"[run_debate_sync] {symbol} 无 next_review, 不回写 schedule")
        except Exception as e:
            logging.warning(f"[run_debate_sync] schedule 回写异常(忽略): {e}")
    return "ran"


# ──────────────────────────────────────────────────────────────────────────────
# DAG 调度核心组件
# ──────────────────────────────────────────────────────────────────────────────

def build_dependency_graph(symbols_config: Dict, target_names: List[str]) -> Dict[str, List[str]]:
    """
    根据 symbols.yaml 的 sector 字段构建辩论依赖图。

    GENERAL: root, 无父
    宽基/板块 ETF, 无 sector 个股: parent = [GENERAL]
    有 sector 个股: parent = [sector ETF]  (sector ETF 自身又依赖 GENERAL，传递)
    """
    contracts = symbols_config.get('symbol_contracts', {})
    deps: Dict[str, List[str]] = {"GENERAL": []}
    target_set = set(target_names)

    for sym in target_names:
        if sym == "GENERAL":
            continue
        info = contracts.get(sym, {})
        sector = info.get('sector')
        if sector and sector in target_set:
            deps[sym] = [sector]
        else:
            deps[sym] = ["GENERAL"]
    return deps


def is_symbol_data_fresh(symbol: str, ta_dir: str, max_age: float) -> bool:
    """某 symbol 的行情数据是否在鲜度阈值内 —— 看保留的原始 bar CSV mtime
    (data/market_data/{symbol}/*.csv = 最近一次从 IBKR 拉 bar 的时刻)。
    技术快照已迁 PG、不再落 JSON, 故鲜度改看 CSV; 入参 ta_dir 保留但不再使用。"""
    csv_dir = PROJECT_ROOT / "data" / "market_data" / symbol
    if not csv_dir.exists():
        return False
    files = list(csv_dir.glob(f"{symbol}_*.csv"))
    if not files:
        return False
    latest = max(files, key=lambda f: f.stat().st_mtime)
    return (time.time() - latest.stat().st_mtime) <= max_age


class IbkrFetcher:
    """常驻 IBKR 连接 + 串行请求 + 强制 ≥IBKR_REQUEST_INTERVAL 秒间隔。

    所有调度协程通过 await fetcher.fetch_symbol(sym) 排队请求；
    内部用 asyncio.Lock 串行化，符合 IBKR 单线程约束。

    关键去重逻辑：鲜度检查在锁内执行，确保多个 coroutine 同时请求同一 symbol 时，
    第一个拉完释放锁后，后面的进锁会发现数据已新鲜直接返回，不重复请求 IBKR。
    """

    def __init__(self, symbols_config: Dict, ta_dir: str, max_age: float):
        self.symbols_config = symbols_config
        self.ta_dir = ta_dir
        self.max_age = max_age
        self._fetcher = None
        self._connected = False
        self._last_request_ts: float = 0.0
        self._lock = asyncio.Lock()

    def _ensure_connected_sync(self) -> bool:
        """Layer 1: 主动 liveness probe + 自动重连。
        旧版只看 self._connected flag, IBKR 断了不会被发现 → fetcher 拉到 stale 数据。
        新版每次都用 isConnected() 真探一次, 断了自动重连一次, 重连失败返回 False
        (调用方 _do_fetch_sync 看到 False 会 skip 整个 symbol, 不会触发 builder)。
        """
        # 主动 probe: 之前以为连着, 真探一下 socket
        if self._fetcher is not None:
            try:
                if self._fetcher.isConnected():
                    self._connected = True
                    return True
            except Exception:
                pass  # isConnected 本身抛异常 = 一定断了

        # 此处确认是断的 → 重置并重连
        self._connected = False
        if self._fetcher is not None:
            try:
                self._fetcher.disconnect_and_stop()
            except Exception:
                pass
            self._fetcher = None

        md_config = MarketDataConfig.from_yaml(str(PROJECT_ROOT / "config/data_sources.yaml"))
        # 端口可被 EQUIPOISE_IBKR_PORT 覆盖 (默认 7496=TWS Live; 服务器跑 IB Gateway 时设 4001)
        ibkr_host = os.environ.get("EQUIPOISE_IBKR_HOST", "127.0.0.1")
        ibkr_port = int(os.environ.get("EQUIPOISE_IBKR_PORT", "7496"))
        for attempt in range(1, 4):
            try:
                self._fetcher = MarketDataFetcher(md_config)
                logging.info(f"🔌 [IbkrFetcher] connecting {ibkr_host}:{ibkr_port} (attempt {attempt}/3)")
                ok = self._fetcher.connect_and_start(
                    host=ibkr_host, port=ibkr_port,
                    client_id=random.randint(1000, 9999)
                )
                if ok and self._fetcher.isConnected():
                    self._connected = True
                    logging.info("🔌 [IbkrFetcher] (Re)connected.")
                    return True
                logging.warning("⚠️ [IbkrFetcher] Connect failed - IBKR offline?")
            except Exception as e:
                logging.warning(f"⚠️ [IbkrFetcher] Connection error: {e}")
            try:
                if self._fetcher is not None:
                    self._fetcher.disconnect_and_stop()
            except Exception:
                pass
            self._fetcher = None
            if attempt < 3:
                time.sleep(2.0)
        return False

    def _latest_csv_bar_ts(self, symbol: str) -> Optional[str]:
        """Layer 2 helper: 读 data/market_data/{symbol}/ 下最新 1m CSV 的 filename 编码 bar 时间戳。
        用于"fetch 前后 bar 是否前进"的检测——区分"IBKR 给了新数据"vs"IBKR 给了同样的老数据"。
        """
        csv_root = PROJECT_ROOT / "data" / "market_data" / symbol
        if not csv_root.exists():
            return None
        files = list(csv_root.glob(f"{symbol}_*_1m_*.csv"))
        if not files:
            return None
        latest = max(files, key=lambda f: f.name)  # filename 里 TS 是 YYYYMMDDTHHMMSSZ, lex 排序 = 时间排序
        parts = latest.stem.split('_')
        return parts[-1] if parts else None

    def _do_fetch_sync(self, symbol: str) -> bool:
        """同步执行单个 symbol 的 IBKR 拉取 + 技术指标计算。

        Layer 2 防御:
        - 没连接 (Layer 1 已 set _connected=False) → 直接 skip, 不跑 builder
        - 连接 OK 但 IBKR 返回的最新 bar 没前进 → skip builder (避免 force_update 重写
          stale technical 文件, 那会污染 mtime 让下次 fresh check 被骗)
        - 只有真拿到新 bar 才跑 builder
        """
        contracts_def = self.symbols_config.get('symbol_contracts', {})
        c_info = contracts_def.get(symbol)
        if not c_info:
            logging.warning(f"  ⚠️ [Fetch] No contract config for {symbol}, skipping.")
            return False

        # 没连接 → 整个 symbol skip (不动 builder, 不污染 mtime)
        if not self._connected or not self._fetcher:
            logging.warning(f"  ⚠️ [Fetch] {symbol}: IBKR not connected, SKIP entirely")
            return False

        # 记录 fetch 前的最新 bar timestamp
        pre_ts = self._latest_csv_bar_ts(symbol)

        logging.info(f"  📥 [Fetch] {symbol}...")
        contract = SymbolContract(
            symbol=c_info['symbol'],
            sec_type=c_info['sec_type'],
            exchange=c_info['exchange'],
            currency=c_info['currency'],
            ibkr_ticker=c_info.get('ibkr_ticker'),   # 数字东证/KRX 代码; 美欧股缺省=None → 回退 symbol
        )
        try:
            self._fetcher.fetch_multi_resolutions(contract, force_refresh=True)
        except Exception as e:
            logging.error(f"  [Fetch Error] {symbol}: {e}; SKIP builder to avoid stale-mtime trap")
            return False

        # 检测 IBKR 是否真给了新 bar (区分"成功拉到新数据"vs"成功调用但返回老数据")
        post_ts = self._latest_csv_bar_ts(symbol)
        if pre_ts is not None and pre_ts == post_ts:
            logging.warning(
                f"  ⚠️ [Fetch] {symbol}: IBKR returned no new bars (still {pre_ts}); "
                f"SKIP builder (likely IBKR data feed lagging / weekend / session issue)"
            )
            return False

        # 真有新数据, 才跑 builder
        try:
            process_symbol_technical_analysis(symbol, force_update=True)
        except Exception as e:
            logging.error(f"  [Calc Error] {symbol}: {e}")
            return False
        return True

    async def fetch_symbol(self, symbol: str, force_refresh: bool = False) -> bool:
        """串行请求 + 强制 1s 间隔。锁内复检鲜度，避免并发重复拉同一 symbol。"""
        async with self._lock:
            # 锁内复检：可能在排队等锁期间，前面的协程已经把这个 symbol 拉好了
            if (not force_refresh and
                    is_symbol_data_fresh(symbol, self.ta_dir, self.max_age)):
                logging.info(f"  ⏭️ [Fetch Skip] {symbol}: data fresh (already fetched).")
                return True

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ensure_connected_sync)

            elapsed = time.time() - self._last_request_ts
            if elapsed < IBKR_REQUEST_INTERVAL:
                wait = IBKR_REQUEST_INTERVAL - elapsed
                await asyncio.sleep(wait)

            ok = await loop.run_in_executor(None, self._do_fetch_sync, symbol)
            self._last_request_ts = time.time()
            return ok

    async def close(self):
        if self._connected and self._fetcher:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._fetcher.disconnect_and_stop
                )
                logging.info("🔌 [IbkrFetcher] Disconnected.")
            except Exception as e:
                logging.warning(f"[IbkrFetcher] Disconnect error: {e}")
            finally:
                self._connected = False


async def schedule_target(
    symbol: str,
    ctx_type: str,
    deps: Dict[str, List[str]],
    done_events: Dict[str, asyncio.Event],
    fetcher: IbkrFetcher,
    debate_sem: asyncio.Semaphore,
    engine,
    assembler: ContextAssembler,
    data_config: Dict,
    fetch_symbols: List[str],
):
    """
    单个标的的生命周期协程：
      1. 等所有 parent 的辩论完成 (done_event.set())
      2. 检查所需数据鲜度，过期则进入 fetcher 队列拉取 (串行 + 1s 间隔)
      3. 抢辩论 pool (Semaphore)，跑辩论
      4. set done_event 通知 children
    """
    parent_list = deps.get(symbol, [])
    if parent_list:
        logging.info(f"⏳ [{symbol}] Waiting on parents: {parent_list}")
        for parent in parent_list:
            await done_events[parent].wait()

    ta_dir = data_config.get('data_sources', {}).get('technical_analysis', {}).get('output_dir')
    for fsym in fetch_symbols:
        if not is_symbol_data_fresh(fsym, ta_dir, DATA_FRESHNESS_SECONDS):
            await fetcher.fetch_symbol(fsym)

    try:
        async with debate_sem:
            logging.info(f"🚀 [{symbol}] Acquired debate slot.")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, run_debate_sync, engine, assembler, symbol, ctx_type, data_config
            )
    except Exception as e:
        logging.error(f"❌ [Debate Error] {symbol}: {e}")
    finally:
        done_events[symbol].set()


# ──────────────────────────────────────────────────────────────────────────────
# Phase: DAG-Driven Concurrent Debate
# ──────────────────────────────────────────────────────────────────────────────

async def run_analysis_phase(symbols_map: Dict, data_config: Dict, symbols_config: Dict):
    logging.info("\n>>> Phase: DAG-Driven Concurrent Debate <<<")

    engine = DebateEngine()
    assembler = ContextAssembler(data_config, symbols_config)

    # 构建标的清单 (GENERAL 始终为 root)
    # runnable=false 的标的 (Claude Code 手动分析专用) 自动跳过
    contracts_def = symbols_config.get('symbol_contracts', {})
    def _is_runnable(sym: str) -> bool:
        return contracts_def.get(sym, {}).get('runnable', True) is not False

    all_targets: List[Tuple[str, str]] = [("GENERAL", "general")]
    for s in symbols_map["etfs"]:
        if _is_runnable(s):
            all_targets.append((s, "etf"))
        else:
            logging.info(f"⏭️ [Skip Auto] {s}: runnable=false, manual-only")
    for s in symbols_map["stocks"]:
        if _is_runnable(s):
            all_targets.append((s, "stock"))
        else:
            logging.info(f"⏭️ [Skip Auto] {s}: runnable=false, manual-only")

    target_names = [s for s, _ in all_targets]
    deps = build_dependency_graph(symbols_config, target_names)

    # 每个标的需要的数据 symbol 列表 (= 自己 + benchmark + sector)
    # 鲜度阈值兜底：上游标的已拉过就 skip，没拉过或过期就补拉，自愈不依赖隐式顺序
    contracts = symbols_config.get('symbol_contracts', {})
    fetch_map: Dict[str, List[str]] = {}
    for sym, _ in all_targets:
        if sym == "GENERAL":
            fetch_map[sym] = list(BENCHMARK_SYMBOLS)  # SPY/QQQ/DIA 三镜头
            continue
        info = contracts.get(sym, {})
        needs = [sym]  # 自己的数据
        bench = info.get('benchmark')
        if bench and bench not in needs:
            needs.append(bench)
        sec = info.get('sector')
        if sec and sec not in needs:
            needs.append(sec)
        fetch_map[sym] = needs

    done_events = {sym: asyncio.Event() for sym in target_names}
    ta_dir = data_config.get('data_sources', {}).get('technical_analysis', {}).get('output_dir')
    fetcher = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
    debate_sem = asyncio.Semaphore(MAX_DEBATE_CONCURRENT)

    n_root = sum(1 for v in deps.values() if not v)
    n_dep = len(deps) - n_root
    logging.info(
        f"📊 Plan: {len(all_targets)} targets | "
        f"{n_root} root, {n_dep} dependent | "
        f"max_debate_pool={MAX_DEBATE_CONCURRENT} | "
        f"ibkr_gap={IBKR_REQUEST_INTERVAL}s | "
        f"data_freshness={DATA_FRESHNESS_SECONDS}s"
    )

    tasks = [
        asyncio.create_task(
            schedule_target(
                sym, ctx, deps, done_events, fetcher, debate_sem,
                engine, assembler, data_config, fetch_map[sym]
            ),
            name=f"sched-{sym}"
        )
        for sym, ctx in all_targets
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await fetcher.close()

    logging.info("🏁 All targets completed.")


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry
# ──────────────────────────────────────────────────────────────────────────────

def load_helpers():
    d_cfg = yaml.safe_load(open(PROJECT_ROOT / "config/data_sources.yaml", encoding='utf-8'))
    s_cfg = yaml.safe_load(open(PROJECT_ROOT / "config/symbols.yaml", encoding='utf-8'))
    t = s_cfg.get('analysis_targets', {})
    s_map = {
        "stocks": [s.upper() for s in t.get('stocks', [])],
        "etfs": [s.upper() for s in t.get('etfs', [])],
        "benchmarks": [s.upper() for s in t.get('benchmarks', [])],
    }
    return d_cfg, s_cfg, s_map

def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true", help="立即执行一次分析，不进入守护模式")
    args = parser.parse_args()
    
    data_config, symbols_config, symbols_map = load_helpers()
    
    calendar = get_trading_calendar('NYSE')
    
    # --run-once: 立即执行一轮
    if args.run_once:
        logging.info("🚀 Run-Once Mode: Executing single analysis round...")
        asyncio.run(run_analysis_phase(symbols_map, data_config, symbols_config))
        logging.info("✅ Run-Once Complete.")
        sys.exit(0)
    
    # 守护模式
    executed = set()
    last_date = None
    
    logging.info("Sentinel Started (Daemon Mode - Rolling Batch)")
    while True:
        now = get_now_et()
        
        if last_date != now.date():
            executed.clear()
            last_date = now.date()
        
        if is_trading_day(calendar, now):
            for t in RUN_TIMES:
                t_str = t.strftime("%H:%M")
                if t_str in executed:
                    continue
                
                sched = datetime.combine(now.date(), t, tzinfo=EASTERN)
                
                if 0 <= (now - sched).total_seconds() <= 900:
                    logging.info(f"⏰ Triggering Scheduled Run: {t_str}")
                    asyncio.run(run_analysis_phase(symbols_map, data_config, symbols_config))
                    executed.add(t_str)
        else:
            if executed:
                logging.info("Non-trading day detected. Clearing executed list.")
                executed.clear()
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
