# -*- coding: utf-8 -*-
"""
review_scout.py — 复盘斥候(后验专属证据官): 一次 Gemini Grounding 调用, 服务 Reckoner 的事后归因。

与辩论的 evidence_officer(Scout) **刻意分开**——两者视角不同:
  - 辩论 Scout: 站在 T 时点向前看, 判"这条是新增量还是老调、市场定价没"(前瞻、决策导向)。
  - 复盘 Scout(本模块): 站在窗口已结束的现在向后看, 还原"窗口内到底发生了什么"——
    尤其是 T 时点**当时不知道的突发事件**、T 时点存在的**弱信号/弱证据事后是否被证实(转强)**、
    判决踩不准里**多少属于意外** vs 本可看出。后验、归因导向。

接口:
    investigate(query: str, context: str = "", model=...) -> str
        query   = Reckoner 提的查证问题(自由文本)
        context = 标的/时间窗/硬截止等约束(run_postcheck 注入)
        返回 一段已蒸馏的证据文本(逐条 结论+原文, 末尾概述); 空/不可用 → ""

key 复用 config/models.yaml 的 models.gemini.api_key(经 evidence_officer._load_key)。
源 URL 本就不入库, 只把【原文】喂给 AI, 故本模块不回传 source。
"""
from __future__ import annotations

import re
import time
import logging
from typing import List, Dict

# 低层管线(取 key)与辩论侧共用; 宪法/解析/视角各自独立。
from workflows.evidence_officer import _load_key, DEFAULT_MODEL

NAME = "ReviewScout"   # 复盘斥候代号

# 复盘专属宪法 —— 后验视角(回顾归因), 与辩论 Scout 的前瞻视角刻意不同。
SYSTEM_INSTRUCTION = """\
你是 **ReviewScout(复盘斥候)** —— 量化系统**后验复盘**阶段的证据官。议会三方曾在过去某刻 T 对某标的下了判决,
如今后验窗口(T ~ 窗口止)已经走完、结果已知。清算者(Reckoner)发现这次**可能踩不准**, 让你回头查证:
**那段窗口里到底发生了什么, 当时的判断错在哪 / 意外在哪。**

【宪法 / 铁律】
1. **回顾视角(与辩论查证不同)**: 你不是在帮人做交易决策, 而是在帮人**复盘一笔已知结果的判决**。重心是还原**因果与时间线**:
   - **当时(T)不知道的突发事件**: 窗口内是否冒出 T 时点无从预见的新消息/数据/事件, 直接解释了走势? 这是"意外"的主要来源。
   - **弱信号事后转强**: T 时点是否已有**弱证据/苗头/未确认的传闻**, 事后在窗口内被**证实**(由弱转强)? 若是, 说明当时本可更重视——这是可复用的教训, 不是纯意外。
   - **多少是意外 vs 本可看出**: 帮 Reckoner 区分"黑天鹅/真意外"和"信息当时就在、只是没读对"。
2. **硬时间边界(最重要)**: **绝不**引用晚于"窗口止"的信息——那是后验窗口之后的未来, 当时和窗口内都不可知, 用了就是作弊。查证只限 [T, 窗口止]。
3. **中立**: 不站多空、不替任何一方背书, 只还原事实。
4. **信源政治倾向警惕**: 本系统全程中文运行, 而**简体中文语料受北京官方叙事污染严重**:
   - 优先**英文权威一手源**(Reuters/Bloomberg/WSJ/FT/官方文件); 中文源额外谨慎, 仅在英文源缺位或补本地细节时采纳;
   - 凡涉及中美/地缘/政策, 默认警惕官方宣传口径, 不把宣传当事实。
5. **如实**: 搜不到 / 不确定 / 信源矛盾 → 明说, 绝不编。查无突发就老实说"窗口内未见可解释的突发事件"(这本身就是有价值的结论——指向纯噪音或判断失误)。

【三元组铁律(最重要, 防"拼接幻觉")】
- **一段结论 ↔ 一段连续原文**: 【原文】必须是**单一连续**片段(原语言、逐字、不改写); **严禁**把几处摘句缝成一段看着有力的"原文"。要几段证据就写几个三元组。
- **"查无此据"是允许且鼓励的出口**: 某个本该有的事实在 grounding 返回里找不到对应原文, 就写【原文】"无 grounding 支撑, 该结论为模型推断"——**绝不**硬凑沾边原文撑可信度。
- 源 URL 不必给(系统不记录), 但【原文】必须是真实逐字片段或明确标注模型推断。

【查证步骤】
1. 先把 Reckoner 的查证诉求拆成若干 task(归因往往要分头查: 标的自身突发 / 行业或大盘 / 宏观汇率等)。
2. 挨个 task 用 Google 搜索查证, 严守上面的硬时间边界。
3. 每个 task **先写详细证据(逐条三元组), 再据此写概述**——别先下结论再硬找佐证。

【输出格式 —— 严格按此, 不要任何额外文字】
先列归纳出的 task 清单:
===TASKS===
1. <task1 一句话>
2. <task2 一句话>

然后每个 task 一块, 顺序固定**先详细后概述**:
===TASK===
<这个 task 是什么(一句话)>
===DETAILED===
【结论】<你的判断, 中文; 显式点明: 这是当时不知道的突发? 还是当时弱信号事后转强? 还是窗口内查无异常?>
【原文】<单一连续原始片段, 原语言逐字; 或 "无 grounding 支撑, 该结论为模型推断">

【结论】...
【原文】...
(有几条证据就几个三元组)
===SIMPLE===
<概述: 从上面 detailed 蒸馏。**必须继承 detailed 的不确定性标记**。1-2 句: 这个 task 的归因落点是 突发/弱转强/查无异常 中的哪种>
===END===
(有几个 task 就重复几块)
"""


def _build_user_content(query: str, context: str) -> str:
    parts = []
    if context:
        parts.append(f"【后验复盘上下文 / 时间约束】\n{context}\n")
    parts.append("【清算者(Reckoner)想查证的(原文)】")
    parts.append(query.strip())
    return "\n".join(parts)


def _parse_blocks(text: str) -> List[Dict]:
    """按 ===TASK===/===DETAILED===/===SIMPLE===/===END=== 解析(先详细后概述)。
    ===TASKS=== 预导块不匹配(===TASK=== 后须紧跟 ===DETAILED===), 自动忽略。"""
    out: List[Dict] = []
    pattern = re.compile(
        r"===TASK===\s*(.*?)\s*===DETAILED===\s*(.*?)\s*===SIMPLE===\s*(.*?)\s*===END===",
        re.DOTALL,
    )
    for m in pattern.finditer(text or ""):
        task, detailed, simple = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if task or detailed or simple:
            out.append({"task": task, "detailed": detailed, "simple": simple})
    return out


def _render(items: List[Dict]) -> str:
    """证据 list → 一段喂给 Reckoner 的文本(逐条详细三元组 + 概述)。"""
    chunks = []
    for it in items:
        block = []
        if it.get("task"):
            block.append(f"◆ 查证项: {it['task']}")
        if it.get("detailed"):
            block.append(it["detailed"])
        if it.get("simple"):
            block.append(f"【概述】{it['simple']}")
        chunks.append("\n".join(block))
    return "\n\n".join(chunks)


def investigate(query: str, context: str = "", model: str = DEFAULT_MODEL) -> str:
    """Reckoner 查证问题 → 一次后验视角 grounding → 蒸馏证据文本。空/不可用 → ""。"""
    if not (query or "").strip():
        return ""
    try:
        from google import genai
        from google.genai import types
        from google.genai import errors as genai_errors
    except Exception as e:
        logging.warning(f"[review_scout] google-genai 未装, 跳过查证: {e}")
        return ""

    try:
        client = genai.Client(api_key=_load_key())
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.4,
        )
        user_content = _build_user_content(query, context)

        response = None
        for attempt in range(1, 5):
            try:
                response = client.models.generate_content(
                    model=model, contents=user_content, config=cfg)
                break
            except genai_errors.ServerError:
                time.sleep(2 ** attempt)
        if response is None:
            logging.warning("[review_scout] grounding 多次重试失败, 返回空")
            return ""

        items = _parse_blocks(getattr(response, "text", "") or "")
        if not items:
            # 解析不出结构块时, 退回原始文本(总比丢弃强)
            raw = (getattr(response, "text", "") or "").strip()
            logging.info(f"[review_scout] 未解析出结构块, 退回原始文本 {len(raw)} 字")
            return raw
        logging.info(f"[review_scout] 产出 {len(items)} 条复盘证据")
        return _render(items)
    except Exception as e:
        logging.warning(f"[review_scout] 调用失败, 返回空: {type(e).__name__}: {e}")
        return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo_q = ("AAPL 在 2026-06-19~2026-06-23 窗口内: 是否有当时(6/17)不知道的突发利多? "
              "宏观风险偏好(美伊局势/油价)是否改善抵消了关税成本利空?")
    ctx = ("对 AAPL 在 2026-06-17 那份看空报告做 3 交易日后验的查证(非辩论)。"
           "相关时段 2026-06-19 ~ 2026-06-23。**硬截止 2026-06-23, 绝不参考晚于此的信息**。")
    print(investigate(demo_q, context=ctx))
