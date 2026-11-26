#!/usr/bin/env python
"""CC 复盘循环驱动器: 一标的一进程, 无状态, 永不炸 context。

设计 (用户 2026-06-28 定): 为防"单 session 复盘多了炸掉", 默认
**for 循环 + 每份一个全新 `claude -p` 子进程**。每个子进程是独立的完整
Claude Code 主 agent (自带 WebSearch), 当 reckoner 跑那一份, 跑完即退,
context 不累积。本驱动器(主 agent)只派活, 不判断 —— 聪明在子进程里。

清单与 worklist.py 同一套逻辑: 列出"成熟但还没被 CC(签名 claude* 前缀)
复盘过"的 (symbol, as_of), 从顶部取 --limit 个本批跑。状态全在 PG, 天然可续。

结果价(2026-07-02): 子进程 /postcheck 的 prep 会**连盈透新请求**该标的日线再算摆动线
(不吃残留旧 CSV; 拉不到=immature 跳过)。故子进程环境要有 EQUIPOISE_IBKR_PORT(本驱动器默认 4001)。

跑法 (必须在 /opt/equipoise 用其 venv):
  cd /opt/equipoise && ./venv/bin/python \
    skills/equipoise-postcheck/scripts/cc_loop.py \
    --dt 3 --start 2026-06-18 --universe us_all --limit 5
  # 先 --dry-run 看会跑哪几个, 不真跑
"""
import argparse, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
sys.path.insert(0, "/opt/equipoise")
from datalayer.pg.db import connect                        # noqa: E402
from datalayer.pg.postcheck_store import _maturity_cutoff  # noqa: E402

from workflows.symbol_universe import postcheck_universes  # noqa: E402

UNIVERSES = postcheck_universes()   # 唯一真源 = config/symbols.yaml, 与辩论侧同一份
EQUIPOISE = "/opt/equipoise"


def _is_agent(rk):  # agent 干的 = claude*(CC) 或 gpt*/codex*(GPT); deepseek 等 = 待替换。
    return bool(rk) and rk.lower().startswith(("claude", "gpt", "codex"))
ALLOWED = ("WebSearch,WebFetch,Read,Write,"
           "Bash(./venv/bin/python datalayer/pg/postcheck_store.py *)")


def pending(dt, start, universe, include_redo):
    """返回待 CC 跑的 [(symbol, as_of_ts_str, current_reckoner_or_None)], 顶部优先。"""
    syms = UNIVERSES[universe]
    cutoff = _maturity_cutoff(datetime.now(timezone.utc), dt)
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT symbol, as_of FROM debates "
            "WHERE symbol = ANY(%s) AND as_of >= %s AND as_of <= %s ORDER BY symbol, as_of",
            (syms, start_dt, cutoff))
        mature = cur.fetchall()
        cur.execute("SELECT symbol, as_of, reckoner FROM postcheck "
                    "WHERE symbol = ANY(%s) AND dt = %s", (syms, dt))
        done = {(s, ao): rk for s, ao, rk in cur.fetchall()}
    todo, redo = [], []
    for s, ao in mature:
        rk = done.get((s, ao))
        ts = ao.strftime("%Y-%m-%dT%H:%M:%SZ")
        if _is_agent(rk): continue
        elif rk is None:  todo.append((s, ts, None))
        else:             redo.append((s, ts, rk))
    return todo + (redo if include_redo else [])


def _real_model_id(model_usage):
    """从 claude -p 回执的 modelUsage 里取真实干活的模型 id = output token 最多那个。
    (主 reckoner 生成量最大; haiku 只是 WebSearch 内部小模型。) 取不到回 None。"""
    if not model_usage:
        return None
    best = max(model_usage.items(),
               key=lambda kv: (kv[1] or {}).get("outputTokens", 0), default=(None, None))
    return best[0]


def _stamp_reckoner(symbol, dt, as_of, model_id):
    """用真实模型 id 回写这期 postcheck 行的 reckoner(代码定版本, 不靠模型自报)。"""
    if not model_id:
        return None
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE postcheck SET reckoner=%s "
            "WHERE symbol=%s AND dt=%s AND as_of=%s::timestamptz AND reckoner IS DISTINCT FROM %s",
            (model_id, symbol, dt, as_of, model_id))
        con.commit()
        return cur.rowcount


def run_one(symbol, dt, as_of, timeout):
    """起一个全新 claude -p 子进程跑这一份。
    返回 (ok, receipt_text, cost_usd, dur_s, real_model_id)。real_model_id 取自子进程
    自己的 modelUsage(代码读真值, 不靠模型自报), 用来回写 reckoner。"""
    prompt = f"/postcheck {symbol} --dt {dt} --as_of {as_of}"
    cmd = ["claude", "-p", prompt, "--model", "opus",
           "--permission-mode", "dontAsk", "--allowedTools", ALLOWED,
           "--output-format", "json"]
    env = dict(os.environ)
    env.setdefault("EQUIPOISE_IBKR_PORT", "4001")   # prep 连盈透拉结果价用; 外部已 export 则尊重
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=EQUIPOISE, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s", 0.0, time.monotonic() - t0, None
    dur = time.monotonic() - t0
    try:
        out = json.loads(proc.stdout)
        ok = not out.get("is_error", True) and out.get("subtype") == "success"
        return (ok, out.get("result", proc.stdout[:500]), out.get("total_cost_usd", 0.0),
                dur, _real_model_id(out.get("modelUsage")))
    except (json.JSONDecodeError, ValueError):
        return False, (proc.stdout or proc.stderr)[:500], 0.0, dur, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dt", type=int, default=3)
    ap.add_argument("--start", default="2026-06-18")
    ap.add_argument("--universe", default="us_all", choices=list(UNIVERSES))
    ap.add_argument("--limit", type=int, default=5, help="本批最多跑几份 (防一次跑太多烧钱)")
    ap.add_argument("--no-redo", action="store_true", help="只跑全新待复盘, 不碰 deepseek 待替换")
    ap.add_argument("--timeout", type=int, default=600, help="单份子进程超时秒")
    ap.add_argument("--jobs", "-j", type=int, default=1,
                    help="并发子进程数(默认 1=串行)。各份互不依赖, 复盘是纯后视判断; "
                         "盈透侧每进程随机 client_id 不互踢, 但别开太猛以免撞历史数据配额, 建议 ≤6")
    ap.add_argument("--stagger", type=float, default=0.0,
                    help="首波子进程启动错峰秒数(默认 0=同时起)。只护盈透 init: 每份开头的 prep "
                         "要连盈透拉日线, 高并发时首波会同时打; 错峰把它们摊开, 之后接力补位天然分散")
    ap.add_argument("--dry-run", action="store_true", help="只列会跑哪几个, 不真跑")
    a = ap.parse_args()

    items = pending(a.dt, a.start, a.universe, include_redo=not a.no_redo)
    batch = items[:a.limit]
    print(f"# cc_loop  dt={a.dt}  universe={a.universe}  待跑总数={len(items)}  本批={len(batch)}")
    for i, (s, ts, rk) in enumerate(batch, 1):
        tag = "新" if rk is None else f"替{rk}"
        print(f"  [{i}] {s:7} {ts}  ({tag})")
    if a.dry_run or not batch:
        print("# dry-run / 无可跑, 退出。" if not batch or a.dry_run else "")
        return

    print(f"\n# 开跑 (每份一个全新 claude -p 子进程, 跑完即退, 并发={a.jobs})...\n", flush=True)
    total_cost, ok_n = 0.0, 0
    done_n = 0

    def _work(idx_item):
        """跑一份并把整段回执攒成一个字符串, 避免并发下多份输出交织。"""
        i, (s, ts, rk) = idx_item
        if a.stagger and i <= a.jobs:      # 只错开首波; 后续是接力补位, 本就分散
            time.sleep((i - 1) * a.stagger)
        ok, receipt, cost, dur, model_id = run_one(s, a.dt, ts, a.timeout)
        stamp = ""
        if ok and model_id:
            n = _stamp_reckoner(s, a.dt, ts, model_id)   # 用真实模型 id 回写签名
            stamp = f"  reckoner={model_id}" + ("(回写)" if n else "")
        block = (f"=== [{i}/{len(batch)}] {s} {ts} dt={a.dt} ===\n"
                 f"  {'OK' if ok else 'FAIL'}  {dur:.0f}s  ${cost:.2f}{stamp}\n"
                 "  " + receipt.strip().replace("\n", "\n  ") + "\n")
        return ok, cost, block

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for ok, cost, block in pool.map(_work, enumerate(batch, 1)):
            total_cost += cost
            ok_n += ok
            done_n += 1
            print(block, flush=True)
            print(f"# 进度 {done_n}/{len(batch)}  累计 ${total_cost:.2f}\n", flush=True)

    print(f"# 本批完成: {ok_n}/{len(batch)} 成功, 累计 ${total_cost:.2f}")
    remain = pending(a.dt, a.start, a.universe, include_redo=not a.no_redo)
    print(f"# dt={a.dt} 还剩待 CC 跑: {len(remain)} 份")


if __name__ == "__main__":
    main()
