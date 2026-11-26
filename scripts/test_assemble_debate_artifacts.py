#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("assemble_debate_artifacts.py")
SPEC = importlib.util.spec_from_file_location("assemble_debate_artifacts", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AssemblyError = MODULE.AssemblyError
assemble = MODULE.assemble


class AssembleArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.draft = self.root / "draft.json"
        self.debate = self.root / "debate.jsonl"
        self.evidence_buffer = self.root / "evidence.jsonl"
        self.analysis = self.root / "analysis.json"
        self.evidence = self.root / "evidence.json"
        self.draft.write_text(json.dumps({"symbol": "TEST", "asset_tier": "na"}), encoding="utf-8")
        role = {"composite_score": 50, "analysis_text": "x", "claims": [{"id": "Z1"}]}
        self.debate.write_text(
            json.dumps({"round": "Init", "role": "Zealot", "content": role}) + "\n" +
            json.dumps({"round": "Init", "role": "Reaper", "content": role}) + "\n",
            encoding="utf-8",
        )
        rows = [
            {"round": "Init", "arbiter_private_score": {"round": "Init", "composite_score": 50}},
            {"round": "回合1", "arbiter_notes": "notes", "tally": "tally"},
            {"provisional_ruling": {"composite_score": 50, "operation_type": "HOLD_NEUTRAL",
                                     "mentality": "wait_for_confirmation", "rationale": "x"}},
            {"round": "回合1", "requested_by": "Zealot", "status": "verified",
             "content": {"question": "check", "simple": "finding", "detailed": "details",
                         "source": ["primary"], "kind": "search"}},
            {"round": "回合1", "requested_by": "Reaper", "status": "verified",
             "model_call_id": "MC1", "model": "P03", "issue": "R1",
             "content": {"target": "breadth", "finding": "mixed", "steps": [1],
                         "payload_text": "raw"}},
        ]
        self.evidence_buffer.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_assembles_audit_fields_and_normalizes_model_call_evidence(self):
        receipt = assemble(self.draft, self.debate, self.evidence_buffer,
                           self.analysis, self.evidence)
        report = json.loads(self.analysis.read_text(encoding="utf-8"))
        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertTrue(receipt["ok"])
        self.assertEqual(len(report["debate_history"]), 2)
        self.assertEqual(report["arbiter_log"][0]["notes"], "notes")
        self.assertEqual(report["provisional_ruling"]["composite_score"], 50)
        self.assertEqual(report["arbiter_private_scores"][0]["composite_score"], 50)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["question"], "check")
        self.assertEqual(evidence[1]["model_call_id"], "MC1")
        self.assertEqual(evidence[1]["kind"], "data")
        # model_calls ledger derives from the same converted item as evidence:
        # content-wrapped rows must not yield empty summary/detail
        self.assertEqual(len(report["model_calls"]), 1)
        call = report["model_calls"][0]
        self.assertEqual(call["model_call_id"], "MC1")
        self.assertEqual(call["model"], "P03")
        self.assertEqual(call["summary"], "mixed")
        self.assertIn("steps", call["detail"])
        self.assertEqual(report["ledger_score_z"], 50)
        self.assertEqual(report["ledger_score_r"], 50)

    def test_legacy_buffer_keys_still_assemble(self):
        """旧对象仓行(task_id / playbook / tier_eligible / arbiter_shadow / task)仍可组装——批 3 改仓前的过渡。"""
        rows = [
            {"round": "Init", "arbiter_shadow": {"round": "Init", "action": 50}},
            {"round": "回合1", "arbiter_notes": "notes", "tally": "tally"},
            {"provisional_ruling": {"action": 50, "operation_type": "HOLD_NEUTRAL"}},
            {"round": "回合1", "requested_by": "Reaper", "status": "verified",
             "task_id": "TK1", "playbook": "P03", "issue": "R1", "tier_eligible": "T2",
             "content": {"task": "q", "finding": "mixed", "legs": [1], "payload_text": "raw"}},
        ]
        self.evidence_buffer.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        assemble(self.draft, self.debate, self.evidence_buffer, self.analysis, self.evidence)
        report = json.loads(self.analysis.read_text(encoding="utf-8"))
        call = report["model_calls"][0]
        self.assertEqual(call["model_call_id"], "TK1")
        self.assertEqual(call["model"], "P03")
        self.assertEqual(call["suggested_evidence_tier"], "T2")
        self.assertEqual(report["arbiter_private_scores"][0]["composite_score"], 50)
        self.assertEqual(report["evidence"][0]["question"], "q")

    def test_rejects_missing_audit_records_without_writing_outputs(self):
        self.evidence_buffer.write_text(
            json.dumps({"question": "x", "simple": "y", "detailed": "z", "source": ["s"]}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(AssemblyError):
            assemble(self.draft, self.debate, self.evidence_buffer, self.analysis, self.evidence)
        self.assertFalse(self.analysis.exists())
        self.assertFalse(self.evidence.exists())


if __name__ == "__main__":
    unittest.main()
