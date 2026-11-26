# -*- coding: utf-8 -*-
"""
review_scout_linkup.py — 复盘斥候 **linkup 模式**(后验侧)。

接口与 gemini 版 review_scout.investigate 一致: investigate(query, context, ...) -> str(蒸馏文本)。
额外吃 to_date(后验窗口止) → Linkup **API 级硬截止**, 比 gemini 版只靠 prompt 软约束更可靠。
gemini 版封存在 review_scout.py。引擎在 linkup_search.py(deepseek + Linkup)。
"""
from __future__ import annotations

from typing import Dict, List

# 复盘侧宪法(回顾、归因导向; 与辩论证据官刻意不同)
CONSTITUTION = """你是 ReviewScout(复盘斥候) —— 量化系统**后验复盘**阶段的证据官。窗口(T~窗口止)已走完、结果已知, 你回头还原窗口内实情, 帮清算者(Reckoner)归因。
- **回顾视角**: 重还原因果与时间线 —— 窗口内有没有当时(T)**不知道的突发事件**直接解释了走势? T 时点的**弱信号/苗头**事后是否在窗口内**被证实(转强)**? 踩不准里**多少属意外** vs 本可看出?
- **硬时间边界**: 绝不引用晚于"窗口止"的信息(系统已用 Linkup to_date 卡过, 你也别据常识脑补窗口之后的未来)。
- **中立** + **信源警惕**: 优先英文权威一手源; 简中源谨慎(受官方叙事污染), 涉地缘/政策警惕宣传。
- **如实**: 查无突发就老实说"窗口内未见可解释的突发事件"——这本身有价值(指向纯噪音或判断失误)。"""


def _render(raw: List[Dict]) -> str:
    """[{task, simple, detailed:[三元组]}] → 一段蒸馏文本(逐条 结论+原文 + 概述)喂给 Reckoner。"""
    blocks = []
    for t in raw:
        lines = [f"## {t.get('task','')}"]
        for tr in (t.get("detailed") or []):
            lines.append(f"【结论】{tr.get('结论','')}\n【原文】{tr.get('原文','')}\n【来源】{tr.get('来源','')}")
        if t.get("simple"):
            lines.append(f"【概述】{t['simple']}")
        blocks.append("\n\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def investigate(query: str, context: str = "", from_date=None, to_date=None, model=None) -> str:
    """Reckoner 的复盘查证 → deepseek 归纳 task → Linkup 搜(硬截止 to_date) → deepseek 合成 → 蒸馏文本。
    空/不可用 → ""。"""
    if not (query or "").strip():
        return ""
    from workflows.linkup_search import synthesize
    raw = synthesize(query, context=context, constitution=CONSTITUTION,
                     from_date=from_date, to_date=to_date)
    return _render(raw)
