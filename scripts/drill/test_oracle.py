#!/usr/bin/env python3
"""演习外呼拦截: 泄漏扫描、排队请求的超时、守护进程处理一条请求(假 claude)。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import leak_scan as L  # noqa: E402
import oracle_daemon as D  # noqa: E402
import oracle_request as R  # noqa: E402

AS_OF = datetime(2026, 10, 9, 13, 37, tzinfo=timezone.utc)


class LeakScanTest(unittest.TestCase):
    def test_leak_scan_flags_later_dates(self):
        text = "On October 12, 2026 QQQ fell 3%.\nThe 2026-10-08 close was 747.58."
        hits = L.leaks(text, AS_OF)
        self.assertEqual([h["date"] for h in hits], ["2026-10-12"])

    def test_formats(self):
        for s in ("2026-10-13", "2026/10/13", "20261013", "Oct 13", "13 October 2026", "10/13", "10月13日", "Sept. 30, 2027"):
            self.assertTrue(L.leaks(f"price on {s} dropped", AS_OF), s)
        for s in ("2026-10-09", "Oct 9", "10/8", "9月30日", "2026-09-30"):
            self.assertFalse(L.leaks(f"price on {s} dropped", AS_OF), s)

    def test_leak_scan_ignores_scheduled_events(self):
        hits = L.leak_scan("The FOMC meeting is scheduled for Oct 28.\nCPI due on 2026-10-15.", AS_OF)
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h["scheduled"] for h in hits))
        self.assertEqual(L.leaks("The FOMC meeting is scheduled for Oct 28.", AS_OF), [])

    def test_pre_published_schedules_are_not_leaks(self):
        for t in ('"result": "official schedules show CPI 2026-10-14 08:30 ET, PPI 10-15, FOMC 10-27/28"',
                  '"result": "SPY to Oct 14 expiry +/-1.37% (straddle $8.49)"',
                  '"target": "next ~15 trading days (Oct 12-30, 2026) confirmed macro releases"',
                  'QQQ fell 1.25% on 10-08; CPI due 2026-10-14',
                  '"Thursday, October 15, 2026 08:30 AM Producer Price Index for September 2026".',
                  '10/20 Netflix (IR); 10/21 Tesla after close (IR) - all dates are pre-announced schedules.'):
            self.assertEqual(L.leaks(t, AS_OF), [], t)
        for t in ("CPI came in hot on Oct 14 at 3.4%", "QQQ fell on 2026-10-20", '"simple": "SPY closed at 760 on Oct 13"'):
            self.assertTrue(L.leaks(t, AS_OF), t)

    def test_numbers_are_not_dates(self):
        self.assertEqual(L.leaks("EPS 13/14 beats; ratio 2/3; index 25087", AS_OF), [])


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "drill").mkdir()
        self.store = self.root / "data" / "store" / "GENERAL" / "20261009T133700Z"
        self.store.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _job(self, jid, body="TASK: scout\nJOB_ID: x\n"):
        p = self.store / f"_job_{jid}.md"
        p.write_text(body, encoding="utf-8")
        return p, self.store / f"_out_job_{jid}.json"

    def test_request_times_out_to_failed(self):
        job, out = self._job("R1B1-1")
        rc = R.request(self.root, job, out, timeout=1, poll=0.2)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["status"], "failed")
        self.assertTrue((self.root / "drill" / "queue" / "R1B1-1.req.json").exists())

    def test_runner_failed_shape_keeps_call_id(self):
        job, out = self._job("R1B1-2", "TASK: model_call\nMODEL_CALL_ID: Z.MC1\nREQUEST (verbatim):\n{\"model\": \"P10\"}\n")
        R.request(self.root, job, out, timeout=1, poll=0.2)
        got = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((got["model_call_id"], got["model"], got["status"]), ("Z.MC1", "P10", "failed"))

    def test_request_returns_when_done(self):
        job, out = self._job("R1B1-3")
        q = self.root / "drill" / "queue"
        q.mkdir()
        out.write_text('{"simple": "ok"}', encoding="utf-8")
        (q / "R1B1-3.done").write_text("", encoding="utf-8")
        self.assertEqual(R.request(self.root, job, out, timeout=5, poll=0.1), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"simple": "ok"})

    def test_daemon_handles_one_request_with_retry_on_leak(self):
        """假 claude: 第一次写带未来日期的产出, 第二次(收到泄漏反馈)写干净的; 审计进封存目录。"""
        job, out = self._job("R1B1-4")
        (self.root / "drill" / "pack.json").write_text(json.dumps({"as_of": "20261009T133700Z",
                                                                    "sealed": str(self.root / "sealed")}), encoding="utf-8")
        fake = self.root / "fake_claude.py"
        fake.write_text(
            "import sys, pathlib, json\n"
            f"out = pathlib.Path({str(out)!r})\n"
            "prompt = sys.stdin.read()\n"
            "text = 'clean as of 2026-10-08' if 'LEAK FEEDBACK' in prompt else 'QQQ fell on 2026-10-20'\n"
            "out.write_text(json.dumps({'simple': text}))\n", encoding="utf-8")
        R.enqueue(self.root, job, out)
        cfg = D.Config(root=self.root, cmd=[sys.executable, str(fake)], model="fake", timeout=30)
        n = D.run_once(cfg)
        self.assertEqual(n, 1)
        self.assertIn("2026-10-08", out.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "drill" / "queue" / "R1B1-4.done").exists())
        audit = json.loads((self.root / "sealed" / "audit" / "R1B1-4.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["attempts"], 2)
        self.assertEqual([h["date"] for h in audit["leaks_first"]], ["2026-10-20"])
        self.assertEqual(audit["leaks_final"], [])
        self.assertEqual(D.run_once(cfg), 0)                           # 已处理过的不再处理

    def test_daemon_env_has_clock_and_no_ibkr(self):
        os.environ["EQUIPOISE_IBKR_PORT"] = "4001"
        try:
            env = D.child_env("20261009T133700Z")
        finally:
            os.environ.pop("EQUIPOISE_IBKR_PORT", None)
        self.assertEqual(env["EQUIPOISE_AS_OF"], "20261009T133700Z")
        self.assertNotIn("EQUIPOISE_IBKR_PORT", env)
        self.assertEqual(env["EQUIPOISE_EVIDENCE_LIVE_FETCH"], "0")

    def test_neon_quarantines_post_cutoff_answer_and_rejects_partial_stdout(self):
        job, _ = self._job("neon")
        cfg = D.Config(root=self.root, cmd=["agy"], model=D.NEON_MODEL, engine="agy")
        output = {"question": "inspection?", "detailed": "On Oct 12, 2026 it fell.",
                  "simple": "It fell.", "source": ["bulletin"], "verdict": "mixed"}
        reply = json.dumps({"output": output, "notes": "reviewer only"})
        for rc, stdout in [(0, reply), (3, reply), (0, "incomplete")]:
            with self.subTest(rc=rc, stdout=stdout), patch.object(
                    D.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, stdout, "")):
                result, audit = D._agy_job(cfg, job, job.read_text(), "20261009T133700Z", self.root / "notes")
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("Oct 12", json.dumps(result))
                self.assertIn("error", audit)
                self.assertEqual(audit["rejected_stdout"], stdout)
                self.assertNotIn("rejected_stdout", result)
                if rc == 0 and stdout == reply:
                    self.assertTrue(audit["quarantined_hits"])


class RunStepDrillSwitchTest(unittest.TestCase):
    def test_drill_jobs_carry_command(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_step", HERE.parent / "run_step.py")
        rs = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(HERE.parent))
        spec.loader.exec_module(rs)
        job = {"job_id": "R1B1-1", "material": "data/store/X/T/_job_R1B1-1.md", "output": "data/store/X/T/_out_job_R1B1-1.json"}
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(rs.drill_command(Path(td), job))         # 生产: 没有 pack.json → 不加
            (Path(td) / "drill").mkdir()
            (Path(td) / "drill" / "pack.json").write_text("{}", encoding="utf-8")
            self.assertEqual(rs.drill_command(Path(td), job),
                             "./venv/bin/python scripts/drill/oracle_request.py --job data/store/X/T/_job_R1B1-1.md "
                             "--out data/store/X/T/_out_job_R1B1-1.json")


if __name__ == "__main__":
    unittest.main()
