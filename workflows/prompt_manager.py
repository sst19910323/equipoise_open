# -----------------------------------------------------------------
# 文件路径: workflows/prompt_manager.py
# (V9 - 重构：适配新架构 constitution/formats/tasks)
# -----------------------------------------------------------------
import yaml
import logging
import re
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

class PromptManager:
    def __init__(self, base_dir="prompts/api"):
        self.base_path = Path(base_dir)
        if not self.base_path.exists():
            self.base_path = Path(__file__).parent.parent / "prompts" / "api"
            
        self.prompts: Dict[str, Any] = {
            "constitution": {},  # shared_rules + 三方soul
            "formats": {},       # debate_output + final_report_output
            "tasks": {}          # task.yaml
        }
        self._load_all_prompts()

    def _load_all_prompts(self):
        try:
            # 1. Load Constitution (shared_rules + souls)
            constitution_dir = self.base_path / "constitution"
            if constitution_dir.exists():
                for f in constitution_dir.glob("*.yaml"):
                    with f.open('r', encoding='utf-8') as file:
                        content = yaml.safe_load(file)
                        if content:
                            # 文件名作为key，如 shared_rules, zealot_soul, etc.
                            self.prompts["constitution"][f.stem] = content
            
            # 2. Load Formats (output formats)
            formats_dir = self.base_path / "formats"
            if formats_dir.exists():
                for f in formats_dir.glob("*.yaml"):
                    with f.open('r', encoding='utf-8') as file:
                        content = yaml.safe_load(file)
                        if content:
                            self.prompts["formats"][f.stem] = content

            # 3. Load Tasks
            tasks_dir = self.base_path / "tasks"
            if tasks_dir.exists():
                for f in tasks_dir.glob("*.yaml"):
                    with f.open('r', encoding='utf-8') as file:
                        content = yaml.safe_load(file)
                        if content:
                            self.prompts["tasks"][f.stem] = content
            
            logger.info(f"✅ PromptManager Ready (V9)")
            
        except Exception as e:
            logger.error(f"❌ Prompt Load Error: {e}", exc_info=True)

    def get_system_prompt(self, agent: str) -> str:
        """
        组装System Prompt = shared_rules + agent_soul
        agent: zealot / reaper / fulcrum
        """
        shared_rules = self.prompts["constitution"].get("shared_rules", {}).get("shared_rules", "")
        soul_key = f"{agent}_soul"
        soul = self.prompts["constitution"].get(soul_key, {}).get(f"{soul_key}", "")
        
        if not soul:
            logger.warning(f"⚠️ Soul not found for agent: {agent}")
            return shared_rules
        
        return f"{shared_rules}\n\n{soul}"

    def get_output_format(self, agent: str, phase: str) -> str:
        """
        获取输出格式
        agent: zealot / reaper / fulcrum / chronicler
        phase: init / debate / moderation / finalize / signoff
        """
        formats = self.prompts["formats"]

        if phase == "init":
            # 三方辩论者共用init_output
            return formats.get("debate_output", {}).get("init_output", "")

        elif phase == "debate":
            # 三方辩论者共用debate_output（Fulcrum剥离仲裁职能后与Z/R同层）
            return formats.get("debate_output", {}).get("debate_output", "")

        elif phase == "moderation":
            # Chronicler每轮结束后的CONTINUE/TERMINATE决策
            return formats.get("debate_output", {}).get("chronicler_moderation_output", "")

        elif phase == "finalize":
            # Chronicler写最终报告
            return formats.get("final_report_output", {}).get("final_report_output", "")

        elif phase == "signoff":
            # Zealot / Reaper / Fulcrum 三方通用签章
            return formats.get("final_report_output", {}).get("signoff_output", "")

        return ""

    def get_task_prompt(self, phase: str, task_type: str, data: Dict[str, Any]) -> str:
        """
        获取任务指令并填充数据
        phase: init / debate / finalize / signoff
        task_type: stock / etf / general
        data: 包含symbol, raw_data, debate_history, turn, final_report等
        """
        tasks = self.prompts["tasks"].get("task", {})
        
        # 1. 获取task模板
        task_key = f"{phase}_task"
        task_template = tasks.get(task_key, "")
        if not task_template:
            return f"[Error: Task '{task_key}' not found]"
        
        # 2. 获取raw_data模板（如果需要）
        if phase in ["init", "debate", "finalize"] and "raw_data" not in data:
            raw_data_key = f"raw_data_{task_type}"
            raw_data_template = tasks.get(raw_data_key, "")
            if raw_data_template:
                data["raw_data"] = self._regex_format(raw_data_template, data)
        
        # 3. 填充数据
        return self._regex_format(task_template, data)

    def _search_toggle_note(self, symbol: str) -> str:
        """按 evidence_search 总开关返回一行注入注释 (贴在宪法/有限数据铁律旁)。
        enabled → ON 注释; 否则 → OFF。一个开关全标的统一, 不再按标的挑。读配置失败 → OFF (安全)。"""
        cfg = {}
        try:
            from workflows.evidence_officer import load_search_cfg
            cfg = load_search_cfg()
            active = bool(cfg.get("enabled"))
        except Exception:
            active = False
        if active:
            note = (
                "【本次外部搜索能力：已启用】\n"
                "每轮辩论你都可以在 <search_request> 写下想让证据官（Scout）查证/搜索的内容，下一轮把新证据喂回给你。适用场景：\n"
                "- raw_data 里没有、但你想知道的信息；\n"
                "- 你想到的、想确认的最新消息或数据；\n"
                "- 你觉得需要了解的前因后果、更详细的背景。\n"
                "本场搜索有固定轮数额度（每轮辩论会机械告知共几轮、已用几轮）。本轮无需查证就留空。仍**严禁直接编造数字与事实**。"
            )
            # linkup 模式: 证据官把你的疑问归纳成 task 丢给 Linkup **网页搜索**, 提问要搜得到 (grounding 模式更聪明, 不加此约束)
            if str(cfg.get("provider", "linkup")).lower() == "linkup":
                note += (
                    "\n**注意搜索是网页搜索（新闻/事件/背景），不是数据终端，提的问题要搜得到**：\n"
                    "- 别搜你 raw_data 里**已有的量化数据**（最新价、成交量、RSI、均线、布林带等都已喂给你，搜了也白搜）；\n"
                    "- 别搜冷门到**只有专门数据源才有的精确数字**（如每周外资净买卖额、某指标实时读数）——网搜给不出干净结果；\n"
                    "- 该搜的是**定性**信息：具体新闻/事件/政策/催化剂、某叙事是否被证实、某消息的前因后果与时间线——你不确定、想核实的那些。"
                )
            return note
        return (
            "【本次外部搜索能力：未启用】\n"
            "本次无外部搜索，<search_request> 留空忽略。严守\"只用 raw_data 提供的信息、不臆造数字与事实\"。"
        )

    def build_full_prompt(self, agent: str, phase: str, task_type: str, data: Dict[str, Any]) -> Dict[str, str]:
        """
        构建完整的prompt
        返回: {"system": system_prompt, "user": user_prompt}
        """
        # System = shared_rules + soul + (搜索开关注释) + output_format
        system_prompt = self.get_system_prompt(agent)
        # 仅辩论相位(init/debate)注入搜索开关一行注释; 主持/终报/签章无关
        if phase in ("init", "debate"):
            note = self._search_toggle_note(data.get("symbol", ""))
            if note:
                system_prompt += f"\n\n{note}"
        output_format = self.get_output_format(agent, phase)
        if output_format:
            system_prompt += f"\n\n## 输出格式\n{output_format}"
        
        # User = task + data
        user_prompt = self.get_task_prompt(phase, task_type, data)
        
        return {
            "system": system_prompt,
            "user": user_prompt
        }

    def _regex_format(self, template: str, data: Dict[str, Any]) -> str:
        def replace_match(match):
            key = match.group(1)
            if key in data:
                val = data[key]
                if isinstance(val, (dict, list)):
                    return str(val)
                return str(val)
            return match.group(0)

        pattern = re.compile(r'\{([a-zA-Z0-9_]+)\}')
        try:
            return pattern.sub(replace_match, template)
        except Exception as e:
            logger.error(f"Regex Format Error: {e}")
            return template