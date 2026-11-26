import unittest
from datetime import datetime, timezone

from scripts.duty_record import verify_decision
from workflows.symbol_universe import dispatch_jobs, load_symbol_config


class GroupDutyTest(unittest.TestCase):
    def test_weighted_job_dispatch(self):
        jobs = dispatch_jobs(load_symbol_config())
        members = [s for job in jobs.values() for s in job["members"]]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(len(members), 89)
        chips = jobs["GROUP_US_CHIPS"]
        self.assertEqual(chips["cost_units"], 2.75)
        status = dict(dispatch_jobs=jobs, recommended_harness="codex2",
                      markets={"us": {"market": {"in_window": True}, "due": [{"symbol": "MU"}]}},
                      usage={"codex2": {"units_left": 3.50, "available": True}})
        decision = dict(harness="codex2", launch=[dict(market="us", units=["MU", "GROUP_US_CHIPS", "GROUP_US_CHIPS", "TSLA"])])
        def check(slots):
            return verify_decision(status, decision, "codex2", slots, datetime.now(timezone.utc), 24, {},
                                   {"us": set(members)})
        result = check(4)
        self.assertEqual([(j["flag"], j["key"]) for j in result["launch"]], [("--group", "GROUP_US_CHIPS")])
        self.assertFalse(result["skips"])
        status["usage"]["codex2"]["units_left"] = 5
        self.assertEqual(len(check(1)["launch"]), 1)
        status["running"] = [{"symbols": ["SMH"]}]
        self.assertEqual([j["key"] for j in check(4)["launch"]], ["TSLA"])
        status["markets"]["us"]["market"]["in_window"] = False
        self.assertFalse(check(4)["launch"])
