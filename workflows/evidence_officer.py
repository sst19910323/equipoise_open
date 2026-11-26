# -*- coding: utf-8 -*-
"""
evidence_officer.py — 证据官: 一次 Gemini Grounding 调用, 吃三方"想搜什么", 吐结构化证据 list。

改自 search_demo.py(已验证 grounding 能捋前因后果 + 中文源把关)。
设计原则(库主定): grounding API 很强, 把三方请求**原样丢给它**, 让它自己:
  合并/拆分 task(多人说的可能是一个、也可能几个)、派生概念、研究、下结论、搜不到/不确定就如实说。

接口:
    investigate(requests: dict, context: str = "") -> list[dict]
        requests = {"zealot": "...", "reaper": "...", "fulcrum": "..."}  (自由文本, 可空)
        返回 [{ "task", "simple", "detailed", "source": [..] }, ...]
        全空请求 / 不可用 → []  (调用方据此跳过)

key 复用 config/models.yaml 的 models.gemini.api_key。
"""
from __future__ import annotations

import os
import re
import time
import logging
from typing import Dict, List

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_YAML = os.path.join(PROJECT_ROOT, "config", "models.yaml")
DATA_SOURCES_YAML = os.path.join(PROJECT_ROOT, "config", "data_sources.yaml")
NAME = "Scout"   # 证据官代号(斥候)
DEFAULT_MODEL = "gemini-3.5-flash"   # 同 search_demo; 不够换 gemini-3.5-pro

_DEFAULT_SEARCH_CFG = {
    "enabled": False,
    "max_rounds": 3,
    "provider": "linkup",      # 搜源: linkup(默认, deepseek+Linkup) | gemini(本文件老 grounding, 封存待启)
    "model": DEFAULT_MODEL,    # 仅 provider=gemini 时用
}


def load_search_cfg() -> Dict:
    """读 data_sources.yaml 的 evidence_search 块 (总开关/预算/模型)。
    缺失或读失败 → 安全默认 (关闭)。env EQUIPOISE_EVIDENCE_SEARCH=1/0 覆盖总开关。
    一个总开关 enabled 全标的统一: 开就全开、关就全关, 不再按标的挑。"""
    cfg = dict(_DEFAULT_SEARCH_CFG)
    try:
        with open(DATA_SOURCES_YAML, "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
        es = (y.get("data_sources", {}) or {}).get("evidence_search", {}) or {}
        for k in cfg:
            if k in es and es[k] is not None:
                cfg[k] = es[k]
    except Exception:
        pass
    env = os.environ.get("EQUIPOISE_EVIDENCE_SEARCH")
    if env is not None:
        cfg["enabled"] = env.strip() in ("1", "true", "True", "yes", "on")
    return cfg

# Scout 的宪法 + 输出格式(强制分隔符, grounding 工具下不宜再强 JSON schema, 故机械解析)
SYSTEM_INSTRUCTION = """\
你是 **Scout(斥候)** —— 量化辩论系统的"证据官"。三个辩论 AI(Zealot/Reaper/Fulcrum)本轮各自提出
"想搜/想核实什么", 交给你用 Google 搜索查证。

【宪法 / 铁律】
1. **中立**: 不站多空、不替任何一方背书, 只还原事实。给倾向也基于证据、不基于立场。
2. **缕清前因后果 + 时间线**: 你的核心价值是把一条孤立信息还原成"谁、何时、什么背景下说/做的,
   之前发生过什么, 是第一次还是反复出现"。重点是**时间线**。
3. **信源政治倾向警惕(尤其重要)**: 本系统全程中文运行, 而**简体中文语料受北京官方叙事污染严重**, 须尤其慎重:
   - 引用关键信息时标注来自英文源还是中文源;
   - **优先英文权威一手源**(Reuters/Bloomberg/WSJ/FT/官方文件);
   - 中文源**额外谨慎**, 仅在英文源缺位或仅补本地细节时采纳, 并明确提示"此处依赖中文源, 可信度打折";
   - 凡涉及中美/地缘/政策, 默认警惕官方宣传口径, 不把宣传当事实。
4. **信息增量导向**: 辩论 AI 要的是"这条到底是**新增量**还是**老生常谈**"。
   你的结论要帮他们判断: 是新消息/新税率/新时间表(有意义), 还是反复喊过、市场早已定价的老调(没意义)。
   **老调重弹就明说没意义; 真新增量就明说新在哪。**
   每条结论都带上**该信息何时公开**(他们判定价程度的原料), 来源顺手标档(官方/主流媒体/单一来源/传闻)。
5. **如实**: 搜不到 / 不确定 / 信源矛盾 → 明说, 绝不编。

【task 归纳 + 查证步骤】
1. 先把三方诉求归纳成若干 task(重叠合并、不同分开), 列出 task 清单。
2. 再**挨个 task** 用 Google 搜索查证; 查证中若发现关键缺口, 自然派生子搜索(并入该 task)。
3. 每个 task **先写详细证据(逐条三元组), 再据此写概述** —— 别先下结论再硬找佐证。

【三元组铁律(最重要, 防"拼接幻觉")】
- **一段结论 ↔ 一段连续原文**: 【原文】必须是**单一连续**片段(原语言、逐字、不改写); **严禁**把几处摘句 / 几个来源缝成一段看着有力的"原文"。要几段证据就写几个三元组。
- **"查无此据"是允许且鼓励的出口**: 某个本该有的事实(如利率水平)在 grounding 返回里找不到对应原文, 就写 `【原文】无 grounding 支撑, 该结论为模型推断`(来源写"无")——**绝不**硬凑一段沾边原文来撑(那是制造虚假可信度)。
- 没真实原文+来源, 就别写那条结论(除非按上一条明确标成模型推断)。

【输出格式 —— 严格按此, 不要任何额外文字】
先列你归纳出的 task 清单:
===TASKS===
1. <task1 一句话>
2. <task2 一句话>

然后每个 task 一块, 顺序固定**先详细后概述**:
===TASK===
<这个 task 是什么(一句话)>
===DETAILED===
【结论】<你的判断, 中文>
【原文】<单一连续原始片段, 原语言逐字; 或 "无 grounding 支撑, 该结论为模型推断">
【来源】<对应那条 source URL; 模型推断则写 无>

【结论】...
【原文】...
【来源】...
(有几条证据就几个三元组)
===SIMPLE===
<概述: 从上面 detailed **蒸馏**。**必须继承 detailed 的不确定性标记**——detailed 标了"预期/未发布/单源/模型推断"的, 这里不许说成定论(例: 别把"预期 4.1%"写成"4.1%")。1-2 句核心判断 + 是新增量还是老调; 搜不到/矛盾就明说>
===END===
(有几个 task 就重复几块)
"""


def _load_key() -> str:
    with open(MODELS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["models"]["gemini"]["api_key"]


def _build_user_content(requests: Dict[str, str], context: str) -> str:
    parts = []
    if context:
        parts.append(f"【本轮辩论标的/上下文】\n{context}\n")
    parts.append("【三方各自想搜/想核实的(原文)】")
    for role in ("zealot", "reaper", "fulcrum"):
        txt = (requests.get(role) or "").strip()
        if txt:
            parts.append(f"- {role.upper()}: {txt}")
    return "\n".join(parts)


def _parse_blocks(text: str) -> List[Dict]:
    """按 ===TASK===/===DETAILED===/===SIMPLE===/===END=== 解析成 list (顺序: 先详细后概述)。
    ===TASKS=== 任务清单预导块不匹配本正则(===TASK=== 后须紧跟 ===DETAILED===), 自动忽略。"""
    out: List[Dict] = []
    # 每块顺序: TASK ... DETAILED(逐条三元组) ... SIMPLE(概述) ... END
    pattern = re.compile(
        r"===TASK===\s*(.*?)\s*===DETAILED===\s*(.*?)\s*===SIMPLE===\s*(.*?)\s*===END===",
        re.DOTALL,
    )
    for m in pattern.finditer(text or ""):
        task = m.group(1).strip()
        detailed = m.group(2).strip()
        simple = m.group(3).strip()
        if task or simple or detailed:
            out.append({"task": task, "simple": simple, "detailed": detailed, "source": []})
    return out


def _grounding_sources(response) -> List[str]:
    try:
        meta = response.candidates[0].grounding_metadata
        chunks = getattr(meta, "grounding_chunks", None) or []
        urls = []
        for ch in chunks:
            web = getattr(ch, "web", None)
            if web and getattr(web, "uri", None):
                urls.append(web.uri)
        return urls
    except (AttributeError, IndexError, TypeError):
        return []


def investigate(requests: Dict[str, str], context: str = "", model: str = DEFAULT_MODEL) -> List[Dict]:
    """三方请求 → 一次 grounding → 结构化证据 list。全空/不可用 → []。"""
    if not requests or not any((requests.get(r) or "").strip() for r in ("zealot", "reaper", "fulcrum")):
        return []
    try:
        from google import genai
        from google.genai import types
        from google.genai import errors as genai_errors
    except Exception as e:
        logging.warning(f"[evidence_officer] google-genai 未装, 跳过搜索: {e}")
        return []

    try:
        client = genai.Client(api_key=_load_key())
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.4,
        )
        user_content = _build_user_content(requests, context)

        response = None
        for attempt in range(1, 5):
            try:
                response = client.models.generate_content(
                    model=model, contents=user_content, config=cfg
                )
                break
            except genai_errors.ServerError:
                time.sleep(2 ** attempt)
        if response is None:
            logging.warning("[evidence_officer] grounding 多次重试失败, 返回空")
            return []

        items = _parse_blocks(getattr(response, "text", "") or "")
        sources = _grounding_sources(response)
        for it in items:
            it["source"] = sources   # 整体信源(source 仅存档/人看, 不给辩论 AI)
        logging.info(f"[evidence_officer] 产出 {len(items)} 条证据, {len(sources)} 个信源")
        return items
    except Exception as e:
        logging.warning(f"[evidence_officer] 调用失败, 返回空: {type(e).__name__}: {e}")
        return []


if __name__ == "__main__":
    # 单测: 三方假请求 → 看它合并/拆分 + 结构化
    logging.basicConfig(level=logging.INFO)
    demo = {
        "zealot": "川普说要对中国半导体加新关税, 这是新利空还是老调重弹? 市场定价了吗?",
        "reaper": "半导体关税的具体税率和时间表确定了吗?",
        "fulcrum": "美联储下次议息是什么时候, 市场预期降息还是按兵不动?",
    }
    res = investigate(demo, context="GENERAL 大盘辩论")
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))
