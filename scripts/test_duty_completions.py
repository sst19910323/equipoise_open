import json
import subprocess
import unittest
from unittest.mock import patch

from workflows.duty_completions import UNIT_FAILED, UNIT_INACTIVE, recent_completions


class DutyCompletionTest(unittest.TestCase):
    @patch("workflows.duty_completions.subprocess.run")
    def test_reused_unit_preserves_each_exit_and_event_time(self, run):
        unit = "equipoise-codex-duty-us-GROUP_US_CHIPS.service"
        end = {"UNIT": unit, "MESSAGE_ID": UNIT_INACTIVE, "INVOCATION_ID": "second",
               "__REALTIME_TIMESTAMP": "1800000000123456"}
        failure = dict(end, MESSAGE_ID=UNIT_FAILED, INVOCATION_ID="first",
                       UNIT_RESULT="exit-code", __REALTIME_TIMESTAMP="1799999999000000")
        run.return_value = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, [end, failure])), "")
        events = recent_completions()
        self.assertEqual([(r["invocation_id"], r["state"], r["reason"]) for r in events],
                         [("second", "ended", None), ("first", "failed", "exit-code")])
        self.assertEqual(events[0]["at"], "2027-01-15T08:00:00.123456+00:00")
        self.assertEqual(events[0]["key"], "GROUP_US_CHIPS")

    @patch("workflows.duty_completions.subprocess.run")
    def test_no_exits_and_journal_unavailable_are_distinct(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "-- No entries --\n", "")
        self.assertEqual(recent_completions(), [])
        run.side_effect = subprocess.TimeoutExpired("journalctl", 3)
        self.assertIsNone(recent_completions())
