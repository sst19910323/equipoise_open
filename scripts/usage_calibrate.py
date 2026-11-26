#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_calibrate.py — 量"当前版本一场辩论烧多少周额度", 写进 config/usage_calibration.yaml。

方法(确定性): 在一个时间窗 [since, until] 里, 从该身份的会话日志取 rate_limits 的 used_percent 曲线,
delta = 窗末 − 窗首(跨重置时按段相加); n = PG 里该身份在窗内入库的辩论数; cost = delta ÷ n。
窗里若还跑了别的(复盘 / 值班 / 聊天), 会被摊进去 → 选只跑辩论的窗(一波批跑完到入库)。

用法(服务器 /opt/equipoise):
  ./venv/bin/python scripts/usage_calibrate.py --harness codex1 --since 2026-11-02T14:00Z --until 2026-11-02T18:30Z --label "11-02 US batch"
  ./venv/bin/python scripts/usage_calibrate.py --harness codex1 --last-hours 6          # 最近 6 小时
  加 --write --version "v3.4 (C12)" 才写进 yaml; 不加只打印。--note 自由文字。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

from workflows.harness_resolver import _CODEX_VARIANTS  # noqa: E402

CAL = ROOT / "config" / "usage_calibration.yaml"


def _ts(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def timeline(home: str, since: datetime, until: datetime) -> list[tuple[datetime, float, int | None]]:
    """窗内(前后各多看一天的文件)所有 rate_limits 采样: (时刻, used_percent, resets_at)。"""
    pts = []
    day = since.date() - timedelta(days=1)
    while day <= until.date() + timedelta(days=1):
        for f in Path(home, "sessions", f"{day:%Y}", f"{day:%m}", f"{day:%d}").glob("*.jsonl"):
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rl = _find(rec, "rate_limits")
                    prim = (rl or {}).get("primary") if isinstance(rl, dict) else None
                    if not prim or prim.get("used_percent") is None or not rec.get("timestamp"):
                        continue
                    pts.append((_ts(rec["timestamp"]), float(prim["used_percent"]), prim.get("resets_at")))
            except OSError:
                continue
        day += timedelta(days=1)
    pts.sort()
    return pts


def delta_in_window(pts, since, until) -> tuple[float | None, dict]:
    inside = [p for p in pts if since <= p[0] <= until]
    before = [p for p in pts if p[0] < since]
    start = before[-1] if before else (inside[0] if inside else None)
    if not start or not inside:
        return None, {"samples": len(inside)}
    # 并行线程的采样会交错、小幅回落是正常的, 不能当重置; 只按 resets_at 分段, 段内 delta = 段内最高 − 段首
    segs: list[list] = []
    cur_reset = start[2]
    seg = [start[1]]
    for _t, u, r in inside:
        if r != cur_reset:
            segs.append(seg)
            seg, cur_reset = [u], r
        seg.append(u)
    segs.append(seg)
    total = sum(max(s) - s[0] for s in segs)
    return round(total, 2), {"samples": len(inside), "segments": len(segs), "used_from_pct": start[1],
                             "used_to_pct": inside[-1][1], "used_max_pct": max(max(s) for s in segs),
                             "first_sample": inside[0][0].isoformat(), "last_sample": inside[-1][0].isoformat()}


def debates_in_window(harness: str, since, until, mode: str = "full") -> int:
    """Debates of this identity stored in the window. mode 'full' (default) excludes follow-up (delta) sessions — they
    burn less and would dilute the full-session cost; 'delta' counts only those; 'all' counts both (11-06)."""
    spec = importlib.util.spec_from_file_location("pre_round_brief", ROOT / "skills/equipoise-dispatch/scripts/pre_round_brief.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    import psycopg2
    c = m._pg_cfg()
    con = psycopg2.connect(host=c["host"], port=c["port"], dbname=c["dbname"], user=c["user"], password=c["password"], connect_timeout=4)
    try:
        with con.cursor() as cur:
            cond = {"full": "AND COALESCE(report->'lineage'->>'mode', 'full') <> 'delta' ",
                    "delta": "AND report->'lineage'->>'mode' = 'delta' "}.get(mode, "")
            cur.execute("SELECT count(*) FROM debates WHERE report->'harness_truth'->>'harness' = %s "
                        f"AND report_generated_at BETWEEN %s AND %s {cond}", (harness, since, until))
            return int(cur.fetchone()[0])
    finally:
        con.close()


def current_version() -> str:
    v = ""
    try:
        v = (yaml.safe_load((ROOT / "config/odds.yaml").read_text(encoding="utf-8")) or {}).get("schema", "")
    except Exception:
        pass
    commit = ""
    p = ROOT / "DEPLOY_COMMIT"
    if p.exists():
        commit = p.read_text(encoding="utf-8").strip()[:8]
    return f"v{v} {commit}".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", default="codex1", choices=sorted(_CODEX_VARIANTS))
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--last-hours", type=float)
    ap.add_argument("--label", default="")
    ap.add_argument("--version", default=None, help="语料版本标签; 省略 = odds.yaml schema + DEPLOY_COMMIT")
    ap.add_argument("--note", default="")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    until = _ts(a.until) if a.until else now
    since = _ts(a.since) if a.since else (until - timedelta(hours=a.last_hours or 6))

    home = _CODEX_VARIANTS[a.harness][1]
    delta, meta = delta_in_window(timeline(home, since, until), since, until)
    n = debates_in_window(a.harness, since, until)
    n_delta = debates_in_window(a.harness, since, until, mode="delta")
    cost = round(delta / n, 3) if (delta is not None and n) else None
    if n_delta:
        print(f"note: {n_delta} follow-up (delta) session(s) in the window are excluded from `debates`; their quota is "
              f"still inside delta_pct — pick a window without them for a clean full-session cost", file=sys.stderr)
    duty = yaml.safe_load((ROOT / "config/duty.yaml").read_text(encoding="utf-8")) or {}
    stop = float((duty.get("usage_stop_used_pct") or {}).get(a.harness, 70))
    entry = {"version": a.version or current_version(), "harness": a.harness, "measured_at": now.strftime("%Y-%m-%d"),
             "method": "usage_calibrate.py: rate_limits delta over window ÷ debates stored in window",
             "window": {"since": since.isoformat(), "until": until.isoformat(), "label": a.label, **meta},
             "debates": n, "delta_sessions_excluded": n_delta, "delta_pct": delta, "cost_pct": cost,
             "debates_per_week_at_100": int(100 / cost) if cost else None,
             "debates_per_week_at_stop": int(stop / cost) if cost else None,
             "note": a.note}
    print(json.dumps(entry, ensure_ascii=False, indent=1))
    if not a.write:
        return 0
    if cost is None:
        print("cost undetermined (no samples or no debates in window); not written", file=sys.stderr)
        return 1
    doc = yaml.safe_load(CAL.read_text(encoding="utf-8")) if CAL.exists() else {}
    doc.setdefault("entries", []).append(entry)
    CAL.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"written: {CAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
