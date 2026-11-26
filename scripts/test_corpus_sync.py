#!/usr/bin/env python3
"""语料与代码的同步测试(重建批 2, 2026-09-30)。

1. 心态表(manuals/mentality/mentality.md)的枚举与容许操作 == validate_debate.ADMISSIBLE。
2. 生成的 agent 文件与源头一致(gen_agent_files --check); skill 路标与正文一致(gen_skill_stubs --check)。
3. principles.md 手册索引里的路径都存在。
4. 格式册里的枚举与校验器一致(claims.kind / status / operation_type / verdict)。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
AGENT = ROOT / "prompts" / "agent"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_mentality_table(text: str) -> dict:
    """合法性表: | `name` | 定义 | `OP` `OP` | → {name: {ops}}; "四种交易型皆可" → 四种交易型。"""
    trade = {"ENTRY", "TRIM_POSITION", "LIQUIDATE_NOW"}
    out = {}
    for line in text.splitlines():
        m = re.match(r"\|\s*`([a-z_]+)`\s*\|[^|]*\|\s*([^|]*)\|", line)
        if not m:
            continue
        ops = set(re.findall(r"`([A-Z_]+)`", m.group(2)))
        if "交易型皆可" in m.group(2) or re.search(r"any trade type", m.group(2), re.I):   # 2026-10-24 语料英文化: 两种写法都认
            ops = set(trade)
        out[m.group(1)] = ops
    return out


class MentalityTableSyncTest(unittest.TestCase):
    def test_table_matches_validator_enum(self):
        """v3.4: the mentality table lists the seven decision reasons (no permitted-operation column any more);
        the set must equal validate_debate.MENTALITY_V34. ADMISSIBLE stays only for reading pre-3.4 reports."""
        vd = _load("validate_debate")
        text = (AGENT / "manuals/mentality/mentality.md").read_text(encoding="utf-8")
        names = {m.group(1) for line in text.splitlines() for m in [re.match(r"\|\s*`([a-z_]+)`\s*\|", line)] if m}
        self.assertEqual(names, set(vd.MENTALITY_V34), "心态七类集合不一致")
        self.assertEqual(len(vd.MENTALITY_V34), 7)
        self.assertNotIn("avoid_uncertainty", names)   # the uncertainty-as-free-exit labels are gone
        self.assertNotIn("hedge_position", names)


class GeneratedAgentsTest(unittest.TestCase):
    def test_agent_files_are_fresh(self):
        gen = _load("gen_agent_files")
        self.assertEqual(gen.outdated(), [])

    def test_agent_files_carry_frontmatter_and_source(self):
        z = (ROOT / ".claude/agents/zealot.md").read_text(encoding="utf-8")
        self.assertTrue(z.startswith("---\nname: zealot\n"))
        self.assertIn("tools: Read, Glob\n", z)
        self.assertIn("The Optimist", z)                 # 2026-10-24 起语料英文
        self.assertIn("## 5. What you will receive", z)   # debater.md 已并入
        r = (ROOT / ".claude/agents/runner.md").read_text(encoding="utf-8")
        self.assertIn("WebSearch", r)
        self.assertIn("Model-call executor", r)


class GeneratedSkillStubsTest(unittest.TestCase):
    def test_skill_stubs_are_fresh(self):
        gen = _load("gen_skill_stubs")
        self.assertEqual(gen.stale(gen.REPO_OUT, ""), [],
                         "skill 路标过期或有多余的旧路标: 跑 python scripts/gen_skill_stubs.py")

    def test_skill_docs_point_to_existing_files(self):
        """skill 正文与参考页里写到的工程内路径都要存在(合并目录后最容易漏改的就是这些)。"""
        missing = []
        pat = re.compile(r"`((?:skills|scripts|orchestration|prompts|\.claude|\.codex|docs|datalayer|workflows)/[A-Za-z0-9_./-]+?\.(?:md|py|yaml|sh))`")
        docs = list((ROOT / "skills").rglob("*.md")) + [ROOT / "CLAUDE.md", ROOT / "AGENTS.md",
                                                        ROOT / "docs" / "ARCHITECTURE.md"]
        for doc in docs:
            if not doc.exists():
                missing.append(f"{doc.relative_to(ROOT)}: 文件不存在")
                continue
            for rel in pat.findall(doc.read_text(encoding="utf-8")):
                if "<" in rel or "*" in rel:
                    continue
                if not (ROOT / rel).exists():
                    missing.append(f"{doc.relative_to(ROOT)}: {rel}")
        self.assertEqual(missing, [])


class IndexPathsTest(unittest.TestCase):
    def test_manual_index_paths_exist(self):
        text = (AGENT / "principles.md").read_text(encoding="utf-8")
        paths = set(re.findall(r"`(manuals/[A-Za-z0-9_./<>-]+\.md)`", text))
        self.assertGreaterEqual(len(paths), 9)   # 2026-10-02 合本后: 模型手册 7 本 + 心态表 + 申报 + 能力地图
        for p in paths:
            if "<" in p:
                continue
            self.assertTrue((AGENT / p).exists(), f"索引路径不存在: {p}")
        for ctx in ("stock", "etf", "general", "general_eu", "general_jp", "general_kr"):
            self.assertTrue((AGENT / f"manuals/evidence/context_{ctx}.md").exists())


class FormatEnumSyncTest(unittest.TestCase):
    def test_debate_format_enums_match_validator(self):
        vd = _load("validate_debate")
        vr = _load("validate_role_record")
        fmt = (AGENT / "formats/debate.md").read_text(encoding="utf-8")
        for k in vd.KIND_ENUM:
            self.assertIn(f"`{k}`", fmt, f"claims.kind 枚举 {k} 不在格式册")
        self.assertEqual(vd.KIND_ENUM, vr.KINDS)
        for s in vd.STATUS_ENUM:
            self.assertIn(f"`{s}`", fmt, f"status 枚举 {s} 不在格式册")
        for op in vd.DECLARED_OPS:          # MARKET_ENTRY / LIMIT_ENTRY 由组装器派生, 不在格式册里
            self.assertIn(op, fmt)
        self.assertNotIn("MARKET_ENTRY", fmt); self.assertNotIn("LIMIT_ENTRY", fmt)
        self.assertNotIn("boost", fmt)
        self.assertNotIn('"action"', fmt)
        self.assertNotIn("operation_volume", fmt)

    def test_ruler_debate_format_shapes(self):
        """v3.4 (C1-C3): effects as ops, answers, per-side execution; no authored cost, no baseline, no prose effects (effect_if_*)."""
        fmt = (AGENT / "formats/debate.md").read_text(encoding="utf-8")
        for s in ('"op": "p_move"', '"answers"', '"effect"', '"scenario_updates"', '"applies"',
                  '"overweight"', '"underweight"', '"schema": "3.4"'):
            self.assertIn(s, fmt, f"debate.md 缺 3.4 形状 {s}")
        for s in ("cost_bps", "effect_if_true", "effect_if_false", "baseline"):
            self.assertNotIn(s, fmt, f"debate.md 仍有 {s}")
        self.assertNotIn('"composite_score"', fmt)   # 由脚本写, 不在辩手形状里

    def test_ruler_final_format_arbiter_fields_only(self):
        """v3.4 (C6): the arbiter writes claims_review / mixture / voided; scenario / operation_* / composite_score are derived."""
        fmt = (AGENT / "formats/final.md").read_text(encoding="utf-8")
        authored = fmt.split("## Fields injected", 1)[0]
        for s in ('"claims_review"', '"claim_rev"', '"mixture"', '"voided"'):
            self.assertIn(s, authored, f"final.md 缺仲裁者字段 {s}")
        for s in ('"scenario": {', '"operation_type"', '"operation_target"', '"composite_score"', '"operation_size"'):
            self.assertNotIn(s, authored, f"final.md 仍把派生字段 {s} 当仲裁者手写")
        self.assertNotIn("delta_vs_model", fmt)
        self.assertNotIn("cost_bps\": \"<int>", fmt)
        injected = fmt.split("## Fields injected", 1)[1]
        for s in ("`scenario`", "`operation_type`", "`operation_target`", "`operation_size`", "`composite_score`",
                  "`scenario.derived`", "`s_ref_z`", "`odds`", "`spawns`", "endorsed_scenario"):
            self.assertIn(s, injected, f"final.md 注入清单缺 {s}")

    def test_ruler_revision_formats(self):
        for name in ("objection", "signoff"):
            self.assertIn('"scenario_revision"', (AGENT / f"formats/{name}.md").read_text(encoding="utf-8"))

    def test_ruler_no_position_baseline_strings(self):
        """v3.4 (③.10, fixture g): one neutral reference; no long_1 / flat / cost_bps in mentality, formats, principles."""
        files = [AGENT / "manuals/mentality/mentality.md", AGENT / "principles.md"] + sorted((AGENT / "formats").glob("*.md"))
        for f in files:
            txt = f.read_text(encoding="utf-8")
            self.assertNotIn("long_1", txt, f.name)
            self.assertIsNone(re.search(r"`flat`|\bflat\b", txt), f"{f.name}: flat")
            self.assertNotIn("cost_bps", txt, f.name)

    def test_verdict_vocabulary(self):
        ev = (AGENT / "formats/evidence.md").read_text(encoding="utf-8")
        for v in ("supports", "refutes", "mixed", "not_found", "unrelated"):
            self.assertIn(v, ev)
        for role in ("scout", "runner"):
            txt = (AGENT / f"roles/{role}.md").read_text(encoding="utf-8")
            self.assertIn("`unrelated`", txt)


class Round8CorpusTest(unittest.TestCase):
    """2nd fix wave (GPT round 8): the corpus states what the code enforces."""

    def test_debate_md_carried_effect_and_one_challenge_rules_agree(self):
        fmt = (AGENT / "formats/debate.md").read_text(encoding="utf-8")
        self.assertIn("An effect your `applies` has already carried cannot be changed by revising the claim", fmt)
        self.assertIn("never a second challenge", fmt)
        self.assertIn("change it by revising it (`revises`), never by filing a second challenge on the same target", fmt)
        self.assertNotIn("revise it rather than filing a second.", fmt)
        vrr = (ROOT / "scripts" / "validate_role_record.py").read_text(encoding="utf-8")
        self.assertIn("effect already carried", vrr)
        self.assertIn("never a second challenge", vrr)
        self.assertIn("one challenge per target per side", vrr)

    def test_debate_md_P_T_set_sign_restriction_matches_the_code(self):
        import scenario_replay as sr
        fmt = (AGENT / "formats/debate.md").read_text(encoding="utf-8")
        self.assertIn("it may not move `P_T` across `P0`", fmt)
        sc = {"schema": "3.4", "T_days": 15, "P0": 100.0, "nodes": [
            {"id": "A", "p": 0.5, "P_T": 110.0, "refs": [], "events": []},
            {"id": "B", "p": 0.5, "P_T": 90.0, "refs": [], "events": []}]}
        with self.assertRaises(sr.OpError) as cm:
            sr.apply_ops(sc, [{"op": "P_T_set", "node": "A", "value": 95.0}])
        self.assertIn("wrong side of P0", str(cm.exception))
        self.assertEqual(sr.apply_ops(sc, [{"op": "P_T_set", "node": "A", "value": 104.0}])["nodes"][0]["P_T"], 104.0)

    def test_debate_md_states_self_settlement_and_concession(self):
        fmt = (AGENT / "formats/debate.md").read_text(encoding="utf-8")
        self.assertIn("your own `settled_for` / `uncontested` does not close your claim", fmt)
        self.assertIn("`conceded` / `withdrawn` on your own challenge concedes its target", fmt)
        self.assertIn("`withdrawn` on your own claim that a challenge ever targeted counts as `conceded`", fmt)
        self.assertIn("a challenge you mark `settled_for` stays open until its target's owner concedes or withdraws", fmt)

    def test_mentality_neutral_only_when_best_action_ce_not_positive(self):
        txt = (AGENT / "manuals/mentality/mentality.md").read_text(encoding="utf-8")
        self.assertIn("it wins only when the best action's certainty equivalent is ≤ 0", txt)
        self.assertNotIn("preferred as the smaller change", txt)
        om = _load("odds_model")
        self.assertIn("if ce_best <= 0:", (ROOT / "scripts" / "odds_model.py").read_text(encoding="utf-8"))
        self.assertTrue(hasattr(om, "compare_strategies"))

    def test_evidence_review_effect_whole_or_not_at_all(self):
        txt = (AGENT / "manuals/synthesis/arbiter-evidence-review.md").read_text(encoding="utf-8")
        self.assertIn("a registered effect still applies whole or not at all", txt)
        self.assertIn("a partial result is a new claim or a revision of the claim", txt)
        self.assertNotIn("applied only for the scope that holds", txt)

    def test_final_md_void_rule_matches_the_code(self):
        txt = (AGENT / "formats/final.md").read_text(encoding="utf-8")
        self.assertIn('"void rejected: effect is executable"', txt)
        self.assertIn("void rejected: effect is executable", (ROOT / "scripts" / "scenario_replay.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
