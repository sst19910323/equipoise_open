"""Exercise the built dashboard with synthetic reports and intercepted HTTP requests."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workflows.group_report_view import project_member_report


def check(dist: Path):
    fixture = ROOT / "docs/handoff/fixtures/group-report/group.json"
    group = json.loads(fixture.read_text(encoding="utf-8"))
    cutoff = group["market_data_timestamp_utc"]
    # The list uses compact UTC; the group archive can identify that instant in ISO.
    group["market_data_timestamp_utc"] = "2026-11-18T14:29:59Z"
    group["members"][0]["scenario"] = {"P0": 100, "T_days": 3, "nodes": [
        {"id": "up", "P_T": 105, "p": 0.5}, {"id": "down", "P_T": 95, "p": 0.5}]}
    for member in group["members"]:
        member["dissent"] = {"zealot_composite_score": 60, "reaper_composite_score": 40}
    reports = {m["symbol"]: project_member_report(group, m["symbol"]) for m in group["members"]}
    reports["V"] = {"symbol": "V", "composite_score": 51, "debate_history": group["debate_history"]}
    calls, errors = [], []
    tree = [{"region": "US", "region_label": "US", "groups": [
        {"group": "Fixture", "items": [{"symbol": s, "type": "stock"} for s in reports]}]}]

    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname != "127.0.0.1":
            return route.abort()
        if url.port != 8001:
            rel = url.path.removeprefix("/equipoise_dash/") or "index.html"
            asset = (dist / rel).resolve()
            if not asset.is_relative_to(dist) or not asset.is_file():
                return route.fulfill(status=404)
            mime = "application/javascript" if asset.suffix == ".js" else mimetypes.guess_type(asset)[0]
            return route.fulfill(path=asset, content_type=mime or "application/octet-stream")
        path = url.path.removeprefix("/api/")
        q = dict(parse_qsl(url.query))
        if path == "symbol-tree":
            data = tree
        elif path == "dashboard":
            data = [{"symbol": s} for s in reports]
        elif path.startswith("reports/"):
            sym = path.split("/")[-1]
            data = [{"path": sym, "as_of": cutoff, "timestamp": 1795012199}]
        elif path == "file":
            data = reports[q["path"]]
        elif path in ("tasks", "evidence/items"):
            owner = q.get("symbol")
            calls.append((path, q.get("id"), owner, q.get("as_of")))
            if path == "tasks":
                data = {"model_calls": [{"model_call_id": "T1", "model": "P01", "round": "R1",
                                         "evidence_ids": ["E1"], "detail": f"Task detail for {owner}"}]}
            else:
                data = {"items": [{"model_call_id": "T1", "round": "R1", "evidence_id": "E1",
                                   "payload_text": f"Evidence for {owner}"}]}
        else:
            data = {}
        return route.fulfill(json=data, headers={"Access-Control-Allow-Origin": "*"})

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(service_workers="block")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", serve)
        page.goto("http://127.0.0.1:5173/equipoise_dash/")
        page.locator(".ticker-card").filter(has_text="MU").click()
        for sym, report in reports.items():
            if sym != "MU":
                page.locator(".symbol-select").select_option(sym)
            page.locator(".tab-btn").first.click()
            expect(page.locator(".score-text")).to_have_text(str(report["composite_score"]))
            if sym == "MU":
                expect(page.locator(".lb-p0")).to_contain_text("100")
            else:
                expect(page.locator(".lb-p0")).to_have_count(0)
            calls.clear()
            page.locator(".tab-btn").nth(1).click()
            owner = group["symbol"] if sym in ("MU", "AMD") else sym
            stamp = report.get("debate_ref", {}).get("as_of", cutoff)
            if "debate_ref" in report:
                expect(page.locator(".sig-divider")).to_contain_text(sym)
                expect(page.locator(".sig-summary")).to_contain_text(str(report["composite_score"]))
            page.locator(".re-task-top").click()
            page.locator(".ev-head").click()
            expected = {(path, detail, owner, stamp) for path, key in
                        [("tasks", "T1"), ("evidence/items", "E1")] for detail in [None, key]}
            page.locator(".ev-pre").wait_for()
            assert set(calls) == expected, (sym, calls, expected)
            expect(page.locator(".ev-pre")).to_have_text(f"Evidence for {owner}")
            expect(page.locator(".re-detail-text")).to_have_text(f"Task detail for {owner}")
            print(sym, report["composite_score"], owner, stamp, "index and expanded details OK")
            # Return to the verdict so the next member starts without a debate request.
            page.locator(".tab-btn").first.click()
        for late_url, body in [
            ("**/api/file?path=MU&*", reports["MU"]),
            ("**/api/reports/MU?*", [{"path": "MU", "as_of": cutoff}]),
        ]:
            delayed = []
            page.route(late_url, lambda route: delayed.append(route))
            with page.expect_request(lambda request: "/reports/MU" in request.url if "reports/MU" in late_url
                                     else "/file?path=MU" in request.url):
                page.locator(".symbol-select").select_option("MU")
            page.locator(".symbol-select").select_option("AMD")
            expect(page.locator(".score-text")).to_have_text("61")
            assert len(delayed) == 1, late_url
            delayed[0].fulfill(json=body, headers={"Access-Control-Allow-Origin": "*"})
            page.wait_for_load_state("networkidle")
            expect(page.locator(".score-text")).to_have_text("61")
            page.unroute(late_url)
        print("late member list and detail responses preserve the selected stock")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    check(parser.parse_args().dist.resolve())
