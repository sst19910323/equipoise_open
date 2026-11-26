import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from scripts.breadth_audit import SECTORS, disjoint, features, read_panel, replay


class BreadthAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(19)
        dates = pd.bdate_range("2016-01-04", periods=1200)
        shared = rng.normal(0, .013, len(dates))
        cls.prices = pd.DataFrame(index=dates)
        for name in ("QQQ", *SECTORS):
            cls.prices[name] = 100 * np.exp((shared + rng.normal(0, .004, len(dates))).cumsum())
        cls.prices["qqq_open"] = cls.prices.QQQ.shift().fillna(100) * 1.002

    def test_breadth_is_symmetric_and_flat_sectors_do_not_vote(self):
        data = self.prices.copy()
        data[list(SECTORS)[:2]] = 100.
        self.assertTrue((features(data).breadth == 0).all())
        pd.testing.assert_series_equal(features(self.prices).breadth, -features(1 / self.prices).breadth)

    def test_next_open_labels_and_prefix_invariance(self):
        data = self.prices
        for horizon in (3, 20):
            full = replay(data, horizon)
            self.assertGreater(full.mean_supported.sum(), 0)
            expected = 100 * (data.QQQ.shift(-horizon) / data.qqq_open.shift(-1) - 1)
            np.testing.assert_allclose(full.y, expected.loc[full.index])
            shortened = replay(data.iloc[:-80], horizon)
            pd.testing.assert_frame_equal(shortened, full.loc[shortened.index])

    def test_labels_crossing_fit_boundary_are_purged(self):
        data = self.prices.copy()
        baseline = replay(data, 20)
        quarter = baseline.index[-1].to_period("Q")
        first = data.index.searchsorted(quarter.start_time)
        # Training rows just before the boundary can end within this quarter.
        # Change labels through the entry opens of those rows, keeping all signals intact.
        changed = data.copy()
        changed.iloc[first-18:first, changed.columns.get_loc("qqq_open")] *= 2
        check = replay(changed, 20)
        cols = ["child_n", "child_spaced_n", "parent_n", "base_mean", "base_lower", "split_mean", "split_upper"]
        dates = baseline.index[baseline.index.to_period("Q") == quarter]
        pd.testing.assert_frame_equal(baseline.loc[dates, cols], check.loc[dates, cols])

    def test_support_and_disjoint_windows(self):
        result = replay(self.prices, 3)
        unavailable = result.loc[~result.lower_supported & (result.parent_n > 0)]
        self.assertGreater(len(unavailable), 0)
        np.testing.assert_array_equal(unavailable.split_lower, unavailable.base_lower)
        self.assertTrue((result.loc[result.upper_supported].child_spaced_n >= 100).all())
        kept = disjoint(result)
        self.assertTrue((kept.index[1:] >= kept.end.iloc[:-1].to_numpy()).all())

    def test_archive_cutoff_and_calendar(self):
        stamp = "20260106T170000Z"  # Before the Jan 6 US close.
        bars = pd.DataFrame({"date": ["2026-01-02", "2026-01-05", "2026-01-06"],
                             "open": [100, 101, 102], "close": [101, 102, 103]})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for symbol in ("QQQ", *SECTORS):
                (root / symbol).mkdir()
                bars.to_csv(root / symbol / f"{symbol}_5y_1d_{stamp}.csv", index=False)
            panel, _ = read_panel(root, stamp)
            self.assertEqual(panel.index[-1], pd.Timestamp("2026-01-05"))
            for symbol in ("QQQ", *SECTORS):
                bars.drop(1).to_csv(root / symbol / f"{symbol}_5y_1d_{stamp}.csv", index=False)
            with self.assertRaisesRegex(ValueError, "missing or extra"):
                read_panel(root, stamp)
