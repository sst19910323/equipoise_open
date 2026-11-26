"""Exercise group exchange, ordinary signoff and release reconstruction without a model."""
import json
from contextlib import chdir
from pathlib import Path
import tempfile
import unittest

from scripts import group_run as group
from debate_driver import Driver, MockEngine, MockTools, ruler_root, ruler_script
import yaml


class FormalGroupTest(unittest.TestCase):
    def test_legacy_two_member_hearing(self):
        self.run_hearing(["TEST", "PEER"], None)

    def test_seven_members_on_changing_agendas(self):
        members = ["TEST", "PEER", "CCC", "DDD", "EEE", "FFF", "GGG"]
        self.run_hearing(members, [members[:3], members[2:], members[:2] + members[3:]])

    def run_hearing(self, members, agendas):
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            root = Path(temp)
            ruler_root(root)
            cfg = root / "config/symbols.yaml"
            config = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            config["analysis_targets"]["stocks"] = members
            for sym in members[1:]:
                config["symbol_contracts"][sym] = dict(config["symbol_contracts"]["TEST"], symbol=sym)
            cfg.write_text(yaml.safe_dump(config), encoding="utf-8")
            for source in list((root / "data").rglob("TEST_*")):
                for sym in members[1:]:
                    dest = root / str(source.relative_to(root)).replace("TEST", sym)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(source.read_text(encoding="utf-8").replace("TEST", sym), encoding="utf-8")
            folder = group.freeze_group(root, "GROUP_TEST", dict(members=members, region="US", benchmark="BENCH"),
                                        "mock-judge", "mock-worker")
            sessions = {s: group.Session(s, folder.name, root, tools=MockTools(root)) for s in members}
            scripts = {s: ruler_script() for s in members}
            for symbol, fixture in scripts.items():
                if agendas is None:
                    sessions[symbol].state.pop("protection_required")
                    sessions[symbol].save()
                else:
                    reading = {"phase": "easing" if symbol == "TEST" else "persistent",
                               "basis": f"{symbol} opening stress range", "review": "Next close"}
                    for stage in ("proposed", "ruling"):
                        fixture[stage][0]["protection_by_symbol"] = {symbol: reading}
            drivers = {s: Driver(s, root, MockEngine(scripts[s]), "mock-judge", "mock-worker") for s in members}
            with self.assertRaisesRegex(ValueError, "signed-off"):
                group.build_report(folder, root)
            legacy = agendas is None
            if legacy:
                opening = group.read(folder / "opening.json")
                del opening["round_scope"]
                group._atomic_json(folder / "opening.json", opening)
                agendas = [members, members]
            keys = {1: ["beat_zealot_r1_b2", "beat_reaper_r1_b1"],
                    2: ["beat_zealot_r2_b1_fix", "beat_reaper_r2_b1_fix"]}
            local = dict.fromkeys(members, 0)
            for n, active in enumerate(agendas, 1):
                before = {s: (dict(sessions[s].state), sessions[s].store.ids()) for s in members if s not in active}
                for s in active:
                    local[s] += 1
                turns = {role: dict(shared=f"Shared {role} R{n}", members={
                    s: scripts[s][keys[local[s]][i]][0] for s in active}) for i, role in enumerate(group.ROLES)}
                bad = json.loads(json.dumps(turns))
                del bad["Zealot"]["members"][active[0]]
                with self.assertRaisesRegex(ValueError, "roster"):
                    group.exchange(folder, n, bad, root)
                if legacy:
                    del bad["Reaper"]["members"][active[0]]
                    with self.assertRaisesRegex(ValueError, "full frozen roster"):
                        group.exchange(folder, n, bad, root)
                self.assertFalse((folder / f"R{n}").exists())
                sealed = group.exchange(folder, n, turns, root)
                self.assertEqual(sealed, group.exchange(folder, n, turns, root))
                if not legacy and n == 1:
                    with self.assertRaisesRegex(ValueError, "previous sealed round"):
                        group.exchange(folder, 2, turns, root)
                for s in active:
                    for role, key in zip(group.ROLES, keys[local[s]]):
                        scripts[s][key.removesuffix("_fix")] = [group.read(sealed / f"{role}-{s}.json")]
                    drivers[s].run_round(sessions[s])
                for s, (state, ids) in before.items():
                    reopened = group.Session(s, folder.name, root)
                    self.assertEqual(reopened.state, state)
                    self.assertEqual(reopened.store.ids(), ids)
                if not legacy and n == 1:
                    sessions = {s: group.Session(s, folder.name, root, tools=MockTools(root)) for s in members}
            for s in members:
                drivers[s].finalize(sessions[s])
            with self.assertRaisesRegex(ValueError, "scoped_items.json"):
                group.build_report(folder, root)
            scope = {}
            for symbol in members:
                doc = group.read(root / "data/debate" / symbol / f"{symbol}_Analysis_{folder.name}.json")
                refs = ["price"]
                for field, prefix in (("key_drivers", "driver"), ("risks", "risk")):
                    refs.extend(f"{prefix}:{i}" for i in range(len(doc["reasoning"][field])))
                refs.extend(f"review:{e['id']}" for e in doc["next_review"].get("events", []))
                scope[symbol] = refs
            local_ref = scope["TEST"].pop(0)
            catalog = [dict(id="G1", summary="Common driver", applies_to=scope),
                       dict(id="G2", summary="Local price rationale", applies_to={"TEST":[local_ref]})]
            from copy import deepcopy
            from workflows.group_report_view import bind_scoped_items
            raw = {"members": [group.read(root / "data/debate" / sym /
                               f"{sym}_Analysis_{folder.name}.json") for sym in members]}
            snapshot = deepcopy(raw)
            with self.assertRaisesRegex(ValueError, "missing group references"):
                bind_scoped_items(raw, catalog[:1])
            self.assertEqual(raw, snapshot)
            duplicate = deepcopy(raw)
            duplicate["members"][0]["next_review"]["events"] = [{"id": "E1"}, {"id": "E1"}]
            with self.assertRaisesRegex(ValueError, "duplicate review event"):
                bind_scoped_items(duplicate, catalog)
            group._atomic_json(folder / "scoped_items.json", catalog)
            report = group.build_report(folder, root)
            from workflows.group_report_view import project_member_report
            local = project_member_report(report, "TEST")
            peer = project_member_report(report, "PEER")
            if legacy:
                self.assertNotIn("hedge_summary", local)
            else:
                self.assertEqual(local["downside_protection"]["phase"], "easing")
                self.assertEqual(peer["downside_protection"]["phase"], "persistent")
                self.assertNotEqual(local["hedge_summary"], peer["hedge_summary"])
            self.assertEqual(local["short_term_path"]["group_refs"], ["G2"])
            self.assertEqual([x["id"] for x in peer["reasoning"]["group_context"]], ["G1"])
            self.assertNotIn("scoped_items", peer)
            self.assertEqual([m["symbol"] for m in report["members"]], members)
            self.assertEqual(len(report["debate_history"]), 2 * len(agendas))
            for turn in report["debate_history"]:
                n = int(turn["round"][1:])
                content = turn["content"]
                self.assertEqual(set(content["members"]), set(agendas[n - 1]))
                if legacy:
                    self.assertNotIn("member_rounds", content)
                else:
                    self.assertEqual(content["member_rounds"], {
                        s: sum(s in agenda for agenda in agendas[:n]) for s in agendas[n - 1]})
            self.assertNotIn("composite_score", report)
            group.verify_envelope(folder / "group.json", report, root)
            if not legacy:
                last = folder / f"R{len(agendas)}" / "exchange.json"
                altered = group.read(last)
                original = last.read_bytes()
                altered["turns"]["Zealot"]["shared"] += " Changed after signoff."
                group._atomic_json(last, altered)
                with self.assertRaisesRegex(ValueError, "differs from the sealed exchange"):
                    group.build_report(folder, root)
                last.write_bytes(original)
            report["members"][1]["composite_score"] += 1
            with self.assertRaisesRegex(ValueError, "reconstruction"):
                group.verify_envelope(folder / "group.json", report, root)
