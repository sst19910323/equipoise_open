#!/usr/bin/env python3
"""终报出厂校验闸的回归(重建批 2: 新字段名 composite_score / claims / evidence_tier / ledger_score /
deviation_support / operation_size / short_term_path / arbiter_private_scores / dissent.*_composite_score)。"""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_debate", ROOT / "scripts" / "validate_debate.py")
VD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VD)


def _round(role, rnd, score, claims, moves=()):
    return {"round": rnd, "role": role, "content": {
        "composite_score": score, "analysis_text": "x", "claims": claims,
        "mentality": [{"name": "buy_the_dip", "weight": 0.7}, {"name": "wait_for_confirmation", "weight": 0.3}],
        "stance_ledger": {"moves": list(moves), "held": [], "contingent": [], "reclaims": []},
    }}


def valid_report() -> dict:
    z_claims = [{"id": "Z1", "kind": "own", "claim": "a", "evidence_tier": "T2", "stake": 6, "status": "settled_for"}]
    r_claims = [{"id": "R1", "kind": "own", "claim": "b", "evidence_tier": "T2", "stake": 5, "status": "settled_against"}]
    hist = [
        _round("Zealot", "Init", 66, z_claims),
        _round("Reaper", "Init", 40, r_claims),
        _round("Zealot", "回合1", 66, z_claims),
        _round("Reaper", "回合1", 45, r_claims,
               moves=[{"id": "R-R1-1", "issue": "R1", "delta": 5, "evidence_tier": "T2",
                       "consideration": "MC1 verified"}]),
    ]
    return {
        "debate_history": hist,
        "arbiter_log": [{"round": "回合1", "tally": "t", "notes": "本轮 R1 证据结案"}],
        "arbiter_private_scores": [{"round": "Init", "composite_score": 55},
                                   {"round": "回合1", "composite_score": 60}],
        "provisional_ruling": {"claims_review": [{"claim": "c", "ref": "Z1", "verdict": "adopted", "reason": "r"}],
                               "composite_score": 60, "operation_type": "LIMIT_ENTRY", "mentality": "buy_the_dip",
                               "rationale": "x"},
        "ledger_score_z": 66, "ledger_score_r": 45,
        "composite_score": 60,
        "operation_type": "LIMIT_ENTRY", "operation_target": "100", "operation_size": "PILOT_SIZE",
        "mentality": [{"name": "buy_the_dip", "weight": 0.7}, {"name": "wait_for_confirmation", "weight": 0.3}],
        "mentality_basis": "回踩 100 未破前低, 论文完好",
        "short_term_path": {"direction": "上行", "target": "110", "invalid": "95", "horizon_days": 5, "rationale": "x"},
        "asset_tier": "quality_large",
        "deviation_support": {"legs": [{"basis": "leg", "refs": ["Z1"], "points": 6}], "total_points": 6},
        "known_flaws": [],
        "dissent": {"zealot_composite_score": 70, "zealot_reservation": "ok",
                    "reaper_composite_score": 45, "reaper_reservation": "ok"},
        "next_review": {"structured_triggers": [{"type": "price_below", "symbol": "X", "value": 95}]},
        "model_calls": [{"model_call_id": "MC1", "suggested_evidence_tier": "T2"}],
        "data_files": {},
    }


class ValidateDebateTest(unittest.TestCase):
    def test_valid_report_passes(self):
        fatal, warn = VD.validate(valid_report())
        self.assertEqual(fatal, [], fatal)

    def test_operation_target_must_be_a_bare_number(self):
        # 2026-10-16 KB(CC 引擎): 把分格、成交条件与灾难线全写进 operation_target
        d = valid_report()
        d["operation_target"] = "159000 / 153600 分格(质量修复框架; 153600 格若成交前已两日收盘低于 154500 则暂停); 灾难线 143000"
        fatal, _ = VD.validate(d)
        self.assertTrue(any("operation_target has only two forms" in f for f in fatal), fatal)
        d["operation_target"] = "159000"
        self.assertEqual([f for f in VD.validate(d)[0] if "operation_target" in f], [])
        d["operation_type"], d["mentality"] = "HOLD_NEUTRAL", [{"name": "hold_and_ride_profit", "weight": 1}]
        d["provisional_ruling"]["operation_type"] = "HOLD_NEUTRAL"
        d["operation_target"] = "N/A"                       # HOLD 也要一个价位作锚点(库主 10-16)
        self.assertTrue(any("operation_target" in f for f in VD.validate(d)[0]))
        d["operation_target"] = "100"
        self.assertEqual([f for f in VD.validate(d)[0] if "operation_target" in f], [])
        d["operation_target"] = "95-105"                    # 震荡: 下沿-上沿, 第二种合法写法
        self.assertEqual([f for f in VD.validate(d)[0] if "operation_target" in f], [])
        d["operation_target"] = "95-105 区间内分批"
        self.assertTrue(any("operation_target" in f for f in VD.validate(d)[0]))
        g = valid_report(); g["symbol"] = "GENERAL"
        g["operation_target"] = "SPY=772.92; QQQ=724.60; DIA=522.61"
        self.assertEqual([f for f in VD.validate(g)[0] if "operation_target" in f], [])
        g["operation_target"] = "SPY=760-780; QQQ=724.60"
        self.assertEqual([f for f in VD.validate(g)[0] if "operation_target" in f], [])
        g["operation_target"] = "SPY 约 772 附近分批; QQQ 看 724"
        self.assertTrue(any("operation_target" in f for f in VD.validate(g)[0]))

    def test_closing_below_round_ref_warns_but_does_not_block(self):
        d = valid_report()
        fatal, warn = VD.validate(d)
        self.assertFalse(any("reference length" in w for w in warn), warn)   # no round_ref recorded: pre-anchor report, untouched
        d["caps"] = {"max_rounds": 20, "round_ref": 5}
        fatal, warn = VD.validate(d)
        self.assertEqual(fatal, [], fatal)
        self.assertTrue(any("closed at round 2, below the reference length 5" in w for w in warn), warn)
        d["caps"]["round_ref"] = 2
        fatal, warn = VD.validate(d)
        self.assertFalse(any("reference length" in w for w in warn), warn)

    def test_soft_condition_warns_but_does_not_block(self):
        d = valid_report()
        d["risk_management"] = {"stop_loss": "365500 收盘失守两日并同步验证公司级持续损伤时 PILOT 减仓"}
        fatal, warn = VD.validate(d)
        self.assertEqual([f for f in fatal if "soft condition" in f], [])
        self.assertTrue(any("[soft condition]" in w and "stop_loss" in w for w in warn), warn)
        d["risk_management"]["stop_loss"] = "收盘跌破 95 且次日未收回 → 减仓"
        self.assertFalse(any("[soft condition]" in w for w in VD.validate(d)[1]))
        # English corpus (2026-10-24): the same kinds of conditions trip the same warning; hard daily-bar conditions do not
        d["risk_management"]["stop_loss"] = "two closes below 365500 and fundamental verification of company-level damage → PILOT trim"
        self.assertTrue(any("[soft condition]" in w and "stop_loss" in w for w in VD.validate(d)[1]))
        d["risk_management"]["stop_loss"] = "close below 95 and SPY also breaks 600 → trim"
        self.assertTrue(any("[soft condition]" in w for w in VD.validate(d)[1]))
        d["risk_management"]["stop_loss"] = "close below 95 and next day fails to reclaim, or volume ratio ≥ 1.5× → trim"
        self.assertFalse(any("[soft condition]" in w for w in VD.validate(d)[1]))

    def test_provisional_mentality_may_be_a_weight_list(self):
        """2026-09-30 AAPL 冒烟案: 仲裁者把第一判的 mentality 写成与终报同形的数组, 校验器不得崩。"""
        doc = valid_report()
        doc["provisional_ruling"]["mentality"] = [{"name": "buy_the_dip", "weight": 0.6},
                                                  {"name": "wait_for_confirmation", "weight": 0.4}]
        fatal, _ = VD.validate(doc)
        self.assertEqual(fatal, [], fatal)

    def test_provisional_mentality_empty_is_fatal_not_a_crash(self):
        for bad in ([], None, "", [{"weight": 1.0}], 7):
            doc = valid_report()
            doc["provisional_ruling"]["mentality"] = bad
            fatal, _ = VD.validate(doc)
            self.assertTrue(any("provisional_ruling.mentality" in f for f in fatal), (bad, fatal))

    def _open_at_close(self, unresolved, max_rounds):
        doc = valid_report()
        for e in doc["debate_history"]:
            if e["role"] == "Zealot":
                e["content"]["claims"][0]["status"] = "open"
        doc["unresolved"] = unresolved
        doc["caps"] = {"max_rounds": max_rounds}
        return VD.validate(doc)

    def test_open_claims_at_round_cap_are_unresolved_not_fatal(self):
        # 轮数硬上限强制收场: 编排层登记的保留未决主张不该让出厂闸必败
        fatal, warn = self._open_at_close(["Z1"], max_rounds=2)
        self.assertFalse([f for f in fatal if "open" in f], fatal)
        self.assertTrue([w for w in warn if "unresolved claims kept" in w and "Z1" in w], warn)

    def test_open_claims_before_round_cap_stay_fatal(self):
        fatal, _ = self._open_at_close(["Z1"], max_rounds=20)
        self.assertTrue([f for f in fatal if "open claims remain at close" in f], fatal)

    def test_open_claims_not_registered_stay_fatal_at_cap(self):
        fatal, _ = self._open_at_close([], max_rounds=2)
        self.assertTrue([f for f in fatal if "open claims remain at close" in f], fatal)

    def test_partial_settlement_is_not_fatal_but_over_stake_is(self):
        doc = valid_report()
        mv = doc["debate_history"][3]["content"]["stance_ledger"]["moves"][0]
        mv["delta"] = 3  # R1 押 5, 部分证实折价到 3
        doc["debate_history"][3]["content"]["composite_score"] = 43
        doc["ledger_score_r"] = 43
        fatal, _ = VD.validate(doc)
        self.assertFalse([f for f in fatal if "押分" in f], fatal)
        mv["delta"] = 8  # 超过押分
        doc["debate_history"][3]["content"]["composite_score"] = 48
        doc["ledger_score_r"] = 48
        fatal, _ = VD.validate(doc)
        self.assertTrue([f for f in fatal if "exceeds the stake" in f], fatal)

    # ---- 定价链(2026-10-01): 不重复计入 / 撤回能退分 / 大证据能推动大变化 / 合理不动不被罚 ----
    def _with_moves(self, moves, stake=5, kind="own"):
        doc = valid_report()
        claim = doc["debate_history"][3]["content"]["claims"][0]
        claim["stake"], claim["kind"] = stake, kind
        doc["debate_history"][1]["content"]["claims"][0].update({"stake": stake, "kind": kind})
        doc["debate_history"][3]["content"]["stance_ledger"]["moves"] = moves
        end = 40 + sum(m["delta"] for m in moves)
        doc["debate_history"][3]["content"]["composite_score"] = end
        doc["ledger_score_r"] = end
        return doc

    @staticmethod
    def _mv(i, delta):
        return {"id": f"R-R1-{i}", "issue": "R1", "delta": delta, "evidence_tier": "T2", "consideration": "c"}

    def test_same_impact_claimed_twice_is_flagged(self):
        # 同一项影响连领两次: 每笔都不超押分, 净计入越过押分 → 点名
        fatal, warn = VD.validate(self._with_moves([self._mv(1, 4), self._mv(2, 4)], stake=5))
        self.assertTrue([w for w in warn if "net credited" in w and "R1" in w], warn)

    def test_withdrawal_refunds_and_is_not_double_counting(self):
        # 计入 +5, 证据被推翻撤回 -5, 再恢复 +5: 当前有效计入仍是 +5, 不算领了三份
        fatal, warn = VD.validate(self._with_moves([self._mv(1, 5), self._mv(2, -5), self._mv(3, 5)], stake=5))
        self.assertEqual(fatal, [], fatal)
        self.assertFalse([w for w in warn if "net credited" in w], warn)
        # 撤回之后停在原处: 整理分回到开局分
        doc = self._with_moves([self._mv(1, 5), self._mv(2, -5)], stake=5)
        fatal, warn = VD.validate(doc)
        self.assertEqual(doc["ledger_score_r"], 40)
        self.assertEqual(fatal, [], fatal)

    def test_big_new_evidence_can_move_a_lot(self):
        # 加强主张自带大押分: 一笔 25 分的移动合法, 终裁落到极端也不再有腿数带 / 腿价帽警告
        doc = self._with_moves([self._mv(1, -25)], stake=25, kind="reinforce")
        doc["composite_score"] = 18
        doc["provisional_ruling"]["composite_score"] = 18
        doc["operation_type"] = doc["provisional_ruling"]["operation_type"] = "TRIM_POSITION"
        doc["mentality"] = [{"name": "profit_taking", "weight": 1.0}]
        doc["provisional_ruling"]["mentality"] = "profit_taking"
        for e in doc["debate_history"]:
            e["content"]["mentality"] = [{"name": "profit_taking", "weight": 1.0}]
        doc["deviation_support"] = {"legs": [{"basis": "leg", "refs": ["R1"], "points": 32}], "total_points": 32}
        doc["debate_history"][3]["content"]["claims"][0]["status"] = "settled_for"
        doc["residual_adjustment"] = {"delta_vs_ledger": "x", "basis": [{"audit": "x", "reason": "R1 Z1"}]}
        fatal, warn = VD.validate(doc)
        self.assertFalse([f for f in fatal if "押分" in f or "deviation" in f], fatal)
        self.assertFalse([w for w in warn if "帽" in w or "参考带" in w or "包络" in w], warn)

    def test_reasonable_hold_near_midpoint_is_not_penalised(self):
        # 账面一边倒、价差很宽而终裁落在中间: 不再因"离中点近"被判致命或点名
        doc = valid_report()
        doc["composite_score"] = 55
        doc["provisional_ruling"]["composite_score"] = 55
        doc["residual_adjustment"] = {"delta_vs_ledger": "x", "basis": []}
        fatal, warn = VD.validate(doc)
        self.assertFalse([f for f in fatal if "兑现" in f or "净差" in f], fatal)
        self.assertFalse([w for w in warn if "中点" in w or "兑换率" in w], warn)

    def test_surviving_claim_without_a_place_in_the_ruling_is_flagged(self):
        doc = valid_report()
        doc["provisional_ruling"]["claims_review"][0]["ref"] = "MC1"
        doc["deviation_support"] = {"legs": [{"basis": "leg", "refs": ["MC1"], "points": 6}], "total_points": 6}
        _, warn = VD.validate(doc)
        self.assertTrue([w for w in warn if "placed nowhere" in w and "Z1" in w], warn)
        _, warn = VD.validate(valid_report())
        self.assertFalse([w for w in warn if "placed nowhere" in w], warn)

    def test_ledger_score_mismatch_is_fatal(self):
        doc = valid_report()
        doc["ledger_score_r"] = 44
        fatal, _ = VD.validate(doc)
        self.assertTrue(any("ledger_score_r" in f for f in fatal), fatal)

    def test_support_gate_uses_weight(self):
        doc = valid_report()
        doc["mentality"] = [{"name": "wait_and_see", "weight": 1.0}]
        fatal, _ = VD.validate(doc)
        self.assertTrue(any("Support gate" in f for f in fatal), fatal)

    def test_legacy_field_names_are_rejected(self):
        doc = valid_report()
        doc["action"] = doc.pop("composite_score")
        fatal, _ = VD.validate(doc)
        self.assertTrue(any("composite_score" in f for f in fatal), fatal)

    def test_band_direction_requires_two_edges(self):
        doc = valid_report()
        doc["short_term_path"] = {"direction": "震荡", "target": "100", "invalid": "", "horizon_days": 5, "rationale": "x"}
        fatal, _ = VD.validate(doc)
        self.assertTrue(any("short_term_path" in f and "range" in f for f in fatal), fatal)

    def test_no_arb_score_line_warning(self):
        _, warn = VD.validate(valid_report())
        self.assertFalse(any("ARB 评分" in w for w in warn), warn)

    def test_heavy_operation_needs_known_flaws(self):
        doc = valid_report()
        doc["operation_size"] = "AGGRESSIVE_SIZE"
        fatal, _ = VD.validate(doc)
        self.assertTrue(any("known_flaws" in f for f in fatal), fatal)


if __name__ == "__main__":
    unittest.main()


class NextReviewEventsTest(unittest.TestCase):
    """2026-11-06: events are the reasons, recommend_after the call time; price lines retired (warnings), shape errors fatal.
    Only ruler reports are checked; the fixture is pre-ruler, so a minimal ruler marker is added."""

    def ruler_doc(self):
        d = valid_report()
        d["scenario"] = {"schema": "3.4", "P0": 100.0, "T_days": 15, "nodes": [{"id": "M1", "p": 1.0, "P_T": 105.0, "events": []}]}
        d["schema_version"] = "3.4"
        return d

    def nr_checks(self, nr):
        d = self.ruler_doc(); d["next_review"] = nr
        return VD._next_review_checks(d, True)

    def test_retired_lines_warn_and_events_missing_warns(self):
        f, w = self.nr_checks({"recommend_after": "2026-11-10", "structured_triggers": [{"type": "price_below", "value": 95}], "frequency_hint": "daily"})
        self.assertEqual(f, [])
        self.assertTrue(any("structured_triggers is retired" in x for x in w) and any("frequency_hint is retired" in x for x in w), w)
        self.assertTrue(any("events missing" in x for x in w), w)

    def test_shape_errors_are_fatal(self):
        f, _ = self.nr_checks({"events": [{"id": "E1", "date": "17 Nov", "tests": ["Z1"], "coverage": "full", "check": "x"},
                                          {"id": "E2", "name": "n", "date": "2026-11-17", "date_precision": "window", "tests": ["M1"], "coverage": "half", "check": "x"}]})
        self.assertTrue(any("events[0] missing name" in x for x in f), f)
        self.assertTrue(any("events[0] date must be" in x for x in f), f)
        self.assertTrue(any("events[1] date_precision window needs window_end" in x for x in f), f)
        self.assertTrue(any("events[1] coverage" in x for x in f), f)

    def test_call_time_must_name_an_event_or_the_horizon(self):
        ev = [{"id": "E1", "name": "Q3 report", "date": "2026-11-17", "tests": ["Z1", "M1"], "coverage": "full", "check": "guidance"}]
        f, w = self.nr_checks({"recommend_after": "2026-11-17T12:00:00Z", "expires_at": "2026-11-25", "events": ev})
        self.assertEqual(f, []); self.assertEqual([x for x in w if "recommend_after" in x], [])
        _, w = self.nr_checks({"recommend_after": "2026-11-10", "expires_at": "2026-11-25", "events": ev})
        self.assertTrue(any("names no event" in x for x in w), w)
        _, w = self.nr_checks({"recommend_after": "2026-11-25", "expires_at": "2026-11-25", "events": ev})
        self.assertEqual([x for x in w if "recommend_after" in x], [])                               # the horizon end is a reason
        _, w = self.nr_checks({"recommend_after": "2026-12-01", "expires_at": "2026-11-25", "events": ev})
        self.assertTrue(any("after expires_at" in x for x in w), w)
        _, w = self.nr_checks({"recommend_after": "2026-11-12", "expires_at": "2026-11-25",
                               "events": [{**ev[0], "date_precision": "window", "date": "2026-11-12", "window_end": "2026-11-19"}]})
        self.assertEqual([x for x in w if "recommend_after" in x], [])                               # window start is fine

    def test_structural_fatals_from_review(self):
        ev = [{"id": "E1", "name": "Q3 report", "date": "2026-11-17", "tests": "M1", "coverage": "full", "check": "x"}]
        f, _ = self.nr_checks({"expires_at": "2026-11-25", "events": ev})
        self.assertTrue(any("recommend_after missing" in x for x in f), f)
        self.assertTrue(any("tests must be a list" in x for x in f), f)
        f, _ = self.nr_checks({"recommend_after": "2026-11-10", "events": [{**ev[0], "tests": ["M1"], "date_precision": "window", "date": "2026-11-10", "window_end": "2026-11-09"}]})
        self.assertTrue(any("window_end 2026-11-09 is before date" in x for x in f), f)
        f, w = self.nr_checks({"recommend_after": "2026-11-16", "expires_at": "2026-11-25",
                               "events": [{**ev[0], "tests": ["M1"], "date_precision": "window", "date": "2026-11-10", "window_end": "2026-11-17"}]})
        self.assertEqual(f, []); self.assertTrue(any("names no event" in x for x in w), w)          # late inside the window is not "at the start"
        _, w = self.nr_checks({"recommend_after": "2026-11-10", "expires_at": "2026-11-25", "events": []})
        self.assertTrue(any("no events written" in x for x in w), w)                              # a call with no reason

    def test_known_event_identity(self):
        ev = [{"id": "E1", "name": "US CPI", "who": "macro", "date": "2026-11-17", "tests": ["M1"], "coverage": "full", "check": "x"}]
        _, w = self.nr_checks({"recommend_after": "2026-11-17", "expires_at": "2026-11-25", "events": ev,
                               "known_events": [{"kind": "earnings_expected", "date": "2026-11-17", "window": ["2026-11-10", "2026-11-24"]}]})
        self.assertTrue(any("earnings_expected" in x for x in w), w)                                 # a macro event does not cover the issuer's report

    def test_unknown_test_ids_warn_and_known_events_uncovered_warn(self):
        ev = [{"id": "E1", "name": "Q3 report", "date": "2026-11-17", "tests": ["Z9", "M1"], "coverage": "partial", "check": "x"}]
        _, w = self.nr_checks({"recommend_after": "2026-11-17", "expires_at": "2026-11-25", "events": ev,
                               "known_events": [{"kind": "earnings_expected", "date": "2026-11-18", "window": ["2026-11-11", "2026-11-25"]},
                                                {"kind": "macro", "name": "cpi_yoy", "date": "2026-11-13"}]})
        self.assertTrue(any("tests ['Z9']" in x for x in w), w)
        self.assertEqual([x for x in w if "known event" in x and "earnings_expected" in x], [])     # covered by E1 (3 days)
        self.assertTrue(any("known event not in next_review.events: macro cpi_yoy 2026-11-13" in x for x in w), w)

    def test_pre_ruler_reports_untouched(self):
        d = valid_report()
        f, w = VD._next_review_checks(d, False)
        self.assertEqual((f, w), ([], []))
