"""Pure parts of the slow-pressure store: CSV parsing and row building."""
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datalayer.pg import slow_pressure as sp  # noqa: E402


class SlowPressureTest(unittest.TestCase):
    def test_parse_skips_missing_and_bad(self):
        txt = "observation_date,BAMLH0A0HYM2\n2026-11-05,3.48\n2026-11-06,.\n2026-11-07,\nbad,1\n2026-11-09,3.34\n"
        self.assertEqual(sp.parse_fred_csv(txt), [(date(2026, 11, 5), 3.48), (date(2026, 11, 9), 3.34)])
        self.assertEqual(sp.parse_fred_csv(""), [])

    def test_rows_carry_source_unit_and_flag(self):
        at = datetime(2026, 11, 12, tzinfo=timezone.utc)
        rows = sp.rows_from("hy_oas", [(date(2026, 11, 9), 3.34)], at, backfill=True)
        self.assertEqual(rows, [{"indicator": "hy_oas", "obs_date": date(2026, 11, 9), "value": 3.34, "unit": "pct",
                                 "source": "FRED:BAMLH0A0HYM2", "at": at, "backfill": True}])

    def test_every_series_is_raw_with_a_cadence(self):
        for _ind, (sid, unit, cadence, what) in sp.SERIES.items():
            self.assertTrue(sid and unit and what)
            self.assertIn(cadence, ("daily", "weekly"))

    def test_summarise_changes_and_percentile(self):
        obs = [(date(2026, 1, 1 + i), float(i)) for i in range(25)]          # 0..24, latest is the maximum
        r = sp.summarise(obs, "daily")
        self.assertEqual((r["value"], r["chg_short"], r["chg_long"], r["pctile"], r["n_hist"]), (24.0, 5.0, 20.0, 1.0, 25))
        w = sp.summarise([(date(2026, 1, 7), 10.0), (date(2026, 1, 14), 8.0)], "weekly")
        self.assertEqual((w["chg_short"], w["chg_long"], w["pctile"], w["steps"]), (-2.0, None, 0.5, [1, 4]))
        self.assertIsNone(sp.summarise([], "daily"))


if __name__ == "__main__":
    unittest.main()
