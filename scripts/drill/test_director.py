#!/usr/bin/env python3
"""演习导演: sol 进程断网与时钟、派单执行、同回合续会话、close-round 停点、越界读取、守护进程心跳(假 codex)。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import director as D  # noqa: E402
import sol_proc as S  # noqa: E402

FAKE = r'''
import json, os, sys, uuid
argv = sys.argv[1:]
prompt = sys.stdin.read()
reply_path = argv[argv.index("-o") + 1]
thread = argv[argv.index("-") - 1] if "resume" in argv else str(uuid.uuid4())
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "thread": thread, "prompt": prompt[:600],
                        "as_of": os.environ.get("EQUIPOISE_AS_OF"), "ibkr": os.environ.get("EQUIPOISE_IBKR_PORT")}) + "\n")
rules = json.load(open(os.environ["FAKE_RULES"], encoding="utf-8"))
reply, cmd = "ok", None
for sub, rep, c in rules:
    if sub in prompt:
        reply, cmd = rep, c
        break
print(json.dumps({"type": "thread.started", "thread_id": thread}))
if cmd:
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": cmd, "exit_code": 0}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": reply}}))
open(reply_path, "w", encoding="utf-8").write(reply)
'''

FAKE_JOB = r'''
import sys, json, pathlib
a = sys.argv
out = pathlib.Path(a[a.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"status": "verified", "simple": "fake"}))
'''


class DirectorTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "drill" / "queue").mkdir(parents=True)
        (self.root / "drill" / "pack.json").write_text(json.dumps(
            {"symbol": "GENERAL", "as_of": "20261009T133700Z", "sealed": str(self.root / "sealed")}), encoding="utf-8")
        (self.root / "drill" / "queue" / ".daemon_alive").write_text("x", encoding="utf-8")
        for rel in (".codex/run.md", ".claude/agents/zealot.md", ".claude/agents/reaper.md", ".claude/agents/scout.md",
                    ".claude/agents/runner.md"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(f"# {rel}\n", encoding="utf-8")
        st = self.root / "data" / "store" / "GENERAL" / "T"
        st.mkdir(parents=True)
        (st / "_spawn_R1_zealot.md").write_text("ZEALOT MATERIAL R1", encoding="utf-8")
        (st / "_spawn_R1_reaper.md").write_text("REAPER MATERIAL R1", encoding="utf-8")
        (st / "_resume_R1_B2_zealot.md").write_text("ZEALOT RESUME B2", encoding="utf-8")
        (st / "_job_R1B1-1.md").write_text("TASK: scout", encoding="utf-8")
        self.fake = self.root / "fake_codex.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.fake_job = self.root / "fake_job.py"
        self.fake_job.write_text(FAKE_JOB, encoding="utf-8")
        self.log = self.root / "fake_log.jsonl"
        self.rules = self.root / "rules.json"
        self.env = {"FAKE_LOG": str(self.log), "FAKE_RULES": str(self.rules), "EQUIPOISE_IBKR_PORT": "4001",
                    "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        self.cfg = S.SolConfig(root=self.root, exe=[sys.executable, str(self.fake)], model="gpt-6.1-sol", timeout=60,
                               env_base=self.env)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _rules(self, rules):
        self.rules.write_text(json.dumps(rules), encoding="utf-8")

    def _calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]

    def test_sol_cmd_has_no_search(self):
        self._rules([])
        s = S.run(self.cfg, "zealot", "hello")
        call = self._calls()[0]
        self.assertNotIn("--search", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("-C") + 1], str(self.root))
        self.assertEqual(call["argv"][call["argv"].index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual((call["as_of"], call["ibkr"]), ("20261009T133700Z", None))
        self.assertTrue(s["thread"])
        r = S.run(self.cfg, "zealot", "again", s["thread"])
        call = self._calls()[1]
        self.assertIn("resume", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("-m") + 1], "gpt-6.1-sol")   # resume 必须再给模型
        self.assertNotIn("--search", call["argv"])
        self.assertEqual(r["thread"], s["thread"])

    def test_refuses_without_daemon(self):
        (self.root / "drill" / "queue" / ".daemon_alive").unlink()
        with self.assertRaises(D.DirectorError):
            D.advance(self.cfg)

    def test_spawn_sheet_dispatch_and_pause(self):
        sheet = {"dispatch": [
            {"kind": "debater", "side": "zealot", "mode": "new", "material": "data/store/GENERAL/T/_spawn_R1_zealot.md"},
            {"kind": "debater", "side": "reaper", "mode": "new", "material": "data/store/GENERAL/T/_spawn_R1_reaper.md"},
            {"kind": "job", "job_id": "R1B1-1", "material": "data/store/GENERAL/T/_job_R1B1-1.md",
             "output": "data/store/GENERAL/T/_out_job_R1B1-1.json"}]}
        self._rules([["DIRECTOR DISPATCH RESULT", json.dumps({"round_closed": 1, "decision": "CONTINUE"}), None],
                     ["ZEALOT MATERIAL", "data/store/GENERAL/T/_out_R1_B1_zealot.json", None],
                     ["REAPER MATERIAL", "data/store/GENERAL/T/_out_R1_B1_reaper.json", None],
                     ["DRILL EXECUTION ADDENDUM", json.dumps(sheet), None]])
        (self.root / "prompts" / "drill").mkdir(parents=True)
        (self.root / "prompts" / "drill" / "arbiter-addendum.md").write_text("## DRILL EXECUTION ADDENDUM", encoding="utf-8")
        out = D.advance(self.cfg, py=[sys.executable, str(self.fake_job)])
        self.assertEqual((out["status"], out["round"], out["terminate"]), ("paused", 1, False))
        calls = self._calls()
        prompts = [c["prompt"] for c in calls]
        self.assertTrue(any(p.startswith("Session status") and "ZEALOT MATERIAL" in p for p in prompts))
        self.assertTrue(any("REAPER MATERIAL" in p for p in prompts))
        arb = [c for c in calls if "resume" in c["argv"]]
        self.assertEqual(len(arb), 1)                                        # 结果续回同一个仲裁者会话
        st = D.load_state(self.root)
        self.assertEqual(arb[0]["thread"], st["arbiter"])
        self.assertTrue((self.root / "data/store/GENERAL/T/_out_job_R1B1-1.json").exists())
        msg = json.loads((self.root / "drill" / "runs" / f"{max(int(p.name[:3]) for p in (self.root / 'drill' / 'runs').glob('*-arbiter.json')):03d}-arbiter.json")
                         .read_text(encoding="utf-8"))
        self.assertTrue(msg["resumed"])
        resume_prompt = (self.root / "drill" / "runs" / f"{msg['n']:03d}-arbiter.prompt.md").read_text(encoding="utf-8")
        for frag in ("_out_R1_B1_zealot.json", "_out_R1_B1_reaper.json", "_out_job_R1B1-1.json"):
            self.assertIn(frag, resume_prompt)
        # 再推进: 带着"库主已看过第 1 回合"续同一会话, 不重起仲裁者
        self._rules([["has reviewed round 1", json.dumps({"round_closed": 2, "decision": "TERMINATE"}), None]])
        out2 = D.advance(self.cfg)
        self.assertEqual((out2["round"], out2["terminate"]), (2, True))
        self.assertEqual(self._calls()[-1]["thread"], st["arbiter"])

    def test_debater_followup_resumes_thread(self):
        st = D.load_state(self.root)
        self._rules([])
        first = D.dispatch(self.cfg, st, [{"kind": "debater", "side": "zealot", "mode": "new",
                                           "material": "data/store/GENERAL/T/_spawn_R1_zealot.md"}])
        D.dispatch(self.cfg, st, [{"kind": "debater", "side": "zealot", "mode": "follow_up",
                                   "material": "data/store/GENERAL/T/_resume_R1_B2_zealot.md"}])
        calls = self._calls()
        self.assertNotIn("resume", calls[0]["argv"])
        self.assertIn("resume", calls[1]["argv"])
        self.assertEqual(calls[1]["thread"], first[0]["thread"])
        bad = D.dispatch(self.cfg, st, [{"kind": "debater", "side": "reaper", "mode": "follow_up", "message": "x"}])
        self.assertIn("error", bad[0])                                       # 没开过线程的一方不能追问

    def test_out_of_root_read_flagged(self):
        self._rules([["peek", "done", "/bin/bash -lc 'cat /opt/equipoise/data/market_data/SPY_1d.csv'"]])
        s = S.run(self.cfg, "reaper", "peek")
        self.assertEqual(len(s["out_of_root"]), 1)
        root = Path("/opt/equipoise_drill/GENERAL_X_a")
        ev = lambda c: json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": c}})
        self.assertEqual(S.out_of_root_reads(ev("cat /opt/equipoise_drill/GENERAL_X_a/data/x.json"), root), [])
        self.assertEqual(S.out_of_root_reads(ev("/opt/equipoise/venv/bin/python scripts/run_step.py status"), root), [])
        for c in ("cat /opt/equipoise_drill/_sealed/GENERAL_X_a/outcome.json", "ls ../", "cat /root/.codex/x",
                  "sudo -u postgres psql -d equipoise"):
            self.assertEqual(len(S.out_of_root_reads(ev(c), root)), 1, c)

    def test_claude_debater_engine(self):
        """天花板对照: 辩手走 `claude -p`(stream-json), 续会话用 --resume, 越界读取从 tool_use 里扫, 回 JSON 时落到 OUTPUT。"""
        fake = self.root / "fake_claude.py"
        fake.write_text(
            "import json, os, sys, uuid\n"
            "argv = sys.argv[1:]\n"
            "prompt = sys.stdin.read()\n"
            "sid = argv[argv.index('--resume') + 1] if '--resume' in argv else str(uuid.uuid4())\n"
            "open(os.environ['FAKE_LOG'], 'a').write(json.dumps({'argv': argv, 'sid': sid}) + '\\n')\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}))\n"
            "print(json.dumps({'type': 'assistant', 'session_id': sid, 'message': {'content': [\n"
            "  {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/opt/equipoise/CLAUDE.md'}}]}}))\n"
            "print(json.dumps({'type': 'result', 'session_id': sid, 'result': json.dumps({'beat': {'status': 'final'}})}))\n",
            encoding="utf-8")
        (self.root / "data/store/GENERAL/T/_spawn_R1_reaper.md").write_text(
            "REAPER MATERIAL R1\nOUTPUT: data/store/GENERAL/T/_out_R1_B1_reaper.json — write the JSON", encoding="utf-8")
        dcfg = S.SolConfig(root=self.root, exe=[sys.executable, str(fake)], model="claude-opus-5-5", timeout=60,
                           env_base=self.env, engine="claude")
        st = D.load_state(self.root)
        r = D.dispatch(self.cfg, st, [{"kind": "debater", "side": "reaper", "mode": "new",
                                       "material": "data/store/GENERAL/T/_spawn_R1_reaper.md"}], debater_cfg=dcfg)[0]
        self.assertEqual(r["landed"], "data/store/GENERAL/T/_out_R1_B1_reaper.json")
        self.assertEqual(json.loads((self.root / r["landed"]).read_text(encoding="utf-8")), {"beat": {"status": "final"}})
        self.assertEqual(len(r["out_of_root"]), 1)
        call = self._calls()[0]
        self.assertIn("claude-opus-5-5", call["argv"])
        self.assertNotIn("--resume", call["argv"])
        D.dispatch(self.cfg, st, [{"kind": "debater", "side": "reaper", "mode": "follow_up", "message": "next beat"}],
                   debater_cfg=dcfg)
        call = self._calls()[1]
        self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], r["thread"])

    def test_probe_reply_is_sealed(self):
        self._rules([["Answer from your own knowledge", "don't know: all four", None]])
        (self.root / "prompts" / "drill").mkdir(parents=True, exist_ok=True)
        (self.root / "prompts" / "drill" / "knowledge-probe.md").write_text(
            "Answer from your own knowledge only. {AS_OF}", encoding="utf-8")
        rec = D.probe(self.cfg)
        self.assertEqual(rec["reply"], "don't know: all four")
        self.assertTrue((self.root / "sealed" / "probe.json").exists())
        runs = list((self.root / "drill" / "runs").iterdir())
        self.assertFalse(any("don't know" in p.read_text(encoding="utf-8") for p in runs))


if __name__ == "__main__":
    unittest.main()
