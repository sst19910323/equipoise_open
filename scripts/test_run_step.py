#!/usr/bin/env python3
"""编排步骤脚本(run_step)与全脚本驱动器(debate_driver)的回归: mock 引擎跑通整条协议 + CLI 边界。"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DriverSelfTest(unittest.TestCase):
    def test_full_protocol_with_mock_engine(self):
        _load("debate_driver")._self_test()


class RunStepCliTest(unittest.TestCase):
    def test_open_refuses_when_data_missing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config").mkdir()
            (Path(td) / "config" / "symbols.yaml").write_text(
                json.dumps({"analysis_targets": {}, "symbol_contracts": {"X": {"benchmark": "SPY"}}}), encoding="utf-8")
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_step.py"), "open", "--symbol", "X",
                                "--root", td], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 10, p.stdout + p.stderr)
            self.assertIn("Data not ready", p.stdout)

    def test_route_without_session_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_step.py"), "tally", "--symbol", "X",
                                "--ts", "20260930T000000Z", "--root", td, "--round", "1"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 1)
            self.assertIn("open", p.stdout)

    def test_gate_reports_validator_crash_as_fatal(self):
        from unittest import mock
        rs = _load("run_step")
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "X_Analysis_20260930T000000Z.json"
            a.write_text(json.dumps({"symbol": "X"}), encoding="utf-8")
            with mock.patch("validate_debate.validate", side_effect=AttributeError("boom")):
                g = rs.gate(a, Path(td))
            self.assertFalse(g["ok"])
            self.assertTrue(any("Validator exception" in f and "boom" in f for f in g["fatal"]), g)

    def test_gate_does_not_demand_payload_text_from_failed_data_calls(self):
        # 2026-10-13 MU 停机案: 执行者掉线的模型调用回执 payload_text 为空, 闸不该拿它当证据要全文; 真用到的仍要
        rs = _load("run_step")
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "X_Analysis_20260930T000000Z.json"
            base = {"question": "q", "simple": "s", "detailed": "d", "source": ["data:model:P10"], "kind": "data"}
            doc = {"symbol": "X", "evidence": [
                {**base, "status": "failed", "payload_text": ""},
                {**base, "status": "answered", "payload_text": ""},
            ]}
            errs = rs.gate_extras(doc, a, Path(td))
            self.assertFalse(any("evidence[0]" in e for e in errs), errs)
            self.assertTrue(any("evidence[1] data call is missing the payload_text" in e for e in errs), errs)

    def test_failed_job_can_be_requested_again(self):
        # 执行者挂了的外呼不算"本场已执行": 同一条申报之后再发不该被字面去重跳过
        import shutil
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        try:
            dd._mock_root(tmp)
            s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))
            req = {"type": "search", "target_claim": "Z1", "question": "q1", "would_change_my_mind": "x"}

            def beat(cid, score, q):
                r = ds.role_record(score, cid, status="need_more")
                r["request_evidence"] = [q]
                return r

            def rq(q):
                return {"type": "search", "target_claim": "R1", "question": q, "would_change_my_mind": "x"}
            rc = s.route(1, 1, {"Zealot": beat("Z1", 60, req), "Reaper": beat("R1", 40, rq("r-q1"))})
            jobs = {j["by"]: j["job_id"] for j in rc["jobs"]}
            ok = {"question": "r-q1", "simple": "s", "detailed": "d", "source": ["u"], "verdict": "mixed"}
            s.close_beat(1, 1, [{"job_id": jobs["Zealot"], "failed": "scout crashed"},
                                {"job_id": jobs["Reaper"], "status": "unverified", "scope": "s", "result": ok}])
            rc2 = s.route(1, 2, {"Zealot": beat("Z1", 60, req), "Reaper": beat("R1", 40, rq("r-q2"))})
            self.assertIn("Zealot", [j["by"] for j in rc2["jobs"]], rc2.get("skipped"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_routing_one_side_at_a_time_keeps_both_sides_jobs(self):
        # 第二场冒烟 R2: 一方先交拍先路由, 另一方后到再路由同一拍, 先到那方的在途作业被覆盖丢失
        import shutil
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        try:
            dd._mock_root(tmp)
            s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))

            def beat(cid, score, reqs):
                r = ds.role_record(score, cid, status="need_more")
                r["request_evidence"] = reqs
                return r
            mc = {"type": "model_call", "model": "P02", "issue": "R1", "params": {"symbol": "TEST"}}
            sr = {"type": "search", "target_claim": "R1", "question": "rq", "would_change_my_mind": "x"}
            rc1 = s.route(1, 1, {"Reaper": beat("R1", 40, [mc, sr])})
            self.assertEqual(len(rc1["jobs"]), 2, rc1)
            mc2 = {"type": "model_call", "model": "P02", "issue": "Z1", "params": {"symbol": "TEST"}}
            rc2 = s.route(1, 1, {"Zealot": beat("Z1", 60, [mc2])})
            self.assertEqual(len(rc2["jobs"]), 1, rc2)
            ids = [j["job_id"] for j in rc1["jobs"] + rc2["jobs"]]
            self.assertEqual(len(set(ids)), 3, ids)
            self.assertEqual(sorted(rc2["open_jobs"]), sorted(ids))
            ok_s = {"question": "rq", "simple": "s", "detailed": "d", "source": ["u"], "verdict": "mixed"}

            def ok_m(j):
                return {"model_call_id": j["model_call_id"], "model": "P02", "issue": "x", "target": "t", "steps": [],
                        "finding": "f", "verdict": "mixed", "suggested_evidence_tier": "T2", "payload_text": ""}
            results = []
            for j in rc1["jobs"] + rc2["jobs"]:
                results.append({"job_id": j["job_id"], "status": "unverified", "scope": "s",
                                "result": ok_m(j) if j["kind"] == "runner" else ok_s})
            rc3 = s.close_beat(1, 1, results)
            self.assertEqual(len(rc3["archived"]), 3, rc3)
            self.assertEqual(sorted(rc3["pending"]), ["Reaper", "Zealot"])
            self.assertTrue(s.store.verify()["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_executor_is_its_own_state(self):
        # 库主 10-01: 掉线是唯一性质不同的状态(不是结果, 可重发); 没查到是正常答案
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            rc = s.route(1, 1, {"Zealot": self._need_more(ds, "Z1", 60, "zq")})
            out = s.close_beat(1, 1, [{"job_id": rc["jobs"][0]["job_id"], "failed": "scout crashed"}], role="Zealot")
            eid = out["archived"][0]["id"]
            self.assertEqual(out["archived"][0]["status"], "failed")
            self.assertEqual(s.store.obj(eid)["meta"]["status"], "failed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_every_answered_result_needs_a_scope_sentence(self):
        # 每条有结果的回执都写一句"哪部分可以当事实用"; status 只是这句话的标题
        import shutil
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        try:
            dd._mock_root(tmp)
            s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))
            req = {"type": "search", "target_claim": "Z1", "question": "q1", "would_change_my_mind": "x"}
            z = ds.role_record(60, "Z1", status="need_more")
            z["request_evidence"] = [req]
            rc = s.route(1, 1, {"Zealot": z})
            jid = rc["jobs"][0]["job_id"]
            res = {"question": "q1", "simple": "s", "detailed": "d", "source": ["u"], "verdict": "supports"}
            with self.assertRaises(rs.StepError) as cm:
                s.close_beat(1, 1, [{"job_id": jid, "status": "verified", "result": res}])
            self.assertIn("scope", str(cm.exception))
            out = s.close_beat(1, 1, [{"job_id": jid, "status": "verified", "scope": "同比增速两处来源一致; 需求持续性未核实",
                                      "result": res}])
            eid = out["archived"][0]["id"]
            self.assertEqual(s.store.obj(eid)["meta"]["verified_scope"], "同比增速两处来源一致; 需求持续性未核实")
            seg = s.store._segment({"id": eid, "kind": "evidence"})
            self.assertEqual(seg["verified_scope"], "同比增速两处来源一致; 需求持续性未核实")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _session(self, engine="mock"):
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        _ruler_root(dd, tmp)
        s = rs.Session.open("TEST", tmp, engine=engine, model="m", tools=rs.MockTools(tmp))
        return rs, ds, tmp, s

    def test_p11_routed_receipt_excludes_later_vintages_and_same_day_bars(self):
        import shutil
        import numpy as np
        import pandas as pd
        import analogue_audit

        _, ds, tmp, session = self._session()
        self.addCleanup(shutil.rmtree, tmp)
        manual = Path("prompts/agent/manuals/understanding/P11-historical-analogue.md")
        shutil.copyfile(ROOT / manual, tmp / manual)
        day = pd.to_datetime(session.ts, utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
        dates = pd.bdate_range(end=day - pd.Timedelta(days=1), periods=900)
        params = {"symbol": "TEST", "condition": "relative_strength_5d"}
        rng = np.random.default_rng(91)
        for sym in ("TEST", "SPY"):
            folder = tmp / "data" / "market_data" / sym
            folder.mkdir(parents=True, exist_ok=True)
            price = 100 * np.exp(np.cumsum(rng.normal(0, .01, len(dates))))
            frame = pd.DataFrame({"high": 1.01 * price, "low": .99 * price, "close": price}, index=dates)
            frame.index.name = "date"
            frame.loc[day] = [9999, 9999, 9999]
            frame.to_csv(folder / f"{sym}_5y_1d_{session.ts}.csv")
            (folder / f"{sym}_5y_1d_20990101T000000Z.csv").write_text("bad", encoding="utf-8")
        request = {"type": "model_call", "model": "P11", "params": params, "issue": "Z1"}
        beat = ds.role_record(60, "Z1", status="need_more")
        beat["request_evidence"] = [request]
        job = session.route(1, 1, {"Zealot": beat})["jobs"][0]
        material = (tmp / job["material"]).read_text(encoding="utf-8")
        self.assertIn(manual.as_posix(), material)
        payload = next(line.removeprefix("P11 deterministic receipt: ") for line in material.splitlines()
                       if line.startswith("P11 deterministic receipt: "))
        measured = json.loads(payload)
        self.assertEqual(measured["last_close_date"], dates[-1].date().isoformat())
        self.assertEqual([r["sessions"] for r in measured["horizons"]], [3, 15])
        self.assertTrue(all(r["validation_dates"] > 0 for r in measured["horizons"]))
        self.assertNotIn("2099", payload)
        output = {"model_call_id": "Z.MC1", "model": "P11", "issue": "Z1", "target": "TEST",
                  "steps": [], "finding": "Test receipt.", "verdict": "mixed",
                  "suggested_evidence_tier": "T2", "payload_text": "See attached machine receipt."}
        session.close_beat(1, 1, [{"job_id": job["job_id"], "status": "verified", "scope": "calculation",
                                   "result": output}], role="Zealot")
        self.assertEqual(session.store.obj("Z.MC1.EV1")["content"]["payload_text"], payload)
        for invalid in ({}, {"symbol": "../TEST", "condition": "relative_strength_5d"}):
            self.assertEqual(analogue_audit.receipt(tmp, session.state, session.ts, invalid)["status"], "not_covered")

    @staticmethod
    def _need_more(ds, cid, score, q):
        r = ds.role_record(score, cid, status="need_more")
        r["request_evidence"] = [{"type": "search", "target_claim": cid, "question": q, "would_change_my_mind": "x"}]
        return r

    def test_debaters_advance_independently_within_a_round(self):
        # 库主 10-01: 辩手各走各的拍, 谁的任务结果齐了谁先拿回执, 不等另一方
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            rz = s.route(1, 1, {"Zealot": self._need_more(ds, "Z1", 60, "zq")})
            rr = s.route(1, 1, {"Reaper": self._need_more(ds, "R1", 40, "rq")})
            zj, rj = rz["jobs"][0]["job_id"], rr["jobs"][0]["job_id"]
            ok = {"question": "q", "simple": "s", "detailed": "d", "source": ["u"], "verdict": "supports"}
            out = s.close_beat(1, 1, [{"job_id": zj, "status": "unverified", "scope": "s", "result": ok}], role="Zealot")
            self.assertEqual([a["job_id"] for a in out["archived"]], [zj])
            self.assertEqual(list(out["pending"]), ["Zealot"])
            self.assertEqual(s.state["open_jobs"], [rj])
            w = s.where()
            self.assertIn("Reaper", w["roles"])
            self.assertIn(rj, json.dumps(w["roles"]["Reaper"], ensure_ascii=False))
            # Zealot 可以先交终稿; Reaper 的作业仍在途
            fin = s.route(1, 2, {"Zealot": _ruler_rec(ds, 60, "Z1")})
            self.assertEqual(fin["finals"], {"Zealot": "R1.Z"})
            self.assertEqual(s.state["open_jobs"], [rj])
            out2 = s.close_beat(1, 1, [{"job_id": rj, "status": "unverified", "scope": "s", "result": ok}], role="Reaper")
            self.assertEqual(list(out2["pending"]), ["Reaper"])
            self.assertEqual(s.state["open_jobs"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_close_beat_reads_result_from_the_job_output_file(self):
        # 一份产物只生成一次: 执行者把 JSON 写到材料给定的 OUTPUT 路径, 核定清单里不再誊抄结果
        import shutil
        rs, ds, tmp, s = self._session(engine="codex")
        try:
            rc = s.route(1, 1, {"Zealot": self._need_more(ds, "Z1", 60, "zq")})
            job = rc["jobs"][0]
            mat = (tmp / job["material"]).read_text(encoding="utf-8")
            self.assertIn("OUTPUT: ", mat)
            self.assertEqual(job["output"], f"data/store/TEST/{s.ts}/_out_job_{job['job_id']}.json")
            self.assertIn(job["output"], mat)
            (tmp / job["output"]).write_text(json.dumps(
                {"question": "zq", "simple": "from file", "detailed": "d", "source": ["u"], "verdict": "supports"}),
                encoding="utf-8")
            out = s.close_beat(1, 1, [{"job_id": job["job_id"], "status": "unverified", "scope": "s"}], role="Zealot")
            self.assertEqual(s.store.obj(out["archived"][0]["id"])["content"]["simple"], "from file")
            # 辩手材料同样给定独占输出路径
            spawn = (tmp / s.spawn_material("Zealot", 1)).read_text(encoding="utf-8")
            self.assertIn(f"OUTPUT: data/store/TEST/{s.ts}/_out_R1_B1_zealot.json", spawn)
            resume = (tmp / out["pending"]["Zealot"]["material"]).read_text(encoding="utf-8")
            self.assertIn(f"OUTPUT: data/store/TEST/{s.ts}/_out_R1_B2_zealot.json", resume)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_close_beat_with_one_result_not_ready_archives_nothing_and_can_be_retried(self):
        """一方有两条在途作业(一条模型调用, 一条搜索取证), 关拍时第二条的结果还没写出来:
        报错且什么都不归档; 等它写完再关, 模型调用拿到的仍是预派的编号, 会话不死。
        (2026-10-02 美国大盘场: 旧逻辑先归档了第一条才报错, 重试时又归档一次, 编号顺延一位, 会话被判失败。)"""
        import shutil
        rs, ds, tmp, s = self._session(engine="codex")
        try:
            beat = ds.role_record(60, "Z1", status="need_more")
            beat["request_evidence"] = [
                {"type": "model_call", "model": "P03", "issue": "Z1", "params": {"symbol": "TEST"}},
                {"type": "search", "target_claim": "Z1", "question": "zq", "would_change_my_mind": "x"}]
            rc = s.route(1, 1, {"Zealot": beat})
            mc = {j["job_id"]: j.get("model_call_id") for j in json.loads((tmp / rc["jobs_file"]).read_text(encoding="utf-8"))}
            runner = dict([j for j in rc["jobs"] if j["kind"] == "runner"][0])
            runner["model_call_id"] = mc[runner["job_id"]]
            scout = [j for j in rc["jobs"] if j["kind"] == "scout"][0]
            (tmp / runner["output"]).write_text(json.dumps(
                {"model_call_id": runner["model_call_id"], "model": "P03", "issue": "Z1", "target": "t", "steps": [],
                 "finding": "f", "verdict": "supports", "suggested_evidence_tier": "T2", "payload_text": ""}),
                encoding="utf-8")
            results = [{"job_id": runner["job_id"], "status": "verified", "scope": "s"},
                       {"job_id": scout["job_id"], "status": "unverified", "scope": "s"}]
            before = len(s.store.rows())
            with self.assertRaises(rs.StepError):
                s.close_beat(1, 1, results, role="Zealot")           # 搜索取证的 OUTPUT 还不存在
            self.assertEqual(len(s.store.rows()), before)             # 什么都没有归档
            self.assertEqual(s.state["status"], "debating")
            (tmp / scout["output"]).write_text(json.dumps(
                {"question": "zq", "simple": "ok", "detailed": "d", "source": ["u"], "verdict": "supports"}),
                encoding="utf-8")
            out = s.close_beat(1, 1, results, role="Zealot")          # 写完再关
            ids = {a["job_id"]: a["id"] for a in out["archived"]}
            self.assertEqual(ids[runner["job_id"]], runner["model_call_id"])   # 仍是预派的编号
            self.assertEqual(s.state["status"], "debating")
            self.assertEqual(out["open_jobs"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_close_beat_retry_after_a_crash_between_archive_and_state_does_not_archive_twice(self):
        """对象已经写进仓、状态还没落就断了: 重试时认出已归档的那条模型调用, 不再铸一个新编号。"""
        import shutil
        rs, ds, tmp, s = self._session(engine="codex")
        try:
            beat = ds.role_record(60, "Z1", status="need_more")
            beat["request_evidence"] = [{"type": "model_call", "model": "P03", "issue": "Z1", "params": {"symbol": "TEST"}}]
            rc = s.route(1, 1, {"Zealot": beat})
            job = dict(rc["jobs"][0])
            job["model_call_id"] = json.loads((tmp / rc["jobs_file"]).read_text(encoding="utf-8"))[0]["model_call_id"]
            content = {"model_call_id": job["model_call_id"], "model": "P03", "issue": "Z1", "target": "t", "steps": [],
                       "finding": "f", "verdict": "supports", "suggested_evidence_tier": "T2", "payload_text": ""}
            (tmp / job["output"]).write_text(json.dumps(content), encoding="utf-8")
            put = s.store.put("model_call", content, by="Zealot", round_=1, beat=1, meta={"status": "verified"})
            self.assertEqual(put["id"], job["model_call_id"])         # 模拟: 对象写了, 状态里它还在途
            n = len(s.store.rows())
            out = s.close_beat(1, 1, [{"job_id": job["job_id"], "status": "verified", "scope": "s"}], role="Zealot")
            self.assertEqual(out["archived"][0]["id"], job["model_call_id"])
            self.assertEqual(len(s.store.rows()), n)                  # 没有再铸一个
            self.assertEqual(s.state["status"], "debating")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tally_refuses_while_jobs_are_in_flight(self):
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            s.state["proactive"] = [{"type": "search", "target_claim": "盲区", "question": "aq", "would_change_my_mind": "x"}]
            rc = s.route(1, 1, {"Zealot": _ruler_rec(ds, 60, "Z1"), "Reaper": _ruler_rec(ds, 40, "R1")})
            self.assertEqual(len(rc["jobs"]), 1)
            with self.assertRaises(rs.StepError) as cm:
                s.tally(1)
            self.assertIn("in flight", str(cm.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_second_rejected_final_stops_the_debate(self):
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            bad = ds.role_record(60, "Z1")
            bad.pop("analysis_text")
            rc = s.route(1, 1, {"Zealot": bad})
            self.assertFalse(rc["ok"])
            with self.assertRaises(rs.StepError) as cm:
                s.route(1, 1, {"Zealot": bad})
            self.assertEqual(cm.exception.code, 20)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merged_manual_id_gets_its_section_and_snapshot(self):
        """合本里的旧编号: 材料指到所在的那一本并点明只做哪一节; 判法要读快照的编号附上技术快照路径。"""
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            def beat(cid, score, reqs):
                r = ds.role_record(score, cid, status="need_more")
                r["request_evidence"] = reqs
                return r
            s.state["raw_data_files"] += chr(10) + "- data/technical/TEST/TEST_technical_x.json   (自身技术快照)"
            rc = s.route(1, 1, {"Reaper": beat("R1", 40, [
                {"type": "model_call", "model": "P03", "issue": "R1", "params": {"symbol": "TEST"}}])})
            text = (tmp / rc["jobs"][0]["material"]).read_text(encoding="utf-8")
            self.assertIn("P02-participation-and-flow.md", text)
            self.assertIn('"Flow attribution" (P03)', text)
            self.assertNotIn("data/technical/", text.split("REQUEST (verbatim)")[0])   # P03 不读快照
            self.assertEqual(rs.MODEL_SECTIONS["P07"], ("P05", "Trend structure"))
            self.assertIn("P06", rs.MODELS_NEED_SNAPSHOT)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_calc_check_shows_only_the_selected_line(self):
        """算术提示三档: 任务材料里只出现当前档的那一句; off 档什么都不出现; 别的档与档位的名字都不露。"""
        import os
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            dst = tmp / rs.CALC_LINES_FILE
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rs.CALC_LINES_FILE, dst)
            remind, always = rs.calc_line("remind", tmp), rs.calc_line("always", tmp)
            self.assertTrue(remind and always and remind != always)
            self.assertEqual(rs.calc_line("off", tmp), "")
            self.assertEqual(s.state["calc_check"], "remind")        # 不点就是提醒档
            self.assertNotIn("本地命令", remind)                      # 提醒档不要求跑命令
            job = {"job_id": "T1", "by": "Zealot", "model_call_id": "Z.MC9",
                   "req": {"type": "model_call", "model": "P01", "issue": "Z1", "params": {"symbol": "TEST"}}}

            def materials(engine):
                s.state["engine"] = engine
                return {"runner": (tmp / s.runner_material(dict(job))).read_text(encoding="utf-8"),
                        "scout": (tmp / s.scout_material({"job_id": "T2", "by": "Reaper", "req": {"question": "q"}}))
                        .read_text(encoding="utf-8"),
                        "debater": (tmp / s.spawn_material("Zealot", 1)).read_text(encoding="utf-8")}
            for engine in ("codex", "cc"):
                for mode in rs.CALC_MODES:
                    s.state["calc_check"] = mode
                    for who, text in materials(engine).items():
                        tail = text.split("ROLE FILE")[-1]
                        want = {"off": "", "remind": remind, "always": always}[mode]
                        if mode == "always" and who == "debater" and engine != "codex":
                            want = remind                              # 没有工具的辩手不拿做不到的要求
                        for line in (remind, always):
                            (self.assertIn if line == want else self.assertNotIn)(line, text, (engine, mode, who))
                        if not want:
                            self.assertNotIn("Arithmetic:", text)
                        self.assertNotIn("calc", tail.lower(), (engine, mode, who))   # 档位的名字不进材料
            # 角色文件里不再常驻算术的话
            for f in ("prompts/agent/roles/executor.md", "prompts/agent/debater.md"):
                txt = (ROOT / f).read_text(encoding="utf-8")
                self.assertNotIn("本地命令", txt)
                self.assertNotIn("算式", txt)
            # 开场: 命令行优先, 其次环境变量, 乱填的值回到默认档
            old = os.environ.pop("EQUIPOISE_CALC_CHECK", None)
            try:
                for arg, env, want in (("always", "off", "always"), ("", "off", "off"), ("", "bogus", "remind")):
                    t2 = Path(tempfile.mkdtemp())
                    try:
                        _load("debate_driver")._mock_root(t2)
                        os.environ["EQUIPOISE_CALC_CHECK"] = env
                        s2 = rs.Session.open("TEST", t2, engine="mock", model="m", tools=rs.MockTools(t2),
                                             calc_check=arg)
                        self.assertEqual(s2.state["calc_check"], want)
                    finally:
                        shutil.rmtree(t2, ignore_errors=True)
            finally:
                os.environ.pop("EQUIPOISE_CALC_CHECK", None)
                if old is not None:
                    os.environ["EQUIPOISE_CALC_CHECK"] = old
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reference_prices_are_read_from_the_snapshot_not_invented(self):
        """本场参考价: 从开场登记的技术快照机械取, 带它自己的行情时刻; 取不到就没有这一条, 不填零。"""
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            rel = s.state["data_files"]["symbol"]["technical"]
            (tmp / rel).write_text("{}", encoding="utf-8")
            self.assertEqual(s._reference_prices(), [])               # 快照里没有价格 → 空, 不造数
            (tmp / rel).write_text(json.dumps({
                "TEST_current_snapshot_last_price": 123.45,
                "TEST_latest_data_timestamp": "2026-10-01T11:51:00+00:00",
                "TEST_session_context_current_session": "pre-market"}), encoding="utf-8")
            got = s._reference_prices()
            self.assertEqual(len(got), 1)
            self.assertEqual((got[0]["symbol"], got[0]["price"], got[0]["as_of"], got[0]["session"], got[0]["source"]),
                             ("TEST", 123.45, "2026-10-01T11:51:00+00:00", "pre-market", rel))
            self.assertEqual(s._meta()["reference_prices"], got)      # 随机械元数据进终报
            for bad in (0, -1, None, "123", True):
                (tmp / rel).write_text(json.dumps({"TEST_current_snapshot_last_price": bad}), encoding="utf-8")
                self.assertEqual(s._reference_prices(), [], bad)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_intraday_bars_get_the_session_time_and_own_symbol_is_allowed(self):
        """盘中档只许读到本场时刻: 机械执行前补上 as_of; 自标的的日线仍按重复拒绝, 盘中档放行。"""
        import shutil
        rs, ds, tmp, s = self._session()
        try:
            self.assertEqual(s._with_as_of({"mode": "bars", "symbol": "X", "interval": "5m"})["as_of"], s.ts)
            self.assertEqual(s._with_as_of({"mode": "bars", "symbol": "X", "interval": "1h", "as_of": "Q"})["as_of"], "Q")
            self.assertNotIn("as_of", s._with_as_of({"mode": "bars", "symbol": "X"}))
            self.assertNotIn("as_of", s._with_as_of({"mode": "trend_fit", "symbol": "X"}))
            own = {"type": "data", "mode": "bars", "symbol": "TEST"}
            self.assertIsNotNone(s._mechanical_gate(own))
            self.assertIsNone(s._mechanical_gate({**own, "interval": "5m"}))
            self.assertIsNotNone(s._mechanical_gate({"type": "data", "mode": "trend_fit", "symbol": "TEST"}))
            job = {"job_id": "T1", "by": "Zealot", "model_call_id": "Z.MC9",
                   "req": {"type": "model_call", "model": "P05", "issue": "Z1", "params": {"symbol": "TEST"}}}
            self.assertIn(f"AS_OF: {s.ts}", (tmp / s.runner_material(job)).read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_model_manual_path_resolves_real_manuals(self):
        rs = _load("run_step")
        for m in ("P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10", "P11"):
            self.assertIsNotNone(rs.model_manual_path(m), m)
        self.assertIsNone(rs.model_manual_path("P99"))
        # 合本(2026-10-02): 旧编号不变, 指到所在的那一本, 且那一本里确有以该编号标出的一节
        self.assertEqual(rs.model_manual_path("P04"), rs.model_manual_path("P07"))
        self.assertEqual(rs.model_manual_path("P02"), rs.model_manual_path("P03"))
        for m, (_file_id, section) in rs.MODEL_SECTIONS.items():
            text = (ROOT / rs.model_manual_path(m)).read_text(encoding="utf-8")
            self.assertIn(f"{section} ({m})", text, f"{m}: 手册里缺「{section} ({m})」这一节")
        for key, rel in rs.MANUALS.items():
            self.assertTrue((ROOT / rel).exists(), f"{key}: {rel}")
        for rel in rs.ROLE_FILES.values():
            self.assertTrue((ROOT / rel).exists(), rel)


def _ruler_root(dd, tmp):
    """The mock root plus TEST's opening price (100, the _ruler_rec scenarios' P0): a 3.4 session freezes it at open as
    odds.reference_price and ingestion refuses every final without one."""
    ts = dd._mock_root(tmp)
    (tmp / "data" / "technical" / "TEST" / f"TEST_technical_{ts}.json").write_text(
        json.dumps({"TEST_current_snapshot_last_price": 100.0}), encoding="utf-8")
    return ts


def _ruler_rec(ds, score, cid, status="final"):
    """A minimal schema-3.4 debater record (Task 4 ingestion: scenario + claims[].effect; one fixed scenario per side,
    so no scenario_updates are needed)."""
    r = ds.role_record(score, cid, status=status)
    for c in r["claims"]:
        c.pop("stake", None)
        c["effect"] = {"if_true": [], "if_false": []}
    r["scenario"] = {"schema": "3.4", "T_days": 15, "P0": 100.0, "execution": {},
                     "nodes": [{"id": "A", "label": "up", "p": 0.5, "P_T": 110.0, "refs": [cid], "events": []},
                               {"id": "B", "label": "down", "p": 0.5, "P_T": 92.0, "refs": [cid], "events": []}]}
    return r


class CloseRoundTest(unittest.TestCase):
    """v3.4 (C5): spawn materials record view_seq; close-round takes closing records only, never arbiter `unresolved`."""

    def _round1(self):
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        _ruler_root(dd, tmp)
        s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))
        s.open_receipt()
        s.route(1, 1, {"Zealot": _ruler_rec(ds, 60, "Z1"), "Reaper": _ruler_rec(ds, 40, "R1")})
        s.tally(1)
        return rs, tmp, s

    @staticmethod
    def _notes(**kw):
        n = {"arbiter_notes": "no ruling this round", "private_note": "nothing moved", "focus": "Z1",
             "proactive_requests": [], "decision": "TERMINATE"}
        n.update(kw)
        return n

    def test_spawns_carry_view_seq(self):
        import shutil
        rs, tmp, s = self._round1()
        try:
            state = json.loads(s.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["spawns"]["R1.Z"]["view_seq"], 0)
            self.assertIn("view_seq: 0\n", (tmp / state["spawns"]["R1.Z"]["material"]).read_text(encoding="utf-8"))
            rc = s.close_round(1, self._notes(decision="CONTINUE"))
            self.assertEqual(rc["decision"], "CONTINUE")
            seqs = {r["id"]: i for i, r in enumerate(s.store.rows(), 1)}
            state = json.loads(s.state_path.read_text(encoding="utf-8"))
            for side in ("Z", "R"):
                self.assertEqual(state["spawns"][f"R2.{side}"]["view_seq"], seqs["NOTES.R1"])
            text = (tmp / rc["spawn"]["Zealot"]).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("ROLE FILE"))
            self.assertIn(f"view_seq: {seqs['NOTES.R1']}\n", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_close_round_rejects_arbiter_unresolved_and_bare_labels(self):
        import shutil
        rs, tmp, s = self._round1()
        try:
            n_rows = len(s.store.rows())
            with self.assertRaises(rs.StepError) as cm:
                s.close_round(1, self._notes(terminal_open={"Z1": {"state": "unresolved"}}))
            self.assertIn("script-only", str(cm.exception))
            with self.assertRaises(rs.StepError) as cm:
                s.close_round(1, self._notes(terminal_open={"Z1": "unresolved"}))
            self.assertIn("script-only", str(cm.exception))
            with self.assertRaises(rs.StepError) as cm:
                s.close_round(1, self._notes(terminal_open={"Z1": "deadlocked", "R1": "deadlocked"}))
            self.assertIn("closing record", str(cm.exception))
            self.assertEqual(len(s.store.rows()), n_rows)            # nothing archived on a rejected close
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_terminate_without_passing_records_continues_and_archives_dispositions(self):
        import shutil
        rs, tmp, s = self._round1()
        try:
            disp = [{"claim": "R1", "claim_rev": "R1.R", "evidence": ["A.EV1"], "ruling": "r"}]
            rec = {"state": "deadlocked", "claim_rev": "R1.R", "remaining": "r", "tried": [], "next": "none: x"}
            rc = s.close_round(1, self._notes(terminal_open={"R1": rec}, dispositions=disp))
            self.assertEqual(rc["decision"], "CONTINUE")
            self.assertIn("R1", rc["override_reason"])
            self.assertEqual(set(rc["closure"]["open"]), {"Z1", "R1"})
            notes = s.store.obj("NOTES.R1")["content"]
            self.assertEqual(notes["dispositions"], disp)
            self.assertEqual(notes["terminal_open"], {"R1": rec})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cap_writes_unresolved(self):
        import shutil
        rs, tmp, s = self._round1()
        try:
            s.state["caps"]["max_rounds"] = 1
            rc = s.close_round(1, self._notes(decision="CONTINUE"))
            self.assertEqual(rc["decision"], "TERMINATE")
            self.assertEqual(set(s.state["unresolved"]), {"Z1", "R1"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LiveClosureTest(unittest.TestCase):
    """End to end on the live store: Z.EV1 (Z's search on R1) returned in round 1, R answers it in round 2."""

    def _two_rounds(self, r2_updates=True):
        rs, dd, ds = _load("run_step"), _load("debate_driver"), _load("debate_store")
        tmp = Path(tempfile.mkdtemp())
        _ruler_root(dd, tmp)
        s = rs.Session.open("TEST", tmp, engine="mock", model="m", tools=rs.MockTools(tmp))
        s.open_receipt()
        beat = _ruler_rec(ds, 60, "Z1", status="need_more")
        beat["request_evidence"] = [{"type": "search", "target_claim": "R1", "question": "peers re-rated?",
                                     "would_change_my_mind": "x"}]
        job = s.route(1, 1, {"Zealot": beat, "Reaper": _ruler_rec(ds, 40, "R1")})["jobs"][0]
        ok = {"question": "q", "simple": "peers re-rated", "detailed": "d", "source": ["u"], "verdict": "refutes"}
        out = s.close_beat(1, 1, [{"job_id": job["job_id"], "status": "verified", "scope": "s", "result": ok}], role="Zealot")
        self.assertEqual(out["archived"][0]["id"], "Z.EV1")
        s.route(1, 2, {"Zealot": _ruler_rec(ds, 60, "Z1")})
        s.tally(1)
        s.close_round(1, CloseRoundTest._notes(decision="CONTINUE"))
        z2 = _ruler_rec(ds, 60, "Z1")
        z2["claims"][0]["status"] = "uncontested"
        r2 = _ruler_rec(ds, 40, "R1")
        r2["claims"][0]["status"] = "deadlocked"
        if r2_updates:
            r2["scenario_updates"] = [{"claim": "R1", "applies": None, "ops": [], "changes": [],
                                       "why": "Z.EV1 shows the re-rating; already inside", "refs": ["Z.EV1"]}]
        s.route(2, 1, {"Zealot": z2, "Reaper": r2})
        s.tally(2)
        return rs, tmp, s

    @staticmethod
    def _record(engaged_by):
        return {"R1": {"state": "deadlocked", "claim_rev": "R1.R", "remaining": "multiple 18x vs 15x",
                       "tried": [{"evidence": "Z.EV1", "engaged_by": engaged_by}],
                       "next": "none: no source distinguishes the multiples"}}

    def test_resume_material_records_view_seq(self):
        import shutil
        rs, tmp, s = self._two_rounds()
        try:
            seqs = {r["id"]: i for i, r in enumerate(s.store.rows(), 1)}
            b2 = s.state["spawns"]["R1.Z.B2"]
            self.assertEqual(b2["view_seq"], seqs["Z.EV1"])
            self.assertLess(b2["at_seq"], seqs["R1.Z"])
            self.assertTrue((tmp / b2["material"]).read_text(encoding="utf-8").startswith(f"view_seq: {seqs['Z.EV1']}\n"))
            self.assertGreaterEqual(s.state["spawns"]["R2.R"]["view_seq"], seqs["Z.EV1"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stall_reference_is_on_the_arbiters_tally_only(self):
        """Neither tree moved in round 2: a full session (round_ref 5) says nothing before round 3; a follow-up session
        (round_ref 3) prints the stall reference with the open claims — in the arbiter's brief, never the debaters' copy."""
        import shutil
        rs, tmp, s = self._two_rounds()
        try:
            self.assertNotIn("Stall reference", s.state["last_tally"]["brief"])
            s.state["caps"]["round_ref"] = 3
            lt = s.tally(2) and s.state["last_tally"]
            line = next(ln for ln in lt["brief"].splitlines() if ln.startswith("[Stall reference"))
            self.assertIn("Z +0.0 · R +0.0", line)
            self.assertIn("outcalls returned this round: 0", line)
            self.assertIn("all of them at once", line)
            self.assertIn("the session can end this round", line)
            self.assertIn("Open claims: R1.", line)
            self.assertNotIn("Stall reference", lt["brief_public"])
            deltas = s._stall_deltas(2, lt["odds"])
            self.assertEqual([(t, L) for t, L, _ in deltas], [("Z", None), ("R", None)])
            self.assertIsNone(s._stall_deltas(1, lt["odds"]))
            import closure_check as cc
            real = cc.claim_revs
            try:                                    # an open claim revised this round is an unanswered front: no line
                cc.claim_revs = lambda records: dict(real(records), R1="R2.R")
                self.assertIsNone(s._stall_line(2, lt["odds"], ["R1"]))
            finally:
                cc.claim_revs = real
            self.assertIsNotNone(s._stall_line(2, lt["odds"], ["R1"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_passing_record_lets_terminate_stand(self):
        import shutil
        rs, tmp, s = self._two_rounds()
        try:
            self.assertEqual(s.state["last_tally"]["open_total"], ["R1"])
            rc = s.close_round(2, CloseRoundTest._notes(terminal_open=self._record(["R2.R"])))
            self.assertEqual(rc["decision"], "TERMINATE", rc)
            self.assertIsNone(rc["override_reason"])
            self.assertEqual(rc["closure"]["closable"], ["R1"])
            self.assertEqual(s.state["deadlocked"], ["R1"])
            self.assertEqual(s.state["unresolved"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disposition_in_this_rounds_notes_closes(self):
        import shutil
        rs, tmp, s = self._two_rounds(r2_updates=False)
        try:
            rc = s.close_round(2, CloseRoundTest._notes(decision="TERMINATE", terminal_open=self._record(["R2.R"])))
            self.assertEqual(rc["decision"], "CONTINUE")                   # R2.R did not engage Z.EV1
            self.assertIn("Z.EV1", rc["override_reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        rs, tmp, s = self._two_rounds(r2_updates=False)
        try:
            disp = [{"claim": "R1", "claim_rev": "R1.R", "evidence": ["Z.EV1"], "ruling": "re-rating shown, persistence not"}]
            rc = s.close_round(2, CloseRoundTest._notes(terminal_open=self._record(["NOTES.R2"]), dispositions=disp))
            self.assertEqual(rc["decision"], "TERMINATE", rc)
            self.assertEqual(s.store.obj("NOTES.R2")["content"]["dispositions"], disp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_claim_closed_in_a_continue_round_stays_closed(self):
        import shutil
        rs, tmp, s = self._two_rounds()
        try:
            ds = _load("debate_store")
            rc = s.close_round(2, CloseRoundTest._notes(decision="CONTINUE", terminal_open=self._record(["R2.R"])))
            self.assertEqual(rc["decision"], "CONTINUE")
            self.assertIn("R1", s.state["closed_records"])
            z3 = _ruler_rec(ds, 60, "Z1")
            z3["claims"][0]["status"] = "uncontested"
            r3 = _ruler_rec(ds, 40, "R1")
            r3["claims"][0]["status"] = "deadlocked"
            s.route(3, 1, {"Zealot": z3, "Reaper": r3})
            self.assertEqual(s.tally(3)["hard_stop"]["open_claims"], [])
            rc = s.close_round(3, CloseRoundTest._notes(decision="TERMINATE"))
            self.assertEqual(rc["decision"], "TERMINATE")
            self.assertEqual(s.state["deadlocked"], ["R1"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
