#!/usr/bin/env python3
"""build_pack 的单测: 迷你 src 树, 不连 PG(pg=False), 不跑真 manifest 自检(check=False)。"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import build_pack as B  # noqa: E402

AS_OF = "20261009T133700Z"


class BuildPackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src, self.dest = self.tmp / "src", self.tmp / "drill"
        s = self.src
        # 代码: 真 evidence_quant + 一个报告自身 ROOT 的小脚本(照 run_step 的 resolve 写法)
        (s / "scripts").mkdir(parents=True)
        shutil.copy(REPO / "evidence_quant.py", s / "evidence_quant.py")
        (s / "scripts" / "where.py").write_text(
            "from pathlib import Path\nprint(Path(__file__).resolve().parent.parent)\n", encoding="utf-8")
        (s / "config").mkdir()
        (s / "config" / "symbols.yaml").write_text(
            "symbol_contracts:\n  GENERAL:\n    region: US\n    lenses:\n"
            "    - {symbol: SPY, weight: 0.5}\n    - {symbol: QQQ, weight: 0.5}\n", encoding="utf-8")
        (s / "venv").mkdir()
        (s / "docs").mkdir()
        (s / "docs" / "big.md").write_text("x", encoding="utf-8")
        for private_dir in (".local", "drill_sealed"):
            (s / private_dir).mkdir()
            (s / private_dir / "review.txt").write_text("private review material", encoding="utf-8")
        (s / "AGENTS.md").write_text(f"cd {s}\nread {s}/CLAUDE.md and {s}_drill/x\n", encoding="utf-8")
        # 原场对象仓
        store = s / "data" / "store" / "GENERAL" / AS_OF
        store.mkdir(parents=True)
        (store / "_meta.json").write_text(json.dumps({"data_files": {
            "macro": {"news_general_us": "data/news/GENERAL/GENERAL_news_20261009T134047Z.json"},
            "config": "config/symbols.yaml",
            "lenses": {"SPY": "data/technical/SPY/SPY_technical_20261009T133700Z.json",
                       "QQQ": "data/technical/QQQ/QQQ_technical_20261009T133700Z.json"}}}), encoding="utf-8")
        for rel in ("data/news/GENERAL/GENERAL_news_20261009T133733Z.json",   # 同族、早于原场那份
                    "data/news/GENERAL/GENERAL_news_20261009T134047Z.json",   # 原场用的
                    "data/news/GENERAL/GENERAL_news_20261009T141732Z.json",   # 晚于 as_of
                    "data/technical/SPY/SPY_technical_20261009T133700Z.json",
                    "data/technical/SPY/SPY_technical_20261009T134500Z.json",  # 别的场 8 分钟后备的: 不能进
                    "data/technical/QQQ/QQQ_technical_20261009T133700Z.json",
                    "data/technical/NVDA/NVDA_technical_20261008T133700Z.json",
                    "data/technical/NVDA/NVDA_technical_20261013T133700Z.json",
                    "data/economic/economic_US_20261009T134050Z.txt",            # 备餐余量内
                    "data/economic/economic_US_20261009T160000Z.txt",
                    "data/evidence_prep/GENERAL/GENERAL_peer_compare_20261009T133735Z.json",
                    "data/fundamentals/NVDA/NVDA_fundamentals_20261106T104603Z.txt",
                    "data/fundamentals/NVDA/NVDA_fundamentals_20260901T104603Z.txt",
                    "data/market_data/SPY/SPY_7d_5m_20261009T133500Z.csv",
                    "data/market_data/SPY/SPY_7d_5m_20261009T200000Z.csv",
                    "data/store/NVDA/20261008T133700Z/_meta.json",
                    "data/debate/GENERAL/GENERAL_Analysis_20261009T133700Z.json"):
            p = s / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rel, encoding="utf-8")
        self._daily("SPY", ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"])
        self._daily("QQQ", ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"])
        self.root = B.build_pack("GENERAL", AS_OF, "t", src=self.src, dest_base=self.dest, pg=False, check=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _daily(self, sym, dates):
        d = self.src / "data" / "market_data" / sym
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{sym}_5y_1d_20261106T000000Z.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for i, day in enumerate(dates):
                w.writerow([day, 1, 2, 0.5, 1 + i, 100])

    def _files(self, sub):
        base = self.root / sub
        return sorted(str(p.relative_to(self.root)).replace(os.sep, "/") for p in base.rglob("*") if p.is_file())

    def test_pack_root_is_isolated(self):
        self.assertEqual(self.root, self.dest / f"GENERAL_{AS_OF}_t")
        out = subprocess.run([sys.executable, "scripts/where.py"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(Path(out.stdout.strip()), self.root.resolve())
        out = subprocess.run([sys.executable, "-c", "import evidence_quant as q; print(q.ROOT)"],
                             cwd=self.root, capture_output=True, text=True)
        self.assertEqual(Path(out.stdout.strip()).resolve(), self.root.resolve())
        self.assertFalse((self.root / "docs").exists())
        self.assertFalse((self.root / ".local").exists())
        self.assertFalse((self.root / "drill_sealed").exists())

    def test_pack_md_paths_point_at_sandbox(self):
        t = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(f"cd {self.root}\n", t)
        self.assertIn(f"read {self.root}/CLAUDE.md", t)
        self.assertIn(f"{self.src}_drill/x", t)                          # 别的路径前缀不误改

    def test_pack_copies_session_data_files(self):
        news = self._files("data/news")
        self.assertEqual(news, ["data/news/GENERAL/GENERAL_news_20261009T134047Z.json"])  # 只要原场那份
        self.assertEqual(self._files("data/technical"), [
            "data/technical/NVDA/NVDA_technical_20261008T133700Z.json",
            "data/technical/QQQ/QQQ_technical_20261009T133700Z.json",
            "data/technical/SPY/SPY_technical_20261009T133700Z.json"])
        self.assertEqual(self._files("data/economic"), ["data/economic/economic_US_20261009T134050Z.txt"])
        self.assertEqual(self._files("data/fundamentals"), ["data/fundamentals/NVDA/NVDA_fundamentals_20260901T104603Z.txt"])
        self.assertIn("data/evidence_prep/GENERAL/GENERAL_peer_compare_20261009T133735Z.json", self._files("data/evidence_prep"))

    def test_pack_truncates_daily_csv(self):
        files = [f for f in self._files("data/market_data") if "_1d_" in f]
        self.assertEqual(files, [f"data/market_data/QQQ/QQQ_drill_1d_{AS_OF}.csv",
                                 f"data/market_data/SPY/SPY_drill_1d_{AS_OF}.csv"])
        with open(self.root / files[1], encoding="utf-8") as f:
            self.assertEqual([r["date"] for r in csv.DictReader(f)], ["2026-10-07", "2026-10-08"])

    def test_pack_intraday_not_after(self):
        self.assertEqual([f for f in self._files("data/market_data") if "_5m_" in f],
                         ["data/market_data/SPY/SPY_7d_5m_20261009T133500Z.csv"])

    def test_pack_store_empty(self):
        self.assertEqual(self._files("data/store"), [])
        self.assertEqual(self._files("data/debate"), [])

    def test_strict_pack_uses_prior_vintages_and_omits_operational_files(self):
        (self.src / ".aws").mkdir()
        (self.src / ".aws" / "fixture").touch()
        (self.src / "pc_export.json").touch()
        spy = self.src / "data/market_data/SPY"
        intraday = spy / "SPY_7d_5m_20261009T133500Z.csv"
        intraday.write_text("date,close\n2026-10-09 13:30:00+00:00,99\n2026-10-09 13:35:00+00:00,123\n",
                            encoding="utf8")
        (spy / "SPY_5y_1d_20261008T000000Z.csv").write_text(
            "date,close\n2026-10-08,99\n2026-10-09,123\n", encoding="utf8")
        root = B.build_pack("GENERAL", AS_OF, "strict", src=self.src, dest_base=self.dest,
                            pg=False, check=False, strict=True)
        self.assertFalse((root / ".aws").exists())
        self.assertFalse((root / "pc_export.json").exists())
        self.assertEqual((root / intraday.relative_to(self.src)).read_text().splitlines(),
                         ["date,close", "2026-10-09 13:30:00+00:00,99"])
        self.assertIn(",123", intraday.read_text())
        files = json.loads((root / "drill/pack.json").read_text())["files"]
        self.assertFalse(any(B.stamp_of(Path(f).name) > B.parse_ts(AS_OF) for f in files))
        self.assertEqual((root / f"data/market_data/SPY/SPY_drill_1d_{AS_OF}.csv").read_text().splitlines(),
                         ["date,close", "2026-10-08,99"])

    def test_pack_sealed_outcome_outside_root(self):
        sealed = self.dest / "_sealed" / f"GENERAL_{AS_OF}_t"
        self.assertFalse(str(sealed.resolve()).startswith(str(self.root.resolve())))
        out = json.loads((sealed / "outcome.json").read_text(encoding="utf-8"))
        self.assertEqual([b["date"] for b in out["bars"]["SPY"]], ["2026-10-09", "2026-10-12", "2026-10-13"])
        for f in self._files("data/market_data"):
            if "_1d_" in f:
                self.assertNotIn("2026-10-09", (self.root / f).read_text(encoding="utf-8"))

    def test_pack_manifest(self):
        pack = json.loads((self.root / "drill" / "pack.json").read_text(encoding="utf-8"))
        self.assertEqual((pack["symbol"], pack["as_of"], pack["run"]), ("GENERAL", AS_OF, "t"))
        self.assertIn("data/news/GENERAL/GENERAL_news_20261009T134047Z.json", pack["files"])
        self.assertEqual(len(pack["files"]["data/news/GENERAL/GENERAL_news_20261009T134047Z.json"]), 64)

    def test_refuses_existing_root(self):
        with self.assertRaises(FileExistsError):
            B.build_pack("GENERAL", AS_OF, "t", src=self.src, dest_base=self.dest, pg=False, check=False)

    def test_refuses_without_origin_session(self):
        with self.assertRaises(FileNotFoundError):
            B.build_pack("GENERAL", "20261010T133700Z", "t", src=self.src, dest_base=self.dest, pg=False, check=False)


class HelpersTest(unittest.TestCase):
    def test_strict_rebuild_includes_peer_snapshots_and_seals_backups(self):
        from unittest.mock import patch
        import rebuild_technical as rebuild
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pack"
            backup = Path(tmp) / "sealed" / "old_technical"
            primary = f"data/technical/ASML/ASML_technical_{AS_OF}.json"
            peer = f"data/technical/TSM/TSM_technical_{AS_OF}.json"
            for rel in (primary, peer, peer.replace(AS_OF, "20261008T120000Z")):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with patch.object(rebuild, "technical_paths", return_value=[primary]), \
                    patch.object(rebuild, "rebuild_one", return_value=(True, "done")) as run:
                result = rebuild.rebuild(root, "ASML", AS_OF, backup)
                self.assertEqual(set(result["rebuilt"]), {primary, peer})
                self.assertEqual([c.args for c in run.call_args_list],
                                 [(root, rel, AS_OF, backup) for rel in sorted((primary, peer))])
                with self.assertRaisesRegex(ValueError, "outside"):
                    rebuild.rebuild(root, "ASML", AS_OF, root / "drill/old_technical")

    def test_asml_closed_bars_at_1058_utc(self):
        cutoff = B.parse_ts("20261104T105800Z")
        cases = {"1m": ("10:57", "10:58"), "5m": ("10:50", "10:55"), "1h": ("09:58", "10:00")}
        with tempfile.TemporaryDirectory() as tmp:
            for interval, (closed, partial) in cases.items():
                with self.subTest(interval=interval):
                    source = Path(tmp) / f"ASML_test_{interval}_20261104T105800Z.csv"
                    target = Path(tmp) / f"frozen_{interval}.csv"
                    source.write_text(f"datetime,close\n2026-11-04 {closed}:00Z,99\n"
                                      f"2026-11-04 {partial}:00Z,123\n", encoding="utf8")
                    self.assertEqual(B.copy_closed_intraday(source, target, cutoff), 1)
                    self.assertEqual(target.read_text().splitlines(), ["datetime,close", f"2026-11-04 {closed}:00Z,99"])

    def test_stamp_and_family(self):
        self.assertEqual(B.stamp_of("SPY_7d_5m_20261009T133500Z.csv").strftime("%H%M"), "1335")
        self.assertIsNone(B.stamp_of("symbols.yaml"))
        self.assertEqual(B.family_of("data/news/GENERAL/GENERAL_news_20261009T134047Z.json"),
                         "data/news/GENERAL/GENERAL_news_*.json")

    def test_news_items_filtered_by_publication(self):
        from datetime import datetime, timezone
        a = datetime(2026, 10, 9, 13, 37, tzinfo=timezone.utc)
        rows = [{"fetched_at": "2026-10-09T13:40:00+00:00",
                 "items": [{"id": 1, "datetime": int(a.timestamp()) - 60, "headline": "before"},
                           {"id": 2, "datetime": int(a.timestamp()) + 60, "headline": "after"}]},
                {"fetched_at": "2026-10-09T12:00:00+00:00",
                 "items": [{"id": 1, "datetime": int(a.timestamp()) - 60, "headline": "before"}]}]
        out = B.news_items(rows, a)
        self.assertEqual([x["title"] for x in out], ["before"])        # 去重, 发布晚于 as_of 的不要
        self.assertEqual(out[0]["source"], "pg:news")


if __name__ == "__main__":
    unittest.main()


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "a.py").write_text("x = 1\r\n", encoding="utf-8", newline="")   # 生产拷贝是 CRLF
        od = self.root / "scripts" / "drill" / "overlay" / "t"
        (od / "files" / "scripts").mkdir(parents=True)
        (od / "files" / "scripts" / "a.py").write_text("x = 2\n", encoding="utf-8")
        import hashlib
        (od / "overlay.json").write_text(json.dumps({"base_sha256_lf": {
            "scripts/a.py": hashlib.sha256(b"x = 1\n").hexdigest()}}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_overlay_applies_on_matching_base(self):
        done = B.apply_overlays(self.root, ["t"])
        self.assertEqual(done[0]["files"], ["scripts/a.py"])
        self.assertEqual((self.root / "scripts" / "a.py").read_text(encoding="utf-8"), "x = 2\n")

    def test_overlay_refuses_when_production_changed(self):
        (self.root / "scripts" / "a.py").write_text("x = 3\n", encoding="utf-8")
        with self.assertRaises(B.OverlayError):
            B.apply_overlays(self.root, ["t"])
        self.assertEqual((self.root / "scripts" / "a.py").read_text(encoding="utf-8"), "x = 3\n")

    def _new_file_overlay(self):
        od = self.root / "scripts" / "drill" / "overlay" / "n"
        (od / "files" / "scripts" / "sub").mkdir(parents=True)
        (od / "files" / "scripts" / "sub" / "new.py").write_text("y = 1\n", encoding="utf-8")
        (od / "overlay.json").write_text(json.dumps({"base_sha256_lf": {"scripts/sub/new.py": None}}), encoding="utf-8")

    def test_overlay_adds_new_file(self):
        self._new_file_overlay()
        B.apply_overlays(self.root, ["n"])
        self.assertEqual((self.root / "scripts" / "sub" / "new.py").read_text(encoding="utf-8"), "y = 1\n")

    def test_overlay_refuses_new_file_when_production_has_it(self):
        self._new_file_overlay()
        (self.root / "scripts" / "sub").mkdir()
        (self.root / "scripts" / "sub" / "new.py").write_text("y = 0\n", encoding="utf-8")
        with self.assertRaises(B.OverlayError):
            B.apply_overlays(self.root, ["n"])


@unittest.skipUnless(
    (HERE / "overlay/lens-weights-vol/files/scripts/odds_snapshot.py").exists(),
    "optional historical overlay is not included in the public source snapshot",
)
class LensWeightsVolOverlayTest(unittest.TestCase):
    def test_vol_weights(self):
        import importlib.util
        sys.path.insert(0, str(REPO / "scripts"))
        spec = importlib.util.spec_from_file_location(
            "odds_snapshot_overlay", HERE / "overlay" / "lens-weights-vol" / "files" / "scripts" / "odds_snapshot.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        w = m.vol_weights({"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2},
                          {"SPY": {"atr_pct_14": 0.866}, "QQQ": {"atr_pct_14": 1.265}, "DIA": {"atr_pct_14": 0.956}})
        self.assertEqual(list(w), ["SPY", "QQQ", "DIA"])
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        self.assertEqual((round(w["SPY"], 2), round(w["QQQ"], 2), round(w["DIA"], 2)), (0.43, 0.38, 0.19))
        self.assertEqual(m.vol_weights({"SPY": 0.5, "QQQ": 0.5}, {"SPY": {"atr_pct_14": None}, "QQQ": {"atr_pct_14": 1}}),
                         {"SPY": 0.5, "QQQ": 0.5})

    def test_overlay_base_matches_repo(self):
        """补丁的基线必须是当前仓库版本(生产部署的同一份), 或叠在前一个补丁之上时是那个补丁的替换件;
        null = 补丁新增的文件, 仓库里还不能有。生产改了这份文件, 这条测试先红。"""
        import hashlib
        sha = lambda p: hashlib.sha256(p.read_bytes().replace(bytes([13]), b"")).hexdigest()  # noqa: E731
        mans = sorted((HERE / "overlay").glob("*/overlay.json"))
        for man_p in mans:
            man = json.loads(man_p.read_text(encoding="utf-8"))
            for rel, base in man["base_sha256_lf"].items():
                if base is None:
                    self.assertFalse((REPO / rel).exists(), f"{man_p.parent.name}: {rel} is new but the repo has it")
                    continue
                ok = ({sha(REPO / rel)} if (REPO / rel).exists() else set()) | {sha(m.parent / "files" / rel) for m in mans
                                          if m != man_p and (m.parent / "files" / rel).exists()}
                self.assertIn(base, ok, f"{man_p.parent.name}: {rel}")


class OpeningAnchorsTest(unittest.TestCase):
    """r1-rational-3 的开局锚: 只读包内日线, 中性参照树与盘整带按 s_s 算, 各段都在。"""
    def setUp(self):
        import importlib.util
        import math
        spec = importlib.util.spec_from_file_location(
            "opening_anchors", HERE.parent / "opening_anchors.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.root = Path(tempfile.mkdtemp())
        (self.root / "config").mkdir()
        shutil.copy(HERE.parent.parent / "config" / "short_base.yaml", self.root / "config" / "short_base.yaml")
        from datetime import date, timedelta
        for sym, drift in (("AAA", 0.0006), ("SPY", 0.0004), ("IEF", 0.0)):
            d = self.root / "data" / "market_data" / sym
            d.mkdir(parents=True)
            rows, day, px = ["date,open,high,low,close,volume"], date(2022, 1, 3), 100.0
            for k in range(1100):
                px *= 1 + drift + 0.01 * math.sin(k * (1.3 if sym == "AAA" else 0.7))
                rows.append(f"{day.isoformat()} 00:00:00+00:00,{px},{px * 1.01},{px * 0.99},{px},1000")
                day += timedelta(days=1)
            (d / f"{sym}_drill_1d_20261014T145900Z.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lines(self):
        state = {"symbol": "AAA", "ts": "20261014T145900Z", "benchmark": "SPY", "sector": None,
                 "odds": {"reference_price": 100.0, "unit_basis": {"s_s": 0.05, "venue": "US", "adr": False}}}
        lines = self.m.build_anchor_lines(state, self.root)
        text = "\n".join(lines)
        self.assertIn("SD 3.54% (0.707·ATR·√15", text)
        self.assertIn("inside ±0.10·ATR·√15 0.11", text)
        self.assertIn("Consolidation band: 99.50–100.50", text)
        self.assertIn("Valuation day: 2026-11-04", text)
        for key in ("15-day width (simple returns):", "Extension:", "Trend facts (machine, no forecast):", "Relative strength vs SPY", "Rates co-movement:",
                    "Short-term state (machine;"):
            self.assertIn(key, text)
        self.assertNotIn("unavailable", text)

    def test_decompose(self):
        """S_ref/(100-S_ref) = direction ratio x magnitude ratio (synthesis-4)."""
        nodes = [{"id": "A", "p": 0.4, "P_T": 106.0}, {"id": "B", "p": 0.2, "P_T": 100.2}, {"id": "C", "p": 0.4, "P_T": 97.0}]
        t = self.m.decompose(nodes, 100.0, 0.05)
        self.assertIn("direction ratio 1.50 × magnitude ratio 1.36", t)   # exact G/L: B at +0.2% counts as up
        self.assertIn("band mass 0.20", t)
        self.assertIn("A +6.00% (+1.20 ATR·√15) → G 2.40pp", t)
        self.assertIn("one side empty", self.m.decompose([{"p": 1.0, "P_T": 101.0}], 100.0, 0.05))

    def test_never_raises(self):
        self.assertTrue(self.m.build_anchor_lines({}, self.root)[0].startswith("(opening anchors unavailable"))
