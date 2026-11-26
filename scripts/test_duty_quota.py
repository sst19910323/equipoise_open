"""Quota regression tests: no real accounts, model turns, PG or IBKR."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import duty_quota as q, duty_status as ds

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
RESET = int(NOW.timestamp()) + 86400


def response(used=2):
    return {"ordinaryUsageAllowed": True, "rateLimits": {
        "planType": "pro", "primary": {"usedPercent": used,
        "windowDurationMins": 10080, "resetsAt": RESET}}}


class QuotaTests(unittest.TestCase):
    def test_fresh_reset_replaces_exhausted_session(self):
        fresh = q.normalize_limits(response(2), NOW)
        with patch.object(ds, "CHOICES", ["codex1"]), \
             patch.object(ds, "live_quota", return_value=fresh), \
             patch.object(ds, "last_rate_limits", side_effect=AssertionError("old logs used")), \
             patch.object(ds, "debates_since", return_value=0), \
             patch.object(ds, "calibrated_cost", return_value={"cost_pct": 0.64}), \
             patch.object(ds, "ledger_append"):
            usage = ds.usage_block(None, {"usage_stop_used_pct": {"codex1": 85}}, NOW)
        self.assertTrue(usage["codex1"]["available"])
        self.assertEqual(ds.pick_harness(usage, set(), ["codex1"]), "codex1")

    def test_unknown_does_not_fall_back_or_claim_exhaustion(self):
        with patch.object(q, "read_account_limits", side_effect=TimeoutError):
            u = q.live_quota("fake", "home", "codex3")
        self.assertFalse(u["available"])
        self.assertEqual(u["quota_state"], "unknown")
        self.assertIn("query failed", q.unavailable_summary({"codex3": u}))

    def test_weekly_may_be_secondary_and_short_window_blocks(self):
        r = response()
        r["rateLimits"]["secondary"] = r["rateLimits"].pop("primary")
        r["rateLimits"]["primary"] = {"usedPercent": 100,
                                      "windowDurationMins": 300, "resetsAt": RESET}
        u = q.normalize_limits(r, NOW)
        self.assertEqual(u["used_percent"], 2)
        self.assertEqual(q.availability(u, 85), (False, "quota exhausted"))

    def test_threshold_and_backend_denial(self):
        u = q.normalize_limits(response(90), NOW)
        self.assertFalse(q.availability(u, 85)[0])
        r = response(0)
        r["ordinaryUsageAllowed"] = False
        self.assertFalse(q.availability(q.normalize_limits(r, NOW), 85)[0])
        r["ordinaryUsageAllowed"] = None
        with self.assertRaises(ValueError):
            q.normalize_limits(r, NOW)

    def test_invalid_missing_or_expired_data_never_implies_reset(self):
        for change in ({"resetsAt": int(NOW.timestamp())}, {"usedPercent": float("nan")},
                       {"usedPercent": -1}, {"windowDurationMins": 300}, {"resetsAt": None}):
            with self.subTest(change=change):
                r = response()
                r["rateLimits"]["primary"].update(change)
                with self.assertRaises((ValueError, TypeError)):
                    q.normalize_limits(r, NOW)

    def test_transport_identity_notification_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "server"
            pidfile = Path(tmp) / "pid"
            exe.write_text(f'''#!{sys.executable}
import json, os, sys
open({str(pidfile)!r}, 'w').write(str(os.getpid()))
assert os.environ['CODEX_HOME'] == {tmp!r}
assert os.environ['EQUIPOISE_HARNESS'] == 'test-only'
for line in sys.stdin:
    r = json.loads(line)
    if r['method'] == 'initialized': continue
    assert r['method'] in ('initialize', 'account/rateLimits/read')
    print(json.dumps({{'method':'notification'}}), flush=True)
    print(json.dumps({{'id':r['id'], 'result':{{}} if r['id']==1 else {response()!r}}}), flush=True)
''')
            exe.chmod(0o700)
            self.assertEqual(q.read_account_limits(str(exe), tmp, "test-only"), response())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
            exe.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
            with self.assertRaises(TimeoutError):
                q.read_account_limits(str(exe), tmp, "test-only", timeout=0.1)


if __name__ == "__main__":
    unittest.main()
