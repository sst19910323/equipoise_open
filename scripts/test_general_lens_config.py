"""The regional generals' lens lists must agree everywhere they are spelled out (2026-11-02, JP / KR second lenses):
config/symbols.yaml (the source the open step and the odds snapshot read), workflows/data_manifest.GENERAL_LENSES (the
material manifest), run_one_debate._fetch_needs (what preparation fetches) and workflows/postcheck_quant._GENERAL_MEMBERS
(what the postcheck settles). A mismatch makes `run_step open` refuse the session (code 20) or leaves a lens without bars."""
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from workflows.data_manifest import GENERAL_LENSES  # noqa: E402
from workflows.postcheck_quant import _GENERAL_MEMBERS  # noqa: E402
import run_one_debate  # noqa: E402

GENERALS = ("GENERAL", "GENERAL_EU", "GENERAL_JP", "GENERAL_KR")


class GeneralLensConfig(unittest.TestCase):
    def setUp(self):
        with open(ROOT / "config" / "symbols.yaml", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.contracts = self.cfg["symbol_contracts"]

    def _yaml_lenses(self, g):
        return [str(x["symbol"]) for x in self.contracts[g]["lenses"]]

    def test_lists_agree_in_order(self):
        for g in GENERALS:
            with self.subTest(general=g):
                lenses = self._yaml_lenses(g)
                self.assertEqual(GENERAL_LENSES[g], lenses)
                self.assertEqual(_GENERAL_MEMBERS[g], lenses)
                self.assertEqual(run_one_debate._fetch_needs(g, self.cfg), lenses)

    def test_every_lens_has_a_contract_in_its_region(self):
        for g in GENERALS:
            region = self.contracts[g]["region"]
            for L in self._yaml_lenses(g):
                with self.subTest(general=g, lens=L):
                    c = self.contracts[L]
                    self.assertEqual(str(c.get("region") or "US").upper(), str(region or "US").upper())   # US contracts may omit region
                    self.assertIn(str(c.get("sec_type") or "STK"), ("IND", "STK"))

    def test_weights_sum_to_one(self):
        for g in GENERALS:
            with self.subTest(general=g):
                w = sum(float(x["weight"]) for x in self.contracts[g]["lenses"])
                self.assertAlmostEqual(w, 1.0, places=6)

    def test_jp_kr_second_lenses(self):
        self.assertEqual(self._yaml_lenses("GENERAL_JP"), ["N225", "TOPX"])
        self.assertEqual(self._yaml_lenses("GENERAL_KR"), ["KODEX200", "KOSDQ150"])
        for L in ("TOPX", "KOSDQ150"):
            self.assertEqual(self.contracts[L]["sec_type"], "IND")   # observe-only lenses: levels, no shares


if __name__ == "__main__":
    unittest.main()
