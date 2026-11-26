import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ensure_report_translations as repair


class EnsureTranslationTest(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 11, 19, 15, 45, tzinfo=timezone.utc)
        self.report = {"statement": "Wait for the next close."}
        self.row = {"lang": "en", "report": self.report, "present": False}
        self.cur = Mock(rowcount=1)
        self.cur.fetchone.return_value = self.row
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.staging = Path(temp.name)
        self.enterContext(patch.object(repair.tr, "STAGING_DIR", self.staging))
        self.translate = self.enterContext(patch.object(repair.tr, "translate_one",
                                                       return_value=({"statement": "等待下一次收盘。"}, 1)))

    def stage(self, *, prefix="", translator="gpt-6-luna"):
        doc = repair.tr.build_extract(self.report, "en", "zh", "debates", "XLV", self.as_of)
        doc.update(translator=translator, translations={k: "等待下一次收盘。" for k in doc["items"]})
        (self.staging / (prefix + "debates_XLV_20261119T154500Z_zh.json")).write_text(
            json.dumps(doc), encoding="utf-8")

    def run_repair(self):
        return repair.ensure_overlay(self.cur, "XLV", self.as_of, "zh")

    def test_existing_overlay_skips_model_and_update(self):
        self.row["present"] = True
        self.assertEqual(self.run_repair(), "already available")
        self.translate.assert_not_called()
        self.assertFalse(any("UPDATE debates" in call.args[0] for call in self.cur.execute.call_args_list))

    def test_valid_cheap_staging_is_reused(self):
        self.stage()
        self.assertEqual(self.run_repair(), "loaded verified staging")
        self.translate.assert_not_called()

    def test_quarantine_and_wrong_model_are_not_reused(self):
        self.stage(prefix="unverified_")
        self.stage(translator="gpt-6.2-sol")
        self.assertEqual(self.run_repair(), "translated by gpt-6-luna")
        self.translate.assert_called_once_with("codex", self.report, "en", "zh")

    def test_changed_source_after_generation_does_not_report_success(self):
        self.cur.rowcount = 0
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.run_repair()
        sql = self.cur.execute.call_args_list[-2].args[0]
        self.assertIn("report=%s::jsonb", sql)
        self.assertIn("NOT (report_i18n ? %s)", sql)
