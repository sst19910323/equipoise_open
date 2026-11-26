"""Formal group hearing. One arbiter, one sealed exchange per round, member-specific rulers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from assemble_debate_artifacts import _atomic_json
from closure_check import round_no
from ruling import final_records
from run_step import Session, gate, session_cfg
from validate_role_record import ingest
from workflows.group_report_view import SHARED_FIELDS
from workflows.symbol_universe import research_group

ROLES = ("Zealot", "Reaper")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_dir(root, key, cutoff):
    if not re.fullmatch(r"[A-Z0-9_]+", key) or not re.fullmatch(r"\d{8}T\d{6}Z", cutoff):
        raise ValueError("Invalid group key or cutoff")
    return root / "data" / "group_runs" / key / cutoff


def fingerprint(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def participants(opening, turns):
    roster = set(opening["members"])
    if set(turns) != set(ROLES):
        raise ValueError("Both sides must file before sealing")
    for role in ROLES:
        turn = turns[role]
        if not isinstance(turn.get("members"), dict) or not isinstance(turn.get("shared"), str) or not turn["shared"].strip():
            raise ValueError(f"{role}: need shared argument and member filings")
    active = set(turns[ROLES[0]]["members"])
    if not active or not active <= roster or active != set(turns[ROLES[1]]["members"]):
        raise ValueError("Both sides must address the same nonempty subset of the frozen roster")
    if opening.get("round_scope") != "agenda" and active != roster:
        raise ValueError("This opening requires the full frozen roster")
    return [sym for sym in opening["members"] if sym in active]


def sealed_history(folder, opening):
    """Index immutable group exchanges by each member's own contiguous rounds."""
    rounds = {}
    by_member = {sym: [] for sym in opening["members"]}
    dirs = sorted((p for p in folder.iterdir() if re.fullmatch(r"R[1-9]\d*", p.name)),
                  key=lambda p: int(p.name[1:]))
    for n, path in enumerate(dirs, start=1):
        seal = read(path / "exchange.json")
        if path.name != f"R{n}" or seal["round"] != n or seal["ref"] != opening["ref"]:
            raise ValueError("Invalid sequence or identity of sealed exchanges")
        rounds[n] = seal
        for sym in participants(opening, seal["turns"]):
            by_member[sym].append(n)
    return rounds, by_member


def verify_filing(session, n, role, content):
    ref = session.state["group_session"]
    folder = run_dir(session.root, ref["symbol"], ref["as_of"])
    rounds, by_member = sealed_history(folder, read(folder / "opening.json"))
    entries = by_member[session.symbol]
    if not 1 <= n <= len(entries):
        raise ValueError(f"{session.symbol}: missing sealed filing for local R{n}")
    seal = rounds[entries[n - 1]]
    expected = dict(seal["turns"][role]["members"][session.symbol], group_turn_hash=fingerprint(seal))
    if seal["ref"] != ref or content != expected:
        raise ValueError(f"{session.symbol}: final is not this hearing's sealed R{n} {role} filing")


def freeze_group(root, key, group, model, worker):
    cutoff = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = run_dir(root, key, cutoff)
    folder.mkdir(parents=True, exist_ok=False)
    ref = {"symbol": key, "as_of": cutoff}
    opening = dict(ref=ref, members=list(group["members"]), region=group["region"], benchmark=group["benchmark"],
                   group_definition=group, scoped_items_version=1, round_scope="agenda",
                   odds={}, quotes={}, materials={}, model=model, worker_model=worker)
    for sym in opening["members"]:
        session = Session.open(sym, root, engine="codex", model=model, worker_model=worker, group_ref=ref)
        quotes = session._reference_prices()
        if session.state["region"] != group["region"] or len(quotes) != 1 or quotes[0]["symbol"] != sym:
            raise ValueError(f"{sym}: missing member quote or incorrect trading region")
        stamp = datetime.fromisoformat(str(quotes[0]["as_of"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > datetime.fromisoformat(cutoff.replace("Z", "+00:00")):
            raise ValueError(f"{sym}: invalid quote timestamp")
        opening["quotes"][sym] = quotes
        opening["odds"][sym] = session.state["odds"]
        opening["materials"][sym] = session.open_receipt()["spawn"]
    _atomic_json(folder / "opening.json", opening)
    return folder


def exchange(folder, round_no, turns, root):
    """Validate both sides and all members before making any routable final available."""
    opening = read(folder / "opening.json")
    ref = opening["ref"]
    target = folder / f"R{round_no}"
    seal = dict(ref=ref, round=round_no, turns=turns)
    if target.exists():
        if read(target / "exchange.json") != seal:
            raise ValueError("This round already has a different sealed exchange")
        return target
    members = participants(opening, turns)
    prior, by_member = sealed_history(folder, opening)
    if round_no != len(prior) + 1:
        raise ValueError("Missing previous sealed exchange")
    if prior:
        for sym in participants(opening, prior[round_no - 1]["turns"]):
            state = Session(sym, ref["as_of"], root).state
            if state.get("group_session") != ref or state.get("status") not in ("debating", "finalizing", "done"):
                raise ValueError(f"{sym}: missing or mismatched prior member ledger")
            if state["status"] == "debating" and state["round"] <= len(by_member[sym]):
                raise ValueError(f"{sym}: finish routing and closing the previous sealed round first")
    digest = fingerprint(seal)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        stage = Path(tmp) / "sealed"
        stage.mkdir()
        for role, turn in turns.items():
            for sym in members:
                s = Session(sym, ref["as_of"], root)
                local_round = len(by_member[sym]) + 1
                if s.state.get("group_session") != ref or s.state.get("round") != local_round or s.state.get("status") != "debating":
                    raise ValueError(f"{sym}: member ledger is not at the round barrier")
                content = dict(turn["members"][sym], group_turn_hash=digest)
                if (content.get("beat") or {}).get("status", "final") != "final":
                    raise ValueError(f"{sym}: unresolved draft at round barrier")
                errors, _ = ingest(content, role, local_round, final_records(s.store), session_cfg(s),
                                   ref_price=opening["odds"][sym]["reference_price"])
                if errors:
                    raise ValueError(f"{role}/{sym}: {errors}")
                _atomic_json(stage / f"{role}-{sym}.json", content)
        _atomic_json(stage / "exchange.json", seal)
        stage.rename(target)
    return target


def group_evidence(doc, symbol, archive):
    """Member-local store IDs are qualified on the shared evidence page."""
    offset = len(archive["evidence"])
    for item in doc["evidence"]:
        item = dict(item, member_symbol=symbol)
        for key in ("model_call_id", "task_id"):
            if item.get(key):
                item[key] = f"{symbol}.{item[key]}"
        archive["evidence"].append(item)
    for task in doc["model_calls"]:
        task = dict(task, model_call_id=f"{symbol}.{task['model_call_id']}", member_symbol=symbol)
        refs = []
        for ev in task.get("evidence_refs", []):
            match = re.fullmatch(r"EV([1-9]\d*)", str(ev))
            if not match or int(match[1]) > len(doc["evidence"]):
                raise ValueError(f"{symbol}: invalid evidence reference {ev}")
            refs.append(f"EV{offset + int(match[1])}")
        task["evidence_refs"] = refs
        archive["model_calls"].append(task)


def build_report(folder, root):
    opening = read(folder / "opening.json")
    ref, members = opening["ref"], opening["members"]
    if folder.resolve() != run_dir(root, ref["symbol"], ref["as_of"]).resolve():
        raise ValueError("Opening does not belong to this formal hearing")
    if len(set(members)) != len(members) or len(members) < 2 or set(opening["odds"]) != set(members):
        raise ValueError("Invalid frozen roster")
    report = dict(report_kind="sector_group", symbol=ref["symbol"], market_data_timestamp_utc=ref["as_of"],
                  members=[], reference_prices=[], debate_history=[], evidence=[], model_calls=[])
    if "group_definition" in opening:
        report["group_definition"] = opening["group_definition"]
    history = {}
    turns, by_member = sealed_history(folder, opening)
    digests = {n: fingerprint(seal) for n, seal in turns.items()}
    summaries = []
    stamps = []
    for sym in members:
        s = Session(sym, ref["as_of"], root)
        version = s.state.get("ruling_version")
        required = {f"RULING.v{version}", f"SIGN{version}.Z", f"SIGN{version}.R"}
        if (s.state.get("status") != "done" or s.state.get("group_session") != ref
                or not version or not required <= s.store.ids()):
            raise ValueError(f"{sym}: signed-off group session required")
        path = root / "data/debate" / sym / f"{sym}_Analysis_{ref['as_of']}.json"
        evidence = root / "data/evidence" / sym / path.name.replace("_Analysis_", "_Evidence_")
        doc = read(path)
        if (doc.get("symbol") != sym or doc.get("group_session") != ref or doc["odds"] != opening["odds"][sym]
                or doc["reference_prices"] != opening["quotes"][sym] or doc["evidence"] != read(evidence)):
            raise ValueError(f"{sym}: report differs from its opening or evidence archive")
        verdict = gate(path, root)
        if not verdict["ok"]:
            raise ValueError(f"{sym}: {verdict['fatal']}")
        if report.setdefault("signatures", doc["signatures"]) != doc["signatures"]:
            raise ValueError(f"{sym}: signatures differ across hearing")
        rows = final_records(s.store)
        indexed = {(int(r["round"]), ROLES[0 if r["role"] == "Z" else 1]): r["content"] for r in rows}
        if not indexed:
            raise ValueError(f"{sym}: missing archived finals")
        keys = {(n, side) for n in range(1, len(by_member[sym])+1) for side in ROLES}
        if set(indexed) != keys or len(doc["debate_history"]) != len(keys):
            raise ValueError(f"{sym}: incomplete member rounds")
        seen = set()
        for event in doc["debate_history"]:
            n, role = round_no(event["round"]), event["role"]
            key = (n, role)
            if key in seen or key not in keys:
                raise ValueError(f"{sym}: duplicate or unknown final")
            seen.add(key)
            group_round = by_member[sym][n - 1]
            seal = turns[group_round]
            raw = dict(seal["turns"][role]["members"][sym], group_turn_hash=digests[group_round])
            previous = [r for r in rows if r["round"] < n]
            errors, normalized = ingest(raw, role, n, previous, session_cfg(s), ref_price=doc["odds"]["reference_price"])
            if (errors or indexed[key] != normalized
                    or event["content"] != normalized):
                raise ValueError(f"{sym}: {role} R{n} differs from the sealed exchange")
            history.setdefault((group_round, role), {})[sym] = event["content"]
        group_evidence(doc, sym, report)
        summaries.append(f"{sym}: {doc['debate_summary']}")
        stamps.append(doc["report_generated_at"])
        report["reference_prices"].extend(doc["reference_prices"])
        report["members"].append({k:v for k,v in doc.items() if k not in SHARED_FIELDS and k not in
                                  ("group_session","reference_prices")})
    for n, role in sorted(history, key=lambda k:(k[0], ROLES.index(k[1]))):
        content = turns[n]["turns"][role]["shared"]
        entries = history[n, role]
        detail = "\n\n".join(f"{s}: {entry.get('analysis_text') or entry['summary_statement']}" for s, entry in entries.items())
        scope = {}
        if opening.get("round_scope") == "agenda":
            scope["member_rounds"] = {s: by_member[s].index(n) + 1 for s in entries}
        report["debate_history"].append(dict(round=f"R{n}", role=role, content=dict(
            summary_statement=content, analysis_text=detail, members=entries, **scope)))
    report.update(debate_summary="\n\n".join(summaries), report_generated_at=max(stamps),
                  evidence_generated_at=max(stamps))
    if "scoped_items_version" in opening:
        if type(opening["scoped_items_version"]) is not int or opening["scoped_items_version"] != 1:
            raise ValueError("Unsupported scoped_items_version")
        path = folder / "scoped_items.json"
        if not path.exists():
            raise ValueError("Write scoped_items.json for this hearing before assembly")
        from workflows.group_report_view import bind_scoped_items
        bind_scoped_items(report, read(path))
    return report


def verify_envelope(path, report, root):
    folder = run_dir(root, report["symbol"], report["market_data_timestamp_utc"])
    if path.resolve() != (folder / "group.json").resolve() or build_report(folder, root) != report:
        raise ValueError("Group publication requires the exact reconstruction of this formal hearing")


def prepare_group(root, py, key, members, env, timeout):
    budget = timeout if timeout is not None else 180 * len(members)
    print(f"Preparing {key}: {len(members)} members in one IBKR batch, "
          f"{budget}s after lock acquisition", flush=True)
    proc = subprocess.run([py, "prepare_debate_data.py", "--active-timeout", str(budget), *members],
                          cwd=root, env=env)
    if proc.returncode == -signal.SIGALRM:
        print(f"{key}: active preparation exceeded {budget}s after acquiring the lock", file=sys.stderr)
        return 124
    return proc.returncode


def launch(args, root, harness, child_command, run_child):
    """Trusted dispatcher. A resume always names the existing cutoff."""
    import yaml
    key = args.group.upper()
    group = research_group(key) if not args.resume_group else None
    if args.dry_run:
        members = group["members"] if group else read(run_dir(root,key,args.resume_group) / "opening.json")["members"]
        print(json.dumps(dict(group=key, slots=1, members=members, arbiter_processes=1,
                              harness=harness.name)))
        return 0
    import fcntl  # The production launcher runs on Linux; offline plans are portable.
    if args.resume_group:
        run_dir(root, key, args.resume_group)
    parent = root / "data/group_runs" / key
    parent.mkdir(parents=True, exist_ok=True)
    py = str(root / "venv/bin/python")
    env = harness.child_env()
    env.setdefault("EQUIPOISE_IBKR_PORT", "4001")
    env["CODEX_BIN"] = harness.executable
    with (parent / ".run.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.resume_group:
            folder = run_dir(root, key, args.resume_group)
        else:
            rc = prepare_group(root, py, key, group["members"], env, args.prepare_timeout)
            if rc not in (0, 12):  # 12: fresh inputs; solo reports already exist at their anchors.
                return rc
            folder = freeze_group(root, key, group, args.model, "gpt-6-luna")
        opening = read(folder / "opening.json")
        print(f"Group session: {folder}", flush=True)
        path = folder / "group.json"
        if not path.exists():
            prompt = (f"Host one independent formal group hearing. Read "
                      f"skills/equipoise-run-debates/references/group-production.md first. "
                      f"GROUP_DIRECTORY={folder.relative_to(root).as_posix()} "
                      f"Frozen roster ({len(opening['members'])} members): {', '.join(opening['members'])}. "
                      "Round and research references are calibrated on one symbol. "
                      f"JUDGE_MODEL={opening['model']} WORKER_MODEL={opening['worker_model']}. "
                      "The trusted parent has opened the member ledgers. Complete the hearing and assemble group.json.")
            cmd = child_command(prompt, folder / "arbiter-receipt.txt", "workspace-write", opening["model"])
            rc, _, err, killed = run_child(cmd, args.timeout, args.stall_timeout)
            if rc != 0 or killed or not path.is_file():
                raise ValueError(f"Arbiter failed ({rc}): {killed or err[-800:]}; resume {folder.name}")
        subprocess.run([py, "scripts/pg_store_debate.py", key, str(path)], cwd=root, env=env, check=True)
        policy = yaml.safe_load((root / "config/i18n.yaml").read_text(encoding="utf-8"))
        lang = policy["owner_lang"]
        if lang != policy["core_lang"]:
            for sym in opening["members"]:
                stage = folder / f"{sym}-{lang}.json"
                translator = [py, "scripts/translate_reports.py"]
                if not stage.exists():
                    subprocess.run(translator + ["--extract", "--table", "debates", "--symbol", sym, "--as-of",
                                   folder.name, "--to", lang, "--out", str(stage)], cwd=root, env=env, check=True)
                from translate_reports import validate_translations
                if not read(stage).get("translator") or validate_translations(read(stage)):
                    subprocess.run(translator + ["--fill", str(stage), "--engine", "codex"], cwd=root, env=env, check=True)
                subprocess.run(translator + ["--load", str(stage)], cwd=root, env=env, check=True)
        print(json.dumps(dict(ok=True, group=key, as_of=folder.name, members=opening["members"])))
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seal", "assemble"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--round", type=int)
    parser.add_argument("--zealot", type=Path)
    parser.add_argument("--reaper", type=Path)
    a = parser.parse_args()
    if a.command == "seal":
        if not (a.round and a.zealot and a.reaper):
            parser.error("seal requires --round, --zealot and --reaper")
        print(exchange(a.directory, a.round, dict(zip(ROLES, (read(a.zealot), read(a.reaper)))), ROOT))
    else:
        _atomic_json(a.directory / "group.json", build_report(a.directory, ROOT))
        print(a.directory / "group.json")
