"""入场枚举合并(2026-10-17 晚, 库主定): 辩手与仲裁者只写 ENTRY, 市价 / 限价由组装器按价位与本场参考价派生;
复盘 digest 带替代策略的机械结果(alternatives), 评分员并排清算。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


asm = _load("assemble_debate_artifacts")
vd = _load("validate_debate")
import workflows.postcheck_quant as pq  # noqa: E402


class DeriveExecutionTest(unittest.TestCase):
    def _rep(self, target, refs, symbol="NVDA"):
        return {"symbol": symbol, "operation_type": "ENTRY", "operation_target": target,
                "reference_prices": [{"symbol": s, "price": p} for s, p in refs]}

    def test_at_reference_price_is_market(self):
        r = self._rep("745.6", [("NVDA", 745.63)])                 # 只差显示舍入 → 现在做
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "MARKET_ENTRY")
        self.assertEqual(r["operation_execution"]["declared"], "ENTRY")

    def test_slightly_below_reference_is_still_waiting(self):
        r = self._rep("745.0", [("NVDA", 745.6)])                  # 0.08% 也是"等那个价", 不是噪音带
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "LIMIT_ENTRY")

    def test_below_reference_is_limit(self):
        r = self._rep("730", [("NVDA", 745.6)])
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "LIMIT_ENTRY")
        self.assertIn("2.09%", r["operation_execution"]["basis"])

    def test_range_is_limit(self):
        r = self._rep("730-740", [("NVDA", 745.6)])
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "LIMIT_ENTRY")

    def test_general_all_lenses_at_market(self):
        r = self._rep("SPY=745.2; QQQ=660.5", [("SPY", 745.21), ("QQQ", 660.5)], symbol="GENERAL")
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "MARKET_ENTRY")
        r = self._rep("SPY=745; QQQ=640", [("SPY", 745.2), ("QQQ", 660.0)], symbol="GENERAL")
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "LIMIT_ENTRY")

    def test_general_missing_lens_reference(self):
        r = self._rep("SPY=745.2; QQQ=660", [("SPY", 745.2)], symbol="GENERAL")   # QQQ 没参考价 → 判不了, 不能当市价
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "ENTRY"); self.assertIsNone(r["operation_execution"]["derived"])
        r = self._rep("SPY=700; QQQ=660", [("SPY", 745.2)], symbol="GENERAL")     # 已有一个镜头在等价位 → LIMIT
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "LIMIT_ENTRY")

    def test_no_reference_keeps_entry(self):
        r = self._rep("730", [])
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "ENTRY")
        self.assertIsNone(r["operation_execution"]["derived"])

    def test_other_ops_untouched(self):
        r = {"symbol": "NVDA", "operation_type": "TRIM_POSITION", "operation_target": "800", "reference_prices": [{"symbol": "NVDA", "price": 745}]}
        asm._derive_execution(r)
        self.assertEqual(r["operation_type"], "TRIM_POSITION"); self.assertNotIn("operation_execution", r)


class ValidatorEntryTest(unittest.TestCase):
    def test_entry_and_legacy_names_admissible_for_buy_mentality(self):
        self.assertIn("ENTRY", vd.OP_ENUM)
        self.assertEqual(vd.ADMISSIBLE["buy_the_dip"], {"ENTRY"})
        self.assertEqual(vd.ADMISSIBLE["fomo_buy"], {"ENTRY"})
        for op in ("ENTRY", "MARKET_ENTRY", "LIMIT_ENTRY"):
            self.assertIn(vd._norm_op(op), vd.ADMISSIBLE["buy_the_dip"])
        self.assertNotIn(vd._norm_op("TRIM_POSITION"), vd.ADMISSIBLE["buy_the_dip"])


class AlternativesTest(unittest.TestCase):
    bars = [{"date": "2026-10-01", "high": 103, "low": 99, "close": 102},
            {"date": "2026-10-02", "high": 106, "low": 101, "close": 105},
            {"date": "2026-10-03", "high": 105, "low": 94, "close": 96}]

    def test_immediate_entry_fills_at_T_even_if_price_runs_away(self):
        alt = pq.immediate_action_stats(self.bars, 100.0, stop=95.0)
        self.assertEqual(alt["immediate_entry"]["fill_price"], 100.0)
        self.assertEqual(alt["immediate_entry"]["window_end_ret_pct"], -4.0)
        self.assertEqual(alt["immediate_entry"]["exit_reason"], "stop")
        self.assertEqual(alt["immediate_entry"]["simulated_exit_ret_pct"], -5.0)
        self.assertEqual(alt["immediate_entry"]["realized_ret_pct"], -5.0)
        self.assertEqual(alt["immediate_trim"]["vs_keep_pct"], 4.0)
        self.assertEqual(alt["no_action"]["market_ret_pct"], -4.0)
        self.assertEqual(alt["no_action"]["opportunity_cost_pct"], -5.0)          # 不动避开了按现价进场的 -5%
        # 止损在入场价上方 = 卖侧条件, 不借用: 买入仍是多头, 持到窗末并标明
        alt = pq.immediate_action_stats(self.bars, 100.0, stop=105.0)
        self.assertTrue(alt["immediate_entry"]["stop_not_applicable"])
        self.assertEqual(alt["immediate_entry"]["realized_ret_pct"], -4.0)
        # 止损后反弹: 进场被止损 -5%, 行情却收 +10% → 行情与机会成本分开记
        bars = self.bars + [{"date": "2026-10-06", "high": 112, "low": 97, "close": 110}]
        alt = pq.immediate_action_stats(bars, 100.0, stop=95.0)
        self.assertEqual(alt["no_action"]["market_ret_pct"], 10.0)
        self.assertEqual(alt["no_action"]["opportunity_cost_pct"], -5.0)
        # 限价 98 在这三根里从未触到(最低 99 → 94 第三根才到), 与"按现价进场"并排才看得见差别
        fs = pq.entry_fill_stats(self.bars, 100.0, 98.0, valid_days=1)
        self.assertFalse(fs["entry_price_reached"])

    def test_simulate_exit_close_rule(self):
        out = pq._simulate_exit(self.bars, 0, 100.0, 97.0, True, ("close", 1))
        self.assertEqual(out["exit_reason"], "stop_close"); self.assertEqual(out["exit_date"], "2026-10-03")
        self.assertEqual(out["simulated_exit_ret_pct"], -4.0)

    def test_entry_op_is_buy_side(self):
        self.assertEqual(pq._direction({"action": 60, "operation_type": "ENTRY"}), 1)
        self.assertTrue(pq._dir_op_conflict("下行", "ENTRY"))


if __name__ == "__main__":
    unittest.main()
