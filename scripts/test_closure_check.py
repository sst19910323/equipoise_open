"""v3.4 closing check (contracts C5 / C2 / C9): one validator for close-round and release.

Fixtures (a)-(d) of proposal §5, built by hand as manifest-ordered records {seq, id, kind, role, round, content}."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import closure_check as cc  # noqa: E402
from ledger_tally import tally  # noqa: E402


# ---------------------------------------------------------------- record builders (importable by later tasks)

def claim(cid, kind="own", status="open", text="c", target=None, answers=None, revises=None):
    c = {"id": cid, "kind": kind, "claim": text, "evidence_tier": "T3", "status": status,
         "effect": {"if_true": [], "if_false": []}}
    if target:
        c["target"] = target
    if answers is not None:
        c["answers"] = list(answers)
    if revises:
        c["revises"] = revises
    return c


def final(seq, rid, claims, updates=None, held=None):
    rnd, side = rid.split(".")
    content = {"claims": claims, "scenario": {"schema": "3.4"}}
    if updates is not None:
        content["scenario_updates"] = updates
    if held is not None:
        content["stance_ledger"] = {"held": held, "moves": []}
    return {"seq": seq, "id": rid, "kind": "final", "role": side, "round": int(rnd[1:]), "content": content}


def evidence(seq, eid, round_, target_claim=None, kind="evidence", **filing):
    if target_claim is not None:
        filing["target_claim"] = target_claim
    return {"seq": seq, "id": eid, "kind": kind, "role": eid.split(".")[0], "round": round_, "content": filing}


def notes(seq, round_, dispositions=None, terminal_open=None):
    c = {"tally": "", "arbiter_notes": "n"}
    if dispositions is not None:
        c["dispositions"] = dispositions
    if terminal_open is not None:
        c["terminal_open"] = terminal_open
    return {"seq": seq, "id": f"NOTES.R{round_}", "kind": "notes", "role": "A", "round": round_, "content": c}


def _round_name(n):
    return "Init" if n == 1 else f"Round {n - 1}"


def test_round_labels_english_with_legacy_parse():
    from closure_check import round_no, round_name
    from debate_store import _round_name as store_name
    assert round_name(1) == "Init" and round_name(3) == "Round 2" and store_name(3) == "Round 2"
    assert round_no("Round 2") == 3 and round_no("回合2") == 3 and round_no("Init") == 1 and round_no("R4") == 4
    from ledger_tally import round_key
    assert round_key("Round 3") == 3 and round_key("回合3") == 3 and round_key("Init") == 0


def report_from_records(records):
    """The assembled report's view of the same session: debate_history (finals), evidence (+ model_calls), arbiter_log."""
    who = {"Z": "Zealot", "R": "Reaper", "A": "Arbiter"}
    doc = {"debate_history": [], "evidence": [], "model_calls": [], "arbiter_log": []}
    for r in records:
        if r["kind"] == "final":
            doc["debate_history"].append({"round": _round_name(r["round"]), "role": who[r["role"]], "id": r["id"],
                                          "seq": r["seq"], "content": r["content"]})
        elif r["kind"] in ("evidence", "model_call"):
            item = {"question": "q", "simple": "s", "round": _round_name(r["round"]), "requested_by": who[r["role"]],
                    "kind": "search", "request": dict(r["content"]), "seq": r["seq"]}
            if r["kind"] == "evidence":
                item["evidence_id"] = r["id"]
            if r["kind"] == "model_call":
                item["model_call_id"] = r["id"]
                doc["model_calls"].append({"model_call_id": r["id"], "round": item["round"], "request": dict(r["content"])})
            doc["evidence"].append(item)
        elif r["kind"] == "notes":
            doc["arbiter_log"].append({"round": _round_name(r["round"]), "tally": "", "notes": "n", "seq": r["seq"],
                                       **{k: v for k, v in r["content"].items() if k in ("terminal_open", "dispositions")}})
    return doc


def tally_of(records, closed=None):
    return tally(cc.entries_from_records(records), closed=closed, ruler=True)


def closing(rev, tried, nxt="none: both routes exhausted; no source distinguishes the multiples"):
    return {"state": "deadlocked", "claim_rev": rev, "remaining": "A p 0.55 vs 0.35", "tried": tried, "next": nxt}


def base_world():
    """R's own claim R2 (rev R1.R) is challenged by Z's evidence Z.EV1 (round 2); R answers it in R3.R with a
    scenario update that refs Z.EV1. view_seq per spawn: round n material saw everything up to NOTES.R(n-1)."""
    recs = [
        final(1, "R1.Z", [claim("Z1")]),
        final(2, "R1.R", [claim("R2", text="multiples hold")]),
        notes(3, 1),
        evidence(4, "Z.EV1", 2, target_claim="R2"),
        evidence(5, "Z.EV2", 2, target_claim="Z1"),
        final(6, "R2.Z", [claim("Z1")]),
        final(7, "R2.R", [claim("R2", text="multiples hold")]),
        notes(8, 2),
        final(9, "R3.Z", [claim("Z1", status="uncontested")]),
        final(10, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")],
              updates=[{"claim": "R2", "applies": None, "ops": [], "why": "Z.EV1 shows peers re-rated; already inside",
                        "refs": ["Z.EV1"]}]),
    ]
    spawns = {"R1.Z": {"view_seq": 0}, "R1.R": {"view_seq": 0}, "R2.Z": {"view_seq": 3}, "R2.R": {"view_seq": 3},
              "R3.Z": {"view_seq": 8}, "R3.R": {"view_seq": 8}}
    return recs, spawns


def run(recs, spawns, terminal, max_rounds=20, round_=3):
    return cc.check(terminal, tally_of(recs), recs, spawns, None, max_rounds, round_)


class EngagementPredicate(unittest.TestCase):
    def test_update_with_refs_engages(self):
        recs, spawns = base_world()
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), ["R3.R"])

    def test_material_without_the_evidence_does_not_engage(self):
        recs, spawns = base_world()
        spawns["R3.R"] = {"view_seq": 3}          # R3.R's material predates Z.EV1
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), [])

    def test_held_text_mention_never_counts(self):
        recs, spawns = base_world()
        recs[-1] = final(10, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")],
                         held=[{"id": "R2", "text": "Z.EV1 does not move R2"}])
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), [])


class FixtureA(unittest.TestCase):
    """(a) tried empty / non-returned / non-targeting rejected; engaged by the opposing side or disposed by the arbiter
    accepted; stale claim_rev rejected; same-beat engagement via view_seq accepted; a later challenge naming other evidence
    does not discharge E."""

    def test_empty_tried_rejected(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [])})
        self.assertIn("R2", out["open"])
        self.assertNotIn("R2", out["closable"])
        self.assertFalse(out["ok"])

    def test_non_returned_evidence_rejected(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV9", "engaged_by": ["R3.R"]}])})
        self.assertIn("R2", out["open"])
        self.assertIn("not returned", out["reasons"]["R2"])

    def test_non_targeting_evidence_rejected(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV2", "engaged_by": ["R3.R"]}])})
        self.assertIn("R2", out["open"])
        self.assertIn("does not target", out["reasons"]["R2"])

    def test_engaged_by_opposing_side_accepted(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertEqual(out["closable"], ["R2"], out)
        self.assertEqual(out["open"], [])
        self.assertTrue(out["ok"])

    def test_engaged_by_naming_a_non_engaging_record_rejected(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R2.R"]}])})
        self.assertIn("R2", out["open"])

    def test_arbiter_disposition_accepted(self):
        recs, spawns = base_world()
        recs[-1] = final(10, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")])   # no R engagement
        recs.append(notes(11, 3, dispositions=[{"claim": "R2", "claim_rev": "R1.R", "evidence": ["Z.EV1"],
                                                 "ruling": "Z.EV1 shows the re-rating but not its persistence"}]))
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), ["NOTES.R3"])
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["NOTES.R3"]}])})
        self.assertEqual(out["closable"], ["R2"], out)

    def test_disposition_on_stale_rev_does_not_count(self):
        recs, spawns = base_world()
        recs[-1] = final(10, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")])
        recs.append(notes(11, 3, dispositions=[{"claim": "R2", "claim_rev": "R0.R", "evidence": ["Z.EV1"], "ruling": "r"}]))
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), [])

    def test_stale_claim_rev_rejected(self):
        recs, spawns = base_world()
        # R revises R2's text in R3.R (answering Z.EV1): the current rev becomes R3.R
        recs[-1] = final(10, "R3.R", [claim("R2", status="deadlocked", text="multiples hold above 18x",
                                            revises="R1.R", answers=["Z.EV1"])])
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), ["R3.R"])
        stale = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertIn("R2", stale["open"])
        self.assertIn("stale claim_rev", stale["reasons"]["R2"])
        fresh = run(recs, spawns, {"R2": closing("R3.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertEqual(fresh["closable"], ["R2"], fresh)

    def test_same_beat_engagement_via_view_seq(self):
        # A.EV1 (arbiter's neutral check) and R3.R sit in the same round 3; the material that produced R3.R contained it.
        recs, spawns = base_world()
        recs = recs[:8] + [evidence(9, "A.EV1", 3, target_claim="R2"),
                           final(10, "R3.Z", [claim("Z1")]),
                           final(11, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")],
                                 updates=[{"claim": "R2", "applies": None, "ops": [], "why": "A.EV1: peers flat",
                                           "refs": ["A.EV1", "Z.EV1"]}])]
        spawns["R3.R"] = {"view_seq": 9}
        tried = [{"evidence": "A.EV1", "engaged_by": ["R3.R"]}, {"evidence": "Z.EV1", "engaged_by": ["R3.R"]}]
        self.assertEqual(run(recs, spawns, {"R2": closing("R1.R", tried)})["closable"], ["R2"])
        spawns["R3.R"] = {"view_seq": 8}           # same beat, but the material did not contain A.EV1
        out = run(recs, spawns, {"R2": closing("R1.R", tried)})
        self.assertIn("R2", out["open"])
        self.assertIn("A.EV1", out["reasons"]["R2"])

    def test_later_challenge_naming_other_evidence_does_not_discharge(self):
        # Z's own claim Z1 is backed by Z.EV2; R's later challenge on Z1 answers R.EV1, not Z.EV2.
        recs, spawns = base_world()
        recs = recs[:8] + [evidence(9, "R.EV1", 3, target_claim="Z1"),
                           final(10, "R3.Z", [claim("Z1", status="deadlocked")]),
                           final(11, "R3.R", [claim("R2", text="multiples hold"),
                                              claim("R5", kind="challenge", target="Z1", answers=["R.EV1"])])]
        spawns["R3.R"] = {"view_seq": 9}
        term = {"Z1": closing("R1.Z", [{"evidence": "Z.EV2", "engaged_by": ["R3.R"]}])}
        out = run(recs, spawns, term)
        self.assertIn("Z1", out["open"])
        self.assertEqual(cc.engaged("Z.EV2", "Z1", recs, spawns), [])
        recs[-1]["content"]["claims"][1]["answers"] = ["R.EV1", "Z.EV2"]
        self.assertEqual(cc.engaged("Z.EV2", "Z1", recs, spawns), ["R3.R"])

    def test_filed_next_keeps_the_claim_open(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}], nxt="filed:A.MC4")})
        self.assertIn("R2", out["open"])
        self.assertIn("filed:", out["reasons"]["R2"])
        self.assertEqual(out["fatal"], [])


class FixtureB(unittest.TestCase):
    """(b) arbiter-authored `unresolved` rejected (any round below the cap); bare labels rejected."""

    def test_arbiter_unresolved_is_fatal(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {"R2": {"state": "unresolved"}})
        self.assertTrue(any("unresolved" in f for f in out["fatal"]), out)
        self.assertFalse(out["ok"])

    def test_bare_label_is_fatal(self):
        recs, spawns = base_world()
        for label in ("deadlocked", "unresolved"):
            out = run(recs, spawns, {"R2": label})
            self.assertTrue(out["fatal"], label)
            self.assertIn("R2", out["open"])

    def test_cap_reports_what_is_still_open_as_unresolved(self):
        recs, spawns = base_world()
        out = run(recs, spawns, {}, max_rounds=3, round_=3)
        self.assertEqual(out["unresolved"], ["R2"])
        self.assertTrue(out["ok"])


class FixtureC(unittest.TestCase):
    """(c) a debater's `deadlocked` is a request: it stays in open_total until a closing record passes."""

    def test_debater_deadlocked_stays_open(self):
        recs, _ = base_world()
        t = tally_of(recs)
        self.assertIn("R2", t["open_total"])
        self.assertEqual(t["reaper"]["deadlock_requested"], ["R2"])
        self.assertEqual(t["reaper"]["claim_rev"]["R2"], "R1.R")
        self.assertNotIn("R2", tally_of(recs, closed={"R2"})["open_total"])

    def test_no_first_three_rounds_forensic_for_ruler_tallies(self):
        recs, _ = base_world()
        self.assertFalse(any("as early as round" in f for f in tally_of(recs)["forensics"]))

    def test_release_rebuilds_from_notes_and_ignores_report_lists(self):
        import validate_debate as vd
        recs, spawns = base_world()
        recs.append(notes(11, 3))
        doc = report_from_records(recs)         # debate_history / evidence / arbiter_log shaped like the assembler's
        doc["schema_version"] = "3.4"
        doc["spawns"] = spawns                   # the assembler carries _session.json.spawns (3.4 fails closed without)
        doc["deadlocked"] = ["R2"]               # report-level list: never trusted
        res = tally_of(recs)
        fatal, warn = vd.closure(doc, res)
        self.assertTrue(any("R2" in f for f in fatal), fatal)
        doc["arbiter_log"][-1]["terminal_open"] = {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])}
        fatal, warn = vd.closure(doc, res)
        self.assertEqual(fatal, [])
        doc["caps"] = {"max_rounds": 3}          # at the cap the script's unresolved is a warning, not a leak
        doc["arbiter_log"][-1].pop("terminal_open")
        fatal, warn = vd.closure(doc, res)
        self.assertEqual(fatal, [])
        self.assertTrue(any("R2" in w for w in warn), warn)

    def test_release_records_match_live_records(self):
        recs, _ = base_world()
        recs.append(notes(11, 3))
        rebuilt = cc.records_from_report(report_from_records(recs))
        strip = lambda rs: [(r["id"], r["kind"], r["role"], r["round"]) for r in rs]
        self.assertEqual(strip(rebuilt), strip(recs))
        self.assertEqual([r["seq"] for r in rebuilt], list(range(1, len(recs) + 1)))

    def test_report_view_seq_fallback(self):
        recs, spawns = base_world()
        recs.append(notes(11, 3))
        rebuilt = cc.records_from_report(report_from_records(recs))
        self.assertEqual(cc.spawns_from_report({}, rebuilt), spawns)

    def test_release_fails_closed_without_evidence_ids_or_owner(self):
        import validate_debate as vd
        recs, spawns = base_world()
        recs.append(notes(11, 3, terminal_open={"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])}))
        doc = report_from_records(recs)
        doc["schema_version"] = "3.4"
        doc["spawns"] = spawns
        res = tally_of(recs)
        self.assertEqual(vd.closure(doc, res)[0], [])                   # ids present: passes
        no_id = copy.deepcopy(doc)
        no_id["evidence"][0].pop("evidence_id")
        fatal, _ = vd.closure(no_id, res)
        self.assertTrue(any("evidence ids missing; cannot rebuild closure records" in f for f in fatal), fatal)
        no_owner = copy.deepcopy(doc)
        no_owner["evidence"][0]["requested_by"] = "someone"
        fatal, _ = vd.closure(no_owner, res)
        self.assertTrue(any("requested_by" in f for f in fatal), fatal)

    def test_unmarked_doc_with_35_finals_takes_the_35_gate(self):
        import validate_debate as vd
        recs, _ = base_world()
        doc = report_from_records(recs)                                   # no schema_version, no top-level scenario
        doc["deadlocked"] = ["R2"]
        self.assertTrue(vd.is_ruler(doc))
        self.assertFalse(vd.is_ruler({"debate_history": []}))
        self.assertTrue(vd.is_ruler({}, {"reaper": {"deadlock_requested": []}}))
        fatal, _ = vd.validate(doc, None)
        self.assertTrue(any("no passing closing record" in f and "R2" in f for f in fatal), fatal)

    def test_brief_shows_deadlock_requests(self):
        from ledger_tally import brief
        recs, _ = base_world()
        self.assertIn("deadlock requested by R: R2 — needs a closing record", brief(tally_of(recs)))


class OutOfOrderEvidenceTest(unittest.TestCase):
    """Review fix 1: evidence tagged round 2 but archived after R2.Z; R3.Z's material saw R2.Z (store seq 4) but not
    R.EV1 (seq 5). Live close-round and the release gate (store seqs carried on every report record) must agree."""

    @staticmethod
    def world(view_r3z):
        rec = closing("R1.Z", [{"evidence": "R.EV1", "engaged_by": ["R3.Z"]}])
        recs = [
            final(1, "R1.Z", [claim("Z1")]),
            final(2, "R1.R", [claim("R1")]),
            notes(3, 1),
            final(4, "R2.Z", [claim("Z1")]),
            evidence(5, "R.EV1", 2, target_claim="Z1"),
            final(6, "R2.R", [claim("R1")]),
            notes(7, 2),
            final(8, "R3.Z", [claim("Z1", status="deadlocked")],
                  updates=[{"claim": "Z1", "applies": None, "ops": [], "why": "R.EV1 answered; already inside",
                            "refs": ["R.EV1"]}]),
            final(9, "R3.R", [claim("R1", status="uncontested")]),
            notes(10, 3, terminal_open={"Z1": rec}),
        ]
        spawns = {"R1.Z": {"view_seq": 0}, "R1.R": {"view_seq": 0}, "R2.Z": {"view_seq": 3}, "R2.R": {"view_seq": 3},
                  "R3.Z": {"view_seq": view_r3z}, "R3.R": {"view_seq": 7}}
        return recs, spawns, rec

    def check_both(self, view_r3z):
        import validate_debate as vd
        recs, spawns, rec = self.world(view_r3z)
        live = cc.check({"Z1": rec}, tally_of(recs[:-1]), recs, spawns, None, 20, 3)
        doc = report_from_records(recs)
        doc["schema_version"] = "3.4"
        doc["spawns"] = spawns
        fatal, _ = vd.closure(doc, tally_of(recs))
        return live, fatal, doc

    def test_unseen_evidence_rejected_live_and_at_release(self):
        live, fatal, _ = self.check_both(4)
        self.assertIn("Z1", live["open"])
        self.assertTrue(any("Z1" in f for f in fatal), fatal)

    def test_seen_evidence_passes_live_and_at_release(self):
        live, fatal, _ = self.check_both(7)
        self.assertEqual(live["open"], [])
        self.assertEqual(fatal, [])

    def test_35_report_without_store_seq_fails_closed(self):
        import validate_debate as vd
        _live, _fatal, doc = self.check_both(7)
        recs, _s, _r = self.world(7)
        doc["evidence"][0].pop("seq")
        fatal, _ = vd.closure(doc, tally_of(recs))
        self.assertTrue(any("store seq missing" in f for f in fatal), fatal)


class SupersededAndMaterials(unittest.TestCase):
    def test_engagement_against_a_superseded_text_does_not_count(self):
        recs, spawns = base_world()
        # R3.R engages Z.EV1 (update refs it); R4.R then revises R2's text without answering Z.EV1
        recs += [notes(11, 3), final(12, "R4.Z", [claim("Z1", status="uncontested")]),
                 final(13, "R4.R", [claim("R2", status="deadlocked", text="multiples hold above 18x", revises="R1.R")])]
        spawns.update({"R4.Z": {"view_seq": 11}, "R4.R": {"view_seq": 11}})
        self.assertEqual(cc.engaged("Z.EV1", "R2", recs, spawns), [])
        out = run(recs, spawns, {"R2": closing("R4.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])}, round_=4)
        self.assertIn("R2", out["open"])

    def test_later_beat_material_gives_access(self):
        # A.EV1 lands in round 3 after R3.R's spawn; R's beat-2 material (written at seq 9) contained it
        recs, spawns = base_world()
        recs = recs[:8] + [evidence(9, "A.EV1", 3, target_claim="R2"), final(10, "R3.Z", [claim("Z1", status="uncontested")]),
                           final(11, "R3.R", [claim("R2", status="deadlocked", text="multiples hold")],
                                 updates=[{"claim": "R2", "applies": None, "ops": [], "why": "w", "refs": ["A.EV1"]}])]
        self.assertEqual(cc.engaged("A.EV1", "R2", recs, spawns), [])          # spawn view_seq 8 < 9
        spawns["R3.R.B2"] = {"view_seq": 9, "at_seq": 9}
        self.assertEqual(cc.engaged("A.EV1", "R2", recs, spawns), ["R3.R"])
        spawns["R3.R.B2"] = {"view_seq": 9, "at_seq": 11}                      # written after the final: no access
        self.assertEqual(cc.engaged("A.EV1", "R2", recs, spawns), [])


class FixtureD(unittest.TestCase):
    """(d) an evidence-backed challenge not engaged by the target's owner blocks `deadlocked` on its target."""

    def world(self):
        recs, spawns = base_world()
        recs = recs[:8] + [evidence(9, "Z.EV3", 2, target_claim="Z3")] + recs[8:]
        for i, r in enumerate(recs, 1):
            r["seq"] = i
        next(r for r in recs if r["id"] == "R2.Z")["content"]["claims"].append(
            claim("Z3", kind="challenge", target="R2", answers=[]))
        spawns["R3.Z"] = spawns["R3.R"] = {"view_seq": 9}
        return recs, spawns

    def test_unengaged_challenge_blocks(self):
        recs, spawns = self.world()
        term = {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])}
        out = run(recs, spawns, term)
        self.assertIn("R2", out["open"])
        self.assertIn("Z3", out["reasons"]["R2"])

    def test_text_mention_does_not_unblock(self):
        recs, spawns = self.world()
        recs[-1]["content"]["stance_ledger"] = {"held": [{"id": "Z3", "text": "Z.EV3 on Z3 is noise"}], "moves": []}
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertIn("R2", out["open"])

    def test_counter_challenge_answering_the_evidence_unblocks(self):
        recs, spawns = self.world()
        recs[-1]["content"]["claims"].append(claim("R6", kind="challenge", target="Z3", answers=["Z.EV3"]))
        out = run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertEqual(out["closable"], ["R2"], out)


class Shapes(unittest.TestCase):
    def test_shape_errors_name_the_rule(self):
        self.assertTrue(any("script-only" in e for e in cc.shape_errors({"R2": "unresolved"})))
        self.assertTrue(any("closing record" in e for e in cc.shape_errors({"R2": "deadlocked"})))
        self.assertEqual(cc.shape_errors({"R2": closing("R1.R", [])}), [])

    def test_disposition_errors(self):
        self.assertTrue(cc.disposition_errors([{"claim": "R2"}]))
        self.assertEqual(cc.disposition_errors([{"claim": "R2", "claim_rev": "R1.R", "evidence": ["Z.EV1"], "ruling": "r"}]), [])

    def test_inputs_not_mutated(self):
        recs, spawns = base_world()
        before = copy.deepcopy(recs)
        run(recs, spawns, {"R2": closing("R1.R", [{"evidence": "Z.EV1", "engaged_by": ["R3.R"]}])})
        self.assertEqual(recs, before)


if __name__ == "__main__":
    unittest.main()
