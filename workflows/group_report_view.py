"""Project a frozen group report into the dashboard's single-symbol detail shape.

Offline entry point: python -m workflows.group_report_view REPORT.json SYMBOL
"""
import argparse
from copy import deepcopy
import json
import re
from pathlib import Path


SHARED_FIELDS = frozenset({
    "market_data_timestamp_utc", "report_generated_at", "debate_history",
    "signatures", "debate_summary", "shared_context",
})


def bind_scoped_items(report: dict, items: list) -> None:
    """Bind authored session items to the signed member rows at assembly."""
    if "scoped_items" in report:
        raise ValueError("group report already has scoped items")
    members = {m["symbol"]: m for m in report["members"]}
    targets = {}
    bindings = {}
    contexts = {symbol: [] for symbol in members}
    for symbol, member in members.items():
        reasoning = member["reasoning"]
        if "group_context" in reasoning:
            raise ValueError(f"{symbol}: group_context is generated at assembly")
        rows = {"price": member.get("short_term_path")}
        for field, prefix in (("risks", "risk"), ("key_drivers", "driver")):
            for index, row in enumerate(reasoning.get(field, [])):
                rows[f"{prefix}:{index}"] = row
        for event in member["next_review"].get("events", []):
            event_id = event.get("id") if isinstance(event, dict) else None
            ref = f"review:{event_id}"
            if not isinstance(event_id, str) or not event_id.strip() or ref in rows:
                raise ValueError(f"{symbol}: missing or duplicate review event id {event_id!r}")
            rows[ref] = event
        if any(not isinstance(row, dict) or "group_refs" in row for row in rows.values()):
            raise ValueError(f"{symbol}: expected report objects without generated group_refs")
        targets[symbol] = rows
        bindings[symbol] = {ref: [] for ref in rows}
    if not isinstance(items, list) or not items:
        raise ValueError("scoped_items must be a nonempty list")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "summary", "applies_to"}:
            raise ValueError("expected scoped item with id, summary and applies_to")
        key, summary, scope = item["id"], item["summary"], item["applies_to"]
        if not isinstance(key, str) or not re.fullmatch(r"G[1-9][0-9]*", key) or key in seen:
            raise ValueError(f"invalid or duplicate session item id {key!r}")
        seen.add(key)
        if not isinstance(summary, str) or not summary.strip() or not isinstance(scope, dict) or not scope:
            raise ValueError(f"{key}: expected summary and affected members")
        for symbol, refs in scope.items():
            if (symbol not in members or not isinstance(refs, list) or not refs
                    or not all(isinstance(r, str) and r in targets[symbol] for r in refs)
                    or len(set(refs)) != len(refs)):
                raise ValueError(f"{key}: invalid member or report references for {symbol}")
            for ref in refs:
                bindings[symbol][ref].append(key)
            contexts[symbol].append({"id": key, "summary": summary,
                                     "applies_to": [s for s in members if s in scope]})
    for symbol, refs in bindings.items():
        missing = [ref for ref, ids in refs.items() if not ids]
        if missing:
            raise ValueError(f"{symbol}: missing group references for {missing}")
    for symbol, refs in bindings.items():
        for ref, ids in refs.items():
            targets[symbol][ref]["group_refs"] = ids
        members[symbol]["reasoning"]["group_context"] = contexts[symbol]
    report["scoped_items"] = items


def _scope_rows(member):
    """Yield address, row and display field in stable archive order."""
    price = member.get("short_term_path")
    if isinstance(price, dict):
        yield "price", price, "rationale"
    reasoning = member.get("reasoning") or {}
    for field, prefix, label in (("key_drivers", "driver", "factor"), ("risks", "risk", "risk")):
        for i, row in enumerate(reasoning.get(field, [])):
            yield f"{prefix}:{i}", row, label
    for event in (member.get("next_review") or {}).get("events", []):
        yield f"review:{event['id']}", event, "name"


def group_for_display(report):
    """Adapt an archived hearing for readers only. Publication uses the original envelope."""
    if report.get("report_kind") != "sector_group":
        return report
    if "scoped_items" in report:
        return dict(report, scoped_items_version=1, scope_origin="authored")
    catalog = {}
    members = []
    for original in report["members"]:
        member = dict(original)
        symbol = member["symbol"]
        for key in ("reasoning", "next_review", "short_term_path"):
            if key in member:
                member[key] = deepcopy(member[key])
        members.append(member)
        for ref, row, field in _scope_rows(member):
            summary = row.get(field)
            if not isinstance(summary, str) or not summary.strip():
                continue
            # Join only an explicitly dated common macro event or identical substantive rows.
            key = (symbol, ref)
            if field == "name" and row.get("who") == "macro" and row.get("date"):
                key = ("macro", summary, row["date"], row.get("date_precision"))
            elif field in ("risk", "factor"):
                key = (field, json.dumps(row, sort_keys=True, ensure_ascii=False))
            if key not in catalog:
                catalog[key] = dict(id=f"G{len(catalog) + 1}", summary=summary, applies_to={})
            item = catalog[key]
            item["applies_to"].setdefault(symbol, []).append(ref)
            row["group_refs"] = [item["id"]]
    for member in members:
        member.setdefault("reasoning", {})["group_context"] = [
            dict(id=item["id"], summary=item["summary"], applies_to=list(item["applies_to"]))
            for item in catalog.values() if member["symbol"] in item["applies_to"]]
    return dict(report, members=members, scoped_items=list(catalog.values()),
                scoped_items_version=1, scope_origin="legacy_mapped")


def localize_group_scope(source, localized):
    """Keep machine links authoritative across whole-subtree translation overlays."""
    doc = dict(localized)
    for key in ("reasoning", "next_review", "short_term_path"):
        if key in doc:
            doc[key] = deepcopy(doc[key])
    # Driver and risk positions are part of the overlay contract. If a historical
    # overlay lost rows, use the complete source list for that field.
    for field in ("key_drivers", "risks"):
        rows = source["reasoning"].get(field, [])
        translated = doc["reasoning"].get(field, [])
        if len(translated) != len(rows):
            doc["reasoning"][field] = deepcopy(rows)
    original_rows = {ref: row for ref, row, _ in _scope_rows(source)}
    labels = {}
    for ref, row, field in _scope_rows(doc):
        ids = original_rows.get(ref, {}).get("group_refs", [])
        if ids:
            row["group_refs"] = list(ids)
            for key in ids:
                labels[key] = row.get(field)
    translated = {item["id"]: item.get("summary")
                  for item in doc["reasoning"].get("group_context", []) if "id" in item}
    if source["scope_origin"] == "legacy_mapped":
        translated = labels
    doc["reasoning"]["group_context"] = [
        dict(item, summary=translated.get(item["id"]) or item["summary"])
        for item in source["reasoning"]["group_context"]]
    return doc


def project_member_report(report: dict, symbol: str) -> dict:
    """Return an independent view. A missing member or malformed envelope is an error."""
    wanted = symbol.strip().upper()
    if report.get("report_kind") != "sector_group":
        if not wanted or report.get("symbol") != wanted:
            raise ValueError("report does not belong to the requested symbol")
        return deepcopy(report)

    group = report.get("symbol")
    cutoff = report.get("market_data_timestamp_utc")
    members = report.get("members")
    if not isinstance(group, str) or not group or not isinstance(cutoff, str) or not cutoff:
        raise ValueError("group report needs a frozen symbol and cutoff")
    if not isinstance(members, list) or not members:
        raise ValueError("group report needs a frozen member list")
    if not isinstance(report.get("debate_history"), list):
        raise ValueError("group debate history is missing")

    selected = None
    symbols = set()
    for member in members:
        name = member.get("symbol") if isinstance(member, dict) else None
        if not isinstance(name, str) or not name or name != name.strip().upper() or name in symbols:
            raise ValueError("invalid or duplicate member symbol")
        if name == group or SHARED_FIELDS.intersection(member) or any(
            k in member for k in ("members", "debate_members", "debate_ref", "report_kind", "by_lens", "lenses",
                                  "final_report", "reference_prices")
        ):
            raise ValueError(f"conflicting group and member fields for {name}")
        symbols.add(name)
        if name == wanted:
            selected = member
    if selected is None:
        raise ValueError(f"{wanted} is not in this frozen group")

    view = {key: report[key] for key in SHARED_FIELDS if key in report}
    view.update(selected)
    if "scope_origin" in report:
        view.update(scope_origin=report["scope_origin"], scoped_items_version=report["scoped_items_version"])
    prices = report.get("reference_prices", [])
    if not isinstance(prices, list) or any(not isinstance(p, dict) for p in prices):
        raise ValueError("invalid reference price list")
    view["reference_prices"] = [p for p in prices if p.get("symbol") == wanted]
    if len(view["reference_prices"]) > 1:
        raise ValueError(f"duplicate opening quote for {wanted}")
    view["debate_ref"] = {"symbol": group, "as_of": cutoff}
    view["debate_members"] = [member["symbol"] for member in members]
    return deepcopy(view)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("symbol")
    args = parser.parse_args()
    document = json.loads(args.report.read_text(encoding="utf-8-sig"))
    print(json.dumps(project_member_report(document, args.symbol), ensure_ascii=False))
