"""后验分数分段 / 分组 / 配对统计(equipoise_atlas 复盘页接口, 2026-10-23)——纯 Python 部分, 不碰 PG。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datalayer import cohorts as C  # noqa: E402
from datalayer.pg import postcheck_agg as A  # noqa: E402

RM = {"AAPL": "US", "NVDA": "US", "MUFG": "JP", "DAX": "EU"}


def raw(sym, as_of, dt, score, ruler="R24", reck="gpt-6-astra", op="LIMIT_ENTRY", op_exec=None, lesion=None, quant=None):
    return {"symbol": sym, "as_of": as_of, "dt": dt, "ruler": ruler, "score": score, "reckoner": reck,
            "operation_type": op, "op_exec": op_exec, "lesion": lesion, "quant": quant, "composite_score": 55, "checked_at": None}


class CohortTest(unittest.TestCase):
    def test_boundaries_are_minute_precise_and_half_open(self):
        self.assertEqual(C.cohort_of("2026-10-08T13:00:59Z"), "C8")
        self.assertEqual(C.cohort_of("2026-10-08T13:01:00Z"), "C9")
        self.assertEqual(C.cohort_of("2026-10-17T13:38:00Z"), "C10")        # 最新段开区间
        self.assertIsNone(C.cohort_of("2026-10-01T00:00:00Z"))               # 冒烟场不入段
        self.assertEqual(C.era_of("C9"), "v3.1")


class EnrichTest(unittest.TestCase):
    def test_execution_mapping_and_fields(self):
        r = A.enrich(raw("AAPL", "2026-10-20T14:00:00Z", 3, 72, op="ENTRY", op_exec="MARKET_ENTRY", lesion="L2 判断层 解读错",
                         quant={"ret_pct": 1.5, "mae_pct": -0.8, "mfe_pct": 2.2}), RM)
        self.assertEqual(r["operation_execution"], "market"); self.assertEqual(r["cohort"], "C10"); self.assertEqual(r["region"], "US")
        self.assertEqual(r["lesion_key"], "L2"); self.assertEqual(r["week"], "2026-10-19"); self.assertEqual(r["score_bin"], 70)
        self.assertEqual((r["ret"], r["mae"], r["mfe"]), (1.5, -0.8, 2.2))
        self.assertEqual(A.enrich(raw("MUFG", "2026-09-14T05:00:00Z", 20, 28, op="LIMIT_ENTRY"), RM)["operation_execution"], "limit")
        self.assertEqual(A.enrich(raw("MUFG", "2026-09-14T05:00:00Z", 20, 28, op="ENTRY"), RM)["operation_execution"], "entry_unresolved")
        self.assertEqual(A.enrich(raw("MUFG", "2026-09-14T05:00:00Z", 20, 28, op="HOLD_NEUTRAL"), RM)["operation_execution"], "n/a")
        g = A.enrich(raw("GENERAL", "2026-10-20T14:00:00Z", 3, 60, quant={"members": [{"ret_pct": 0.4, "mae_pct": -1, "mfe_pct": 1}]}), RM)
        self.assertEqual(g["ret"], 0.4); self.assertEqual(g["score_bin"], 60)
        self.assertEqual(A.enrich(raw("AAPL", "2026-10-20T14:00:00Z", 3, 100), RM)["score_bin"], 90)   # 100 落最后一桶

    def test_dedupe_keeps_latest_ruler(self):
        rows = A.dedupe_latest_ruler([raw("AAPL", "2026-10-10T14:00:00Z", 20, 40, ruler="R23"),
                                      raw("AAPL", "2026-10-10T14:00:00Z", 20, 57, ruler="R24"),
                                      raw("AAPL", "2026-10-10T14:00:00Z", 3, 70, ruler="R23")])
        self.assertEqual(sorted((r["dt"], r["ruler"], r["score"]) for r in rows), [(3, "R23", 70), (20, "R24", 57)])


class AggregateTest(unittest.TestCase):
    def setUp(self):
        base = [("AAPL", "2026-10-09T14:00:00Z", 3, 85, "LIMIT_ENTRY", None), ("NVDA", "2026-10-09T14:00:00Z", 3, 45, "ENTRY", "MARKET_ENTRY"),
                ("MUFG", "2026-10-13T05:00:00Z", 3, 60, "HOLD_NEUTRAL", None), ("DAX", "2026-10-13T08:00:00Z", 3, 70, "TRIM_POSITION", None),
                ("AAPL", "2026-10-09T14:00:00Z", 20, 55, "LIMIT_ENTRY", None), ("NVDA", "2026-10-09T14:00:00Z", 20, 65, "ENTRY", "MARKET_ENTRY"),
                ("AAPL", "2026-10-20T14:00:00Z", 3, 90, "ENTRY", "LIMIT_ENTRY")]
        self.rows = [A.enrich(raw(s, a, d, sc, op=op, op_exec=ex), RM) for s, a, d, sc, op, ex in base]

    def test_overall_and_groups(self):
        out = A.aggregate(self.rows, dt=3)
        self.assertEqual(out["n"], 5); self.assertEqual(out["mean"], 70.0); self.assertEqual(out["ge80"], 2); self.assertEqual(out["lt50"], 1)
        us = A.aggregate(self.rows, dt=3, region="US", group_by="execution")
        self.assertEqual(us["n"], 3)
        self.assertEqual({g["key"]: g["n"] for g in us["groups"]}, {"limit": 2, "market": 1})
        c9 = A.aggregate(self.rows, dt=3, cohort="C9", group_by="region")
        self.assertEqual({g["key"]: g["n"] for g in c9["groups"]}, {"US": 2, "JP": 1, "EU": 1})
        bins = A.aggregate(self.rows, dt=3, group_by="score_bin")["groups"]
        self.assertEqual(len(bins), 10); self.assertEqual(bins[8]["n"], 1); self.assertEqual(bins[9]["n"], 1); self.assertEqual(bins[9]["label"], "[90,100]")
        wk = A.aggregate(self.rows, dt=3, group_by="week")["groups"]
        self.assertEqual([g["key"] for g in wk], ["2026-10-05", "2026-10-12", "2026-10-19"])
        with self.assertRaises(ValueError):
            A.aggregate(self.rows, dt=3, group_by="moon_phase")

    def test_cohorts_payload_and_paired(self):
        cp = A.cohorts_payload(self.rows)
        c9 = next(c for c in cp["cohorts"] if c["id"] == "C9")
        self.assertEqual(c9["d3"]["n"], 4); self.assertEqual(c9["d20"]["n"], 2); self.assertEqual(c9["rulers"]["d3"], {"R24": 4})
        self.assertEqual(cp["ruler_latest"], C.RULER_LATEST)
        p = A.paired(self.rows, min_n=1)
        self.assertEqual(len(p["groups"]), 1)
        g = p["groups"][0]
        self.assertEqual((g["cohort"], g["n"], g["d3_mean"], g["d20_mean"], g["diff"]), ("C9", 2, 65.0, 60.0, -5.0))
        self.assertFalse(g["mixed_reckoners"])
        self.assertEqual(A.paired(self.rows, reckoner20="opus")["groups"], [])     # d+20 评分员筛选只约束 d+20 行

    def test_stats_breakdown_and_summary_list(self):
        sb = A.stats_breakdown(self.rows)
        self.assertEqual(sb["by_dt"]["3"]["n"], 5); self.assertEqual(sb["by_dt"]["20"]["n"], 2); self.assertIn("R24", sb["by_ruler"])
        self.assertEqual(sb["by_reckoner_dt"][0]["reckoner"], "gpt-6-astra")
        ls = A.list_summary(self.rows, limit=3)
        self.assertEqual(len(ls), 3); self.assertEqual(ls[0]["as_of"], "2026-10-20T14:00:00Z")
        self.assertEqual(set(ls[0]), set(A._SUMMARY_FIELDS))


if __name__ == "__main__":
    unittest.main()
