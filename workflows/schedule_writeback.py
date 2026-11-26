# -*- coding: utf-8 -*-
"""
schedule_writeback.py — 把一份辩论终报的 next_review 翻译并回写到 debate_schedule.yaml。

定位: live 辩论跑完(STATUS:ran)后, 由 run_debate_sync 调用, best-effort 更新该标的的
next.time.at(完整 UTC 时刻) + next.condition(structured_triggers 原样存, Hermes 未来才消费)。
历史回放(as_of)不调用本模块。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import yaml

# 标准复盘时刻 (UTC): 13:30Z ≈ 美东开盘(EDT)。recommend_after 只给日期, 配此时刻成完整时刻。
STD_REVIEW_HOUR_UTC = 13
STD_REVIEW_MIN_UTC = 30

FALLBACK_DAYS = 3  # AI 没给有效时刻时的兜底: 排到几天后(别空着, 也别排太远)


def _parse_anchor(s: str) -> Optional[datetime]:
    """recommend_after → UTC datetime。
    优先按完整 UTC 时刻解析 'YYYY-MM-DDTHH:MM:SSZ' (AI 现在直接给时刻, 原样用);
    退化兼容纯日期 'YYYY-MM-DD' (配标准复盘时刻 13:30Z)。失败 → None。"""
    if not s:
        return None
    s = str(s).strip()
    try:  # 纯日期 (无时间部分) → 配标准复盘时刻 13:30Z (兼容旧报告)
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.replace(hour=STD_REVIEW_HOUR_UTC, minute=STD_REVIEW_MIN_UTC,
                         second=0, tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # 完整 ISO 时刻 (含 Z / 偏移) → 原样用
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return None


def next_at_from_review(next_review: Dict, now_utc: datetime) -> datetime:
    """next_review → 下一轮 UTC 时刻 (纯函数)。
    AI 只给一个时间点 recommend_after: 在未来 → 原样用 (即下次时点);
    缺失/已过期 → 兜底排到几天后的标准复盘时刻。"""
    nr = next_review or {}
    ra = _parse_anchor(nr.get("recommend_after", ""))
    if ra is not None and ra >= now_utc:
        return ra
    today = now_utc.replace(hour=STD_REVIEW_HOUR_UTC, minute=STD_REVIEW_MIN_UTC,
                            second=0, microsecond=0)
    return today + timedelta(days=FALLBACK_DAYS)


def _find_group(sched: Dict, symbol: str) -> Optional[str]:
    """symbol 在 generals/etfs/us_stocks 哪一段。找不到 → None。"""
    for grp in ("generals", "etfs", "us_stocks"):
        if isinstance(sched.get(grp), dict) and symbol in sched[grp]:
            return grp
    return None


# symbols.yaml analysis_targets 分组 → 排班表分组 (新标的自动入册的推组依据)
_TARGETS_TO_SCHED = {"stocks": "us_stocks", "etfs": "etfs", "generals": "generals"}


def _infer_group(symbol: str, schedule_path: Path) -> Optional[str]:
    """新标的入册推组: 读同目录 symbols.yaml 的 analysis_targets。
    不在 analysis_targets → None(不认识的符号不入册)。"""
    try:
        with open(Path(schedule_path).parent / "symbols.yaml", encoding="utf-8") as f:
            targets = (yaml.safe_load(f) or {}).get("analysis_targets") or {}
        for src, dst in _TARGETS_TO_SCHED.items():
            if symbol in (targets.get(src) or []):
                return dst
    except Exception as e:
        logging.warning(f"[schedule_writeback] 读 symbols.yaml 推组失败: {e}")
    return None


def write_next(symbol: str, next_review: Dict, schedule_path, now_utc: Optional[datetime] = None) -> bool:
    """把 symbol 的 next.time.at(+condition) 回写进 debate_schedule.yaml。best-effort。
    只动这一个标的的 next 子树, 其它标的/分组结构原样保留。
    symbol 不在表里但在 symbols.yaml analysis_targets 里 → 按组自动入册(2026-09-24,
    MCD/LLY 案: 新增标的无 init 路径, 跑成了也进不了 next view); 两处都没有 → 不写返 False。"""
    symbol = str(symbol).upper()
    now_utc = now_utc or datetime.now(timezone.utc)
    p = Path(schedule_path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            sched = yaml.safe_load(f) or {}
    except Exception as e:
        logging.warning(f"[schedule_writeback] 读 {p} 失败, 跳过回写: {e}")
        return False

    grp = _find_group(sched, symbol)
    if grp is None:
        grp = _infer_group(symbol, p)
        if grp is None:
            logging.warning(f"[schedule_writeback] {symbol} 不在排班表也不在 analysis_targets, 不回写")
            return False
        sched.setdefault(grp, {})
        logging.info(f"[schedule_writeback] {symbol} 首次入册 → {grp}")

    at = next_at_from_review(next_review, now_utc)
    nr = next_review or {}
    # reason: AI 给的一句简单原因(给 Hermes/人看); 缺失才退回自动串
    reason = (nr.get("reason") or nr.get("notes") or "").strip() or (
        f"next_review @ {nr.get('recommend_after','')}")
    entry = sched[grp][symbol] = sched[grp].get(symbol) or {}
    entry["next"] = {
        "time": {
            "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason,
        }
    }
    triggers = nr.get("structured_triggers") or []
    if triggers:
        entry["next"]["condition"] = triggers  # 条件层(大盘到X价位), 以后启用; 有则存着

    try:
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(sched, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        logging.info(f"[schedule_writeback] ✅ {symbol} next.at={entry['next']['time']['at']}")
        return True
    except Exception as e:
        logging.warning(f"[schedule_writeback] 写 {p} 失败: {e}")
        return False


if __name__ == "__main__":
    now = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    # 纯函数自检 —— AI 只给一个时间点 recommend_after
    a = next_at_from_review({"recommend_after": "2026-06-22T13:30:00Z"}, now)   # 完整 UTC 时刻, 原样用 (主路径)
    assert a == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc), a
    b = next_at_from_review({"recommend_after": "2026-06-20T20:00:00Z"}, now)   # 非整点催化剂时刻原样保留
    assert b == datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc), b
    c = next_at_from_review({"recommend_after": "2026-06-20"}, now)             # 纯日期(旧报告兼容) → 配 13:30Z
    assert c == datetime(2026, 6, 20, 13, 30, tzinfo=timezone.utc), c
    d = next_at_from_review({"recommend_after": "2026-06-01T13:30:00Z"}, now)   # 已过期 → 兜底 today+3
    assert d == datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc), d
    e = next_at_from_review({}, now)                                           # 缺失 → 兜底 today+3
    assert e == datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc), e
    print("schedule_writeback.next_at_from_review 自检通过")

    # round-trip 自检
    import tempfile, os
    seed = {"generals": {"GENERAL": {"next": {"time": {"at": "2026-06-15T13:30:00Z"}}}},
            "etfs": {"SPY": {"next": {"time": {"at": "2026-06-15T13:30:00Z"}}}},
            "us_stocks": {"AAPL": {"next": {"time": {"at": "2026-06-15T13:30:00Z"}}}}}
    tmpdir = tempfile.mkdtemp()
    tmp = os.path.join(tmpdir, "debate_schedule.yaml")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(seed, f, allow_unicode=True, sort_keys=False)
    with open(os.path.join(tmpdir, "symbols.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"analysis_targets": {"stocks": ["AAPL", "MCD"], "etfs": ["SPY"],
                                              "generals": ["GENERAL"]}}, f)
    ok = write_next("AAPL", {"recommend_after": "2026-06-20T13:30:00Z",
                             "reason": "财报前最后复盘"},
                    tmp, now_utc=now)
    assert ok
    with open(tmp, encoding="utf-8") as f:
        after = yaml.safe_load(f)
    assert after["us_stocks"]["AAPL"]["next"]["time"]["at"] == "2026-06-20T13:30:00Z", after
    assert after["us_stocks"]["AAPL"]["next"]["time"]["reason"] == "财报前最后复盘", after
    assert after["etfs"]["SPY"]["next"]["time"]["at"] == "2026-06-15T13:30:00Z", "别的标的不该被动"
    # 新标的自动入册 (MCD/LLY 案): 不在排班表但在 analysis_targets → 按组建条目
    ok = write_next("MCD", {"recommend_after": "2026-06-21T13:30:00Z", "reason": "首场入册"},
                    tmp, now_utc=now)
    assert ok, "analysis_targets 里的新标的应自动入册"
    with open(tmp, encoding="utf-8") as f:
        after = yaml.safe_load(f)
    assert after["us_stocks"]["MCD"]["next"]["time"]["at"] == "2026-06-21T13:30:00Z", after
    assert write_next("ZZZZ", {}, tmp, now_utc=now) is False, "两处都没有应返 False"
    import shutil; shutil.rmtree(tmpdir)
    print("schedule_writeback.write_next round-trip 自检通过 (含新标的自动入册)")
