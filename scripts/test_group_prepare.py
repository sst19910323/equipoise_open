"""Linux group queue regression. No IBKR connection, PG or model calls."""
import multiprocessing
from pathlib import Path
import signal
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


def prepare_worker(directory, names, delay, ready):
    import prepare_debate_data as prep

    async def fetch(needs, *args, **kwargs):
        for symbol in needs:
            with (directory / "trace").open("a") as trace:
                print(symbol, file=trace)
            time.sleep(delay)
        return False  # End at the market gate without further data calls.

    def config():
        ready.set()
        return {}

    helpers = types.SimpleNamespace(load_helpers=lambda: ({}, {}, None))
    backend = types.SimpleNamespace(_fetch_needs=lambda name, cfg: [name], _fetch_us_news=None)
    with patch.dict(sys.modules, {"horizon_sentinel": helpers, "run_one_debate": backend}), \
         patch.object(prep, "_IBKR_PREPARE_LOCK", directory / "lock"), \
         patch.object(prep, "_load_symbols_cfg", config), patch.object(prep, "_fetch_market", fetch), \
         patch.object(sys, "argv", ["prepare", "--active-timeout", "1", *names]):
        raise SystemExit(prep.main())


@unittest.skipUnless(sys.platform.startswith("linux"), "production flock and SIGALRM")
class GroupQueueTest(unittest.TestCase):
    def test_queue_wait_does_not_consume_budget_and_batches_stay_contiguous(self):
        import fcntl
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with (directory / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                jobs = []
                for names in (["A1", "A2"], ["B1", "B2"]):
                    ready = ctx.Event()
                    p = ctx.Process(target=prepare_worker, args=(directory, names, .04, ready))
                    p.start()
                    jobs.append(p)
                    self.assertTrue(ready.wait(8))
                time.sleep(1.4)
                fcntl.flock(lock, fcntl.LOCK_UN)
            for p in jobs:
                p.join(8)
                self.assertEqual(p.exitcode, 10)
            self.assertIn((directory / "trace").read_text().splitlines(),
                          (["A1", "A2", "B1", "B2"], ["B1", "B2", "A1", "A2"]))

    def test_active_timeout_releases_queue(self):
        import fcntl
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            p = ctx.Process(target=prepare_worker, args=(directory, ["SLOW"], 10, ctx.Event()))
            p.start()
            p.join(8)
            self.assertEqual(p.exitcode, -signal.SIGALRM)
            with (directory / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
