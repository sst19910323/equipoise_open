# -*- coding: utf-8 -*-
"""Wave 3 (lenses, v3.4.1): the multi-lens protocol lives only in prompts/agent/formats/lenses.md; the LENSES block
names it as a required read in every debater material of a multi-lens session (opening, round, resume, objection,
signoff), right after the task meta-information; the arbiter's reads carry it for multi-lens sessions only; the seven
shared corpus files carry nothing multi-lens beyond the one signpost sentence per shared format."""
import copy
import re
import unittest
from pathlib import Path

import test_lens_final_wave as tlfw
import run_step as rs

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "prompts" / "agent"
LENSES_MD = "prompts/agent/formats/lenses.md"
REQUIRED = f"REQUIRED READ: {LENSES_MD}"
DEBATE_REMINDER = "OUTPUT REMINDER: scenario_by_lens; every effect/update op carries lens"
REVISION_REMINDER = "OUTPUT REMINDER: use this task's base format; every revision op carries lens"
SIGNPOST = "When a LENSES block is present, first read the rules file it names; its field overrides apply to this format."
FINAL_SIGNPOST = ("When the material carries a LENSES block, or your reads list `prompts/agent/formats/lenses.md`, "
                  "read that file first; its field overrides apply to this format.")
SHARED = ["formats/debate.md", "formats/final.md", "formats/objection.md", "formats/signoff.md", "debater.md",
          "manuals/synthesis/arbiter-final-ruling.md", "principles.md"]


def _eu_shaped(s):
    """Turn the GENERAL fixture session into a two-lens (GENERAL_EU-shaped) state: the switch is lenses_of(state)."""
    odds = s.state["odds"]
    odds["by_lens"] = {"DAX": dict(odds["by_lens"]["SPY"], reference_price=25800.0),
                       "IAEX": dict(odds["by_lens"]["QQQ"], reference_price=108.0)}
    s.state["lenses"] = ["DAX", "IAEX"]
    return ["DAX", "IAEX"]


class DebaterMaterialTest(tlfw._InTmp):
    def _read(self, rel):
        return (self.tmp / rel).read_text(encoding="utf-8")

    def assertBlock(self, text, reminder, task_line, lenses, before):
        self.assertIn("--- LENSES — THIS SESSION IS MULTI-LENS ---", text)
        self.assertIn(REQUIRED, text)
        self.assertIn("Read it before drafting; its field overrides apply to the base format.", text)
        self.assertIn(task_line, text)
        self.assertIn("Session lenses, frozen order: " + ", ".join(lenses), text)
        self.assertIn(reminder, text)
        other = REVISION_REMINDER if reminder == DEBATE_REMINDER else DEBATE_REMINDER
        self.assertNotIn(other, text)
        self.assertLess(text.index(REQUIRED), text.index(before))

    def _debate_materials(self, s):
        return {"init": self._read(s.spawn_material("Zealot", 1)),
                "round": self._read(s.spawn_material("Reaper", 2)),
                "resume": self._read(s.resume_material("Zealot", 2, 2, "", []))}

    def test_general_opening_round_and_resume(self):
        s = self.open()
        m = self._debate_materials(s)
        self.assertBlock(m["init"], DEBATE_REMINDER, "TASK: init\n", tlfw.F.LENSES, "--- RAW DATA FILES")
        self.assertBlock(m["round"], DEBATE_REMINDER, "TASK: debate round 2\n", tlfw.F.LENSES, "--- RAW DATA FILES")
        self.assertBlock(m["resume"], DEBATE_REMINDER, "TASK: debate round 2\n", tlfw.F.LENSES, "YOUR PREVIOUS BEAT")
        for t in m.values():   # the block sits after the task meta-information
            self.assertLess(t.index("SYMBOL: GENERAL"), t.index(REQUIRED))

    def test_general_eu_shaped_session(self):
        s = self.open()
        lenses = _eu_shaped(s)
        m = self._debate_materials(s)
        for k, t in m.items():
            self.assertBlock(t, DEBATE_REMINDER, "TASK: ", lenses, "YOUR PREVIOUS BEAT" if k == "resume" else "--- RAW DATA FILES")
            self.assertIn("DAX: reference price 25800", t)

    def test_objection_and_signoff(self):
        out = tlfw.F.drive_lens_session(self.tmp, until="ruling")
        s = out["session"]
        obj = self._read(s.objection_material("Zealot", 1))
        sig = self._read(s.signoff_material("Reaper", 1))
        self.assertBlock(obj, REVISION_REMINDER, "TASK: objection\n", tlfw.F.LENSES, "FORMAT:")
        self.assertBlock(sig, REVISION_REMINDER, "TASK: signoff\n", tlfw.F.LENSES, "FORMAT:")
        self.assertIn("read only the format, the view and any required extension named above", sig)

    def test_single_lens_sessions_carry_nothing(self):
        s = self.open("TEST")
        for lenses in (None, ["N225"]):        # no lens list; a one-lens (GENERAL_JP-shaped) state
            if lenses:
                s.state["lenses"] = lenses
            sess = s
            texts = [self._read(sess.spawn_material("Zealot", 1)), self._read(sess.spawn_material("Reaper", 2)),
                     self._read(sess.resume_material("Zealot", 2, 2, "", []))]
            for t in texts:
                for w in ("LENSES", "lenses.md", "OUTPUT REMINDER", "scenario_by_lens", "lens"):
                    self.assertNotIn(w, t)

    def test_single_lens_objection_and_signoff_carry_nothing(self):
        s = tlfw.F.drive_lens_session(self.tmp, until="ruling")["session"]
        for lenses in (None, ["N225"]):        # the lens switch is lenses_of(state): a single-lens state carries nothing
            s.state["lenses"] = lenses
            for t in (self._read(s.objection_material("Zealot", 1)), self._read(s.signoff_material("Reaper", 1))):
                for w in ("LENSES", "lenses.md", "OUTPUT REMINDER", "scenario_by_lens", "lens"):
                    self.assertNotIn(w, t)


class ArbiterReadsTest(tlfw._InTmp):
    def test_multi_lens_reads_carry_lenses_md(self):
        out = tlfw.F.drive_lens_session(self.tmp, until="ruling")
        s = out["session"]
        self.assertIn(LENSES_MD, s.open_receipt()["arbiter_reads"])
        self.assertIn(LENSES_MD, s._arbiter_reads())
        self.assertEqual(s._arbiter_reads("a", "b"), [LENSES_MD, "a", "b"])
        self.assertEqual(s.open_receipt()["arbiter_reads"][0], LENSES_MD)
        note = s._arbiter_lens_note()
        for w in ("`w`", "lens_weights", "by_lens", LENSES_MD):
            self.assertIn(w, note)
        self.assertNotIn("scenario_by_lens", note)   # never the debater's "emit scenario" line

    def test_single_lens_reads_do_not(self):
        s = self.open("TEST")
        self.assertNotIn(LENSES_MD, s._arbiter_reads(rs.MANUALS["final_ruling"]))
        self.assertEqual(s._arbiter_reads("a", "b"), ["a", "b"])
        self.assertEqual(s._arbiter_lens_note(), "")
        self.assertNotIn(LENSES_MD, s.open_receipt()["arbiter_reads"])

    def test_finalize_receipts(self):
        """provisional / audit / post-objection receipts name lenses.md (multi-lens) — and the tally's round-notes reads."""
        src = (ROOT / "scripts" / "run_step.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('"reads": self._arbiter_reads('), 4)
        self.assertNotIn('"reads": [MANUALS', src)
        self.assertEqual(src.count('"arbiter_reads": self._arbiter_reads('), 2)


class SharedCorpusTest(unittest.TestCase):
    def test_lenses_md_exists_and_holds_the_protocol(self):
        t = (ROOT / LENSES_MD).read_text(encoding="utf-8")
        for w in ("scenario_by_lens", "lens_weights", "lens_weights_reason", "by_lens", "composite_by_lens",
                  "every lens the registered effect has ops on", "exactly 0.1 is inside",
                  "even when that revision changed a different lens", "must fail on each of them",
                  "`applies: null`", "compatibility anchor"):
            self.assertIn(w, t)

    def test_shared_files_carry_only_the_signpost(self):
        for rel in SHARED:
            t = (AGENT / rel).read_text(encoding="utf-8")
            body = t.replace(SIGNPOST, "").replace(FINAL_SIGNPOST, "")
            for w in ("scenario_by_lens", "lens_weights", "by_lens", "lens-tagged", "multi-lens", "LENSES",
                      '"lens"', "`lens`", "multi-level"):
                self.assertNotIn(w, body, f"{rel}: {w}")
            if rel.startswith("formats/"):
                self.assertEqual(t.count(FINAL_SIGNPOST if rel == "formats/final.md" else SIGNPOST), 1, rel)
            else:
                self.assertNotIn(SIGNPOST, t, rel)
                self.assertNotIn(FINAL_SIGNPOST, t, rel)

    def test_context_manuals_keep_region_facts_only(self):
        for name in ("context_general.md", "context_general_eu.md"):
            t = (AGENT / "manuals" / "evidence" / name).read_text(encoding="utf-8")
            self.assertNotIn("scenario_by_lens", t)
            self.assertNotIn("compatibility anchor", t)
            self.assertIn("each lens writes a range", t)
        self.assertIn("traded through a proxy", (AGENT / "manuals/evidence/context_general_eu.md").read_text(encoding="utf-8"))

    def test_debater_reads_the_required_extension(self):
        t = (AGENT / "debater.md").read_text(encoding="utf-8")
        self.assertIn("load any required extension the material names (first:", t)
        self.assertLess(t.index("any required extension the material names"), t.index("prompts/agent/formats/debate.md"))
        self.assertIn("read only any required extension, the format and the view named in the prompt", t)
        for f in ("zealot", "reaper"):
            self.assertIn("load any required extension the material names (first:",
                          (ROOT / ".claude" / "agents" / f"{f}.md").read_text(encoding="utf-8"))

    def test_run_protocol_names_the_conditional_extension(self):
        t = (ROOT / "orchestration" / "run-protocol.md").read_text(encoding="utf-8")
        self.assertIn(LENSES_MD, t)
        self.assertIn("multi-lens sessions only", t)
        ops = (ROOT / "docs" / "handoff" / "V34-OPERATORS-2026-10-31.md").read_text(encoding="utf-8")
        self.assertIn(f"`{LENSES_MD}`", ops)


if __name__ == "__main__":
    unittest.main()
