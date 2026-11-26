# -----------------------------------------------------------------
# 文件路径: workflows/nodes.py
# (V10.0 - 四方架构：Z/R/F 三辩论者 + Chronicler 史官)
#
# 变化要点：
#   - Fulcrum从"仲裁者"剥离为纯辩论者，与Z/R同层
#   - 新增Chronicler：秩序维护(CONTINUE/TERMINATE) + 写最终报告 + 记忆管理
#   - Chronicler不接触raw_data，仅基于辩论记录工作
#   - 三方签章统一（Fulcrum也签章）
#   - debate_history和turn_count由chronicler_moderation_node维护
# -----------------------------------------------------------------
import json
import logging
import re
import yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Callable, Optional, List

from .state import DebateState

import sys
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from .xml_response_parser import parse_response as parse_debate_response
from .xml_final_report_parser import parse_final_response, parse_signoff_response

logger = logging.getLogger(__name__)


class ParliamentNodes:
    def __init__(self, prompt_manager, llm_caller: Callable, config_path: str = "config/data_sources.yaml", llm_client=None):
        self.pm = prompt_manager
        self.call_llm = llm_caller
        self.llm_client = llm_client
        self.config_path = Path(config_path)
        self.output_dir = self._resolve_output_dir()

        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_output_dir(self) -> Path:
        path_to_load = self.config_path
        if not path_to_load.exists():
            path_to_load = Path(__file__).resolve().parents[1] / "config/data_sources.yaml"

        if not path_to_load.exists():
            return Path("data/debate")

        try:
            with open(path_to_load, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            relative_path = config.get("data_sources", {}).get("ai_analysis", {}).get("output_dir", "data/debate")
            project_root = path_to_load.parent.parent
            return (project_root / relative_path).resolve()
        except:
            return Path("data/debate")

    def _get_model_signatures(self) -> Dict[str, str]:
        """获取各角色使用的模型名称 (用于报告签名)"""
        role_mapping = getattr(self.llm_client, 'role_mapping', {})
        models_cfg = getattr(self.llm_client, 'models_cfg', {})

        def _get_name(role_key: str) -> str:
            model_key = role_mapping.get(role_key, 'unknown')
            cfg = models_cfg.get(model_key, {})
            return cfg.get('name', model_key)

        return {
            "zealot":     _get_name("zealot"),
            "reaper":     _get_name("reaper"),
            "fulcrum":    _get_name("fulcrum"),
            "chronicler": _get_name("chronicler"),
        }

    # =========================================================================
    # 解析辅助
    # =========================================================================

    def _parse_debate_output(self, raw_text: str, role: str) -> Dict[str, Any]:
        return parse_debate_response(raw_text, role=role)

    def _parse_final_report(self, raw_text: str) -> Dict[str, Any]:
        return parse_final_response(raw_text)

    def _parse_signoff(self, raw_text: str, role: str) -> Dict[str, Any]:
        return parse_signoff_response(raw_text, role=role)

    def _parse_moderation(self, raw_text: str) -> Dict[str, Any]:
        """解析chronicler的CONTINUE/TERMINATE决策 + 三方精简版本"""
        decision = "CONTINUE"
        reason = ""
        polished = {"zealot": "", "reaper": "", "fulcrum": ""}

        dm = re.search(r"<decision>\s*(\w+)\s*</decision>", raw_text, re.IGNORECASE)
        if dm:
            decision = dm.group(1).upper()

        rm = re.search(r"<reason>(?:\s*<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</reason>", raw_text, re.DOTALL)
        if rm:
            reason = rm.group(1).strip()

        # 解析 round_memory 下的三个子节点
        rm_block = re.search(r"<round_memory>(.*?)</round_memory>", raw_text, re.DOTALL)
        if rm_block:
            block = rm_block.group(1)
            for role in ("zealot", "reaper", "fulcrum"):
                m = re.search(
                    rf"<{role}>(?:\s*<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{role}>",
                    block, re.DOTALL
                )
                if m:
                    polished[role] = m.group(1).strip()

        if decision not in ("CONTINUE", "TERMINATE"):
            decision = "CONTINUE"
        return {"decision": decision, "reason": reason, "polished": polished}

    def _fmt_debate_batch(self, batch: List[Dict[str, Any]]) -> str:
        """格式化辩论记录；优先使用Chronicler精简版(polished_text)，兜底回raw_xml/content"""
        parts = []
        for entry in batch:
            role        = entry.get('role', '?')
            round_label = entry.get('round', '?')
            polished    = entry.get('polished_text', '')
            raw_xml     = entry.get('raw_xml', '')
            content     = entry.get('content', {})
            text = polished or raw_xml or json.dumps(content, ensure_ascii=False, indent=2)
            parts.append(f"### [{role}] {round_label}\n{text}")
        return "\n\n".join(parts)

    # =========================================================================
    # 证据官搜索 (Evidence Officer / Grounding) 辅助
    # =========================================================================

    def _search_state(self, symbol: str) -> Dict[str, Any]:
        """读 evidence_search 总开关, 判断本次是否搜索 + 预算上限。失败 → 安全关闭。
        enabled 一个开关全标的统一(开就全开/关就全关), 不再按标的挑。"""
        try:
            from workflows.evidence_officer import load_search_cfg
            cfg = load_search_cfg()
            active = bool(cfg.get("enabled"))
            return {"active": active, "max_rounds": int(cfg.get("max_rounds", 3) or 3),
                    "model": cfg.get("model", "gemini-3.5-flash")}
        except Exception:
            return {"active": False, "max_rounds": 3, "model": "gemini-3.5-flash"}

    def _fmt_evidence(self, evidence: List[Dict[str, Any]]) -> str:
        """累积证据 → 注入辩论的文本(自身看完整: task+simple+detailed; source 不给 AI)。"""
        if not evidence:
            return ""
        blocks = []
        for i, it in enumerate(evidence, 1):
            task     = (it.get("task") or "").strip()
            simple   = (it.get("simple") or "").strip()
            detailed = (it.get("detailed") or "").strip()
            seg = f"### 证据{i}：{task}\n**结论**：{simple}"
            if detailed:
                seg += f"\n**前因后果/时间线**：{detailed}"
            blocks.append(seg)
        return "\n\n".join(blocks)

    # =========================================================================
    # 1. Init Nodes — 建立三辩论者对话线程
    # =========================================================================

    def _build_init_messages(self, agent: str, state: DebateState) -> List[Dict[str, str]]:
        """Z/R/F初始化：system=宪法+人设+搜索开关，user=格式+原始数据+初始任务"""
        system_content = self.pm.get_system_prompt(agent)
        search_note = self.pm._search_toggle_note(state.get('symbol', ''))
        if search_note:
            system_content += f"\n\n{search_note}"
        output_format  = self.pm.get_output_format(agent, "init")
        task_content   = self.pm.get_task_prompt("init", state['context_type'], {
            "symbol": state.get('symbol', 'UNKNOWN'),
            **state['raw_data']
        })
        user_content = f"## 输出格式\n{output_format}\n\n{task_content}"
        return [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": user_content},
        ]

    def zealot_init_node(self, state: DebateState) -> Dict[str, Any]:
        logger.info("--- [Zealot] Initializing ---")
        messages = self._build_init_messages("zealot", state)
        resp = self.call_llm(messages, role="zealot")
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_debate_output(resp, "zealot")
        action = output.get('action', 50)
        return {
            "zealot_messages":    messages,
            "zealot_latest":      resp,
            "zealot_last_action": int(action) if isinstance(action, (int, float)) else 50,
        }

    def reaper_init_node(self, state: DebateState) -> Dict[str, Any]:
        logger.info("--- [Reaper] Initializing ---")
        messages = self._build_init_messages("reaper", state)
        resp = self.call_llm(messages, role="reaper")
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_debate_output(resp, "reaper")
        action = output.get('action', 50)
        return {
            "reaper_messages":    messages,
            "reaper_latest":      resp,
            "reaper_last_action": int(action) if isinstance(action, (int, float)) else 50,
        }

    def fulcrum_init_node(self, state: DebateState) -> Dict[str, Any]:
        """Fulcrum现在是纯辩论者，只输出自己的立场；不再裁决、不再写batch"""
        logger.info("--- [Fulcrum] Initializing ---")
        messages = self._build_init_messages("fulcrum", state)
        resp = self.call_llm(messages, role="fulcrum")
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_debate_output(resp, "fulcrum")
        action = output.get('action', 50)
        return {
            "fulcrum_messages":    messages,
            "fulcrum_latest":      resp,
            "fulcrum_last_action": int(action) if isinstance(action, (int, float)) else 50,
            "current_phase":       "moderation",
        }

    # =========================================================================
    # 2. Debate Nodes — 三辩论者平等
    # =========================================================================

    def _run_debate_round(self, agent: str, state: DebateState) -> Dict[str, Any]:
        """Z/R/F通用辩论逻辑；使用Chronicler精简后的记忆作为历史注入"""
        turn = state.get('turn_count', 1)
        logger.info(f"--- [{agent.capitalize()}] Round {turn} ---")

        messages_key = f"{agent}_messages"
        messages     = list(state[messages_key])
        output_format = self.pm.get_output_format(agent, "debate")
        symbol        = state.get('symbol', 'UNKNOWN')

        # 优先使用Chronicler精简后的历史，fallback到原始debate_history（兼容性兜底）
        history     = state.get('polished_debate_history', []) or state.get('debate_history', [])
        history_str = self._fmt_debate_batch(history) if history else "（无先前记录）"

        incremental_msg = (
            f"## 输出格式\n{output_format}\n\n"
            f"## 第{turn}轮辩论 | 标的：{symbol}\n\n"
            f"## 完整辩论记录\n{history_str}"
        )

        # 证据官查证结果(若有): 现喂下一轮, 自身看完整(含 detailed)
        evidence_str = self._fmt_evidence(state.get('evidence', []))
        if evidence_str:
            incremental_msg += (
                f"\n\n## 证据官（Scout）查证结果\n"
                f"以下是证据官就此前各方疑问做的外部搜索查证, 请据此更新判断（注意时间线与信源把关）：\n\n"
                f"{evidence_str}"
            )

        # 搜索预算: 纯机械提示本场总轮数 + 已用轮数, 不引导搜或不搜
        ss = self._search_state(symbol)
        if ss["active"]:
            used = int(state.get('search_rounds_used', 0) or 0)
            mx = ss["max_rounds"]
            if used >= mx:
                incremental_msg += (
                    f"\n\n【外部搜索】本场共 {mx} 轮，已用满 {used} 轮，<search_request> 留空即可。"
                )
            else:
                incremental_msg += (
                    f"\n\n【外部搜索】本场共 {mx} 轮，已用 {used} 轮。"
                    f"本轮如有想查证的内容在 <search_request> 写明，无则留空。"
                )

        messages.append({"role": "user", "content": incremental_msg})
        resp = self.call_llm(messages, role=agent)
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_debate_output(resp, agent)
        action = output.get('action', 50)
        return {
            messages_key:           messages,
            f"{agent}_latest":      resp,
            f"{agent}_last_action": int(action) if isinstance(action, (int, float)) else 50,
        }

    def zealot_debate_node(self, state: DebateState) -> Dict[str, Any]:
        return self._run_debate_round("zealot", state)

    def reaper_debate_node(self, state: DebateState) -> Dict[str, Any]:
        return self._run_debate_round("reaper", state)

    def fulcrum_debate_node(self, state: DebateState) -> Dict[str, Any]:
        """Fulcrum现在是普通辩论者，不再裁决、不再写batch"""
        return self._run_debate_round("fulcrum", state)

    # =========================================================================
    # 2.5 Evidence Search — 证据官(Scout)节点 (fulcrum_debate → evidence_search → moderation)
    #
    #   仅当: 搜索总开关 enabled 且 预算未耗尽 且 三方有人提了请求。
    #   否则 pass-through(返回 {} 不改状态)。
    #   流程: 收集本轮三方 search_request → evidence_officer.investigate(一次 grounding)
    #         → 累积进 state.evidence(现喂下一轮) → best-effort 存 PG evidence 表 → 预算 +1。
    #   证据 in-memory 现生成现喂; PG 只管存档/复现。失败绝不搞挂辩论。
    # =========================================================================

    def evidence_search_node(self, state: DebateState) -> Dict[str, Any]:
        symbol = str(state.get('symbol', '') or '').upper()
        turn   = state.get('turn_count', 1)
        ss     = self._search_state(symbol)

        # 收集本轮三方 search_request (从各自 latest 输出解析)
        z = (self._parse_debate_output(state.get('zealot_latest', ''),  "zealot").get('search_request') or '').strip()
        r = (self._parse_debate_output(state.get('reaper_latest', ''),  "reaper").get('search_request') or '').strip()
        f = (self._parse_debate_output(state.get('fulcrum_latest', ''), "fulcrum").get('search_request') or '').strip()
        requests = {"zealot": z, "reaper": r, "fulcrum": f}

        # 门禁: 搜索总开关未启用 → pass-through
        if not ss["active"]:
            return {}
        used = int(state.get('search_rounds_used', 0) or 0)
        # 预算耗尽 → pass-through (debate 节点已会提示收敛)
        if used >= ss["max_rounds"]:
            logger.info(f"--- [Scout] 预算已用完 ({used}/{ss['max_rounds']}), 跳过 ---")
            return {"search_requests": requests}
        # 三方都没提 → 跳过(不计预算)
        if not any(requests.values()):
            logger.info(f"--- [Scout] 第{turn}轮三方无 search_request, 跳过 ---")
            return {"search_requests": requests}

        logger.info(f"--- [Scout] 第{turn}轮触发搜索 ({used + 1}/{ss['max_rounds']}) ---")
        try:
            # 搜源按 provider 选套: linkup(默认, deepseek+Linkup) | gemini(老 grounding, 封存)
            if ss.get("provider", "linkup") == "linkup":
                from workflows.evidence_officer_linkup import investigate
            else:
                from workflows.evidence_officer import investigate
            context = f"标的：{symbol}；第{turn}轮辩论"
            items = investigate(requests, context=context, model=ss["model"])
        except Exception as e:
            logger.warning(f"[Scout] investigate 异常, 本轮无证据: {type(e).__name__}: {e}")
            items = []

        if not items:
            # 搜了但没出证据(全空/失败) → 不计预算(给后续轮机会), 仅记请求
            logger.info("--- [Scout] 本轮未产出证据 ---")
            return {"search_requests": requests}

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_evidence = list(state.get('evidence', []) or []) + items

        # best-effort 存档 PG evidence 表(失败不影响辩论)
        try:
            from datalayer.pg.evidence_pg_store import save_evidence
            save_evidence(symbol, items, generated_at)
        except Exception as e:
            logger.warning(f"[Scout] PG 存档异常(忽略): {e}")

        logger.info(f"--- [Scout] 第{turn}轮产出 {len(items)} 条证据, 累计 {len(new_evidence)} ---")
        return {
            "search_requests":       requests,
            "evidence":              new_evidence,
            "search_rounds_used":    used + 1,
            "evidence_generated_at": generated_at,
        }

    # =========================================================================
    # 3. Chronicler Moderation — 秩序维护 + 记忆管理
    #
    #    在init阶段结束后 + 每轮debate结束后 被调用。
    #    负责：
    #      - 将本轮Z/R/F的XML归档到debate_history
    #      - 更新自己的messages线程（累积记忆）
    #      - 判定CONTINUE / TERMINATE
    #      - 决定下一轮turn_count
    # =========================================================================

    # =========================================================================
    # 3. Moderation (V11+) — 纯代码节点, 史官封存后接管
    # =========================================================================
    # 决策逻辑:
    #   - Init 阶段 (turn=0): 强制 CONTINUE, 至少跑 1 轮真辩论
    #   - 三方 wants_continue 任一为 true => CONTINUE
    #   - 三方都 false => TERMINATE
    #   - turn >= max_turns 触顶 => TERMINATE (硬上限, 由 _route 处理)
    # 历史记录:
    #   - debate_history: 留 raw_xml 给 final JSON 存档
    #   - polished_debate_history: 直接用 analysis_text 作 'polished_text' (代码精简,
    #     不烧 LLM; 后续若想智能压缩可在此处插 chronicler 复活)

    def moderation_node(self, state: DebateState) -> Dict[str, Any]:
        turn = state.get('turn_count', 0)
        is_init_phase = (turn == 0)
        round_label   = "Init" if is_init_phase else f"回合{turn}"
        logger.info(f"--- [Moderation] {round_label} ---")

        zealot_xml  = state.get('zealot_latest', '')
        reaper_xml  = state.get('reaper_latest', '')
        fulcrum_xml = state.get('fulcrum_latest', '')

        z_parsed = self._parse_debate_output(zealot_xml,  "zealot")
        r_parsed = self._parse_debate_output(reaper_xml,  "reaper")
        f_parsed = self._parse_debate_output(fulcrum_xml, "fulcrum")

        # 三方投票 (parser 已确保 wants_continue 是 bool, 缺失默认 True)
        z_wc = bool(z_parsed.get('wants_continue', True))
        r_wc = bool(r_parsed.get('wants_continue', True))
        f_wc = bool(f_parsed.get('wants_continue', True))
        any_wants = z_wc or r_wc or f_wc

        if is_init_phase:
            decision = "CONTINUE"  # Init 不是辩论, 强制至少跑 1 轮
            reason = "Post-init forced CONTINUE"
        elif any_wants:
            decision = "CONTINUE"
            voters = [name for name, v in [("Z", z_wc), ("R", r_wc), ("F", f_wc)] if v]
            reason = f"Vote: continue requested by {voters}"
        else:
            decision = "TERMINATE"
            reason = "Vote: all three agents agreed to end"

        logger.info(f"[Moderation] {round_label} | Z={z_wc} R={r_wc} F={f_wc} | decision={decision}")

        # debate_history: 完整 raw_xml 留档
        batch_updates = [
            {"role": "Zealot",  "round": round_label, "content": z_parsed, "raw_xml": zealot_xml},
            {"role": "Reaper",  "round": round_label, "content": r_parsed, "raw_xml": reaper_xml},
            {"role": "Fulcrum", "round": round_label, "content": f_parsed, "raw_xml": fulcrum_xml},
        ]

        # polished_debate_history: 代码精简 = 直接用 analysis_text + summary_statement
        def _quick_polish(parsed: Dict[str, Any]) -> str:
            summary = parsed.get('summary_statement', '') or ''
            analysis = parsed.get('analysis_text', '') or ''
            action = parsed.get('action', 50)
            op = parsed.get('operation_type', 'HOLD_NEUTRAL')
            head = f"[action={action} op={op}] {summary}".strip()
            return f"{head}\n{analysis}".strip() if analysis else head

        polished_updates = [
            {"role": "Zealot",  "round": round_label, "content": z_parsed,
             "polished_text": _quick_polish(z_parsed) or zealot_xml},
            {"role": "Reaper",  "round": round_label, "content": r_parsed,
             "polished_text": _quick_polish(r_parsed) or reaper_xml},
            {"role": "Fulcrum", "round": round_label, "content": f_parsed,
             "polished_text": _quick_polish(f_parsed) or fulcrum_xml},
        ]

        next_turn = turn + 1

        return {
            "debate_history":          batch_updates,
            "polished_debate_history": polished_updates,
            "debate_status":           decision,
            "turn_count":              next_turn,
            "current_phase":           "finalize" if decision == "TERMINATE" else "debate",
        }

    # =========================================================================
    # 4. Fulcrum Finalize (V11+) — 史官封存后, Fulcrum 兼职写最终报告
    # =========================================================================

    def fulcrum_finalize_node(self, state: DebateState) -> Dict[str, Any]:
        logger.info("--- [Fulcrum] Finalizing (writing final report) ---")

        # 复用 Fulcrum 自己的 messages 线程, 他已有完整辩论上下文
        messages      = list(state.get('fulcrum_messages', []))
        output_format = self.pm.get_output_format("chronicler", "finalize")  # 复用旧的 final_report 格式
        symbol        = state.get('symbol', 'UNKNOWN')

        finalize_msg = (
            f"## 最终报告阶段 | 标的：{symbol}\n\n"
            f"辩论已结束。作为支点裁决人, 基于你和 Zealot/Reaper 完整的辩论历程,\n"
            f"撰写最终综合报告。\n\n"
            f"**关键提示**：报告中的 action 来自三方论证质量的综合权衡, **不一定等于你**\n"
            f"**最后一轮自己的立场**——若 Zealot 或 Reaper 在某些维度提出了你未充分回应的关键证据,\n"
            f"应在最终 action 上体现该侧立场的权重。避免懒惰式直接复用你最后的 fulcrum_last_action.\n\n"
            f"## 输出格式\n{output_format}"
        )
        messages.append({"role": "user", "content": finalize_msg})
        resp = self.call_llm(messages, role="fulcrum")
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_final_report(resp)
        report = output.get('final_report', output)

        # Fulcrum 是报告撰写人, 不需要再对自己的报告 signoff;
        # 直接用 report 的 action 作为他的 final_action, reservation 留空
        fulcrum_signoff = {
            "final_action": report.get('action', state.get('fulcrum_last_action', 50)),
            "reservation":  "(报告撰写人，立场即报告 action)",
        }

        return {
            "fulcrum_messages": messages,
            "final_report":     report,
            "fulcrum_signoff":  fulcrum_signoff,
            "current_phase":    "signoff",
        }

    # =========================================================================
    # [封存] Chronicler Moderation — V9.7 旧实现, 已被 moderation_node 接管
    # 保留代码用于兼容性; 不再被 LangGraph 调用
    # =========================================================================

    def chronicler_moderation_node(self, state: DebateState) -> Dict[str, Any]:
        turn = state.get('turn_count', 0)
        is_init_phase = (turn == 0)
        round_label   = "Init" if is_init_phase else f"回合{turn}"
        logger.info(f"--- [Chronicler] Moderation | {round_label} ---")

        zealot_xml  = state.get('zealot_latest', '')
        reaper_xml  = state.get('reaper_latest', '')
        fulcrum_xml = state.get('fulcrum_latest', '')

        # 解析三方原始发言供 debate_history 留档
        z_parsed = self._parse_debate_output(zealot_xml,  "zealot")
        r_parsed = self._parse_debate_output(reaper_xml,  "reaper")
        f_parsed = self._parse_debate_output(fulcrum_xml, "fulcrum")

        # 构建chronicler的messages线程（首次调用初始化system）
        messages = list(state.get('chronicler_messages', []))
        if not messages:
            system_content = self.pm.get_system_prompt("chronicler")
            messages.append({"role": "system", "content": system_content})

        output_format = self.pm.get_output_format("chronicler", "moderation")
        symbol        = state.get('symbol', 'UNKNOWN')
        round_xml_block = (
            f"[Zealot]\n{zealot_xml}\n\n"
            f"[Reaper]\n{reaper_xml}\n\n"
            f"[Fulcrum]\n{fulcrum_xml}"
        )
        task_msg = (
            f"## 秩序维护 | {round_label} 已结束\n"
            f"标的：{symbol}\n\n"
            f"## 本轮三方发言\n{round_xml_block}\n\n"
            f"## 输出格式\n{output_format}"
        )
        messages.append({"role": "user", "content": task_msg})
        resp = self.call_llm(messages, role="chronicler")
        messages.append({"role": "assistant", "content": resp})

        parsed   = self._parse_moderation(resp)
        decision = parsed["decision"]
        reason   = parsed["reason"]
        polished = parsed["polished"]

        # 硬保险：Init结束后强制CONTINUE至少1轮，Init不是辩论
        if is_init_phase and decision == "TERMINATE":
            logger.warning("[Chronicler] Post-init TERMINATE suppressed; forcing CONTINUE for at least one debate round.")
            decision = "CONTINUE"

        logger.info(f"[Chronicler] {round_label} decision: {decision} | {reason[:100]}")

        # debate_history：原始XML留档（用于最终JSON报告）
        batch_updates = [
            {"role": "Zealot",  "round": round_label, "content": z_parsed, "raw_xml": zealot_xml},
            {"role": "Reaper",  "round": round_label, "content": r_parsed, "raw_xml": reaper_xml},
            {"role": "Fulcrum", "round": round_label, "content": f_parsed, "raw_xml": fulcrum_xml},
        ]

        # polished_debate_history：Chronicler精简版，供下轮辩论者注入记忆
        # 若某方精简失败（polished为空），fallback到raw_xml保证完整性
        polished_updates = [
            {"role": "Zealot",  "round": round_label, "content": z_parsed,
             "polished_text": polished.get("zealot", "")  or zealot_xml},
            {"role": "Reaper",  "round": round_label, "content": r_parsed,
             "polished_text": polished.get("reaper", "")  or reaper_xml},
            {"role": "Fulcrum", "round": round_label, "content": f_parsed,
             "polished_text": polished.get("fulcrum", "") or fulcrum_xml},
        ]

        # 轮次递增：init→1，回合N→N+1
        next_turn = turn + 1

        return {
            "chronicler_messages":     messages,
            "debate_history":          batch_updates,
            "polished_debate_history": polished_updates,
            "debate_status":           decision,
            "turn_count":              next_turn,
            "current_phase":           "finalize" if decision == "TERMINATE" else "debate",
        }

    # =========================================================================
    # 4. Chronicler Finalize — 写最终报告
    # =========================================================================

    def _data_time_anchor(self, state: DebateState) -> str:
        """把数据时点(market_data_timestamp_utc)转成给 LLM 的"现在时间"锚。
        史官不接触 raw_data, 没有它就只能瞎猜年份(曾把 next_review 写成 2025)。
        取不到时间戳 → 返回空串(不注入, 不报错)。"""
        ts = state.get('data_timestamp_for_report')
        if not ts:
            return ""
        try:
            human = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            human = str(ts)
        return (
            f"## 当前时点（数据快照）\n"
            f"本场分析的分时/量化数据截至 **{human}**——这就是\"现在\"。\n"
            f"next_review 里所有日期(recommend_after / date_reached 等)都以此为基准推算, "
            f"**年份务必写对, 不要照抄输出样例里的年份**。\n\n"
        )

    def chronicler_finalize_node(self, state: DebateState) -> Dict[str, Any]:
        """V11: 史官只负责 finalize, 不参与每轮 moderation, 所以这里 fresh 构建对话.
        喂他: chronicler_soul (系统) + 完整 raw_xml 辩论历史 (用户) + finalize 任务.
        不喂 raw_data (保持中立综合者定位, 引用必须来自三方辩论)."""
        logger.info("--- [Chronicler] Finalizing (V11: synthesis-only) ---")

        system_content = self.pm.get_system_prompt("chronicler")
        output_format  = self.pm.get_output_format("chronicler", "finalize")
        symbol         = state.get('symbol', 'UNKNOWN')

        # 完整辩论记录: _fmt_debate_batch 优先用 polished_text, fallback raw_xml
        history     = state.get('debate_history', []) or state.get('polished_debate_history', [])
        history_str = self._fmt_debate_batch(history) if history else "（无辩论记录）"

        # 史官不喂 raw_data, 单独把"数据时点"作为"现在时间"锚注入, 防止 next_review 年份/日期写乱
        time_anchor = self._data_time_anchor(state)

        finalize_msg = (
            f"## 最终报告阶段 | 标的：{symbol}\n\n"
            f"{time_anchor}"
            f"以下是 Z/R/F 三方完整辩论历史 (Init + 各回合)。\n"
            f"基于这些发言, 综合三方论证质量, 撰写最终裁决报告。\n\n"
            f"## 完整辩论记录\n{history_str}\n\n"
            f"## 输出格式\n{output_format}"
        )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": finalize_msg},
        ]
        resp = self.call_llm(messages, role="chronicler")
        messages.append({"role": "assistant", "content": resp})

        output = self._parse_final_report(resp)
        report = output.get('final_report', output)

        return {
            "chronicler_messages": messages,
            "final_report":        report,
            "current_phase":       "signoff",
        }

    # =========================================================================
    # 5. Signoff Nodes — 三方对等签章
    # =========================================================================

    def _run_signoff(self, agent: str, state: DebateState) -> Dict[str, Any]:
        """Z/R/F通用签章逻辑"""
        logger.info(f"--- [{agent.capitalize()}] Signing off ---")

        messages_key     = f"{agent}_messages"
        messages         = list(state[messages_key])
        output_format    = self.pm.get_output_format(agent, "signoff")
        final_report_str = json.dumps(state.get('final_report', {}), ensure_ascii=False, indent=2)
        symbol           = state.get('symbol', 'UNKNOWN')

        # 签章阶段也用精简历史
        history     = state.get('polished_debate_history', []) or state.get('debate_history', [])
        history_str = self._fmt_debate_batch(history) if history else "（无记录）"

        signoff_msg = (
            f"## 完整辩论记录\n{history_str}\n\n"
            f"## 签章确认 | 标的：{symbol}\n\n"
            f"## 最终裁决报告\n{final_report_str}\n\n"
            f"## 输出格式\n{output_format}"
        )
        messages.append({"role": "user", "content": signoff_msg})
        resp   = self.call_llm(messages, role=agent)
        output = self._parse_signoff(resp, agent)

        default_action = state.get(f"{agent}_last_action", 50)
        return {
            f"{agent}_signoff": {
                "final_action": output.get('final_action', default_action),
                "reservation":  output.get('reservation', ''),
            }
        }

    def zealot_signoff_node(self, state: DebateState) -> Dict[str, Any]:
        return self._run_signoff("zealot", state)

    def reaper_signoff_node(self, state: DebateState) -> Dict[str, Any]:
        return self._run_signoff("reaper", state)

    def fulcrum_signoff_node(self, state: DebateState) -> Dict[str, Any]:
        return self._run_signoff("fulcrum", state)

    # =========================================================================
    # 6. Data Saver
    # =========================================================================

    def data_saver_node(self, state: DebateState) -> Dict[str, Any]:
        logger.info("--- [System] Saving Data ---")
        final_json = state.get('final_report', {})
        if not final_json:
            logger.error("No final report to save!")
            return {}

        z_sig = state.get('zealot_signoff', {})  or {}
        r_sig = state.get('reaper_signoff', {})  or {}
        f_sig = state.get('fulcrum_signoff', {}) or {}

        # dissent 三方对等记录
        final_json['dissent'] = {
            "zealot_final_action":  z_sig.get('final_action', 50),
            "zealot_reservation":   z_sig.get('reservation', ''),
            "reaper_final_action":  r_sig.get('final_action', 50),
            "reaper_reservation":   r_sig.get('reservation', ''),
            "fulcrum_final_action": f_sig.get('final_action', 50),
            "fulcrum_reservation":  f_sig.get('reservation', ''),
        }

        final_json['signatures'] = self._get_model_signatures()

        # 证据官产出: 记时间戳(复现锚, 复现拿它查 /api/evidence) + 内嵌证据(前端直接展示, 无需再查 PG)
        evidence_items = state.get('evidence', []) or []
        final_json['evidence_generated_at'] = state.get('evidence_generated_at')
        final_json['evidence'] = evidence_items
        final_json['search_rounds_used'] = int(state.get('search_rounds_used', 0) or 0)

        final_json['debate_history'] = [
            {k: v for k, v in entry.items() if k != 'raw_xml'}
            for entry in state.get('debate_history', [])
        ]

        data_files = state.get('data_files', {})
        if data_files:
            final_json['data_files'] = data_files
            # 顶层暴露 parent_analysis_file (跨层记忆继承的实际依赖文件)
            if isinstance(data_files, dict):
                final_json['parent_analysis_file'] = data_files.get('parent_analysis_file')
            else:
                final_json['parent_analysis_file'] = None
        else:
            final_json['parent_analysis_file'] = None

        # 报告元字段 (无条件设, PG debates 的 as_of 主键/复现锚就靠 market_data_timestamp_utc)
        final_json['market_data_timestamp_utc'] = state.get('data_timestamp_for_report')
        final_json['report_generated_at'] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 报告写入 PG equipoise.debates (jsonb + 决策列); 不再落 JSON 文件 (已全量迁 PG)。
        try:
            from datalayer.pg.debate_pg_store import save_debate
            if save_debate(str(state.get('symbol', '')).upper(), final_json):
                logger.info(f"✅ Final report → PG debates ({state.get('symbol')})")
            else:
                logger.warning("⚠️ PG debates 写入返回 False (报告未入库)")
        except Exception as e:
            logger.warning(f"[data_saver] PG 写入异常: {e}")

        return {"final_report": final_json}
