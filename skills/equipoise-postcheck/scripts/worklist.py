#!/usr/bin/env python
"""agent 复盘工作清单: 列出"还没被 agent 引擎(签名 claude*/gpt*/codex* 前缀)复盘过"的成熟报告。

天然可续: 每复盘一期, 该 (symbol,as_of,dt) 行的 reckoner 会翻成该引擎真实模型 id,
下次再跑本脚本它就从 todo 里消失。新 session 进来先跑这个, 就知道还剩什么。

跑法 (必须在 /opt/equipoise 用其 venv):
  cd /opt/equipoise && ./venv/bin/python \
    skills/equipoise-postcheck/scripts/worklist.py --dt 3 --start 2026-06-18 --universe us_all
"""
import argparse, sys
from datetime import datetime, timezone
sys.path.insert(0, "/opt/equipoise")
from datalayer.pg.db import connect                        # noqa: E402
from datalayer.pg.postcheck_store import _maturity_cutoff  # noqa: E402

from workflows.symbol_universe import postcheck_universes  # noqa: E402

UNIVERSES = postcheck_universes()   # 唯一真源 = config/symbols.yaml, 与辩论侧同一份
def _is_agent(rk):  # agent 干的 = claude*(CC) 或 gpt*/codex*(GPT), 版本无关。
    return bool(rk) and rk.lower().startswith(("claude", "gpt", "codex"))  # deepseek 等其它 = 待替换。

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dt", type=int, default=3)
    ap.add_argument("--start", default="2026-06-18")
    ap.add_argument("--universe", default="us_all", choices=list(UNIVERSES))
    a = ap.parse_args()
    syms = UNIVERSES[a.universe]
    now = datetime.now(timezone.utc)
    cutoff = _maturity_cutoff(now, a.dt)          # as_of <= cutoff 才算窗口跑满
    start = datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    with connect() as con, con.cursor() as cur:
        # 成熟的待复盘报告 (debates 只读)
        cur.execute(
            "SELECT symbol, as_of FROM debates "
            "WHERE symbol = ANY(%s) AND as_of >= %s AND as_of <= %s ORDER BY symbol, as_of",
            (syms, start, cutoff))
        mature = cur.fetchall()
        # 已有 postcheck 行 + 其 reckoner
        cur.execute(
            "SELECT symbol, as_of, reckoner FROM postcheck "
            "WHERE symbol = ANY(%s) AND dt = %s", (syms, a.dt))
        done = {(s, ao): rk for s, ao, rk in cur.fetchall()}

    todo, ds_redo, cc_done = [], [], []
    for s, ao in mature:
        rk = done.get((s, ao))
        if _is_agent(rk):     cc_done.append((s, ao))
        elif rk is None:      todo.append((s, ao))
        else:                 ds_redo.append((s, ao, rk))

    print(f"# agent 复盘清单  dt={a.dt}  start={a.start}  universe={a.universe}")
    print(f"# 成熟报告 {len(mature)} 份 | agent已复盘 {len(cc_done)} | 待复盘(无行) {len(todo)} | 待替换(非agent) {len(ds_redo)}")
    print(f"# 还需 agent 跑: {len(todo) + len(ds_redo)} 份\n")
    print("## 待复盘 (没有任何 postcheck 行):")
    for s, ao in todo:    print(f"  {s:7} {ao.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("\n## 待替换 (deepseek 等非agent 跑过, 默认同键 upsert 覆盖):")
    for s, ao, rk in ds_redo: print(f"  {s:7} {ao.strftime('%Y-%m-%dT%H:%M:%SZ')}  (现={rk})")

if __name__ == "__main__":
    main()
