"""cohorts.py — 后验分段(cohort)定义, 唯一出处(2026-10-23 起; 之前抄在 scripts/postcheck_report_three_eras.py 的 COH / ERA 表里)。

按报告 as_of(UTC)归段, 边界精确到分钟, `to` 为不含上界, 最新段 `to=None`; 未定的切分线写 "TBD"(_key 把它排在所有分钟之后)。切分线来源 docs/MAJOR-CHANGES.md 的版本线。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

COHORTS: list[dict] = [
    {"id": "C0", "era": "v1.0", "label": "上古三元", "from": "2026-07-06T00:00Z", "to": "2026-07-18T00:00Z"},
    {"id": "C1", "era": "v2.0", "label": "二元 v2", "from": "2026-07-18T00:00Z", "to": "2026-07-30T00:00Z"},
    {"id": "C2", "era": "v2.0", "label": "记账制 R11-15", "from": "2026-07-30T00:00Z", "to": "2026-08-10T00:00Z"},
    {"id": "C3", "era": "v2.0", "label": "R16 挂牌/清单", "from": "2026-08-10T00:00Z", "to": "2026-08-15T08:30Z"},
    {"id": "C4", "era": "v2.0", "label": "R17 + task", "from": "2026-08-15T08:30Z", "to": "2026-08-27T00:00Z"},
    {"id": "C5", "era": "v2.0", "label": "R18 转换函数", "from": "2026-08-27T00:00Z", "to": "2026-09-04T12:43Z"},
    {"id": "C6", "era": "v2.0", "label": "R19-20 重建期", "from": "2026-09-04T12:43Z", "to": "2026-09-21T16:38Z"},
    {"id": "C7", "era": "v2.0", "label": "R21 挂点刀", "from": "2026-09-21T16:38Z", "to": "2026-09-30T00:00Z"},
    # 9-30 ~ 10-02 13:38 是冒烟场, 不入任何段
    {"id": "C8", "era": "v3.0", "label": "v3.0 整体重构", "from": "2026-10-02T13:38Z", "to": "2026-10-08T13:01Z"},
    {"id": "C9", "era": "v3.1", "label": "v3.1 平衡窄改", "from": "2026-10-08T13:01Z", "to": "2026-10-17T13:38Z"},
    {"id": "C10", "era": "v3.2", "label": "v3.2 D+20 口径三件定案 + 入场枚举合并(尺 R24)", "from": "2026-10-17T13:38Z", "to": "2026-10-26T00:00Z"},
    {"id": "C11", "era": "v3.3", "label": "v3.3: English core (corpus and protocol in English, ruler R24 text in English) + technical snapshot V16 (causal ATR zigzag structure, telescope digest, no data-layer labels) + own news for non-US single names", "from": "2026-10-26T00:00Z", "to": "2026-10-31T08:50Z"},
    # C12 boundary unset until deploy: _key("TBD") sorts after every real minute, so C11 stays open and C12 matches
    # nothing; at deploy replace both "TBD" with the deploy minute (docs/handoff/V34-OPERATORS-2026-10-31.md §5 step 3).
    # The 2026-10-29T02:55Z deployment was an incomplete v3.4, withdrawn and its reports deleted (owner, 2026-10-31).
    {"id": "C12", "era": "v3.4", "label": "v3.4: one ruler — mixture of replayed scenarios at the arbiter's weight, one closure validator, one neutral reference with five symmetric strategies, observable cost, derived scores (ruler R25); first deployed 2026-10-29 incomplete and withdrawn, reports deleted", "from": "2026-10-31T08:50Z", "to": "2026-11-09T08:08Z"},
    # C13 cut = 08:08Z, not the 08:17Z deploy minute: GENERAL_EU 20261109T080800Z was prepared at 08:08 but opened at 08:25 under v3.5;
    # nothing else has an as_of in 08:08-08:17 and no session was running at deploy.
    {"id": "C13", "era": "v3.5", "label": "v3.5: drill calibration — concede only what was hit (narrowing, conserved stakes), arbiter holds continuation and change to one forward standard, simple card operation_simple + close-stop fill without a next-day open (ruler R25)", "from": "2026-11-09T08:08Z", "to": "2026-11-09T15:12Z"},
    {"id": "C14", "era": "v3.6", "label": "v3.6: prediction/execution separation (debaters see no strategy winner), tally extras (short-term from close events, node contributions, magnitude compensation, stall notice), paths are forecasts, outcall volume follows collisions, simple card carries its price point (ruler R25)", "from": "2026-11-09T15:12Z", "to": "2026-11-19T14:23Z"},
    {"id": "C15", "era": "v3.8.1", "label": "v3.8.1 reporting cohort (ruler R25). Execution policy comes from each session's opening odds snapshot.", "from": "2026-11-19T14:23Z", "to": None},
]

RULER_LATEST = "R25"


def _key(ts: str) -> str:
    """'2026-10-08T13:01Z' / '2026-10-08 13:01:00+00:00' → 'YYYY-MM-DD HH:MM'(UTC), 供字符串比较。"""
    if str(ts).strip() == "TBD":   # an unset boundary sorts after every real minute
        return "9999-12-31 23:59"
    s = str(ts).strip().replace("T", " ").replace("Z", "")
    return s[:16]


def to_utc_key(as_of) -> str:
    if isinstance(as_of, datetime):
        return as_of.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return _key(as_of)


def cohort_of(as_of) -> Optional[str]:
    """报告 as_of → cohort id; 不在任何段(冒烟场、太早)→ None。"""
    k = to_utc_key(as_of)
    for c in COHORTS:
        if _key(c["from"]) <= k and (c["to"] is None or k < _key(c["to"])):
            return c["id"]
    return None


def era_of(cohort_id: Optional[str]) -> Optional[str]:
    for c in COHORTS:
        if c["id"] == cohort_id:
            return c["era"]
    return None
