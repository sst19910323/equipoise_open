#!/usr/bin/env python3
"""hydrate_pg_to_files 的区域路由回归(重建批 1, topics/INDEX Q6: 韩国无父路由)。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hydrate_pg_to_files as hy  # noqa: E402


class ParentRoutingTest(unittest.TestCase):
    def test_kr_stock_routes_to_general_kr(self):
        self.assertEqual(hy.parent_symbol("SSNLF"), "GENERAL_KR")
        self.assertEqual(hy.parent_symbol("HXSCL"), "GENERAL_KR")

    def test_existing_routes_unchanged(self):
        self.assertEqual(hy.parent_symbol("RHM"), "GENERAL_EU")
        self.assertEqual(hy.parent_symbol("NTDOY"), "GENERAL_JP")
        self.assertEqual(hy.parent_symbol("SPY"), "GENERAL")
        self.assertIsNone(hy.parent_symbol("GENERAL_KR"))

    def test_sentiment_regions_include_kr(self):
        self.assertEqual(hy.sentiment_regions(["SSNLF"]), {"US", "KR"})
        self.assertEqual(hy.sentiment_regions(["GENERAL_KR"]), {"US", "KR"})
        self.assertEqual(hy.sentiment_regions(["RHM", "NTDOY"]), {"US", "EU", "JP"})
        self.assertEqual(hy.sentiment_regions(["AAPL"]), {"US"})


if __name__ == "__main__":
    unittest.main()
