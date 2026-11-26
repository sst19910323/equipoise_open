# -*- coding: utf-8 -*-
"""execution_card (2026-11-03): the contract's projection must say "now: hold" for a conditional entry, never a resting
limit; a plain limit below the reference is a resting order; lens-wise reports carry one card per lens with the top level
following the primary lens; unresolved claims resolve to the final filed text; the card is idempotent (fingerprint)."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflows.execution_card import ensure_display_fields, build_card  # noqa: E402
from datalayer.pg import i18n  # noqa: E402


def _amd():
    return {
        "symbol": "AMD", "operation_type": "LIMIT_ENTRY", "operation_target": 520.4, "operation_size": "AGGRESSIVE_SIZE",
        "operation_execution": {"derived": "LIMIT_ENTRY", "declared": "ENTRY", "reference": "opening_snapshot",
                                "conditional": {"fill": "next_open", "observe": "close > 520.4"}, "stop": None},
        "odds": {"reference_price": 492.5},
        "scenario": {"derived": {"chosen": "overweight_on_condition[close>520.4|next_open|stop:none]",
                                 "candidates": [{"id": "overweight_on_condition[close>520.4|next_open|stop:none]",
                                                 "strategy": "overweight_on_condition", "direction": "overweight",
                                                 "kind": "close", "op": ">", "level": 520.4, "fill": "next_open", "stop": None}]},
                     "nodes": [{"id": "M1", "label": "Material shipment impairment"}], "mixture": {"w": 0.55, "reason": "Modest preference"}},
        "deadlocked": ["R3"], "unresolved": [],
        "debate_history": [
            {"id": "R1.R", "seq": 1, "content": {"claims": [{"id": "R3", "claim": "old text"}]}},
            {"id": "R4.R", "seq": 7, "content": {"claims": [{"id": "R3", "claim": "The fifteen-day distribution favors partial exposure"}]}},
        ],
        "report_generated_at": "2026-11-02T16:34:44Z", "statement": "x",
    }


def test_conditional_entry_is_hold_not_resting_limit():
    r = ensure_display_fields(_amd(), source="test")
    c = r["execution_card"]
    assert c["kind"] == "conditional_entry" and c["now"] == "hold" and c["pending"] is True
    assert c["resting_order"] is False and c["relation"] == "above"
    assert c["trigger"] == {"observe": "close > 520.4", "field": "close", "comparator": ">", "level": 520.4, "fill": "next_open"}
    assert c["one_line"] == "Hold; buy at next open after a close above 520.40"   # headline: now, then the trigger; > is not >=
    assert len(c["one_line"].split()) <= 12
    assert c["size"]["direction"] == "overweight" and c["size"]["tier"] == "AGGRESSIVE_SIZE" and c["size"]["baseline"] == 0.5


def test_scientific_trigger_refreshes_stored_card_and_settles_at_next_open():
    from workflows.postcheck_quant import conditional_fill_stats

    report = _amd()
    observe = "close <= 1.018e+06"
    report["operation_type"] = "TRIM_POSITION"
    report["operation_target"] = "1018000"
    report["operation_execution"]["conditional"] = {"observe": observe, "fill": "next_open"}
    card = ensure_display_fields(report)["execution_card"]
    card.update(kind="undeterminable", trigger=None, reason=f"unparseable observe {observe!r}")
    repaired = ensure_display_fields(report)["execution_card"]
    assert repaired["kind"] == "trim_on_condition"
    assert repaired["trigger"] == {"field": "close", "comparator": "<=", "level": 1018000,
                                   "fill": "next_open", "observe": observe}
    bars = [{"date": "2026-11-24", "open": 1020000, "high": 1025000, "low": 1010000, "close": 1018000},
            {"date": "2026-11-25", "open": 1015000, "high": 1019000, "low": 1011000, "close": 1016000}]
    fill = conditional_fill_stats(bars, 1016000, observe, "next_open", long_side=False)
    assert (fill["condition_met_date"], fill["fill_date"], fill["fill_price"]) == (
        "2026-11-24", "2026-11-25", 1015000)


def test_unresolved_claims_take_final_text_and_rev():
    r = ensure_display_fields(_amd(), source="test")
    assert r["unresolved_claims"] == [{"id": "R3", "claim_rev": "R4.R", "state": "deadlocked",
                                       "text": "The fifteen-day distribution favors partial exposure"}]


def test_group_claims_resolve_latest_filing_for_selected_member_only():
    from workflows.execution_card import build_unresolved

    def turn(word):
        return {"content": {"members": {"UNH": {"claims": [{"id": "R1", "claim": word}]},
                                        "ELV": {"claims": [{"id": "R1", "claim": "ELV only"},
                                                           {"id": "R2", "claim": "Also ELV only"}]}}}}

    report = {"symbol": "UNH", "debate_ref": {"symbol": "GROUP_US_HEALTHCARE"},
              "deadlocked": ["R1", "R2"], "debate_history": [turn("Initial"), turn("Final")]}
    saved = [{"id": "R1", "text": "Final", "claim_rev": "R2.R", "state": "deadlocked"},
             {"id": "R2", "text": None, "claim_rev": None, "state": "deadlocked"}]
    report["unresolved_claims"] = saved
    assert build_unresolved(report) == saved
    assert build_unresolved(dict(report, debate_history=[])) == saved
    assert build_unresolved(dict(report, unresolved_claims=[]))[0]["text"] == "Final"
    assert build_unresolved(dict(report, unresolved_claims=[dict(saved[0], text="Stale")]))[0]["text"] == "Final"


def test_idempotent_by_fingerprint():
    r = ensure_display_fields(_amd(), source="assembler")
    fp = r["execution_card"]["fingerprint"]
    r2 = ensure_display_fields(r, source="api")
    assert r2["execution_card"]["fingerprint"] == fp and r2["execution_card"]["source"] == "assembler"


def test_plain_limit_below_reference_is_resting():
    rep = {"symbol": "NVDA", "operation_type": "LIMIT_ENTRY", "operation_target": "203.7",
           "reference_prices": [{"symbol": "NVDA", "price": 211.87}], "statement": "Order valid until 2026-10-30."}
    c = ensure_display_fields(rep)["execution_card"]
    assert c["kind"] == "entry_at_level" and c["resting_order"] is True and c["now"] == "place_limit_buy"
    assert c["relation"] == "below" and c["valid"]["until"] == "2026-10-30"


def test_limit_above_reference_without_condition_is_not_a_breakout_order():
    rep = {"symbol": "X", "operation_type": "LIMIT_ENTRY", "operation_target": "110",
           "reference_prices": [{"symbol": "X", "price": 100}]}
    c = ensure_display_fields(rep)["execution_card"]
    assert c["kind"] == "entry_at_level" and c["resting_order"] is None and c["now"] == "hold"
    assert c["one_line"] == "Hold; wait for 110.00 (no order type stated)"


def test_lenswise_cards_follow_primary():
    rep = {"symbol": "GENERAL", "lenses": ["SPY", "QQQ"], "operation_type": "LIMIT_ENTRY", "operation_target": "SPY=771; QQQ=694",
           "odds": {"by_lens": {"SPY": {"reference_price": 759.29}, "QQQ": {"reference_price": 700.0}}},
           "by_lens": {"SPY": {"operation_type": "LIMIT_ENTRY", "operation_target": "771",
                               "operation_execution": {"conditional": {"observe": "close > 771", "fill": "next_open"},
                                                       "stop": {"level": 742, "semantics": "close"}}},
                       "QQQ": {"operation_type": "TRIM_POSITION", "operation_target": "700.0",
                               "operation_execution": {"stop": {"level": 742.5, "semantics": "close"}}}}}
    r = ensure_display_fields(rep)
    assert r["by_lens"]["SPY"]["execution_card"]["kind"] == "conditional_entry"
    assert r["by_lens"]["QQQ"]["execution_card"]["kind"] == "trim_now"
    assert r["by_lens"]["QQQ"]["execution_card"]["stop"]["action"] == "buy_back"
    assert r["execution_card"]["lens"] == "SPY" and r["execution_card"]["kind"] == "conditional_entry"


def test_i18n_leaves_overlay_only_allowed_string_paths():
    r = ensure_display_fields(_amd())
    leaves = dict(("".join(f".{p}" for p in path)[1:], t) for path, t in i18n.collect_leaves(r))
    assert "execution_card.one_line" in leaves and "scenario.nodes.0.label" in leaves and "unresolved_claims.0.text" in leaves
    doc, served = i18n.localize_report(r, "en", {"zh": {"_leaves": {"execution_card.one_line": "现在: 持有。",
                                                                    "execution_card.kind": "改坏", "nope.x": "y"}}}, "zh")
    # a one_line leaf without a recorded source hash is the legacy long-form translation: dropped (English served)
    assert served == "zh" and doc["execution_card"]["one_line"] == r["execution_card"]["one_line"]
    h = i18n.leaf_src_hash(r["execution_card"]["one_line"])
    doc, _ = i18n.localize_report(r, "en", {"zh": {"_leaves": {"execution_card.one_line": "持有; 收盘站上 520.40 后次日开盘买"},
                                                   "_leaf_src": {"execution_card.one_line": h}}}, "zh")
    assert doc["execution_card"]["one_line"] == "持有; 收盘站上 520.40 后次日开盘买"
    doc, _ = i18n.localize_report(r, "en", {"zh": {"_leaves": {"execution_card.one_line": "旧译文"},
                                                   "_leaf_src": {"execution_card.one_line": "000000000000"}}}, "zh")
    assert doc["execution_card"]["one_line"] == r["execution_card"]["one_line"]   # stale hash → not applied
    assert doc["execution_card"]["kind"] == "conditional_entry" and r["execution_card"]["one_line"].startswith("Hold;")


def test_size_in_reader_units_trim_to_zero():
    """SPY 2026-11-02T15:00Z: tier AGGRESSIVE_SIZE (x* = 1, a full-unit move) with q* = 0 (nothing left of the unit) —
    the card says so in reader's units instead of 'full size / 0.0'."""
    rep = {"symbol": "SPY", "operation_type": "TRIM_POSITION", "operation_target": "766.62", "operation_size": "AGGRESSIVE_SIZE",
           "operation_execution": {"conditional": {"observe": "close > 766.62", "fill": "next_open"},
                                   "stop": {"level": 779.0, "semantics": "close"}},
           "odds": {"reference_price": 760.66},
           "scenario": {"derived": {"chosen": "underweight_on_condition[close>766.62|next_open|stop:779close]", "q_star": 0.0, "x_star": 1.0,
                                    "candidates": [{"id": "underweight_on_condition[close>766.62|next_open|stop:779close]",
                                                    "strategy": "underweight_on_condition", "direction": "underweight",
                                                    "kind": "close", "op": ">", "level": 766.62, "fill": "next_open",
                                                    "stop": {"level": 779.0, "semantics": "close"}}]}}}
    c = ensure_display_fields(rep)["execution_card"]
    assert c["kind"] == "trim_on_condition" and c["schema"] == "1.1"
    assert c["size"] == {"tier": "AGGRESSIVE_SIZE", "direction": "underweight", "move": 1.0, "target": 0.0, "baseline": 0.5, "q_star": 0.0}
    assert c["one_line"] == "Hold; trim at next open after a close above 766.62"
    assert c["stop"]["action"] == "buy_back"
    old = {"symbol": "X", "operation_type": "MARKET_ENTRY", "operation_target": "100", "operation_size": "PILOT_SIZE",
           "reference_prices": [{"symbol": "X", "price": 100}]}
    s = ensure_display_fields(old)["execution_card"]["size"]
    assert s == {"tier": "PILOT_SIZE", "direction": "overweight", "move": None, "target": None, "baseline": 0.5}


def test_old_schema_card_is_rebuilt_at_read():
    r = ensure_display_fields(_amd(), source="assembler")
    r["execution_card"]["schema"] = "1.0"; r["execution_card"]["one_line"] = "Now: hold. (old long form)"
    c = ensure_display_fields(r, source="api")["execution_card"]
    assert c["schema"] == "1.1" and c["one_line"].startswith("Hold;") and c["source"] == "api"


def test_build_card_undeterminable_on_bad_observe():
    c = build_card({"operation_type": "LIMIT_ENTRY", "operation_target": "100",
                    "operation_execution": {"conditional": {"observe": "whenever", "fill": "next_open"}}}, {}, "X", 95.0)
    assert c["kind"] == "undeterminable" and c["now"] == "none" and "unparseable" in c["reason"]


def _strict_report():
    rep = _amd()
    rep["odds"]["conditional_fill_policy"] = "strict_next_open_v1"
    rep["scenario"]["derived"]["comparison"] = {"conditional_execution": {
        "policy": "strict_next_open_v1", "candidates": {
            "excluded": {"eligible": False}, "assessable": {"eligible": True}}}}
    return rep


def test_scope_discloses_restricted_selection_without_changing_action_or_headline():
    legacy = ensure_display_fields(_amd())["execution_card"]
    strict = ensure_display_fields(_strict_report())["execution_card"]
    assert "notices" not in legacy
    assert strict["notices"][0]["severity"] == "warning"
    assert "remaining plans" in strict["notices"][0]["message"]
    assert (strict["now"], strict["one_line"]) == (legacy["now"], legacy["one_line"])


def test_scope_change_refreshes_cached_card_even_when_chosen_action_is_unchanged():
    rep = ensure_display_fields(_strict_report())
    before = rep["execution_card"]["fingerprint"]
    rep["scenario"]["derived"]["comparison"]["conditional_execution"]["candidates"]["excluded"]["eligible"] = True
    c = ensure_display_fields(rep)["execution_card"]
    assert c["fingerprint"] != before
    assert "notices" not in c
    c["comparison_scope"] = {"status": "no_exclusions", "excluded_count": 0}
    assert "comparison_scope" not in ensure_display_fields(rep)["execution_card"]



def test_scope_never_claims_no_exclusions_for_missing_or_malformed_strict_metadata():
    for meta in (None, [], {}, {"policy": "wrong", "candidates": {}},
                 {"policy": "strict_next_open_v1", "candidates": {"x": {"eligible": "false"}}}):
        rep = _strict_report()
        rep["scenario"]["derived"]["comparison"]["conditional_execution"] = meta
        assert "unavailable" in ensure_display_fields(rep)["execution_card"]["notices"][0]["message"]


def test_scope_is_per_lens_and_primary_copy_follows_it():
    a = _strict_report()
    b = copy.deepcopy(a)
    b["scenario"]["derived"]["comparison"]["conditional_execution"]["candidates"] = {}
    rep = {"symbol": "GENERAL", "lenses": ["AMD", "SPY"],
           "odds": {"conditional_fill_policy": "strict_next_open_v1", "by_lens": {
               "AMD": {"reference_price": 492.5}, "SPY": {"reference_price": 492.5}}},
           "by_lens": {"AMD": a, "SPY": b}}
    ensure_display_fields(rep)
    assert rep["execution_card"]["notices"] == rep["by_lens"]["AMD"]["execution_card"]["notices"]
    assert "notices" not in rep["by_lens"]["SPY"]["execution_card"]
    assert ("by_lens", "AMD", "execution_card", "notices", 0, "message") in dict(i18n.collect_leaves(rep))


def test_notice_translation_requires_matching_source_and_preserves_severity():
    rep = ensure_display_fields(_strict_report())
    path = "execution_card.notices.0.message"
    notice = rep["execution_card"]["notices"][0]
    assert tuple(["execution_card", "notices", 0, "message"]) in dict(i18n.collect_leaves(rep))
    overlay = {"_leaves": {path: "translated", "execution_card.notices.0.severity": "ignored"}}
    for hashes, expected in [({}, notice["message"]), ({path: "stale"}, notice["message"]),
                             ({path: i18n.leaf_src_hash(notice["message"])}, "translated")]:
        overlay["_leaf_src"] = hashes
        doc, _ = i18n.localize_report(rep, "en", {"zh": overlay}, "zh")
        assert doc["execution_card"]["notices"] == [{"message": expected, "severity": "warning"}]
    assert notice["message"] != "translated"
