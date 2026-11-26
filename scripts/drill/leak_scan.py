#!/usr/bin/env python3
"""leak_scan.py — 在 oracle 产出里找晚于 as_of 的日期(演习的未来信息漏网检查)。

只做机械的一层: 显式日期(ISO / 斜杠 / 英文月份 / 中文月日)。日期晚于 as_of 的那天才算; 同一行若是事前已排期的事
(scheduled / due / expected / calendar / upcoming / 定于 / 预计 …)标 scheduled, 不算泄漏(日程本来就是事前公开的)。
抓不到的(不带日期的"后来跌了")由 oracle 提示词与校准方把关, 这里不装作能抓。

    python scripts/drill/leak_scan.py <file> --as-of 20261009T133700Z    # 输出 JSON, 有泄漏退出码 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
PATTERNS = [
    ("iso", re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")),
    ("compact", re.compile(r"\b(20\d{2})(\d{2})(\d{2})(?:T\d{4,6}Z?)?\b")),
    ("mdy", re.compile(rf"\b{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", re.I)),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MON}(?:,?\s+(20\d{{2}}))?\b", re.I)),
    ("us_slash", re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?(?![\d/])")),
    ("zh", re.compile(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日")),
]
SCHEDULED = re.compile(r"\b(scheduled|schedule|due|expected|upcoming|calendar|slated|set for|will (?:be )?(?:release|report|meet|publish|hold)"
                       r"|is to|are to|planned|next (?:week|meeting)|ahead of|preview|consensus for)\b|定于|预计|将于|日程|日历|前瞻", re.I)
# 事前公布的日程本身(发布日、会议、到期日、窗口): 片段里提到这类事而没有结果措辞 → 也算 scheduled(11-07 首场: CPI 10-14 / FOMC 10-28 /
# "SPY to Oct 14 expiry" 被当泄漏, oracle 把日程日期删了, 辩手反而比生产少了料)
EVENT = re.compile(r"\b(CPI|PPI|PCE|FOMC|NFP|payrolls?|GDP|retail sales|jobless claims|ISM|PMI|earnings|results|reports?|"
                   r"(?:consumer|producer) price index|employment situation|IR|investor relations|after (?:the )?close|"
                   r"before (?:the )?open|pre-?announced|"
                   r"expiry|expiration|expiries|opex|auction|meeting|release[sd]?|statement|decision|minutes|testimony|speech|"
                   r"window|horizon|through|until|sessions?|trading days)\b", re.I)
OUTCOME = re.compile(r"\b(came in|printed|surged|plunged|tumbled|rallied|slumped|soared|sank|fell|rose|dropped|jumped|"
                     r"closed (?:at|up|down|higher|lower)|ended (?:at|up|down|higher|lower)|hiked|cut rates|raised rates|held rates|"
                     r"beat|missed|(?<!pre-)(?<!pre)announced|after the|following the|in the wake of)\b|公布后", re.I)
_SEG = re.compile(r"(?<=[.;!?])\s|\"\s*[,:]\s*\"|\n")


def _mk(y, m, d) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _dates(line: str, default_year: int):
    for kind, rx in PATTERNS:
        for m in rx.finditer(line):
            g = m.groups()
            if kind in ("iso", "compact"):
                d = _mk(g[0], g[1], g[2])
            elif kind == "mdy":
                d = _mk(g[2] or default_year, MONTHS[g[0].lower()[:3]], g[1])
            elif kind == "dmy":
                d = _mk(g[2] or default_year, MONTHS[g[1].lower()[:3]], g[0])
            elif kind == "us_slash":
                d = _mk(g[2] or default_year, g[0], g[1])
            else:
                d = _mk(g[0] or default_year, g[1], g[2])
            if d is not None:
                yield m.group(0), d, m.start(), m.end()


def _segment(line: str, start: int, end: int) -> str:
    """日期所在的那一句 / 那个 JSON 字段(按句末标点、`", "` 字段边界切), 不拿整行判断。"""
    cuts = [m.end() for m in _SEG.finditer(line, 0, start)]
    lo = cuts[-1] if cuts else 0
    nxt = _SEG.search(line, end)
    return line[lo: nxt.start() if nxt else len(line)]


def is_scheduled(seg: str) -> bool:
    if SCHEDULED.search(seg):
        return True
    return bool(EVENT.search(seg)) and not OUTCOME.search(seg)


def leak_scan(text: str, as_of: datetime) -> list[dict]:
    """返回晚于 as_of 那天的每处日期: {match, date, line, scheduled}; scheduled=False 的才算泄漏。"""
    out, cut = [], as_of.astimezone(timezone.utc).date()
    for line in str(text).splitlines():
        seen = set()
        for match, d, s, e in _dates(line, cut.year):
            if d > cut and (match, d) not in seen:
                seen.add((match, d))
                seg = _segment(line, s, e)
                out.append({"match": match, "date": d.isoformat(), "line": seg.strip()[:300], "scheduled": is_scheduled(seg)})
    return out


def leaks(text: str, as_of: datetime) -> list[dict]:
    return [h for h in leak_scan(text, as_of) if not h["scheduled"]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--as-of", dest="as_of", required=True)
    a = ap.parse_args(argv)
    as_of = datetime.strptime(a.as_of, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    hits = leak_scan(open(a.file, encoding="utf-8").read(), as_of)
    print(json.dumps({"hits": hits, "leaks": sum(1 for h in hits if not h["scheduled"])}, ensure_ascii=False))
    return 1 if any(not h["scheduled"] for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
