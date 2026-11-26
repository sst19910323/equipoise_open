# -*- coding: utf-8 -*-
"""Task 6 (C11.7): postcheck settles every lens of a lens-wise report from its own by_lens entry."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import odds_model as om            # noqa: E402
import scenario_replay as sr       # noqa: E402

BASES = (("SPY", 100.0), ("QQQ", 400.0), ("DIA", 350.0))


def _write_bars(root, syms):
    import pandas as pd
    for sym, base in BASES:
        if sym not in syms:
            continue
        d = root / sym
        d.mkdir()
        dates = pd.bdate_range(start="2026-09-10", periods=40)
        closes = [base * (1 - 0.002 * i) if i < 10 else base * (0.98 + 0.003 * (i - 10)) for i in range(40)]
        pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                      "low": [c * 0.99 for c in closes], "close": closes,
                      "volume": [1000] * 40}).to_csv(d / f"{sym}_5y_1d_20260930T000000Z.csv", index=False)


def _sc(atoms, P0=100.0):
    k = P0 / 100.0
    return {"schema": "3.4", "T_days": 15, "P0": P0, "execution": {},
            "nodes": [{"id": f"N{i}", "p": p, "P_T": pt * k, "refs": ["Z1"], "events": []}
                      for i, (p, pt) in enumerate(atoms)]}


def _A(P0):
    """The report's mixture on a lens whose price is P0 (each lens's scenario is on its own price scale)."""
    return sr.mixture(_sc([(0.7, 108.0), (0.3, 96.0)], P0), _sc([(0.3, 104.0), (0.7, 94.0)], P0), 0.6, reason=None)


def _report():
    A = _A(100.0)
    odds = om.snapshot_from_config(om.DEFAULT_CONFIG, unit_basis={"data_symbol": "SPY", "s_s": 0.08})
    w = {"SPY": 0.5, "QQQ": 0.3, "DIA": 0.2}
    by = {"SPY": {"scenario": A, "operation_type": "LIMIT_ENTRY", "operation_target": 99.0, "composite_score": 55},
          "QQQ": {"scenario": _A(400.0), "operation_type": "LIMIT_ENTRY", "operation_target": 395.0, "composite_score": 54},
          "DIA": {"scenario": _A(350.0), "operation_type": "TRIM_POSITION", "operation_target": 352.0,
                  "composite_score": 45}}
    return {"schema_version": "3.4", "symbol": "GENERAL", "operation_type": "LIMIT_ENTRY",
            "operation_target": "SPY=99; QQQ=395; DIA=352",
            "short_term": {"direction": "上行", "target": "SPY=104; QQQ=450; DIA=390",
                           "invalid": "SPY=95; QQQ=380; DIA=330", "horizon_days": 10},
            "reference_prices": [{"symbol": s, "price": p} for s, p in BASES],
            "scenario": A, "atr_pct_14": 2.0, "odds": odds,
            "lenses": ["SPY", "QQQ", "DIA"], "by_lens": by,
            "lens_weights_default": w, "lens_weights": {"SPY": 0.2, "QQQ": 0.2, "DIA": 0.6}}


def _digest(tmp_path, syms, rep, dt=20):
    from workflows import postcheck_quant as pq
    old = pq.MARKET_DATA_ROOT
    pq.MARKET_DATA_ROOT = tmp_path
    try:
        tmp_path.mkdir(parents=True, exist_ok=True)
        _write_bars(tmp_path, syms)
        return pq.compute_window_digest("GENERAL", "2026-09-15T20:00:00Z", dt, rep)
    finally:
        pq.MARKET_DATA_ROOT = old


def test_lenswise_members_each_execute(tmp_path):
    dg = _digest(tmp_path, {"SPY", "QQQ", "DIA"}, _report())
    by = {m["symbol"]: m for m in dg["members"]}
    assert all(m["role"] == "operation" for m in by.values())
    assert by["QQQ"]["entry"]["entry_price"] == 395.0          # its own level
    assert by["QQQ"]["entry"]["entry_price_reached"] is True   # settled: the QQQ bars cross 395 (low 392 × 0.99 by day 4)
    assert by["QQQ"]["entry"]["reached_date"]
    assert by["SPY"]["entry"]["entry_price"] == 99.0
    assert "trim_exec" in by["DIA"] and "entry" not in by["DIA"]
    assert abs(sum(m["weight"] for m in by.values()) - 1.0) < 1e-9
    assert dg["lenswise"] is True and dg["session_score_weights"] == _report()["lens_weights_default"]
    assert dg["primary_lens"] == "SPY"
    assert all("forecast" in m for m in by.values())
    for m in by.values():
        assert "error" not in m["forecast"], m["forecast"]
        assert m["forecast"]["status"] == "scored" and m["forecast"]["P0"] == dict(BASES)[m["symbol"]], m["forecast"]


def test_short_window_has_no_member_forecast(tmp_path):
    dg = _digest(tmp_path, {"SPY", "QQQ", "DIA"}, _report(), dt=10)
    by = {m["symbol"]: m for m in dg["members"]}
    assert set(by) == {"SPY", "QQQ", "DIA"} and all("error" not in m for m in by.values())
    assert all("forecast" not in m for m in by.values())
    assert by["QQQ"]["entry"]["entry_price_reached"] is True


def test_hold_lens_takes_the_shared_direction(tmp_path):
    rep = _report()
    rep["by_lens"]["SPY"].update(operation_type="HOLD_NEUTRAL", operation_target=100.0)
    by = {m["symbol"]: m for m in _digest(tmp_path, {"SPY", "QQQ", "DIA"}, rep)["members"]}
    assert by["SPY"]["direction_call"] == "看多"                 # short_term_path.direction 上行
    assert "entry" not in by["SPY"] and "trim_exec" not in by["SPY"]
    assert by["QQQ"]["direction_call"] == "看多" and by["DIA"]["direction_call"] == "看空"   # their own families
    rep["short_term"]["direction"] = "下行"
    by = {m["symbol"]: m for m in _digest(tmp_path / "down", {"SPY", "QQQ", "DIA"}, rep)["members"]}
    assert by["SPY"]["direction_call"] == "看空"
    assert by["QQQ"]["direction_call"] == "看多"                 # a lens with an operation keeps its own direction


def test_missing_bars_on_an_operation_lens_is_immature(tmp_path):
    """DIA carries TRIM_POSITION and has no fresh bars: the period is skipped ({}), never scored with DIA unsettled."""
    assert _digest(tmp_path, {"SPY", "QQQ"}, _report()) == {}


def test_missing_bars_on_a_hold_lens_do_not_borrow(tmp_path):
    """DIA is HOLD_NEUTRAL with no execution: its missing bars give an error member; SPY and QQQ still settle."""
    rep = _report()
    rep["by_lens"]["DIA"].update(operation_type="HOLD_NEUTRAL", operation_target=350.0)
    dg = _digest(tmp_path, {"SPY", "QQQ"}, rep)
    by = {m["symbol"]: m for m in dg["members"]}
    assert "error" in by["DIA"] and "entry" not in by["DIA"] and "trim_exec" not in by["DIA"]
    assert "_no_bars" not in by["DIA"]
    assert "error" not in by["SPY"] and "error" not in by["QQQ"]
    assert by["SPY"]["entry"]["entry_price"] == 99.0 and by["QQQ"]["entry"]["entry_price"] == 395.0
    assert dg["win_start"] and dg["win_end"]


def test_window_taken_from_the_first_settled_member(tmp_path):
    """The primary lens (first member) is the one without bars: the window still comes from a settled member."""
    rep = _report()
    rep["by_lens"]["SPY"].update(operation_type="HOLD_NEUTRAL", operation_target=100.0)
    dg = _digest(tmp_path, {"QQQ", "DIA"}, rep)
    by = {m["symbol"]: m for m in dg["members"]}
    assert "error" in by["SPY"]
    assert dg["win_start"] == by["QQQ"]["win_start"] and dg["win_end"] == by["QQQ"]["win_end"] and dg["win_start"]


def test_hold_lens_with_a_stop_carries_an_operation(tmp_path):
    rep = _report()
    rep["by_lens"]["DIA"].update(operation_type="HOLD_NEUTRAL", operation_target=350.0,
                                 operation_execution={"stop": {"level": 330.0, "semantics": "close"}})
    assert _digest(tmp_path, {"SPY", "QQQ"}, rep) == {}


def test_members_are_the_report_lenses(tmp_path):
    """Lens-wise: the members are report["lenses"] (not _GENERAL_MEMBERS); a lens without a by_lens entry or without a
    default weight is an explicit error member, never the unlabelled legacy path."""
    rep = _report()
    rep["lenses"] = ["QQQ", "SPY"]
    del rep["by_lens"]["DIA"]
    rep["lens_weights_default"] = {"QQQ": 0.4, "SPY": 0.6}
    dg = _digest(tmp_path, {"SPY", "QQQ", "DIA"}, rep)
    assert [m["symbol"] for m in dg["members"]] == ["QQQ", "SPY"]
    rep = _report()
    del rep["by_lens"]["DIA"]
    dg = _digest(tmp_path / "b", {"SPY", "QQQ", "DIA"}, rep)
    by = {m["symbol"]: m for m in dg["members"]}
    assert by["DIA"] == {"symbol": "DIA", "error": "no by_lens entry for lens DIA"}
    rep = _report()
    del rep["lens_weights_default"]["QQQ"]
    dg = _digest(tmp_path / "c", {"SPY", "QQQ", "DIA"}, rep)
    by = {m["symbol"]: m for m in dg["members"]}
    assert by["QQQ"] == {"symbol": "QQQ", "error": "no lens_weights_default weight for lens QQQ"}
    assert "error" not in by["SPY"] and "error" not in by["DIA"]


def test_single_lens_general_unchanged(tmp_path):
    rep = _report()
    for k in ("lenses", "by_lens", "lens_weights_default", "lens_weights"):
        rep.pop(k)
    rep["operation_target"] = "SPY=99"
    dg = _digest(tmp_path, {"SPY", "QQQ", "DIA"}, rep)
    assert "lenswise" not in dg and dg["primary_lens"] == "SPY"
    by = {m["symbol"]: m for m in dg["members"]}
    assert (by["SPY"]["role"], by["QQQ"]["role"], by["DIA"]["role"]) == ("operation", "analysis", "analysis")
    assert "weight" not in by["SPY"] and "forecast" in dg
