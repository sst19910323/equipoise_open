"""Serve the frozen five-member drill over the real dashboard API in a disposable PG."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import zipfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import psycopg2
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from check_group_publication_pg import temporary_pg
from scripts import create_task_evidence_tables, odds_model, pg_store_debate, ruling, scenario_replay, translate_reports
from datalayer.pg.lowfreq_reader import get_debate_report
from workflows.execution_card import ensure_display_fields
from assemble_debate_artifacts import _derive_execution

SOURCE = ROOT / "docs/design/drill/sector-group-20261118T142959Z/ohlc-opening"
FIXTURES = ROOT / "docs/handoff/fixtures/group-report"
GROUP = "GROUP_US_CHIPS"
REASON = "Equal weights for the HTTP acceptance replay of the frozen third round."
STATEMENT = "Acceptance replay for {symbol}. The two frozen forecasts are weighted equally."


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scenario(member, p0):
    nodes = []
    for i, path in enumerate(member["paths"], 1):
        events = [{"t": t, "kind": "close", "price": path[f"d{t}"]} for t in (3, 5, 15)]
        nodes.append(dict(id=f"B{i}", label=path["label"], p=path["p"], P_T=path["d15"],
                          events=events, refs=member["refs"]))
    return dict(schema="3.4", T_days=15, P0=p0, nodes=nodes)


def decision(symbol, p0, z, r):
    # The drill has paths but no filed orders or contemporaneous final mixture.
    left, right = scenario(z, p0), scenario(r, p0)
    mixed = scenario_replay.mixture(left, right, 0.5, REASON)
    cfg = dict(schema="3.4", T_days=15, mean_shrink=0.33, stop_fill_without_open="next_price",
               gamma=6.25, cost_bps=2, reference_price=p0)
    candidates = odds_model.build_candidates({}, {}, mixed)
    d = odds_model.derive_for_report(mixed, candidates, cfg)
    if not d["ok"]:
        raise ValueError(d["fatal"])
    mixed["derived"] = dict(d, s_ref=d["score"]["S_ref"], unit_basis=cfg.get("unit_basis"))
    member = dict(symbol=symbol, statement=STATEMENT.format(symbol=symbol), scenario=mixed,
                  odds=cfg, s_ref=d["score"]["S_ref"], operation_simple=ruling.simple_fields(d, symbol, None),
                  dissent={"zealot_composite_score": ruling.derived_score(left),
                           "reaper_composite_score": ruling.derived_score(right),
                           "zealot_reservation": z["counterevidence"], "reaper_reservation": r["counterevidence"]},
                  **ruling.operation_fields(d, symbol, None))
    member["reference_prices"] = [{"symbol": symbol, "price": p0}]
    _derive_execution(member)
    ensure_display_fields(member, source="assembler")
    del member["reference_prices"]
    errors = scenario_replay.envelope_check(mixed, left, right) + ruling.recompute_check(mixed, cfg, member)
    if errors:
        raise ValueError((symbol, errors))
    return member


def build_group():
    bars = read(SOURCE / "ohlc.json")
    report = dict(report_kind="sector_group", symbol=GROUP, market_data_timestamp_utc=bars["cutoff"],
                  debate_summary=REASON, signatures={"zealot": "gpt-6.2-sol", "reaper": "gpt-6.2-sol"},
                  debate_history=[], members=[], reference_prices=[], model_calls=[], evidence=[])
    packet = (SOURCE / "R2-evidence.md").read_text(encoding="utf-8")
    roles = {"Z": "Zealot", "R": "Reaper"}
    for turn in (1, 2, 3):
        sides = {side: read(SOURCE / f"R{turn}-{side}.json") for side in roles}
        for side, doc in sides.items():
            sections = [doc["shared"]]
            for sym, member in doc["members"].items():
                sections.append(sym + "\n" + "\n".join(member[key] for key in
                                ("thesis", "counterevidence", "falsifier", "opponent_response", "revision") if key in member))
            report["debate_history"].append({"round": f"R{turn}", "role": roles[side],
                "content": {"summary_statement": doc["shared"], "analysis_text": "\n\n".join(sections[1:]),
                            "members": doc["members"]}})
            if turn == 1:
                call = side + ".MC1"
                ev = "EV" + str(len(report["evidence"]) + 1)
                report["model_calls"].append(dict(model_call_id=call, round="R1", requested_by=roles[side],
                    request=doc["request"], detail=packet, summary="Shared frozen evidence return.", evidence_refs=[ev]))
                report["evidence"].append(dict(model_call_id=call, round="R1", question=doc["request"],
                    simple="The funded share and dated supplier allocations remain unknown.", payload_text=packet,
                    source=["R2-evidence.md"]))
        if turn > 1:
            ruling_text = (SOURCE / f"R{turn}-ruling.md").read_text(encoding="utf-8")
            report["debate_history"].append(dict(round=f"R{turn}", role="Arbiter",
                                                 content={"analysis_text": ruling_text}))
    for sym, z in sides["Z"]["members"].items():
        days = bars["prices"][sym]["days"]
        last = max(days)
        if last != "2026-11-17":
            raise ValueError(f"Unexpected frozen price date for {sym}: {last}")
        p0 = days[last]["close"]
        report["members"].append(decision(sym, p0, z, sides["R"]["members"][sym]))
        report["reference_prices"].append(dict(symbol=sym, price=p0, currency="USD", as_of=last+"T21:00:00Z",
                                              session="regular_close", source="ohlc-opening/ohlc.json"))
    return report


def seed(cfg, directory):
    with closing(psycopg2.connect(**cfg)) as con, con.cursor() as cur:
        cur.execute(pg_store_debate._CREATE_SQL)
        cur.execute((ROOT / "datalayer/pg/group_sessions.sql").read_text(encoding="utf-8"))
        cur.execute(create_task_evidence_tables.DDL)
        con.commit()
    group = build_group()
    old = read(FIXTURES / "group.json")
    old["market_data_timestamp_utc"] = "20261118T142900Z"
    old["members"][0].update(operation_type="HOLD_NEUTRAL", odds={"conditional_fill_policy":"strict_next_open_v1"})
    solo = dict(symbol="V", market_data_timestamp_utc=old["market_data_timestamp_utc"], lang="en",
                composite_score=51, statement="Legacy single-stock acceptance fixture.")
    for report in (old, solo, group):
        if not pg_store_debate.save_debate(report["symbol"], report):
            raise RuntimeError(f"Failed to publish {report['symbol']}")
    glossary = read(FIXTURES / "replay-zh.json")
    for m in group["members"]:
        sym = m["symbol"]
        stage = translate_reports.build_extract(m, "en", "zh", "debates", sym, group["market_data_timestamp_utc"])
        p = m["scenario"]["P0"]
        terms = {**glossary, m["statement"]: f"{sym} 接口验收回放。两方冻结预测各占一半权重。",
                 f"{sym}: {p:g} vs reference {p:g} (0.00%)": f"{sym}：{p:g}，参考价 {p:g}（0.00%）"}
        stage.update(translator="curated-acceptance-fixture",
                     translations={i: terms[s] for i, s in stage["items"].items()})
        path = directory / f"{sym}-zh.json"
        path.write_text(json.dumps(stage, ensure_ascii=False), encoding="utf-8")
        receipt = translate_reports.load_staging(path)
        if not receipt["ok"]:
            raise RuntimeError(receipt)
    # Only the earlier, wholly synthetic edition has example postcheck results.
    from datalayer.pg import postcheck_store
    if int(postcheck_store.PG["port"]) != cfg["port"] or postcheck_store.PG["user"] != cfg["user"]:
        raise RuntimeError("Postcheck connection is outside the temporary cluster")
    if not postcheck_store.ensure_table():
        raise RuntimeError("Could not create the temporary postcheck schema")
    old_ts = old["market_data_timestamp_utc"]
    judgment = dict(symbol=GROUP, as_of=old_ts, dt=3, reckoner="fixture", members=[])
    for sym, score in (("MU", 44), ("AMD", 67)):
        stage = dict(symbol=sym, as_of=old_ts, dt=3, ruler="UI_FIXTURE", report=get_debate_report(sym, old_ts),
                     digest={"ruler": "UI_FIXTURE", "bars_in_window": 3, "fixture": True})
        path = directory / f"{sym}-postcheck.json"
        path.write_text(json.dumps(stage), encoding="utf-8")
        judgment["members"].append(dict(staging_path=path.name, score=score, verdict=f"Synthetic UI fixture for {sym}."))
    path = directory / "group-judgment.json"
    path.write_text(json.dumps(dict(judgment, members=judgment["members"][:1])), encoding="utf-8")
    if postcheck_store.save_from_judgment(str(path))["status"] != "error":
        raise RuntimeError("Incomplete group judgment accepted")
    path.write_text(json.dumps(judgment), encoding="utf-8")
    receipt = postcheck_store.save_from_judgment(str(path))
    if receipt["status"] != "saved":
        raise RuntimeError(receipt)
    for item, saved in zip(judgment["members"], receipt["members"], strict=True):
        sym = saved["symbol"]
        stage = translate_reports.build_extract({"verdict": item["verdict"]}, "en", "zh", "postcheck", sym, old_ts,
                                                dt=3, ruler="UI_FIXTURE")
        stage.update(translator="curated-acceptance-fixture", translations={"0": f"{sym} 的合成复盘展示样例。"})
        path = directory / f"{sym}-verdict-zh.json"
        path.write_text(json.dumps(stage, ensure_ascii=False), encoding="utf-8")
        if not translate_reports.load_staging(path)["ok"]:
            raise RuntimeError(f"Could not load {sym} fixture verdict translation")
    return group


def verify(port, group, export=None):
    opener = build_opener(ProxyHandler({}))
    responses = {}
    def get(endpoint, **params):
        route = "/api/" + endpoint + ("?" + urlencode(params) if params else "")
        try:
            with opener.open(f"http://127.0.0.1:{port}{route}", timeout=12) as response:
                data = json.load(response)
                if export:
                    responses[route] = {"status": response.status, "json": data}
                return data
        except HTTPError as exc:
            if export:
                responses[route] = {"status": exc.code, "json": json.load(exc)}
            raise
    cutoff = group["market_data_timestamp_utc"]
    home = {r["symbol"]: r for r in get("dashboard", lang="zh")}
    regions = get("symbol-tree")
    tree = [i["symbol"] for region in regions for g in region["groups"] for i in g["items"]]
    assert len(tree) == len(set(tree))
    assert {"GLD", "SMH", "XLE", "ITA", "IGV", "XLP"} <= set(tree) and "QQQ" not in tree
    catalog = next(g for r in regions for g in r["research_groups"] if g["symbol"] == GROUP)
    assert set(m["symbol"] for m in group["members"]) < set(catalog["members"]) and catalog["benchmark"] == "SMH"
    assert "SMH" in catalog["members"]
    assert catalog["label"] and catalog["label_en"]
    for symbol in ("COST", "RMS", "IBIDEN", "KIA"):
        assert symbol in tree and home[symbol]["path"] is None and home[symbol]["composite_score"] is None
        assert get(f"reports/{symbol}", months=0) == []
    for m in group["members"]:
        sym = m["symbol"]
        assert sym in tree
        history = get(f"reports/{sym}", months=0, lang="zh")
        row = history[0]
        en = get("file", path=row["path"], lang="en")
        zh = get("file", path=row["path"], lang="zh")
        assert home[sym]["path"] == row["path"] and row["as_of"] == cutoff
        assert home[sym]["composite_score"] == row["composite_score"] == en["composite_score"] == m["composite_score"]
        assert zh["lang_served"] == "zh" and zh["statement"] != en["statement"] and sym in zh["statement"]
        assert en["debate_history"] == zh["debate_history"] == group["debate_history"]
        assert en["debate_ref"] == {"symbol": GROUP, "as_of": cutoff}
        assert en["debate_members"] == zh["debate_members"] == [m["symbol"] for m in group["members"]]
        assert zh["reference_prices"] == home[sym]["reference_prices"] and en["reference_prices"][0]["symbol"] == sym
        assert zh["execution_card"] == home[sym]["execution_card"] and en["operation_simple"] == home[sym]["operation_simple"]
        assert zh["execution_card"]["one_line"] != en["execution_card"]["one_line"]
        assert not get("postcheck", symbol=sym, as_of=cutoff)["items"]
        if sym in ("MU", "AMD"):
            old = get("file", path=history[1]["path"], lang="zh")
            assert old["debate_members"] == ["MU", "AMD"]
            assert old["lang_served"] == "en" and bool(old["reference_prices"]) == (sym == "MU")
            assert bool(old["execution_card"].get("notices")) == (sym == "MU")
            items = get("postcheck", symbol=sym, as_of=history[1]["as_of"], lang="zh")["items"]
            assert [(r["symbol"], r["dt"], r["score"]) for r in items] == [(sym, 3, {"MU":44, "AMD":67}[sym])]
            assert items[0]["lang_served"] == "zh" and sym in items[0]["verdict"]
        print(sym, en["composite_score"], row["path"], "OK")
    for endpoint, key, id_key in (("tasks","model_calls","model_call_id"), ("evidence/items","items","evidence_id")):
        rows = get(endpoint, symbol=GROUP, as_of=cutoff)[key]
        assert len(rows) == 2
        for row in rows:
            item = get(endpoint, symbol=GROUP, as_of=cutoff, id=row[id_key])[key][0]
            assert item["model_call_id"] and item["detail" if key == "model_calls" else "payload_text"]
        assert not get(endpoint, symbol="MU", as_of=cutoff)[key]
    v = get("file", path=get("reports/V", months=0)[0]["path"], lang="zh")
    assert v["lang_served"] == "en" and "debate_ref" not in v
    try:
        get("file", path="pg/debates/MU/20261118T143000Z")
    except HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("Missing handle returned a different edition")
    if export:
        with zipfile.ZipFile(export, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("responses.json", json.dumps(responses, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--export", type=Path, help="Write verified HTTP responses to a ZIP file.")
    parser.add_argument("--serve-hours", type=float, default=0, help="Keep the verified API available after checks.")
    args = parser.parse_args()
    import equipoise_entry
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
        listener.listen()
        port = listener.getsockname()[1]
        with temporary_pg(args.pg_bin.resolve()) as cfg, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            group = seed(cfg, directory)
            # All file-backed API handles in this process are rooted in the temporary fixture directory.
            equipoise_entry.PROJECT_ROOT = directory
            equipoise_entry.DATA_CACHE_DIR = directory / "data"
            equipoise_entry.RESULTS_DIR = directory / "results"
            server = uvicorn.Server(uvicorn.Config(equipoise_entry.app, log_level="warning", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("Local test API could not start")
                    time.sleep(0.05)
                verify(port, group, args.export)
                print(f"Verified http://127.0.0.1:{port}/docs ; available for {args.serve_hours:g} hours.", flush=True)
                if args.serve_hours > 0:
                    thread.join(timeout=args.serve_hours * 3600)
            finally:
                server.should_exit = True
                thread.join()


if __name__ == "__main__":
    main()
