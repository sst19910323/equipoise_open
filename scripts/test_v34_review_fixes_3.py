"""v3.4 third fix wave (GPT round 9, council/20261030-0900-v34-closing-rules/gpt-9.md §4-5 item 1): the mixture checks
(weight, a reason citing a claim id and an evidence id when |2w−1| > 0.5, and at w ∈ {0, 1} every discarded atom-bearing
claim or "no decision-relevant atoms discarded") are one function, ruling.mixture_errors, run by the live ruling and by
the release gate. In the §1.4 mock session Z′ = Z14 (nothing applied on Z) and R′ carries Z1#true (UPD_Z1), so w = 1
discards Z1 and w = 0 discards nothing; the session's claims are Z1 / R1 / R2 and its returned evidence is Z.EV1."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import ruling as rlg                            # noqa: E402
import test_v34_flow as t_flow                  # noqa: E402
import test_v34_review_fixes as t_rev           # noqa: E402
from ruling import RulingError, derive_ruling   # noqa: E402

_run, _validate = t_rev._run, t_rev._validate

CITE = "mixture.reason: must cite a claim id and an evidence id"
DISCARD = "mixture.reason: must explain each discarded atom-bearing claim"
VALID = {1.0: "R2 is refuted by Z.EV1; R's Z1 move is already in Z′, so discarding R′ drops nothing Z1 does not carry",
         0.0: "Z1 guidance is priced per Z.EV1 and R2 holds; no decision-relevant atoms discarded"}
BAD = {"missing": None, "blank": "   ", "generic": "because",
       "reference-free": "the evidence clearly favours one side",
       "claim only": "R2 decides it", "evidence only": "Z.EV1 decides it",
       "unknown ids": "Z9 per X.EV7", "id inside a word": "R2x per Z.EV1x"}


def _mix(w, reason):
    return {"w": w, "reason": reason}


@pytest.fixture(scope="module")
def flow():
    return _run(t_flow.ruler_script())


def _endpoint_script(w):
    sc = t_flow.ruler_script()
    for k in ("provisional", "proposed", "ruling"):
        for x in sc[k]:
            x["mixture"] = _mix(w, VALID[w])
    return sc


@pytest.fixture(scope="module")
def ends():
    return {w: _run(_endpoint_script(w)) for w in (0.0, 1.0)}


# ---------------------------------------------------------------------------------------------------- live stage

@pytest.mark.parametrize("w", [0.0, 1.0])
@pytest.mark.parametrize("name", list(BAD))
def test_live_endpoint_reason_without_references_is_refused(flow, w, name):
    s = flow["s"]
    with pytest.raises(RulingError, match="mixture.reason: must cite a claim id and an evidence id"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(w, BAD[name])), "proposed")


def test_live_endpoint_must_explain_the_discarded_atoms(flow):
    s = flow["s"]
    # w = 1 discards R′, which carries Z1#true: Z1 must be named
    with pytest.raises(RulingError, match=f"{DISCARD}: Z1"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(1, "R2 is refuted by Z.EV1")), "proposed")
    # w = 0 discards Z′, where nothing was applied: the literal phrase is required
    with pytest.raises(RulingError, match="no decision-relevant atoms discarded"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(0, "Z1 is priced per Z.EV1")), "proposed")


@pytest.mark.parametrize("w", [0.0, 1.0])
def test_live_endpoint_valid_referenced_reason_passes(flow, w):
    s = flow["s"]
    rec = derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(w, VALID[w])), "proposed")
    assert rec["scenario"]["mixture"]["w"] == w and rec["scenario"]["mixture"]["reason"] == VALID[w]


def test_live_high_tilt_needs_references_and_moderate_tilt_needs_no_reason(flow):
    s = flow["s"]
    for reason in (None, "because", "R2 only"):
        with pytest.raises(RulingError, match=CITE):
            derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(0.9, reason)), "proposed")
    assert derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(0.9, "R2 + Z.EV1")), "proposed")
    for w in (0.25, 0.5, 0.75):   # |2w−1| ≤ 0.5
        assert derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(w, None)), "proposed")
    with pytest.raises(RulingError, match=r"mixture.w missing or outside \[0, 1\]"):
        derive_ruling(s.store, s, dict(t_flow.ARBITER, mixture=_mix(1.2, VALID[1.0])), "proposed")


# ---------------------------------------------------------------------------------------------------- release gate

def _set_reason(doc, reason):
    doc["scenario"]["mixture"]["reason"] = reason
    if isinstance(doc.get("mixture"), dict):
        doc["mixture"]["reason"] = reason
    return doc


@pytest.mark.parametrize("w", [0.0, 1.0])
def test_release_endpoint_report_with_valid_reason_passes(ends, w):
    fl = ends[w]
    a = fl["a"]
    assert a["scenario"]["mixture"]["w"] == w
    assert _validate(fl, copy.deepcopy(a))[0] == []


@pytest.mark.parametrize("w", [0.0, 1.0])
@pytest.mark.parametrize("name", list(BAD))
def test_release_endpoint_reason_without_references_is_fatal(ends, w, name):
    fl = ends[w]
    if name == "missing":
        doc = copy.deepcopy(fl["a"])
        doc["scenario"]["mixture"].pop("reason")
        if isinstance(doc.get("mixture"), dict):
            doc["mixture"].pop("reason", None)
    else:
        doc = _set_reason(copy.deepcopy(fl["a"]), BAD[name])
    fatal = _validate(fl, doc)[0]
    assert any(CITE in f for f in fatal), fatal


def test_release_endpoint_must_explain_the_discarded_atoms(ends):
    fatal = _validate(ends[1.0], _set_reason(copy.deepcopy(ends[1.0]["a"]), "R2 is refuted by Z.EV1"))[0]
    assert any(f"{DISCARD}: Z1" in f for f in fatal), fatal
    fatal = _validate(ends[0.0], _set_reason(copy.deepcopy(ends[0.0]["a"]), "Z1 is priced per Z.EV1"))[0]
    assert any(DISCARD in f and "no decision-relevant atoms discarded" in f for f in fatal), fatal


def test_release_moderate_tilt_needs_no_reason(flow):
    a = flow["a"]
    assert a["scenario"]["mixture"]["w"] == 0.5 and not a["scenario"]["mixture"].get("reason")
    assert _validate(flow, copy.deepcopy(a))[0] == []


# ---------------------------------------------------------------------------------------------------- the function

def test_mixture_errors_is_the_one_check_and_matches_whole_tokens():
    fins = [{"content": {"claims": [{"id": "Z1"}, {"id": "R2"}]}}]
    applied = {"Z": [], "R": [{"effect": "Z1#true", "status": "carried"}, {"effect": None, "status": "update"}]}
    ev = ["Z.EV1", "R.MC1"]
    assert rlg.mixture_errors(_mix(0.75, None), fins, applied, ev) == []
    assert rlg.mixture_errors(_mix(0.8, "R2 (see R.MC1)."), fins, applied, ev) == []
    assert rlg.mixture_errors(_mix(0.8, "R22 per Z.EV12"), fins, applied, ev)[0].startswith(CITE)
    assert rlg.mixture_errors(_mix(1, "R2, Z.EV1"), fins, applied, ev) == [f"{DISCARD}: Z1"]
    assert rlg.mixture_errors(_mix(1, "R2, Z.EV1; Z1#true already in Z′"), fins, applied, ev) == []
    assert rlg.mixture_errors(_mix(0, "R2, Z.EV1 — No decision-relevant atoms discarded"), fins, applied, ev) == []
    assert rlg.mixture_errors({"w": "1"}, fins, applied, ev) == [rlg.W_ERROR]
    assert rlg.discarded_claims(1.0, applied) == ["Z1"] and rlg.discarded_claims(0.0, applied) == []
