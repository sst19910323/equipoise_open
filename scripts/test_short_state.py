#!/usr/bin/env python3
"""Short-term state anchor (v3.7): the line reads the fixed-threshold base-rate table and prints the gap to all days."""
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_short_base as bsb  # noqa: E402
import opening_anchors as oa  # noqa: E402


def _rows(tail):
    random.seed(11)
    c = [100.0]
    for _ in range(320):
        c.append(c[-1] * (1 + random.gauss(0, 0.01)))
    for r in tail:
        c.append(c[-1] * (1 + r))
    return [(f"d{i:04d}", x, x, x) for i, x in enumerate(c)]


class ShortStateTest(unittest.TestCase):
    def test_bucket_edges(self):
        self.assertEqual(bsb.bucket(-2.0, bsb.BUCKETS_5D)[0], "5d_down_2")
        self.assertEqual(bsb.bucket(-1.0, bsb.BUCKETS_5D)[0], "5d_down_1")
        self.assertEqual(bsb.bucket(0.0, bsb.BUCKETS_5D)[0], "5d_flat")
        self.assertEqual(bsb.bucket(1.0, bsb.BUCKETS_5D)[0], "5d_up_1")
        self.assertEqual(bsb.bucket(2.0, bsb.BUCKETS_5D)[0], "5d_up_2")
        self.assertIsNone(bsb.bucket(1.9, bsb.BUCKETS_1D))
        self.assertEqual(bsb.bucket(-2.0, bsb.BUCKETS_1D)[0], "1d_down_2")

    def test_sharp_fall_prints_both_states_and_the_gap(self):
        line = oa._short_state(_rows([-0.012, -0.011, -0.012, -0.010, -0.035]), ROOT)
        self.assertIn("After a 5-day fall of 2σ or more", line)
        self.assertIn("after a 1-day fall of 2σ or more", line)
        self.assertIn("against all days", line)
        self.assertRegex(line, r"a gap of [+]\d+ points")
        self.assertIn("supports neither continuation nor reversal by itself", line)

    def test_ordinary_state_reads_as_no_lean(self):
        line = oa._short_state(_rows([0.001, -0.001, 0.001, 0.0, 0.001]), ROOT)
        self.assertIn("After a 5-day move inside ±1σ", line)
        self.assertNotIn("1-day fall", line)
        gap = int(line.split("a gap of ")[1].split(" points")[0])
        self.assertLessEqual(abs(gap), 1)

    def test_moves_are_in_daily_sigma(self):
        rows = _rows([-0.03] * 5)
        p5, p1, sd = bsb.moves([r[3] for r in rows], len(rows) - 1)
        self.assertAlmostEqual(p1, -0.03 / sd, places=6)
        self.assertAlmostEqual(p5, (0.97 ** 5 - 1) / (sd * math.sqrt(5)), places=6)


if __name__ == "__main__":
    unittest.main()
