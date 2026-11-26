# -*- coding: utf-8 -*-
"""
linkup_search.py — 证据官 **linkup 模式** 的底层引擎 (替 Google grounding)。辩论侧/复盘侧共用。

分工 (库主定):
  - **deepseek**: 把查证诉求归纳成 task(一般1, 偶尔几个) + 据搜索结果写【结论】+【概述】。便宜, 跟别的 agent 一样。
  - **Linkup**: 每个 task 搜一次, 返回天然的"原文+来源"(content+url)。原文逐字、模型碰不到 → **拼接幻觉结构上不可能**。
三元组 = 【结论】(deepseek) / 【原文】(linkup content 逐字) / 【来源】(linkup url)。概述由 deepseek 写。

上层 (evidence_officer_linkup / review_scout_linkup) 传入各自宪法(constitution)与诉求, 拿回统一结构:
    [{task, simple(概述), detailed:[{结论, 原文, 来源}, ...]}, ...]
gemini 模式(老 grounding)封存在 evidence_officer.py / review_scout.py, 不走这里。

key 取 config/models.yaml 的 models.linkup.api_key; deepseek 走现成 LLMClient。
"""
from __future__ import annotations

import re
import datetime
import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

MODELS_YAML = Path(__file__).resolve().parents[1] / "config" / "models.yaml"
DEEPSEEK_KEY = "deepseek"          # 跟别的 agent 一样
MAX_RESULTS = 8                    # 每个 task 取多少条 linkup 结果喂 deepseek
MAX_TASKS = 4                      # 归纳出的 task 上限 (防发散)


def _load_linkup_key() -> str:
    with open(MODELS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["models"]["linkup"]["api_key"]


def _as_date(v) -> Optional[datetime.date]:
    if v is None:
        return None
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()[:10]
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def search_one(query: str, from_date=None, to_date=None, max_results: int = MAX_RESULTS,
               exclude_domains=None, depth: str = "standard") -> List[Dict]:
    """一次 Linkup 搜索 → [{name(标题), content(原文逐字), url(来源)}]。失败/不可用 → []。
    depth: 'standard'(快, 够用) | 'deep'(更深更彻底, 贵几倍; 复杂因果/难查时用)。
    exclude_domains: 排除的域名 list (如报价聚合站, 防新闻搜成 quote 页)。"""
    if not (query or "").strip():
        return []
    try:
        from linkup import LinkupClient
    except Exception as e:
        logging.warning(f"[linkup] SDK 未装(pip install linkup-sdk): {e}")
        return []
    try:
        c = LinkupClient(api_key=_load_linkup_key())
        depth = "deep" if str(depth).lower() == "deep" else "standard"
        kw = dict(query=query.strip(), depth=depth,
                  output_type="searchResults", include_images=False)
        fd, td = _as_date(from_date), _as_date(to_date)
        if fd:
            kw["from_date"] = fd
        if td:
            kw["to_date"] = td
        if exclude_domains:
            kw["exclude_domains"] = list(exclude_domains)
        r = c.search(**kw)
        res = getattr(r, "results", r) or []
        out = []
        for it in res[:max_results]:
            content = (getattr(it, "content", "") or "").strip()
            url = getattr(it, "url", "") or ""
            name = (getattr(it, "name", "") or "").strip()   # 标题(新闻当 headline; Scout 侧不用)
            if content:
                out.append({"name": name, "content": content, "url": url})
        return out
    except Exception as e:
        logging.warning(f"[linkup] 搜索失败 ({query[:40]}): {type(e).__name__}: {e}")
        return []


def _ask_deepseek(llm, system: str, user: str, temperature: float = 0.3) -> str:
    try:
        return llm._chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            DEEPSEEK_KEY, temperature) or ""
    except Exception as e:
        logging.warning(f"[linkup] deepseek 调用异常: {e}")
        return ""


_SPLIT_SYS = (
    "你是证据官的任务归纳助手。把下面的查证诉求归纳成可独立去 **Google 网页搜索** 的 task —— "
    "**一般 1 个; 只有诉求确实涉及互不相干的几件事时才拆成 2-4 个**。重叠的合并、不同的分开。"
    "每个 task 写成一句精炼的搜索式问题(中英文皆可, 含关键实体+时间)。\n"
    "**这是网页搜索, 只为网上搜得到的定性信息建 task**(新闻/事件/政策/催化剂/前因后果/某叙事是否被证实)。"
    "**跳过搜不到的**: ①对方数据里通常已有的量化指标(最新价/成交量/RSI/均线/布林带等); "
    "②冷门到只有专门数据源才有的精确数字(如每周外资净买卖额、某指标实时读数)。"
    "若诉求里没有任何值得网搜的, 就**什么都别输出**(留空)。\n"
    "每个 task 末尾用 ` ||| ` 标搜索深度, 你按难度自主判断: "
    "**能一次直接搜到的(具体新闻/明确事实/最新消息) → standard**; "
    "**需要把多源信息拼成因果链/时间线、或较难一次搜到、需深挖的 → deep**(更彻底)。\n"
    "**只输出 task, 每行一个, 格式 `task ||| standard` 或 `task ||| deep`, 不要编号、不要别的话。**"
)


def _split_tasks(llm, requests_text: str, context: str):
    """→ [(task, depth)]; depth ∈ {'standard','deep'}, deepseek 按难度自标。诉求无可搜的 → []。"""
    raw = _ask_deepseek(llm, _SPLIT_SYS, (context + "\n\n【查证诉求】\n" + requests_text).strip(), 0.2)
    out = []
    for ln in raw.splitlines():
        ln = ln.strip(" -*•\t").strip()
        if not ln:
            continue
        if "|||" in ln:
            t, _, d = ln.partition("|||")
            t, depth = t.strip(), ("deep" if "deep" in d.lower() else "standard")
        else:
            t, depth = ln, "standard"
        if len(t) > 3:
            out.append((t, depth))
        if len(out) >= MAX_TASKS:
            break
    if not out and requests_text.strip():
        out = [(requests_text.strip()[:200], "standard")]
    return out


_SYNTH_SYS_TMPL = """{constitution}

【你的工作】下面给你一个 task 和 Linkup 搜索返回的若干条结果(每条有编号、原文片段、来源URL)。
- 挑出**真正相关**的结果; 对每条写一句【结论】(你的判断: 这条说明了什么、是不是新增量)。
- 原文你**不要改写**(系统会原样保留 Linkup 的片段), 你只写结论。
- 最后写一段【概述】: 综合判断 + 是新增量还是老调; 若结果里都没有能回答 task 的实据, 概述里**老实说没搜到**。

严格按此输出, 不要任何额外文字:
===PICK===
相关结果的编号, 逗号分隔(如 1,3,4); 都不相关写 none
===NOTE===
[编号] 一句结论
[编号] 一句结论
===SUMMARY===
一段概述
"""


def _parse_synth(text: str):
    def block(tag):
        m = re.search(rf"==={tag}===\s*(.*?)\s*(?:===|$)", text or "", re.DOTALL)
        return m.group(1).strip() if m else ""
    pick_raw = block("PICK")
    picks = [] if pick_raw.lower().startswith("none") else \
        [int(x) for x in re.findall(r"\d+", pick_raw)]
    notes = {}
    for m in re.finditer(r"\[(\d+)\]\s*(.+)", block("NOTE")):
        notes[int(m.group(1))] = m.group(2).strip()
    return picks, notes, block("SUMMARY")


def _synth_task(llm, task: str, results: List[Dict], constitution: str) -> Dict:
    if not results:
        return {"task": task, "simple": "(Linkup 未返回结果 / 搜索不可用)", "detailed": []}
    numbered = "\n\n".join(f"[{i+1}] 来源: {r['url']}\n原文: {r['content'][:600]}"
                           for i, r in enumerate(results))
    sys = _SYNTH_SYS_TMPL.format(constitution=constitution.strip())
    out = _ask_deepseek(llm, sys, f"【task】{task}\n\n【Linkup 搜索结果】\n{numbered}", 0.3)
    picks, notes, summary = _parse_synth(out)
    detailed = []
    for n in picks:
        if 1 <= n <= len(results):
            r = results[n - 1]
            detailed.append({"结论": notes.get(n, ""), "原文": r["content"], "来源": r["url"]})
    return {"task": task, "simple": summary or "(deepseek 未给概述)", "detailed": detailed}


def synthesize(requests_text: str, context: str, constitution: str,
               from_date=None, to_date=None, llm=None, max_results: int = MAX_RESULTS) -> List[Dict]:
    """全流程: deepseek 归纳 task → 每 task 一次 Linkup → deepseek 合成。
    返回 [{task, simple(概述), detailed:[{结论,原文,来源}]}, ...]。"""
    if not (requests_text or "").strip():
        return []
    if llm is None:
        from workflows.llm_client import LLMClient
        llm = LLMClient()
    tasks = _split_tasks(llm, requests_text, context)
    logging.info(f"[linkup] 归纳出 {len(tasks)} 个 task")
    out = []
    for t, depth in tasks:
        results = search_one(t, from_date=from_date, to_date=to_date,
                             max_results=max_results, depth=depth)
        logging.info(f"[linkup] task={t[:50]!r} depth={depth} → {len(results)} 条")
        out.append(_synth_task(llm, t, results, constitution))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 真实 demo: 模拟一场辩论的三方查证诉求 → task → linkup → deepseek 三元组
    CONSTITUTION = ("你是 Scout(证据官), 中立、只还原事实。优先英文权威一手源(Reuters/Bloomberg/WSJ/FT/官方); "
                    "中文源额外谨慎(简中语料受官方叙事污染)。重信息增量: 分清新消息 vs 市场已定价的老调。如实, 搜不到就说。")
    reqs = ("ZEALOT: 英伟达数据中心/AI capex 需求最新还撑得住吗, 有没有新增订单或指引上调?\n"
            "REAPER: 市场是不是已经充分定价了 AI 资本开支, 有没有见顶/放缓的信号?\n"
            "FULCRUM: 美联储近期利率路径对科技股估值的最新影响?")
    res = synthesize(reqs, context="标的: NVDA / 科技股大盘。当前时点 2026 年。", constitution=CONSTITUTION)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
