"""Run the group publication regression in a disposable local PostgreSQL cluster."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import secrets
import socket
import subprocess
import sys
import tempfile
from threading import Barrier
from unittest.mock import patch

import psycopg2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from datalayer.pg import lowfreq_reader as reader
from scripts import pg_store_debate as store


def exercise(cfg):
    report = json.loads((ROOT / "docs/handoff/fixtures/group-report/group.json").read_text(encoding="utf-8"))
    group, ts = report["symbol"], report["market_data_timestamp_utc"]
    for member in report["members"]:
        p0 = member["composite_score"] + 100
        member["scenario"] = {"P0": p0, "T_days": 15, "nodes": [
            {"id": name, "p": p, "P_T": price} for name, p, price in
            (("down", 0.3, p0 * 0.94), ("flat", 0.4, p0), ("up", 0.3, p0 * 1.06))]}
    con = psycopg2.connect(**cfg)
    con.autocommit = True
    with con, con.cursor() as cur:
        cur.execute(store._CREATE_SQL)
    with con.cursor() as cur:
        cur.execute((ROOT / "datalayer/pg/group_sessions.sql").read_text(encoding="utf-8"))
    def solo(symbol, stamp, score=50):
        return {"symbol": symbol, "market_data_timestamp_utc": stamp, "composite_score": score, "lang": "en"}

    def race(*jobs):
        barrier = Barrier(len(jobs))
        def publish(job):
            barrier.wait(timeout=15)
            return store.save_debate(*job)
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            return list(pool.map(publish, jobs))

    older = solo("MU", "20261117T142959Z")
    older["report_generated_at"] = "20261121T142959Z"  # late backfill of an older market snapshot
    assert store.save_debate("MU", older)
    assert race((group, report), (group, report)) == [True, True]
    with con.cursor() as cur:
        cur.execute("""UPDATE debates SET report_i18n='{"zh":{"statement":"translated MU"}}'
                       WHERE symbol='MU' AND group_symbol IS NOT NULL""")
        cur.execute("SELECT count(*) FROM debates WHERE report ? 'debate_history'")
        assert cur.fetchone() == (0,)
    assert store.save_debate(group, report)
    brief = runpy.run_path(str(ROOT / "skills/equipoise-dispatch/scripts/pre_round_brief.py"))
    from scripts.divergence import divergence
    with con.cursor() as cur:
        cur.execute(brief["_LATEST_SQL"].format(score="composite_score"), (["AMD", "MU"],))
        rows = {r[0]: r[7] for r in cur.fetchall()}
        for sym, price, expected in (("AMD", 161, "inside"), ("MU", 130, "dead")):
            assert divergence(rows[sym]["scenario"], 2, 1, price)["level"] == expected
    assert store.save_debate("MU", solo("MU", "20261119T142959Z", 73))
    assert not store.save_debate("MU", solo("MU", ts))
    altered = deepcopy(report)
    altered["members"][0]["composite_score"] += 1
    assert not store.save_debate(group, altered)
    altered = deepcopy(report)
    altered["reference_prices"][0]["as_of"] = "20261118T143000Z"
    assert not store.save_debate(group, altered)
    second = deepcopy(report)
    second["symbol"] = "GROUP_SECOND"
    second["members"].insert(0, {"symbol": "AA", "composite_score": 32})
    assert not store.save_debate(second["symbol"], second)
    assert not reader.get_debate_latest("AA")
    with con.cursor() as cur:
        cur.execute("SELECT count(*) FROM debate_group_sessions")
        assert cur.fetchone() == (1,)
    mu = reader.get_debate_report_row(group, ts, member_symbol="MU")
    amd = reader.get_debate_report_row("AMD", ts)
    assert mu["report"]["debate_history"] == amd["report"]["debate_history"] == report["debate_history"]
    assert mu["report_i18n"] == {"zh": {"statement": "translated MU"}} and amd["report_i18n"] == {}
    assert not reader.get_debate_report_row(second["symbol"], ts, member_symbol="MU")
    assert reader.get_debate_latest("MU", ts)["report"]["scenario"]["P0"] == 142
    batch = reader.get_debate_latest_batch(["MU", "AMD"])
    assert batch["MU"]["composite_score"] == 73 and batch["AMD"]["report"]["scenario"]["P0"] == 161
    assert [r["composite_score"] for r in reader.list_debates("MU")] == [73, 42, 50]
    assert reader.get_debate_report("AMD", ts) == amd["report"]
    later = "20261120T142959Z"
    report["market_data_timestamp_utc"] = later
    result = race(("MU", solo("MU", later)), (group, report))
    assert sorted(result) == [False, True], result
    with con.cursor() as cur:
        cur.execute("SELECT count(*) FROM debates WHERE as_of=%s AND group_symbol IS NOT NULL", (later,))
        assert cur.fetchone() == ((2 if result[1] else 0),)
    second = deepcopy(report)
    second["symbol"] = "GROUP_OTHER"
    second["members"].reverse()
    for hearing in (report, second):
        hearing["market_data_timestamp_utc"] = "20261121T142959Z"
    assert sorted(race((group, report), (second["symbol"], second))) == [False, True]
    with con.cursor() as cur:
        for sql in ("DELETE FROM debate_group_sessions", "UPDATE debate_group_sessions SET report=report",
                    "DELETE FROM debates WHERE group_symbol IS NOT NULL",
                    "UPDATE debates SET composite_score=0 WHERE group_symbol IS NOT NULL"):
            try:
                cur.execute(sql)
            except psycopg2.errors.RaiseException:
                continue
            raise AssertionError("archive mutation accepted")
    con.close()
    print("PASS: atomic replay, solo/group race, collision rollback, time-bounded history, translation and hydration")


@contextmanager
def temporary_pg(pg_bin):
    with tempfile.TemporaryDirectory(prefix="equipoise-group-test-") as temp:
        root = Path(temp)
        data = str(root / "pgdata")
        password = secrets.token_hex(24)
        pwfile = root / "password"
        pwfile.write_text(password, encoding="ascii")
        subprocess.run([str(pg_bin / "initdb"), "-D", data, "-U", "group_test", "-A", "scram-sha-256",
                        "--pwfile", str(pwfile), "--no-locale", "-E", "UTF8"], check=True)
        pwfile.unlink()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ctl = [str(pg_bin / "pg_ctl"), "-D", data, "-w"]
        subprocess.run(ctl + ["-l", str(root / "postgres.log"), "-o", f"-h 127.0.0.1 -p {port}", "start"], check=True)
        try:
            cfg = dict(host="127.0.0.1", port=port, dbname="postgres", user="group_test", password=password)
            env = {"EQUIPOISE_PG_" + ("DB" if k == "dbname" else k.upper()): str(v) for k, v in cfg.items()}
            with patch.dict(os.environ, env):
                yield cfg
        finally:
            subprocess.run(ctl + ["stop", "-m", "fast"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", required=True, type=Path)
    with temporary_pg(parser.parse_args().pg_bin.resolve()) as cfg:
        exercise(cfg)
