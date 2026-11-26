# -*- coding: utf-8 -*-
"""
reckoner.py — Reckoner(清算者): BP 后验的事后清算 + 复盘归因 agent。

辩论之后登场: 给一份"成熟"的旧判决(报告) 打 0-100 分(0踩空/50错过/100踩中),
值得挖的再复盘归因。三阶段门禁(成本递增):
  阶段1 清算+判复盘  (只给 时间锚+量化摘要+报告)         → score/verdict/review
  阶段2 复盘判定      (review=yes 才追加给当时全部原始数据) → 有不确定?→search_request; 否则→diagnosis
  阶段3 看搜索下结论  (提了 search_request 才跑)            → diagnosis(凭搜索结果/现有信息)
模型 deepseek, 温度 0.5。Scout 历史查证由 search_fn 注入(查询里框 T~窗口止)。

orchestration 复用外部 provider:
  raw_data_provider() -> str   当时(as_of) point-in-time 全部数据文本 (复用 ContextAssembler as_of)
  search_fn(query)    -> str   grounding 结果文本 (Scout, 框历史窗); 挂/空 → 返回 "" 或 None
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Dict, Optional

# Reckoner 固定用的模型 key (单一来源; _ask 与签名记录都引它, 别各写各的)。
RECKONER_MODEL_KEY = "deepseek"


def reckoner_signature(llm) -> str:
    """机械记录"这份后验是谁写的" —— 跟 nodes._get_model_signatures 同源:
    从 models.yaml 的 name 取, 不是模型自报。拿不到则退回 model key 本身。"""
    cfg = getattr(llm, "models_cfg", {}).get(RECKONER_MODEL_KEY, {})
    return cfg.get("name", RECKONER_MODEL_KEY)


SYSTEM_PROMPT = """你是 **Reckoner（清算者）**。议会三方曾在过去某刻 T 对某标的下了判决；如今 dt 个交易日已过，结果摊在桌上。你的职责是冷静清算这笔判决的实际结果，并在需要时复盘归因。

铁律：
- 事后诸葛亮，但**只认事实、不放马后炮的空话**。对就是对，黑天鹅就老实说黑天鹅，别为显得深刻硬编原因。
- **严禁编造数字**；价格事实只能用【后验量化摘要】里给的。
- 目的不是羞辱当时的判决，是**让后来的辩论更准**——找可复用的教训。
- 时间务必以【时间锚】为准，别照抄样例里的年份。

任务一 · 清算打分（必做）：报告 = **操作决策(action) + 市场分析**，两者**并重**。
- **action 无疑很重要**——它是真金白银要执行的方向与力度（买/卖/持、目标、止损）；看操作对不对、有没有兑现还是被打脸。
- **分析同等重要**——它是这笔操作的理解根基（方向、幅度、对风险/催化剂的预判、叙事论点）；看理解准不准、风险预判应没应验、走势是否印证了论点。
你要**两者并重地综合评估这份判决兑现得如何**。【后验量化摘要】是判断依据(事实)，不是打勾清单。
给 0–100 综合分，三个大致锚点：
  · 0 踩空：操作方向 / 核心理解整体错了（看反、被打脸、论点证伪）。
  · 50 错过：判得保守/模糊，行情走出明显机会却没抓住。
  · 100 踩中：操作对、理解准、兑现到位。
锚点只是标尺，**具体几分你综合全报告(操作+分析)权衡**，别机械套规则（如"摸到目标又崩回成本"该扣多少，自己判断）。附一句话结论(verdict)。
再判 review：结果清楚 / 没可复用教训（明显踩中、纯黑天鹅、纯噪音）→ review=no，到此为止；值得挖 → review=yes。

任务二 · 复盘判定（仅当系统追加了【当时全部原始数据】）：你刚读完报告 + 当时 AI 能看到的全部原始数据。先只回一件事——要不要先让 grounding（复盘斥候）查证窗口内的实情？
  · **判得越离谱越要查**：若这次明显踩不准（方向看反 / 大幅错过 / 核心论点被证伪），你**默认应当**先查证——回头看窗口内是不是有当时(T)不知道的突发事件、或当时的弱信号事后被证实，别凭记忆/常识硬归因。此时请**只**输出 <search_request>（具体想查什么：标的自身、行业/大盘、宏观/汇率等分头列清），diagnosis 先留空。
  · 只有当原始数据已**足以**解释结果（纯噪音、铁定的全市场黑天鹅、或数据里白纸黑字写着主因）时，才跳过查证、直接按下面"diagnosis 写法"写归因。

任务三 · 看搜索结果下结论（仅当系统追加了【查证结果】）：综合 报告 + 当时原始数据 + 查证结果，按"diagnosis 写法"给出**充分**归因。
  · 绝大多数情况都该给明确归因——凭现有数据 + 查证结果推断，**该下结论就下，别动辄"不确定"**。
  · 只有实在搜不到 / 查证失败，才退一步凭现有信息写，如实标"待确认"。这是例外、不是默认。

**diagnosis 写法（铁律）**：你读了整份报告 + 大量原始数据(+可能的查证结果)，归因必须**配得上这个信息量**——严禁读了一大堆只憋出两三句话敷衍。
  · **先详细、后概论**：先逐条写【详细结论】，最后用【概论】一段蒸馏。
  · **结论 ↔ 原文**：每条详细结论尽量配一段支撑【原文】——取自查证结果或当时原始数据的**逐字片段**（不必给来源 URL，系统不记录，只看原文）；纯凭推理的标注"（模型推断）"。别把几处缝成一段假原文。
  · **显式回答三问**：① 有没有当时(T)不知道的**突发事件**直接解释了走势？② 当时是否有**弱证据/苗头**事后在窗口内被证实（由弱转强、当时本可更重视）？③ 这次踩不准**多少属于意外**、多少是信息当时就在却没读对（可复用的判断教训）？
  · 落到具体数字/事件/原文上，别空泛；可复用教训要具体到"下次遇到 X 该怎样"。

严格按以下 XML 输出，不要输出 XML 以外的任何内容：
<reckoning>
  <score>0-100 的整数</score>
  <verdict><![CDATA[一句话清算结论]]></verdict>
  <review>yes 或 no</review>
  <search_request><![CDATA[阶段二要查证时只填这个(分头列清想查什么); 否则留空]]></search_request>
  <diagnosis><![CDATA[阶段二(无需查证)或阶段三的归因。按上面"diagnosis 写法"铁律: 先逐条【详细结论】(尽量配【原文】逐字片段)、后【概论】一段; 显式回答 突发/弱转强/意外三问; 充分、落到具体证据。阶段一/无复盘留空]]></diagnosis>
</reckoning>"""


def _time_anchor(symbol: str, as_of, dt: int, win_start, win_end) -> str:
    return (
        "## 时间锚（务必据此推理，别照抄样例年份）\n"
        f"- 这份报告做于 **{as_of}**（标的 {symbol}）。\n"
        f"- 你在验证它之后 **{dt} 个交易日**的表现，窗口 = **{win_start} ~ {win_end}**。\n"
        "- 对报告而言\"现在\"是 T；对你而言这段已成历史、结果已知。查证也只限这个窗口。\n"
    )


def _cdata(tag: str, text: str) -> str:
    return f"<{tag}><![CDATA[\n{text}\n]]></{tag}>"


def _get(xml: str, tag: str, default: str = "") -> str:
    """抠 <tag>...</tag> (含 CDATA)。无 → default。"""
    m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else default


def _parse(xml: str) -> Dict:
    score_raw = _get(xml, "score")
    try:
        score = int(float(re.search(r"-?\d+(?:\.\d+)?", score_raw).group())) if score_raw else None
    except Exception:
        score = None
    review = _get(xml, "review").strip().lower() in ("yes", "true", "1", "是")
    return {
        "score": score,
        "verdict": _get(xml, "verdict"),
        "review": review,
        "search_request": _get(xml, "search_request"),
        "diagnosis": _get(xml, "diagnosis"),
    }


def _ask(llm, user_blocks: list) -> Dict:
    """一次 LLM 调用 (deepseek, t=0.5) + 解析。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_blocks)},
    ]
    try:
        raw = llm._chat_completion(messages, RECKONER_MODEL_KEY, 0.5)
    except Exception as e:
        logging.warning(f"[reckoner] LLM 调用异常: {e}")
        return {"score": None, "verdict": "[LLM error]", "review": False,
                "search_request": "", "diagnosis": ""}
    return _parse(raw or "")


def run_reckoning(symbol: str, as_of, dt: int, win_start, win_end,
                  report: Dict, digest: Dict, llm,
                  raw_data_provider: Optional[Callable[[], str]] = None,
                  search_fn: Optional[Callable[[str], str]] = None) -> Dict:
    """三阶段编排, 返回 {score, verdict, review, search_request, diagnosis}。
    raw_data_provider: () -> 当时全部原始数据文本 (review=yes 才调)。
    search_fn: (query) -> 查证结果文本 (有 search_request 才调; 挂/空 → '')。"""
    import json
    anchor = _time_anchor(symbol, as_of, dt, win_start, win_end)
    digest_block = "## 后验量化摘要（已算好的事实，价格只能用这里的）\n" + \
                   json.dumps(digest, ensure_ascii=False, indent=2, default=str)
    report_block = "## 原报告（当时的判决）\n" + \
                   json.dumps(report, ensure_ascii=False, indent=2, default=str)

    # 阶段1: 清算 + 判复盘
    r = _ask(llm, [anchor, digest_block, report_block])
    if not r["review"]:
        r["search_request"] = ""
        return r

    # 阶段2: 复盘判定 (追加当时原始数据)
    raw_text = ""
    if raw_data_provider is not None:
        try:
            raw_text = raw_data_provider() or ""
        except Exception as e:
            logging.warning(f"[reckoner] 取当时原始数据失败: {e}")
    if not raw_text:
        # 没有原始数据可看 → 让它就现有信息归因 (退化)
        r2 = _ask(llm, [anchor, digest_block, report_block,
                        "## 当时全部原始数据\n(取不到, 凭摘要+报告归因)"])
    else:
        r2 = _ask(llm, [anchor, digest_block, report_block,
                        "## 当时 AI 看到的全部原始数据 (point-in-time)\n" + raw_text])
    # 保留阶段1 的 score/verdict (阶段2/3 只补 diagnosis/search_request)
    r2["score"], r2["verdict"], r2["review"] = r["score"], r["verdict"], True

    # 阶段2 直接给了归因 → 收工
    if r2.get("diagnosis") and not r2.get("search_request"):
        return r2
    # 阶段2 只提了查证 → 阶段3
    sr = r2.get("search_request", "")
    if not sr:
        return r2  # 既没 diagnosis 也没 search_request, 原样返回
    results = ""
    if search_fn is not None:
        try:
            results = search_fn(sr) or ""
        except Exception as e:
            logging.warning(f"[reckoner] Scout 查证异常: {e}")
    res_block = ("## 查证结果\n" + results) if results else \
                "## 查证结果\n(没搜到有效信息 / 查证失败 —— 凭现有信息写，可如实标'待确认')"
    base = [anchor, digest_block, report_block]
    if raw_text:
        base.append("## 当时 AI 看到的全部原始数据 (point-in-time)\n" + raw_text)
    base.append("## 你之前提出的查证\n" + sr)
    base.append(res_block)
    r3 = _ask(llm, base)
    r3["score"], r3["verdict"], r3["review"] = r["score"], r["verdict"], True
    r3["search_request"] = sr
    return r3


if __name__ == "__main__":
    # 不连 LLM 的解析自检
    sample = """<reckoning>
      <score>70</score>
      <verdict><![CDATA[方向看对但没到目标]]></verdict>
      <review>yes</review>
      <search_request><![CDATA[6月18~20 NVDA 有无突发利空]]></search_request>
      <diagnosis><![CDATA[]]></diagnosis>
    </reckoning>"""
    p = _parse(sample)
    assert p["score"] == 70 and p["review"] is True, p
    assert "没到目标" in p["verdict"] and "NVDA" in p["search_request"], p
    assert p["diagnosis"] == "", p
    print("reckoner._parse 自检通过")
