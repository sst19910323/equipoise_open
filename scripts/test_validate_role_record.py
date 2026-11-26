#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("validate_role_record.py")
SPEC = importlib.util.spec_from_file_location("validate_role_record", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidateRoleRecordTest(unittest.TestCase):
    def test_catches_n225_failure_shape(self):
        record = {
            "round": "回合2", "role": "Zealot", "content": {
                "composite_score": 52, "analysis_text": "x",
                "claims": [{"id": "Z1", "kind": "own", "evidence_tier": "T1",
                             "stake": 4, "status": "settled"}],
                "stance_ledger": {"moves": [{"id": "Z-R2-1", "issue": "Z1", "delta": 6}]},
            },
        }
        errors = MODULE.validate(record)
        self.assertTrue(any("invalid status" in error for error in errors))
        self.assertTrue(any("stake=4" in error for error in errors))  # 押分是上限: 超过押分的结算拒收

    def test_partial_settlement_within_stake_is_accepted(self):
        # 库主 2026-10-01: 部分证实时折价是对的, 预检不该要求结算幅度恰好等于押分
        record = {
            "round": "回合2", "role": "Zealot", "content": {
                "operation_target": "100", "composite_score": 58, "analysis_text": "x",
                "claims": [{"id": "Z1", "kind": "own", "evidence_tier": "T2",
                             "stake": 4, "status": "settled_against"}],
                "stance_ledger": {"moves": [{"id": "Z-R2-1", "issue": "Z1", "delta": -2}]},
            },
        }
        self.assertEqual(MODULE.validate(record), [])

    def test_zero_delta_move_is_rejected(self):
        record = {
            "round": "回合2", "role": "Zealot", "content": {
                "composite_score": 58, "analysis_text": "x",
                "claims": [{"id": "Z1", "kind": "own", "evidence_tier": "T2",
                             "stake": 4, "status": "settled_against"}],
                "stance_ledger": {"moves": [{"id": "Z-R2-1", "issue": "Z1", "delta": 0}]},
            },
        }
        self.assertTrue(any("delta" in e for e in MODULE.validate(record)))

    def test_move_must_hang_on_one_known_claim(self):
        # 冻结材料检查(2026-10-01)抓到: issue 写成 "Z1/R2" 能绕过押分上限检查
        record = {
            "round": "回合2", "role": "Reaper", "content": {
                "composite_score": 49, "analysis_text": "x",
                "claims": [{"id": "R2", "kind": "own", "evidence_tier": "T2",
                             "stake": 5, "status": "open"}],
                "stance_ledger": {"moves": [{"id": "R-R5-1", "issue": "Z1/R2", "delta": -2}]},
            },
        }
        self.assertTrue(any("不是在场的主张编号" in e for e in MODULE.validate(record)))

    def test_accepts_valid_record(self):
        record = {
            "round": "回合1", "role": "Reaper", "content": {
                "composite_score": 46, "analysis_text": "x", "operation_target": "100",
                "claims": [{"id": "R1", "kind": "own", "evidence_tier": "T2",
                             "stake": 4, "status": "settled_against"}],
                "stance_ledger": {"moves": [{"id": "R-M1", "issue": "R1", "delta": 4}]},
            },
        }
        self.assertEqual(MODULE.validate(record), [])

    def test_operation_target_must_converge_to_one_number_each_round(self):
        # 库主 2026-10-17: 分格与条件可以在正文讨论, 每轮最上面那个核心字段必须收敛成一个数
        base = {"round": "回合1", "role": "Zealot", "content": {
            "composite_score": 60, "analysis_text": "x",
            "claims": [{"id": "Z1", "kind": "own", "evidence_tier": "T2", "stake": 4, "status": "open"}],
            "stance_ledger": {"moves": []}}}
        for bad in ("159000 / 153600 分格(质量修复框架)", "N/A", "", None, "约 100 附近"):
            rec = {**base, "content": {**base["content"], "operation_target": bad}}
            self.assertTrue(any("operation_target" in e for e in MODULE.validate(rec)), bad)
        for good in ("100", "159000", "772.92", "95-105", "SPY=772.92; QQQ=724.60; DIA=522.61", "N225=64750.00", "SPY=760-780"):
            rec = {**base, "content": {**base["content"], "operation_target": good}}
            self.assertEqual([e for e in MODULE.validate(rec) if "operation_target" in e], [], good)


# ------------------------------------------------------------------------------------------------ 3.4 ingestion (C2 / C3 / C8)

import copy  # noqa: E402
import sys   # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v34_flow import (Z14, R14, MOVE_Z1, ruler_claim, ruler_final, Z_C1, R_C1, R_C2)  # noqa: E402
import scenario_replay as sr  # noqa: E402


def _rec(seq, tag, rnd, content):
    return {"seq": seq, "id": f"R{rnd}.{tag}", "role": tag, "round": rnd, "content": content}


def _history():
    """Round 1: Z1 (own) by Z; R1 (challenge on Z1, if_true = p_move C→A .10) and R2 (own) by R."""
    z1, r1 = ruler_final(60, [Z_C1], Z14), ruler_final(40, [R_C1, R_C2], R14)
    return [_rec(1, "Z", 1, z1), _rec(2, "R", 1, r1)]


class Ingest35Test(unittest.TestCase):
    def ingest(self, content, role="Reaper", rnd=2, history=None):
        return MODULE.ingest(content, role, rnd, _history() if history is None else history, ref_price=100.0)

    def test_opening_derives_score_and_keeps_authored(self):
        errs, out = MODULE.ingest(ruler_final(99, [Z_C1], Z14), "Zealot", 1, [], ref_price=100.0)
        self.assertEqual(errs, [])
        self.assertEqual((out["composite_score"], out["authored_score"]), (79, 99))
        c = ruler_final(0, [Z_C1], Z14)
        del c["composite_score"]
        errs, out = MODULE.ingest(c, "Zealot", 1, [], ref_price=100.0)
        self.assertEqual((errs, out["composite_score"]), ([], 79))
        self.assertNotIn("authored_score", out)

    def test_opening_updates_rejected(self):
        c = ruler_final(50, [Z_C1], sr.apply_ops(Z14, [{"op": "p_move", "from": "C", "to": "B", "amount": 0.05}]),
                    [{"claim": "Z1", "applies": None, "ops": [{"op": "p_move", "from": "C", "to": "B", "amount": 0.05}], "why": "w"}])
        errs, _ = MODULE.ingest(c, "Zealot", 1, [], ref_price=100.0)
        self.assertTrue(any("opening round" in e for e in errs), errs)

    def test_update_must_reproduce_the_scenario(self):
        c = ruler_final(40, [R_C1, R_C2], sr.apply_ops(R14, MOVE_Z1), [])     # changed without an update
        errs, _ = self.ingest(c)
        self.assertTrue(any("first divergence" in e for e in errs), errs)
        good = ruler_final(40, [R_C1, R_C2], sr.apply_ops(R14, MOVE_Z1),
                       [{"claim": "Z1", "applies": "Z1#true", "ops": MOVE_Z1, "why": "Z1 verified"}])
        self.assertEqual(self.ingest(good)[0], [])

    def test_forged_unknown_and_double_applies(self):
        forged = [{"op": "p_move", "from": "C", "to": "A", "amount": 0.15}]
        c = ruler_final(40, [R_C1, R_C2], sr.apply_ops(R14, forged), [{"claim": "Z1", "applies": "Z1#true", "ops": forged, "why": "w"}])
        self.assertTrue(any("applies does not match the registered effect" in e for e in self.ingest(c)[0]))
        c = ruler_final(40, [R_C1, R_C2], R14, [{"claim": "Z9", "applies": "Z9#true", "ops": [], "why": "w"}])
        self.assertTrue(any("unknown claim id Z9" in e for e in self.ingest(c)[0]))
        hist = _history()
        r2 = ruler_final(40, [R_C1, R_C2], sr.apply_ops(R14, MOVE_Z1), [{"claim": "Z1", "applies": "Z1#true", "ops": MOVE_Z1, "why": "w"}])
        hist.append(_rec(3, "R", 2, r2))
        r3 = ruler_final(40, [R_C1, R_C2], sr.apply_ops(sr.apply_ops(R14, MOVE_Z1), MOVE_Z1),
                     [{"claim": "Z1", "applies": "Z1#true", "ops": MOVE_Z1, "why": "again"}])
        errs, _ = self.ingest(r3, rnd=3, history=hist)
        self.assertTrue(any("already claimed" in e for e in errs), errs)

    def test_cost_bps_and_baseline_rejected(self):
        c = ruler_final(60, [Z_C1], dict(Z14, execution={"cost_bps": 10, "overweight": {}}))
        self.assertTrue(any("cost_bps" in e for e in MODULE.ingest(c, "Zealot", 1, [], ref_price=100.0)[0]))
        c = ruler_final(60, [Z_C1], dict(Z14, baseline="flat"))
        self.assertTrue(any("baseline" in e for e in MODULE.ingest(c, "Zealot", 1, [], ref_price=100.0)[0]))

    def test_effect_shape_and_no_3_4_fields_required(self):
        c = ruler_final(60, [dict(Z_C1, effect={"if_true": [{"op": "p_set"}]})], Z14)
        errs, _ = MODULE.ingest(c, "Zealot", 1, [], ref_price=100.0)
        self.assertTrue(any("effect" in e for e in errs), errs)
        self.assertFalse(any("effect_if_true" in e or "stake" in e for e in MODULE.ingest(ruler_final(60, [Z_C1], Z14), "Zealot", 1, [], ref_price=100.0)[0]))

    def test_freezing_and_revision(self):
        c = ruler_final(40, [dict(R_C1, effect={"if_true": [], "if_false": []}), R_C2], R14)
        self.assertTrue(any("effect frozen" in e for e in self.ingest(c)[0]))
        c = ruler_final(40, [dict(R_C1, claim="new text"), R_C2], R14)
        self.assertTrue(any("without revises" in e for e in self.ingest(c)[0]))
        c = ruler_final(40, [dict(R_C1, claim="new text", revises="R1.R", effect={"if_true": [], "if_false": []}), R_C2], R14)
        self.assertEqual(self.ingest(c)[0], [])

    def test_duplicate_challenge_target(self):
        c = ruler_final(40, [R_C1, R_C2, ruler_claim("R3", "another angle", kind="challenge", target="Z1")], R14)
        self.assertTrue(any("duplicate challenge target Z1" in e for e in self.ingest(c)[0]))

    def test_cli_validate_routes_3_5_records(self):
        rec = {"round": "Init", "role": "Zealot", "content": ruler_final(99, [Z_C1], Z14)}
        self.assertEqual(MODULE.validate(rec), [])
        bad = copy.deepcopy(rec)
        bad["content"]["scenario"]["execution"]["cost_bps"] = 5
        self.assertTrue(any("cost_bps" in e for e in MODULE.validate(bad)))


if __name__ == "__main__":
    unittest.main()
