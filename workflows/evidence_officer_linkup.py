# -*- coding: utf-8 -*-
"""
evidence_officer_linkup.py — 证据官 **linkup 模式**(辩论侧)。

接口与 gemini 版 evidence_officer.investigate 完全一致(返回 List[Dict]: task/simple/detailed/source),
下游(nodes 注入证据 / _render_general_evidence / data_saver)无感。
gemini 版(老 grounding)封存在 evidence_officer.py。引擎在 linkup_search.py。
deepseek 归纳 task + 写概述/结论; Linkup 搜 + 原文(逐字)+ 来源。
"""
from __future__ import annotations

from typing import Dict, List

# 辩论侧证据官宪法(前瞻、决策导向; 与复盘斥候视角刻意不同)
CONSTITUTION = """你是 Scout(斥候/证据官) —— 量化辩论系统里给三方(Zealot/Reaper/Fulcrum)查证的中立证据官。
- **中立**: 不站多空, 只还原事实。
- **信源政治倾向警惕**: 本系统全程中文运行, 简中语料受北京官方叙事污染严重 —— 优先**英文权威一手源**(Reuters/Bloomberg/WSJ/FT/官方文件); 中文源额外谨慎, 仅英文源缺位或补本地细节时采纳并提示可信度打折; 涉中美/地缘/政策默认警惕官方宣传。
- **缕清前因后果 + 时间线**: 把孤立信息还原成"谁何时什么背景下说/做、之前发生过什么、第一次还是反复"。
- **信息增量导向**: 帮辩论判断这条是**新增量**(新消息/新数据/新时间表)还是**老调**(反复喊过、市场已定价)。老调明说没意义, 新增量明说新在哪。
- **每条结论必带"该信息何时公开"**: 这是三方判"定价了没有"的原料, 比结论本身更重要; 来源顺手标档(官方/主流媒体/单一来源/传闻)。
- **如实**: 搜不到/不确定/信源矛盾 → 明说, 绝不编。"""


def _requests_text(requests: Dict[str, str]) -> str:
    parts = []
    for role in ("zealot", "reaper", "fulcrum"):
        t = (requests.get(role) or "").strip()
        if t:
            parts.append(f"{role.upper()}: {t}")
    return "\n".join(parts)


def _render_detailed(triples: List[Dict]) -> str:
    """三元组 list → 详细文本(与 gemini 版 detailed 字段同为字符串, 下游无感)。"""
    out = []
    for tr in triples:
        out.append(f"【结论】{tr.get('结论','')}\n【原文】{tr.get('原文','')}\n【来源】{tr.get('来源','')}")
    return "\n\n".join(out)


def investigate(requests: Dict[str, str], context: str = "", model=None) -> List[Dict]:
    """三方诉求 → deepseek 归纳 task → Linkup 搜 → deepseek 合成 → [{task, simple, detailed, source}]。
    model 入参忽略(linkup 模式固定用 deepseek)。全空/不可用 → []。"""
    rtext = _requests_text(requests)
    if not rtext.strip():
        return []
    from workflows.linkup_search import synthesize
    raw = synthesize(rtext, context=context, constitution=CONSTITUTION)
    out = []
    for t in raw:
        triples = t.get("detailed") or []
        out.append({
            "task": t.get("task", ""),
            "simple": t.get("simple", ""),
            "detailed": _render_detailed(triples),
            "source": [tr.get("来源", "") for tr in triples if tr.get("来源")],
        })
    return out
