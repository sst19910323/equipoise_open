#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/delta_session.py: eligibility of a base session, the import rule, re-minting through the store,
the standing-evidence side text. Temp roots only; no engine, no PG."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("delta_session", ROOT / "scripts" / "delta_session.py")
ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
from debate_store import ARBITER, Store  # noqa: E402

BASE, NEW = "20261104T100000Z", "20261106T100000Z"


def seed_base(root: Path, status="done", lineage=None):
    st = Store("X", BASE, root); st.ensure_dirs()
    st.put("evidence", {"kind": "data", "mode": "peer_compare", "prepared": True, "question": "Precomputed: peers",
                        "simple": "machine figures", "detailed": "{}", "payload_text": "{}", "source": ["data:prepare"]},
           by=ARBITER, round_=1, meta={"status": "verified", "kind": "data"})
    st.put("evidence", {"question": "Verify the Q3 release numbers", "simple": "Revenue 57.0bn vs 54.9bn consensus",
                        "detailed": "[Conclusion] …", "source": ["https://ir.example.com/q3"], "verdict": "supports"},
           by="Zealot", round_=1, meta={"status": "verified", "kind": "search", "verified_scope": "the release numbers",
                                        "request": {"type": "search", "target_claim": "Z1", "question": "Verify the Q3 release numbers"}})
    st.put("evidence", {"question": "Any new guidance since?", "simple": "NOT_FOUND", "detailed": "", "source": []},
           by="Reaper", round_=2, meta={"status": "not_found", "kind": "search"})
    st.put("model_call", {"model_call_id": "Z.MC1", "model": "P07", "issue": "Z-STRUCTURE", "target": "structure",
                          "finding": "fading momentum", "verdict": "mixed", "suggested_evidence_tier": "T2"},
           by="Zealot", round_=1, meta={"status": "verified", "kind": "model_call", "model": "P07"})
    st.put("evidence", {"question": "Macro calendar inside the window", "simple": "CPI 11-13, FOMC 12-10", "detailed": "…",
                        "source": ["https://www.bls.gov/schedule"], "verdict": "neutral"},
           by=ARBITER, round_=2, meta={"status": "verified", "kind": "search", "verified_scope": "the dates"})
    state = {"symbol": "X", "ts": BASE, "status": status, "odds": {"reference_price": 100.0}}
    if lineage:
        state["lineage"] = lineage
    (st.dir / "_session.json").write_text(json.dumps(state), encoding="utf-8")
    return st


class CheckBaseTest(unittest.TestCase):
    def test_rules(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); cfg = dict(ds.DEFAULTS)
            self.assertFalse(ds.check_base("X", BASE, NEW, root, cfg)["ok"])                      # no base yet
            seed_base(root)
            ok = ds.check_base("X", BASE, NEW, root, cfg)
            self.assertTrue(ok["ok"]); self.assertEqual(ok["depth"], 1)
            self.assertIn("not earlier", ds.check_base("X", BASE, "20261104T090000Z", root, cfg)["why"])
            self.assertIn("days old", ds.check_base("X", BASE, "20261120T100000Z", root, cfg)["why"])
            self.assertIn("not found", ds.check_base("X", "garbage", NEW, root, cfg)["why"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); seed_base(root, status="debating")
            self.assertIn("not done", ds.check_base("X", BASE, NEW, root, dict(ds.DEFAULTS))["why"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); seed_base(root, lineage={"mode": "delta", "depth": 3})
            self.assertIn("chain depth 4", ds.check_base("X", BASE, NEW, root, dict(ds.DEFAULTS))["why"])
            self.assertTrue(ds.check_base("X", BASE, NEW, root, {**ds.DEFAULTS, "max_chain": 4})["ok"])


class ImportTest(unittest.TestCase):
    def test_only_verified_scout_evidence_is_imported_and_reminted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); base = seed_base(root)
            picks = ds.select_importable(base, dict(ds.DEFAULTS))
            self.assertEqual([p["id"] for p in picks], ["Z.EV1", "A.EV2"])          # not the prepared data, not NOT_FOUND, not the MC
            new = Store("X", NEW, root); new.ensure_dirs()
            done = ds.import_evidence(new, base, BASE, dict(ds.DEFAULTS))
            self.assertEqual([d["id"] for d in done], ["A.EV1", "A.EV2"])
            rec = new.obj("A.EV1")
            self.assertEqual(rec["by"], ARBITER); self.assertEqual(rec["round"], 1)
            self.assertEqual(rec["meta"]["origin_id"], "Z.EV1"); self.assertEqual(rec["meta"]["origin_session"], BASE)
            self.assertTrue(rec["meta"]["reused"]); self.assertEqual(rec["meta"]["status"], "verified")
            self.assertEqual(rec["meta"]["last_verified_at"], base.obj("Z.EV1")["ts"])
            self.assertEqual(rec["content"]["reused_from"]["id"], "Z.EV1")
            self.assertEqual(rec["content"]["simple"], "Revenue 57.0bn vs 54.9bn consensus")
            self.assertEqual(rec["meta"]["origin_chain"], [BASE])
            # chained follow-up: a delta on this delta keeps the first session's verification stamps (GPT review 11-06)
            third = Store("X", "20261003T000000Z", root); third.ensure_dirs()
            done2 = ds.import_evidence(third, new, NEW, dict(ds.DEFAULTS))
            chained = next(d for d in done2 if d["origin_id"] == "A.EV1")
            rec2 = third.obj(chained["id"])
            self.assertEqual(rec2["meta"]["last_verified_at"], base.obj("Z.EV1")["ts"])
            self.assertEqual(rec2["meta"]["source_as_of"], base.obj("Z.EV1")["ts"])
            self.assertEqual(rec2["meta"]["origin_chain"], [BASE, NEW])
            self.assertTrue(new.verify()["ok"])
            txt = ds.standing_text(done, BASE, {"why": "line drift", "price_move": "100 → 97 (-3.00%), -1.50 ATR"})
            self.assertIn("## A.EV1  (was Zealot Z.EV1", txt); self.assertIn("Revenue 57.0bn", txt)
            self.assertIn(ds.STANDING_SENTENCE, txt); self.assertIn(ds.CLAIM_RULE, txt); self.assertIn("Why this follow-up: line drift", txt)
            self.assertIn("by reasoning first", txt)
            self.assertNotIn("ruling", txt.lower().replace("the previous session's ruling", ""))   # no verdict text leaks

    def test_empty_base_gives_empty_text(self):
        self.assertIn("no importable evidence", ds.standing_text([], BASE, {}))

    def test_price_move_text(self):
        self.assertEqual(ds.price_move_text(100.0, 97.0, 2.0), "100 → 97 (-3.00%), -1.50 ATR")
        self.assertEqual(ds.price_move_text(100.0, 103.0, None), "100 → 103 (+3.00%)")
        self.assertIsNone(ds.price_move_text(None, 1, 2))


class OpenDeltaEndToEndTest(unittest.TestCase):
    """A mock full session, then `Session.open(base=…)` on a second data set a day later: the verified Scout evidence of
    the first session is re-minted, the round-1 material carries the standing block, the reference length is 3, the
    lineage is in the state and the receipt; a bad base is refused with code 10."""

    def test_open_with_base(self):
        import os
        from debate_driver import MockEngine, MockTools, ruler_script, run_ruler_session
        from run_step import Session, StepError
        tmp = Path(tempfile.mkdtemp(prefix="delta_e2e_")); cwd = os.getcwd()
        try:
            os.chdir(tmp)
            rc, ts1, _ = run_ruler_session(tmp, None, engine=MockEngine(ruler_script()))
            self.assertTrue(rc["ok"], rc)
            base_store = Store("TEST", ts1, tmp)
            self.assertIn("Z.EV1", base_store.ids())
            ts2 = "20261001T000000Z"
            for f in list((tmp / "data").rglob(f"*{ts1}*")):      # a second data set, one day later (inputs only)
                rel = str(f.relative_to(tmp)).replace("\\", "/")
                if not f.is_file() or rel.startswith(("data/store/", "data/debate/", "data/evidence/")):
                    continue
                g = f.with_name(f.name.replace(ts1, ts2))
                if "technical/TEST/" in str(f).replace("\\", "/"):
                    g.write_text(json.dumps({"TEST_current_snapshot_last_price": 97.0, "TEST_daily_atr_pct_14": 8 / 15 ** 0.5,
                                             "TEST_latest_data_timestamp": "2026-10-01T00:00:00+00:00",
                                             "TEST_session_context_current_session": "closed"}), encoding="utf-8")
                else:
                    g.write_bytes(f.read_bytes())
            with self.assertRaises(StepError) as cm:
                Session.open("TEST", tmp, engine="mock", model="mock-judge", tools=MockTools(tmp), base="20260101T000000Z", why="x")
            self.assertEqual(cm.exception.code, 10); self.assertIn("not found", str(cm.exception))
            s = Session.open("TEST", tmp, engine="mock", model="mock-judge", tools=MockTools(tmp), base=ts1, why="price drifted 1.5 ATR")
            self.assertEqual(s.ts, ts2)
            lin = s.state["lineage"]
            self.assertEqual((lin["mode"], lin["base_as_of"], lin["depth"], lin["why"]), ("delta", ts1, 1, "price drifted 1.5 ATR"))
            self.assertTrue(lin["price_move"].startswith("100 → 97 (-3.00%)"), lin["price_move"])
            self.assertEqual(s.caps["round_ref"], 3)
            self.assertTrue(lin["imported"], lin)
            imp = s.store.obj(lin["imported"][0])
            self.assertEqual(imp["meta"]["origin_session"], ts1); self.assertTrue(imp["meta"]["reused"])
            self.assertEqual(imp["content"]["reused_from"]["id"], lin["imported_origin"][lin["imported"][0]])
            self.assertTrue((s.store.dir / "_standing_evidence.md").exists())
            rc2 = s.open_receipt()
            self.assertEqual(rc2["lineage"]["mode"], "delta")
            mat = (tmp / rc2["spawn"]["Zealot"]).read_text(encoding="utf-8")
            self.assertIn("--- STANDING EVIDENCE (follow-up assessment on base " + ts1, mat); self.assertIn("Claim admission in a follow-up", mat)
            self.assertIn("_standing_evidence.md", mat); self.assertIn("FOCUS THIS ROUND: (follow-up assessment on base", mat)
            self.assertIn("not a stopping target", s._round_ref_note(1))
            self.assertTrue(s.store.verify()["ok"])
            s._meta()  # the report meta carries the lineage
            self.assertEqual(s._meta()["lineage"]["base_as_of"], ts1)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
