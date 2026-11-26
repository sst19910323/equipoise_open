import tempfile
import unittest
from pathlib import Path

import oracle_bridge as bridge


class JobWorkspaceTest(unittest.TestCase):
    def test_worker_sees_only_named_data_and_oracle_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            work = Path(tmp) / "request"
            named = "data/news/ASML/filing.json"
            unrelated = "data/news/MP/filing.json"
            paths = [*bridge.STATIC, named, unrelated, "drill/pack.json", "drill/runs/old.txt"]
            for path in paths:
                file = cache / path
                if path == "prompts/agent":
                    file = file / "manuals/example.md"
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(path, encoding="utf-8")
            job = bridge.job_workspace(cache, work, Path("data/store/ASML/T/_job_A.md"),
                                       f"Read {named}", {named: "", unrelated: ""})
            self.assertEqual(job.read_text(), f"Read {named}")
            visible = work / "material"
            self.assertTrue((visible / named).is_file())
            self.assertTrue((visible / "drill/archive.jsonl").is_file())
            for path in (unrelated, "drill/pack.json", "drill/runs/old.txt"):
                self.assertFalse((visible / path).exists())

    def test_queue_ids_are_path_components(self):
        self.assertEqual(bridge.component("R1B1-1"), "R1B1-1")
        for name in ("../outside", "path/job", r"path\job", "", "x:stream"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bridge.component(name)
