#!/usr/bin/env python3
"""账本整理脚本的回归(重建批 2: 读 composite_score / claims / evidence_tier / model_call; 输出 ledger_score)。"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ledger_tally", ROOT / "scripts" / "ledger_tally.py")
LT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LT)


def _e(role, rnd, score, claims, moves=(), requests=()):
    return {"round": rnd, "role": role, "content": {
        "composite_score": score, "claims": claims,
        "stance_ledger": {"moves": list(moves), "held": []},
        "request_evidence": list(requests)}}


class LedgerTallyTest(unittest.TestCase):
    def test_ledger_score_and_scoreboard(self):
        z = [{"id": "Z1", "kind": "own", "evidence_tier": "T1", "stake": 6, "status": "settled_for"}]
        r_open = [{"id": "R1", "kind": "own", "evidence_tier": "T2", "stake": 5, "status": "open"}]
        r_done = [{"id": "R1", "kind": "own", "evidence_tier": "T2", "stake": 5, "status": "settled_against"}]
        entries = [
            _e("Zealot", "Init", 66, z), _e("Reaper", "Init", 40, r_open),
            _e("Zealot", "回合1", 66, z, requests=[{"type": "model_call", "model": "P02", "issue": "Z1"}]),
            _e("Reaper", "回合1", 45, r_done,
               moves=[{"id": "R-R1-1", "issue": "R1", "delta": 5, "evidence_tier": "T2"}]),
        ]
        res = LT.tally(entries)
        self.assertEqual(res["zealot"]["ledger_score"], 66)
        self.assertEqual(res["reaper"]["ledger_score"], 45)
        self.assertEqual(res["reaper"]["claims"]["R1"]["evidence_tier"], "T2")
        self.assertEqual(res["zealot"]["model_calls"][0]["model"], "P02")
        self.assertEqual(res["scoreboard"]["net"], 5)
        self.assertEqual(res["open_total"], [])
        brief = LT.brief(res)
        self.assertIn("ledger_score", brief)
        self.assertNotIn("organized", brief)

    def test_legacy_keys_still_readable(self):
        """旧报告(action / pillars / tier)仍能整理——同尺重评 09-29 批要读它们。"""
        entries = [
            {"round": "Init", "role": "Zealot", "content": {"action": 60, "pillars": [
                {"id": "Z1", "kind": "pillar", "tier": "T2", "stake": 4, "status": "uncontested"}],
                "stance_ledger": {"moves": [], "held": []}}},
            {"round": "Init", "role": "Reaper", "content": {"action": 40, "pillars": [
                {"id": "R1", "kind": "pillar", "tier": "T2", "stake": 4, "status": "uncontested"}],
                "stance_ledger": {"moves": [], "held": []}}},
        ]
        res = LT.tally(entries)
        self.assertEqual(res["zealot"]["ledger_score"], 60)
        self.assertEqual(res["zealot"]["claims"]["Z1"]["evidence_tier"], "T2")

    def _deadlock_case(self, settled_delta):
        r1 = [{"id": "R1", "kind": "own", "evidence_tier": "T2", "stake": 6, "status": "open"}]
        r1_dead = [{"id": "R1", "kind": "own", "evidence_tier": "T2", "stake": 6, "status": "deadlocked"}]
        z = [{"id": "Z1", "kind": "own", "evidence_tier": "T2", "stake": 4, "status": "open"}]
        entries = [
            _e("Zealot", "Init", 60, z), _e("Reaper", "Init", 40, r1),
            _e("Zealot", "回合4", 60, z),
            _e("Reaper", "回合4", 40 + settled_delta, r1,
               moves=[{"id": "R-R1-1", "issue": "R1", "delta": settled_delta, "evidence_tier": "T2"}]),
            _e("Zealot", "回合5", 60, z), _e("Reaper", "回合5", 40 + settled_delta, r1_dead),
        ]
        return [f for f in LT.tally(entries).get("forensics", []) if "deadlock" in f and "R1" in f]

    def test_partial_settlement_may_still_deadlock(self):
        """部分采纳之后剩余争议僵持, 不算矛盾(定价链允许部分采纳)。"""
        self.assertEqual(self._deadlock_case(2), [])

    def test_fully_settled_claim_cannot_deadlock(self):
        self.assertTrue(self._deadlock_case(6))


if __name__ == "__main__":
    unittest.main()
