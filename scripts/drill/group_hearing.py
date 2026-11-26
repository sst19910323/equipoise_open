"""Open and assemble one operator-led sector hearing in a frontend sandbox."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import odds_model, ruling, scenario_replay
from scripts.odds_snapshot import build_odds_snapshot
from workflows.symbol_universe import load_symbol_config, research_group
from workflows.group_report_view import project_member_report
from workflows.execution_card import ensure_display_fields
from assemble_debate_artifacts import _derive_execution


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


roster = research_group


def freeze(key, group):
    now = datetime.now(timezone.utc)
    cutoff = now.strftime("%Y%m%dT%H%M%SZ")
    source = ROOT / "data"
    packet = dict(symbol=key, cutoff=cutoff, **group, markets={}, news={})
    contracts = load_symbol_config()["symbol_contracts"]
    for sym in [*group["members"], group["benchmark"]]:
        tech = max((source / "technical" / sym).glob(f"{sym}_technical_*.json"))
        as_of = tech.stem.rsplit("_", 1)[1]
        snapshot = read(tech)
        state = dict(region=group["region"], data_files={"symbol": {"technical": tech.relative_to(ROOT).as_posix()}})
        odds = build_odds_snapshot(state, sym, ROOT)
        if not odds["reference_price"] or as_of > cutoff:
            raise ValueError(f"Missing or future opening quote: {sym}")
        packet["markets"][sym] = dict(as_of=as_of, odds=odds, source=tech.name,
            daily=snapshot[f"{sym}_price_digest_daily"], currency=contracts[sym].get("currency", "USD"))
    for sym in [*packet["markets"], "GENERAL"]:
        news = max((source / "news" / sym).glob(f"{sym}_news_*.json"))
        if news.stem.rsplit("_", 1)[1] > cutoff:
            raise ValueError(f"Future news snapshot: {sym}")
        for item in read(news):
            when = item.get("datetime")
            if not isinstance(when, (int, float)) or when > now.timestamp() or not item.get("headline"):
                continue
            ref = str(item.get("id") or item.get("url") or item["headline"])
            entry = packet["news"].setdefault(ref, {k: item.get(k) for k in ("datetime", "headline", "summary", "source", "url")})
            entry.setdefault("feeds", []).append(sym)
    folder = ROOT / "output/group-hearings" / key / cutoff
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "opening.json", packet)
    return folder


def checked_scenario(filing, cfg, sym):
    sc = filing["scenario"]
    fatal, _, normalized = odds_model.validate_scenario(sc, cfg)
    if fatal or sc["P0"] != cfg["reference_price"] or sc["T_days"] != cfg["T_days"]:
        raise ValueError((sym, fatal, "check frozen P0 and horizon"))
    errors, orders = odds_model.validate_execution(filing["execution"], normalized)
    if errors or any(order.get("invalid") for order in orders):
        raise ValueError((sym, errors, "invalid execution filing"))
    return normalized


def assemble(folder):
    opening = read(folder / "opening.json")
    members = opening["members"]
    turns = []
    configs = {sym: odds_model.config_from_snapshot(opening["markets"][sym]["odds"]) for sym in members}
    if not members or len(members) != len(set(members)):
        raise ValueError("Opening roster is empty or contains duplicates")
    for rnd in (1, 2):
        sides, scenarios = {}, {}
        for side, role in (("Z", "Zealot"), ("R", "Reaper")):
            doc = read(folder / f"R{rnd}-{side}.json")
            if set(doc["members"]) != set(members):
                raise ValueError(f"R{rnd}-{side} roster mismatch")
            sides[side] = doc
            scenarios[side] = {s: checked_scenario(doc["members"][s], configs[s], s) for s in members}
            turns.append(dict(round=f"R{rnd}", role=role, content=dict(summary_statement=doc["shared"],
                analysis_text="\n\n".join(f"{s}: {doc['members'][s]['thesis']}" for s in members), members=doc["members"])))
    verdict = read(folder / "verdict.json")
    if set(verdict["members"]) != set(members):
        raise ValueError("Verdict roster mismatch")
    turns.append(dict(round="Final", role="Arbiter", content={"analysis_text": verdict["summary"], "members": verdict["members"]}))
    report = dict(report_kind="sector_group", symbol=opening["symbol"], market_data_timestamp_utc=opening["cutoff"],
        debate_summary=verdict["summary"], signatures=verdict["signatures"], debate_history=turns, members=[], reference_prices=[])
    for sym in members:
        market = opening["markets"][sym]
        cfg = configs[sym]
        p0 = cfg["reference_price"]
        vote = verdict["members"][sym]
        weight = vote["weight_z"]
        if type(weight) not in (float, int) or not 0 <= weight <= 1 or not vote["reason"].strip():
            raise ValueError(f"Invalid arbiter weight or reason: {sym}")
        filings = [sides[side]["members"][sym] for side in ("Z", "R")]
        z, r = (scenarios[side][sym] for side in ("Z", "R"))
        mixed = scenario_replay.mixture(z, r, weight, vote["reason"])
        candidates = odds_model.build_candidates(*(f["execution"] for f in filings), mixed, filers={"Z": z, "R": r})
        derived = odds_model.derive_for_report(mixed, candidates, cfg)
        if not derived["ok"]:
            raise ValueError((sym, derived["fatal"]))
        mixed["derived"] = dict(derived, s_ref=derived["score"]["S_ref"], unit_basis=cfg.get("unit_basis"))
        quote = dict(symbol=sym, price=p0, as_of=market["as_of"], currency=market["currency"], source=market["source"])
        member = dict(symbol=sym, statement=vote["reason"], scenario=mixed, odds=market["odds"],
            s_ref=derived["score"]["S_ref"], operation_simple=ruling.simple_fields(derived, sym, None),
            dissent={f"{role}_{key}": value for role, filing, sc in zip(("zealot", "reaper"), filings, (z, r))
                     for key, value in (("composite_score", ruling.derived_score(sc, cfg)), ("reservation", filing["reservation"]))},
            reference_prices=[quote], **ruling.operation_fields(derived, sym, None))
        _derive_execution(member)
        ensure_display_fields(member, source="assembler")
        del member["reference_prices"]
        errors = scenario_replay.envelope_check(mixed, z, r) + ruling.recompute_check(mixed, cfg, member, candidates=candidates)
        if errors:
            raise ValueError((sym, errors))
        report["reference_prices"].append(quote)
        report["members"].append(member)
    # Stage all views together. A failed close leaves no partially published example.
    target = folder / "frontend"
    if target.exists():
        if read(target / "group.json") != report or any(
                read(target / f"{s}.json") != project_member_report(report, s) for s in members):
            raise ValueError("The completed example differs from the new inputs")
        return target
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        stage = Path(tmp) / "frontend"
        stage.mkdir()
        write(stage / "group.json", report)
        for sym in members:
            write(stage / f"{sym}.json", project_member_report(report, sym))
        stage.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    op = sub.add_parser("open")
    op.add_argument("group")
    close = sub.add_parser("close")
    close.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "list":
        for key in load_symbol_config().get("research_groups", {}):
            print(key, ", ".join(roster(key)["members"]))
    elif args.action == "open":
        group = roster(args.group)
        subprocess.run([sys.executable, str(ROOT / "prepare_debate_data.py"), *group["members"]], cwd=ROOT, check=True)
        print(freeze(args.group, group))
    else:
        print(assemble(args.directory))


if __name__ == "__main__":
    main()
