#!/usr/bin/env python3
"""改名相关的回归(2026-10-01): 历史报告键名转换(report_names) 与 PG 新旧列名探测(datalayer.pg.names)。"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ledger_tally as LT  # noqa: E402
from report_names import upgrade_report  # noqa: E402
from datalayer.pg import names  # noqa: E402


def _old_round(role, rnd, score, pillars, moves=(), as_text=False):
    content = {
        "action": score, "operation_type": "LIMIT_ENTRY", "operation_target": "100",
        "operation_volume": "PILOT_SIZE",
        "preliminary_mentality": [{"name": "buy_the_dip", "probability": 0.6}],
        "summary_statement": "s", "analysis_text": "引用 TK1 的结论",
        "short_term": {"direction": "上行", "target": "110", "invalid": "95", "horizon_days": 5, "rationale": "x"},
        "pillars": pillars,
        "request_evidence": [{"type": "task", "playbook": "P07", "issue": "R1"},
                             {"type": "search", "question": "q"}],
        "stance_ledger": {"moves": list(moves),
                          "held": [{"claim": "R1", "tier": "T2", "response": "rebut", "note": "n"}],
                          "contingent": [], "reclaims": []},
    }
    return {"round": rnd, "role": role, "content": json.dumps(content, ensure_ascii=False) if as_text else content}


def old_report() -> dict:
    z = [{"id": "Z1", "kind": "pillar", "claim": "a", "tier": "T1", "stake": 5, "status": "settled_for"},
         {"id": "Z2", "kind": "boost", "claim": "b", "tier": "T2", "stake": 3, "status": "settled_for"}]
    r = [{"id": "R1", "kind": "pillar", "claim": "c", "tier": "T2", "stake": 5, "status": "settled_against"}]
    return {
        "action": 61, "operation_type": "LIMIT_ENTRY", "operation_target": "100", "operation_volume": "PILOT_SIZE",
        "short_term": {"direction": "震荡", "target": "95-105", "horizon_days": 5, "rationale": "x"},
        "mentality": [{"name": "buy_the_dip", "probability": 0.55}],
        "organized_z": 62, "organized_r": 50,
        "residual_adjustment": {"delta_vs_organized": "d", "basis": []},
        "conviction_budget": {"legs": [{"leg": "腿", "refs": ["Z1", "TK1"], "claim": 5}], "total_claim": 5},
        "dissent": {"zealot_final_action": 62, "zealot_reservation": "z",
                    "reaper_final_action": 55, "reaper_reservation": "r"},
        "provisional_ruling": {"action": 60, "operation_type": "LIMIT_ENTRY", "operation_volume": "PILOT_SIZE"},
        "arbiter_shadow_log": [{"round": "Init", "action": 56, "operation_volume": "PILOT_SIZE", "moved_by": ["TK1"]}],
        "tasks": [{"task_id": "TK1", "playbook": "P07", "issue": "R1", "requested_by": "Reaper", "round": "Init",
                   "status": "verified", "tier_eligible": "T1",
                   "request": {"type": "task", "playbook": "P07", "issue": "R1"}, "evidence_refs": ["EV1"]}],
        "evidence": [
            {"task": "TK3", "playbook": "P07", "issue": "R1", "target": "检验动能衰竭", "legs": [{"kind": "data"}],
             "verdict": "refuted", "tier_eligible": "T1", "simple": "s", "kind": "data", "round": "Init",
             "requested_by": "Reaper", "status": "verified", "task_id": "TK1"},
            {"task": "微软财报后需求如何", "verdict": "supported", "simple": "s", "kind": "search", "round": "Init",
             "requested_by": "Q-PARENT", "status": "unverified"},
        ],
        "debate_history": [
            _old_round("Zealot", "Init", 62, z),
            _old_round("Reaper", "Init", 45, r, as_text=True),
            _old_round("Reaper", "回合1", 50, r, moves=[{"id": "R-R1-1", "issue": "R1", "delta": 5, "tier": "T2"}]),
        ],
        "unknown_extra_key": {"keep": True},
    }


def _entries(doc):
    out = []
    for e in doc["debate_history"]:
        c = e["content"]
        out.append({"round": e["round"], "role": e["role"], "content": json.loads(c) if isinstance(c, str) else c})
    return out


class ReportNamesTest(unittest.TestCase):
    def test_old_report_gets_new_names(self):
        doc, changes, skipped = upgrade_report(old_report())
        self.assertEqual(skipped, "")
        self.assertTrue(changes)
        for gone in ("action", "operation_volume", "short_term", "organized_z", "organized_r",
                     "conviction_budget", "arbiter_shadow_log", "tasks"):
            self.assertNotIn(gone, doc, gone)
        self.assertEqual(doc["composite_score"], 61)
        self.assertEqual(doc["operation_size"], "PILOT_SIZE")
        self.assertEqual(doc["short_term_path"]["direction"], "震荡")
        self.assertEqual(doc["mentality"][0], {"name": "buy_the_dip", "weight": 0.55})
        self.assertEqual((doc["ledger_score_z"], doc["ledger_score_r"]), (62, 50))
        self.assertIn("delta_vs_ledger", doc["residual_adjustment"])
        self.assertEqual(doc["deviation_support"], {"legs": [{"basis": "腿", "refs": ["Z1", "TK1"], "points": 5}],
                                                    "total_points": 5})
        self.assertEqual(doc["dissent"]["zealot_composite_score"], 62)
        self.assertEqual(doc["provisional_ruling"]["composite_score"], 60)
        self.assertEqual(doc["arbiter_private_scores"][0]["composite_score"], 56)
        mc = doc["model_calls"][0]
        self.assertEqual((mc["model_call_id"], mc["model"], mc["suggested_evidence_tier"]), ("TK1", "P07", "T1"))
        self.assertEqual(mc["request"], {"type": "model_call", "model": "P07", "issue": "R1"})
        self.assertEqual(doc["unknown_extra_key"], {"keep": True})

    def test_evidence_items(self):
        doc, _, _ = upgrade_report(old_report())
        runner, search = doc["evidence"]
        # 模型调用条目: task 里是执行者自报的编号, 保留; 问题取自 target; 挂载用 task_id 那个编号
        self.assertEqual(runner["task"], "TK3")
        self.assertEqual(runner["question"], "检验动能衰竭")
        self.assertEqual(runner["model_call_id"], "TK1")
        self.assertEqual((runner["model"], runner["suggested_evidence_tier"], runner["verdict"]), ("P07", "T1", "refutes"))
        self.assertIn("steps", runner)
        self.assertNotIn("legs", runner)
        # 搜索条目: task 就是问题
        self.assertEqual(search["question"], "微软财报后需求如何")
        self.assertNotIn("task", search)
        self.assertEqual((search["verdict"], search["requested_by"], search["status"]), ("supports", "Arbiter", "unverified"))

    def test_round_content_and_string_content_keep_their_type(self):
        doc, _, _ = upgrade_report(old_report())
        z0, r0, r1 = doc["debate_history"]
        self.assertIsInstance(z0["content"], dict)
        self.assertIsInstance(r0["content"], str)      # 原来是字符串就写回字符串
        c = z0["content"]
        self.assertEqual(c["composite_score"], 62)
        self.assertEqual([p["kind"] for p in c["claims"]], ["own", "reinforce"])
        self.assertEqual(c["claims"][0]["evidence_tier"], "T1")
        self.assertEqual(c["mentality"][0]["weight"], 0.6)
        self.assertEqual(c["request_evidence"][0], {"type": "model_call", "model": "P07", "issue": "R1"})
        self.assertEqual(c["stance_ledger"]["held"][0]["evidence_tier"], "T2")
        self.assertEqual(c["analysis_text"], "引用 TK1 的结论")     # 正文不动, 编号的值不改
        self.assertEqual(json.loads(r0["content"])["composite_score"], 45)
        self.assertEqual(r1["content"]["stance_ledger"]["moves"][0]["evidence_tier"], "T2")

    def test_idempotent_and_ledger_unchanged(self):
        old = old_report()
        before = LT.tally(_entries(old))
        doc, _, _ = upgrade_report(copy.deepcopy(old))
        after = LT.tally(_entries(doc))
        for side in ("zealot", "reaper"):
            self.assertEqual(before[side]["ledger_score"], after[side]["ledger_score"])
        snap = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        again, changes, _ = upgrade_report(doc)
        self.assertEqual(changes, [])
        self.assertEqual(json.dumps(again, ensure_ascii=False, sort_keys=True), snap)

    def test_requested_by_may_be_a_list(self):
        # 服务器演练抓到的: 历史证据条目的 requested_by 有时是列表
        doc = old_report()
        doc["evidence"][1]["requested_by"] = ["Zealot", "Q-PARENT"]
        doc["tasks"][0]["requested_by"] = ["Reaper"]
        doc, _, skipped = upgrade_report(doc)
        self.assertEqual(skipped, "")
        self.assertEqual(doc["evidence"][1]["requested_by"], ["Zealot", "Arbiter"])
        self.assertEqual(doc["model_calls"][0]["requested_by"], ["Reaper"])

    def test_odd_value_types_do_not_crash(self):
        doc = old_report()
        doc["evidence"][0]["verdict"] = ["refuted"]
        doc["evidence"][1]["task"] = {"q": "x"}
        doc["mentality"] = "buy_the_dip"
        doc["conviction_budget"] = "n/a"
        doc["dissent"] = None
        doc["debate_history"][0]["content"]["pillars"] = [None, "x", {"id": "Z1", "kind": ["pillar"], "tier": "T1"}]
        doc["debate_history"][0]["content"]["stance_ledger"] = []
        doc, changes, skipped = upgrade_report(doc)
        self.assertEqual(skipped, "")
        self.assertEqual(doc["composite_score"], 61)

    def test_new_key_wins_when_both_present(self):
        doc = old_report()
        doc["composite_score"] = 70
        doc, _, _ = upgrade_report(doc)
        self.assertEqual(doc["composite_score"], 70)
        self.assertNotIn("action", doc)

    def test_older_shapes_are_skipped(self):
        for shape in ({"final_report": {"action": 60}}, {"something": 1}, ["x"]):
            before = copy.deepcopy(shape)
            doc, changes, skipped = upgrade_report(shape)
            self.assertTrue(skipped)
            self.assertEqual(changes, [])
            self.assertEqual(doc, before)


class _Cur:
    """假游标: 按表名回放列清单, 记下执行过的 SQL。"""

    def __init__(self, schema):
        self.schema, self.sql, self._rows = schema, [], []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "information_schema.columns" in sql:
            self._rows = [(c,) for c in self.schema.get(params[0], [])]
        elif "information_schema.tables" in sql:
            self._rows = [(t,) for t in self.schema]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


OLD = {"debates": ["symbol", "as_of", "action", "operation_volume"],
       "tasks": ["task_id", "playbook", "tier_eligible"],
       "evidence_items": ["evidence_id", "task_id"], "evidence_entity": ["eid", "question"]}
NEW = {"debates": ["symbol", "as_of", "composite_score", "operation_size"],
       "model_calls": ["model_call_id", "model", "suggested_evidence_tier"],
       "evidence_items": ["evidence_id", "model_call_id"], "evidence_entity": ["eid", "question", "verified_scope"]}


class PgNamesTest(unittest.TestCase):
    def test_old_schema(self):
        cur = _Cur(OLD)
        self.assertEqual(names.debates(cur), {"composite_score": "action", "operation_size": "operation_volume"})
        self.assertEqual(names.model_calls(cur), {"table": "tasks", "model_call_id": "task_id", "model": "playbook",
                                                   "suggested_evidence_tier": "tier_eligible"})
        self.assertEqual(names.evidence(cur), {"model_call_id": "task_id", "verified_scope": False})

    def test_new_schema(self):
        cur = _Cur(NEW)
        self.assertEqual(names.debates(cur), {"composite_score": "composite_score", "operation_size": "operation_size"})
        self.assertEqual(names.model_calls(cur)["table"], "model_calls")
        self.assertEqual(names.evidence(cur), {"model_call_id": "model_call_id", "verified_scope": True})

    def test_row_key_bridging(self):
        self.assertEqual(names.with_new_keys({"action": 60, "operation_volume": "PILOT_SIZE"})["composite_score"], 60)
        row = names.with_old_keys({"model_call_id": "Z.MC1", "model": "P06", "suggested_evidence_tier": "T2"})
        self.assertEqual((row["task_id"], row["playbook"], row["tier_eligible"]), ("Z.MC1", "P06", "T2"))

    def test_writer_upsert_sql_follows_schema(self):
        spec = importlib.util.spec_from_file_location(
            "pg_store_debate", ROOT / "scripts" / "pg_store_debate.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old_sql = mod._upsert_sql(names.debates(_Cur(OLD)))
        new_sql = mod._upsert_sql(names.debates(_Cur(NEW)))
        self.assertIn("report_generated_at, action, operation_type", old_sql)
        self.assertIn("operation_volume    = EXCLUDED.operation_volume".replace("    ", " "),
                      " ".join(old_sql.split()))
        self.assertIn("report_generated_at, composite_score, operation_type", new_sql)
        self.assertNotIn("action", new_sql)
        self.assertEqual(old_sql.count("%s"), 10)   # 2026-10-24 多语言: 多了 lang 列
        self.assertEqual(new_sql.count("%s"), 10)


class MigrationPlanTest(unittest.TestCase):
    """迁移脚本的结构计划: 旧形状列全、顺序对(先改列名再改表名); 新形状不再有事可做。真跑要在库上先演练。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("migrate", ROOT / "scripts" / "migrate_pg_names_2026_10.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_old_schema_plan(self):
        plan = self.mod.schema_plan(_Cur(OLD))
        self.assertEqual(plan, [
            "ALTER TABLE debates RENAME COLUMN action TO composite_score",
            "ALTER TABLE debates RENAME COLUMN operation_volume TO operation_size",
            "ALTER TABLE tasks RENAME COLUMN task_id TO model_call_id",
            "ALTER TABLE tasks RENAME COLUMN playbook TO model",
            "ALTER TABLE tasks RENAME COLUMN tier_eligible TO suggested_evidence_tier",
            "ALTER TABLE tasks RENAME TO model_calls",
            "ALTER TABLE evidence_items RENAME COLUMN task_id TO model_call_id",
            "ALTER TABLE evidence_entity ADD COLUMN verdict text",
            "ALTER TABLE evidence_entity ADD COLUMN verified_scope text",
        ])

    def test_new_schema_has_nothing_left(self):
        new = dict(NEW)
        new["evidence_entity"] = ["eid", "question", "verdict", "verified_scope"]
        self.assertEqual(self.mod.schema_plan(_Cur(new)), [])

    def test_both_tables_present_stops(self):
        both = dict(OLD)
        both["model_calls"] = ["model_call_id"]
        with self.assertRaises(SystemExit):
            self.mod.schema_plan(_Cur(both))

    def test_value_plan_targets_current_table(self):
        old = dict(OLD)
        old["evidence_entity"] = ["eid", "verdict"]
        labels = [x[0] for x in self.mod.value_plan(_Cur(old))]
        self.assertIn("evidence_entity.verdict supported → supports", labels)
        self.assertTrue(any(x.startswith("tasks.requested_by") for x in labels), labels)
        new = dict(NEW)
        new["evidence_entity"] = ["eid", "verdict", "verified_scope"]
        labels = [x[0] for x in self.mod.value_plan(_Cur(new))]
        self.assertTrue(any(x.startswith("model_calls.requested_by") for x in labels), labels)


if __name__ == "__main__":
    unittest.main()
