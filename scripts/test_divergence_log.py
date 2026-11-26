"""datalayer/pg/divergence_log.rows_from (pure): brief rows → one day row per symbol / report / lens."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datalayer.pg import divergence_log as dl  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
AS_OF = datetime(2026, 11, 4, 18, 27, tzinfo=timezone.utc)


def div(**over):
    d = {"t": 2, "T_days": 15, "price": 190.05, "P0": 204.12, "move_atr": -1.7, "sigma_pct": 4.1, "tail": 0.02,
         "kl_bits": 1.3, "alive": 0.3, "dominant": "D", "level": "dead", "severity": 0.96, "n_nodes": 5}
    d.update(over)
    return d


class RowsFromTest(unittest.TestCase):
    def test_single_tree_row(self):
        rows = dl.rows_from([{"symbol": "nvda", "as_of": AS_OF.isoformat(), "bars_through": "2026-11-06", "divergence": div()}], NOW)   # pull gives ISO text
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["symbol"], r["lens"], str(r["day"]), r["level"], r["t_days"], r["n_nodes"]), ("NVDA", "", "2026-11-06", "dead", 15, 5))
        self.assertEqual((r["tail"], r["dominant"], r["source"], r["at"], r["as_of"]), (0.02, "D", "breath", NOW, AS_OF.isoformat()))

    def test_lens_wise_report_gives_one_row_per_lens(self):
        d = div(lens="SPY", by_lens={"SPY": div(level="inside", tail=0.4), "QQQ": div(level="drift", tail=0.1)})
        rows = dl.rows_from([{"symbol": "GENERAL", "as_of": AS_OF, "bars_through": "2026-11-05", "divergence": d}], NOW)
        self.assertEqual(sorted((r["lens"], r["level"]) for r in rows), [("QQQ", "drift"), ("SPY", "inside")])

    def test_rows_without_divergence_or_as_of_are_skipped_and_day_defaults_to_today(self):
        rows = dl.rows_from([{"symbol": "AAA", "as_of": AS_OF, "divergence": None},
                             {"symbol": "BBB", "divergence": div()},
                             {"symbol": "CCC", "as_of": AS_OF, "divergence": div(kl_bits=float("inf"), t=None)}], NOW)
        self.assertEqual([r["symbol"] for r in rows], ["CCC"])
        self.assertEqual(str(rows[0]["day"]), "2026-11-06")
        self.assertIsNone(rows[0]["kl_bits"]); self.assertIsNone(rows[0]["t"])


if __name__ == "__main__":
    unittest.main()
