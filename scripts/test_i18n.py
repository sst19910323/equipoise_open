"""报告多语言层(datalayer/pg/i18n.py 服务器 CC 2026-10-24 版 + 主工程 10-25 的复盘扩展)的纯函数: 原文语言判定、覆盖、复盘行本地化、
翻译脚本的抽取 / 回填 / 按引擎分流。不连库不调模型。"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


i18n = _load("i18n", "datalayer/pg/i18n.py")

REPORT_EN = {"lang": "en", "statement": "Light entry at 100.", "composite_score": 57,
             "short_term_path": {"direction": "上行", "target": "105", "invalid": "95", "horizon_days": 5, "rationale": "MA50 held."},
             "debate_summary": "Z1 settled for; R2 conceded.", "debate_history": [{"round": "回合1"}]}
OVERLAY_ZH = {"zh": {"statement": "现价轻做一笔。", "debate_summary": "Z1 结算成立; R2 认账。", "dissent": "早先译过的, 读时不用",
                     "short_term_path": {"direction": "上行", "target": "105", "invalid": "95", "horizon_days": 5, "rationale": "MA50 守住。"},
                     "_meta": {"engine": "claude"}}}


class LocalizeTest(unittest.TestCase):
    def test_detect_lang(self):
        self.assertEqual(i18n.detect_lang(REPORT_EN), "en")
        self.assertEqual(i18n.detect_lang({"statement": "现价轻做"}), "zh")
        self.assertEqual(i18n.detect_lang({"statement": "Buy now"}), "en")
        self.assertEqual(i18n.detect_lang({}), "zh")

    def test_overlay_and_fallback(self):
        doc, served = i18n.localize_report(REPORT_EN, "en", OVERLAY_ZH, "zh")
        self.assertEqual(served, "zh"); self.assertEqual(doc["statement"], "现价轻做一笔。")
        self.assertEqual(doc["short_term_path"]["direction"], "上行")          # 协议值原样
        self.assertEqual(doc["debate_history"], REPORT_EN["debate_history"])   # 辩论过程不翻
        self.assertEqual(doc["debate_summary"], REPORT_EN["debate_summary"])   # 10-24 晚服务器口径: 议题结算段算辩论附属, 不翻
        self.assertNotIn("dissent", doc)                                       # 覆盖层里多出来的键(早先译过的 dissent)不用
        self.assertNotIn("_meta", doc); self.assertEqual(REPORT_EN["statement"], "Light entry at 100.")
        doc, served = i18n.localize_report(REPORT_EN, "en", {}, "zh")
        self.assertEqual(served, "en"); self.assertIs(doc, REPORT_EN)
        doc, served = i18n.localize_report(REPORT_EN, "en", OVERLAY_ZH, "en")
        self.assertEqual(served, "en")
        meta = i18n.lang_meta("en", OVERLAY_ZH, "zh")
        self.assertEqual(meta, {"lang_served": "zh", "lang_source": "en", "lang_available": ["en", "zh"]})

    def test_translatable_fields(self):
        for k in ("debate_summary", "debate_history", "dissent", "evidence", "model_calls"):
            self.assertNotIn(k, i18n.TRANSLATABLE_FIELDS)
        self.assertIn("statement", i18n.TRANSLATABLE_FIELDS)
        self.assertEqual(i18n.POSTCHECK_TRANSLATABLE_FIELDS, ("verdict",))


class PostcheckLocalizeTest(unittest.TestCase):
    def test_localize_items(self):
        pps = _load("pps", "datalayer/pg/postcheck_pg_store.py")
        items = [{"symbol": "AAA", "verdict": "Hit.", "diagnosis": "d", "lang": "en",
                  "report_i18n": {"zh": {"verdict": "踩中。", "diagnosis": "不该被用的", "_meta": {}}}},
                 {"symbol": "BBB", "verdict": "踩中(旧中文行)", "diagnosis": "x"}]       # 列未迁移: 没 lang 键
        out = pps.localize_items(items, "zh")
        self.assertEqual(out[0]["verdict"], "踩中。"); self.assertEqual(out[0]["diagnosis"], "d")
        self.assertEqual(out[0]["lang_served"], "zh"); self.assertEqual(out[0]["lang_available"], ["en", "zh"]); self.assertNotIn("report_i18n", out[0])
        self.assertEqual(out[1]["verdict"], "踩中(旧中文行)"); self.assertEqual(out[1]["lang_source"], "zh")
        out_en = pps.localize_items([dict(items[0], report_i18n={})], "zh")
        self.assertEqual(out_en[0]["lang_served"], "en")   # 没译文回原文


class TranslateScriptTest(unittest.TestCase):
    def setUp(self):
        try:
            self.tr = _load("trs", "scripts/translate_reports.py")
        except Exception as e:          # 本地没装 psycopg2 时跳过
            self.skipTest(f"translate_reports 不可导入: {e}")

    def test_extract_put_back_keep_paths(self):
        sub, items = self.tr.extract(REPORT_EN, "en")
        paths = [".".join(str(p) for p in path) for path, _ in items]
        self.assertIn("statement", paths); self.assertIn("short_term_path.rationale", paths); self.assertNotIn("debate_summary", paths)
        self.assertNotIn("short_term_path.direction", paths); self.assertNotIn("short_term_path.target", paths)
        out = {str(i): f"中{i}" for i in range(len(items))}
        doc = self.tr.put_back(sub, items, out)
        self.assertEqual(doc["short_term_path"]["direction"], "上行"); self.assertEqual(doc["statement"], "中" + str(paths.index("statement")))

    def test_last_step_staging_round_trip(self):
        """--extract builds the staging file the Translator fills; validate_translations gates --load; put_back restores the tree."""
        st = self.tr.build_extract(REPORT_EN, "en", "zh", "debates", "nvda", "2026-10-23T23:59:00Z", source="local")
        self.assertEqual(st["symbol"], "NVDA"); self.assertEqual(st["to"], "zh"); self.assertEqual(st["translations"], {})
        self.assertIn("statement", st["paths"].values()); self.assertNotIn("debate_summary", st["paths"].values())
        self.assertEqual(self.tr.validate_translations(st), ["untranslated ids: " + str(list(st["items"]))])
        st["translations"] = {k: f"译{k}" for k in st["items"]}
        self.assertEqual(self.tr.validate_translations(st), [])
        st["translations"]["999"] = "x"
        self.assertTrue(any("unknown ids" in p for p in self.tr.validate_translations(st)))
        del st["translations"]["999"]
        sub, items = self.tr.extract(REPORT_EN, "en", self.tr.i18n.TRANSLATABLE_FIELDS)
        doc = self.tr.put_back(sub, items, {k: st["translations"][k] for k in st["items"]})
        self.assertTrue(doc["statement"].startswith("译")); self.assertEqual(doc["short_term_path"]["direction"], "上行")
        self.assertEqual(self.tr._src_hash(REPORT_EN, "en", self.tr.i18n.TRANSLATABLE_FIELDS), st["src_hash"])
        # the hash covers the translatable strings only: PG jsonb re-serializing numbers (57 vs 57.0) must not break --load
        self.assertEqual(self.tr._src_hash(dict(REPORT_EN, composite_score=57.0), "en", self.tr.i18n.TRANSLATABLE_FIELDS), st["src_hash"])
        self.assertNotEqual(self.tr._src_hash(dict(REPORT_EN, statement="changed"), "en", self.tr.i18n.TRANSLATABLE_FIELDS), st["src_hash"])
        # --load matches by path, not by id: PG jsonb returns nested keys in a different order than the Analysis file
        stp = dict(REPORT_EN, short_term_path=dict(reversed(list(REPORT_EN["short_term_path"].items()))), statement=REPORT_EN["statement"])
        pg_like = {k: stp[k] for k in sorted(stp, key=lambda k: (len(k), k))}
        doc, err = self.tr.apply_staging(pg_like, st, self.tr.i18n.TRANSLATABLE_FIELDS)
        self.assertIsNone(err)
        sid = next(i for i, p in st["paths"].items() if p == "statement"); rid = next(i for i, p in st["paths"].items() if p == "short_term_path.rationale")
        self.assertEqual(doc["statement"], st["translations"][sid]); self.assertEqual(doc["short_term_path"]["rationale"], st["translations"][rid])
        self.assertEqual(doc["short_term_path"]["direction"], "上行")
        self.assertEqual(self.tr.apply_staging(dict(pg_like, composite_score=57.0, extra_key="added after"), st, self.tr.i18n.TRANSLATABLE_FIELDS)[1], None)  # non-translatable changes are fine
        _, err = self.tr.apply_staging(dict(pg_like, statement="edited"), st, self.tr.i18n.TRANSLATABLE_FIELDS)
        self.assertIn("text differs", err or "")
        en = self.tr.build_extract({"verdict": "踩中。"}, "zh", "en", "postcheck", "AAA", "2026-10-23T00:00:00Z", dt=3, ruler="R24")
        en["translations"] = {"0": "Hit 踩中"}
        self.assertTrue(any("CJK left" in p for p in self.tr.validate_translations(en)))

    def test_staging_files_for_report(self):
        """--load-report: the outer runner finds the Translator's files by the Analysis file's own timestamp."""
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            dbg = tmp / "data" / "debate" / "NVDA"; dbg.mkdir(parents=True)
            stg = tmp / "data" / "i18n_staging"; stg.mkdir(parents=True)
            (dbg / "NVDA_Analysis_20261026T011600Z.json").write_text("{}", encoding="utf-8")
            (dbg / "NVDA_Analysis_20261020T140300Z.json").write_text("{}", encoding="utf-8")
            (stg / "debates_NVDA_20261026T011600Z_zh.json").write_text("{}", encoding="utf-8")
            (stg / "debates_NVDA_20261026T011600Z_ja.json").write_text("{}", encoding="utf-8")
            (stg / "debates_NVDA_20261020T140300Z_zh.json").write_text("{}", encoding="utf-8")
            (stg / "debates_NVDAX_20261026T011600Z_zh.json").write_text("{}", encoding="utf-8")
            old_root, old_stg = self.tr.ROOT, self.tr.STAGING_DIR
            self.tr.ROOT, self.tr.STAGING_DIR = tmp, stg
            try:
                analysis, files = self.tr.staging_files_for_report("nvda")
                self.assertEqual(analysis.name, "NVDA_Analysis_20261026T011600Z.json")       # newest
                self.assertEqual([f.name for f in files], ["debates_NVDA_20261026T011600Z_ja.json", "debates_NVDA_20261026T011600Z_zh.json"])
                analysis, files = self.tr.staging_files_for_report("NVDA", dbg / "NVDA_Analysis_20261020T140300Z.json")
                self.assertEqual([f.name for f in files], ["debates_NVDA_20261020T140300Z_zh.json"])
                self.assertEqual(self.tr.staging_files_for_report("AAPL"), (None, []))
            finally:
                self.tr.ROOT, self.tr.STAGING_DIR = old_root, old_stg
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_postcheck_fields_and_auto_engine(self):
        sub, items = self.tr.extract({"verdict": "Hit; filled.", "diagnosis": "long"}, "en", self.tr.i18n.POSTCHECK_TRANSLATABLE_FIELDS)
        self.assertEqual([p for p, _ in items], [("verdict",)])
        self.assertEqual(self.tr._auto_engine({"report": {"signatures": {"zealot": "claude-opus-5-5"}}}, "debates"), "claude")
        self.assertEqual(self.tr._auto_engine({"report": {"signatures": {"zealot": "gpt-6.1-sol"}}}, "debates"), "codex")
        self.assertEqual(self.tr._auto_engine({"reckoner": "gpt-6-astra"}, "postcheck"), "codex")
        self.assertEqual(self.tr._auto_engine({"reckoner": "claude-opus-5-5"}, "postcheck"), "claude")


if __name__ == "__main__":
    unittest.main()
