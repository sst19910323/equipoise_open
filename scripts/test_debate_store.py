#!/usr/bin/env python3
"""对象仓(重建批 3 改名后)的回归: 自检全流程 + 组装器 meta 注入与 evidence_added 机械派生。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StoreSelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        ds = _load("debate_store")
        ds._self_test()  # raises on any assertion


class BufferDirsTest(unittest.TestCase):
    def test_fresh_symbol_gets_buffer_parent_dirs_before_first_archive(self):
        # 2026-10-16 KO 首场: 新标的没有 data/debate/<SYM> 与 data/evidence/<SYM>, 归档终稿时才发现 → 账目错位判 poisoned
        ds = _load("debate_store")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = ds.Store("NEW", "20261016T133100Z", root)
            self.assertFalse((root / "data" / "debate" / "NEW").exists())
            s.ensure_dirs()
            for sub in ("store", "debate", "evidence"):
                self.assertTrue((root / "data" / sub / "NEW").is_dir(), sub)
            # 不先调 ensure_dirs 也行: 第一次归档自己会建
            s2 = ds.Store("NEW2", "20261016T133100Z", root)
            s2.put("final", ds.role_record(55, "Z1"), by="Zealot", round_=1)
            self.assertTrue(s2.debate_buf.exists())
            self.assertEqual(s2.verify().get("ok"), True, s2.verify())


class AssemblerMetaTest(unittest.TestCase):
    def test_meta_injected_and_index_derived(self):
        ds = _load("debate_store")
        asm = _load("assemble_debate_artifacts")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = ds.Store("T", "20260930T000000Z", root)
            s.put("evidence", {"question": "q1", "simple": "yes 12%", "detailed": "d", "source": ["u1"],
                               "verdict": "supports"}, by="Zealot", round_=1, beat=1,
                  meta={"status": "verified", "kind": "search"})
            s.put("evidence", {"question": "q2", "simple": "NOT_FOUND", "detailed": "d", "source": ["u2"],
                               "verdict": "not_found"}, by="Reaper", round_=2, beat=1,
                  meta={"status": "not_found", "kind": "search"})
            s.put("final", ds.role_record(60, "Z1"), by="Zealot", round_=1)
            s.put("final", ds.role_record(40, "R1"), by="Reaper", round_=1)
            s.put("notes", {"tally": "t", "arbiter_notes": "n"}, round_=1)
            s.put("private_score", {"composite_score": 50}, round_=1)
            s.put("provisional", {"claims_review": [], "composite_score": 50, "operation_type": "HOLD_NEUTRAL",
                                  "mentality": "wait_and_see", "rationale": "r"})
            draft = root / "draft.json"
            draft.write_text(json.dumps({"composite_score": 50, "evidence_added": ["bogus"],
                                         "search_rounds_used": 99}), encoding="utf-8")
            meta = root / "meta.json"
            meta.write_text(json.dumps({"symbol": "T", "data_files": {"config": "config/symbols.yaml"},
                                        "signatures": {"zealot": "m", "reaper": "m", "arbiter": "m"},
                                        "dissent": {"zealot_composite_score": 60, "zealot_reservation": "z",
                                                    "reaper_composite_score": 40, "reaper_reservation": "r"},
                                        "objections": [], "report_generated_at": "2026-09-30T01:02:03Z"}),
                            encoding="utf-8")
            rc = asm.assemble(draft, s.debate_buf, s.evidence_buf, root / "A.json", root / "E.json",
                              private_buffer_path=s.private_buf, meta_path=meta)
            self.assertEqual(rc["private_score_records"], 1)
            self.assertIn("dissent", rc["meta_keys"])
            a = json.loads((root / "A.json").read_text(encoding="utf-8"))
            self.assertEqual(a["signatures"]["arbiter"], "m")
            self.assertEqual(a["dissent"]["reaper_composite_score"], 40)
            self.assertEqual(a["search_rounds_used"], 2)
            self.assertEqual(len(a["evidence_added"]), 2)
            self.assertEqual(a["evidence_added"][0]["status"], "answered")
            self.assertEqual(a["evidence_added"][1]["status"], "blocked")
            self.assertEqual(a["evidence_added"][0]["as_of"], "2026-09-30")
            self.assertEqual(a["evidence_added"][0]["requested_by"], "Zealot")


if __name__ == "__main__":
    unittest.main()
