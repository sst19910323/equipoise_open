#!/usr/bin/env python
"""CC 辩论循环驱动器 v2: 整批串行备餐，然后限流、错峰启动 `claude -p "/run"` 子进程。

- 辩论是"生成"新报告(写 PG debates 表), 没有 worklist/maturity 概念; STOP 自检由 /run(run.md) 管,
  **报告锚点去重在备餐**(2026-07-09: prepare exit 12 = fresh-exists → 跳过, 防重辩覆盖同锚点历史行)。
- 市场预设只负责选标的，不构成执行依赖。所有 READY 标的在备餐全部完成后进入同一个滚动池；
  默认 maxpar=1 防烧穿账号滚动限额，需要并发时显式 `--parallel --maxpar N`。

备餐内建(2026-07-02 定): 每标的 fire 前先跑 `prepare_debate_data.py <SYM>`(连盈透拉行情+新闻→PG→
  水合临时文件)。**盈透验证策略: 全程不单独探测** —— 备餐建连拉数本身就是探针(数据 1h 内鲜则连
  盈透都不碰, 批内共享的大盘/基准第一个标的拉过后面秒过); 备餐 exit 10(盈透掉线) → **中止整批**
  (盈透是全局的, 别一个个撞), 已跑完的已落库不丢; exit 3(该标的缺 STOP 级数据) → 只跳过它。

写库: 跑完一个标的立刻写 PG 保住一个(中途撞限额/被打断, 成功的已落库)。
权限: 服务器 root 跑, --dangerously-skip-permissions 被硬拒 → --permission-mode dontAsk
  (Task/Agent 在 dontAsk 下默认免授权, spawn Z/R/F 已验证)。

跑法(在 /opt/equipoise 用其 venv):
  cd /opt/equipoise && ./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --market jp --dry-run
  ./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --symbols GENERAL_JP   # 单跑一个
"""
import argparse, json, os, subprocess, sys, time

EQUIPOISE = "/opt/equipoise"
if EQUIPOISE not in sys.path:
    sys.path.insert(0, EQUIPOISE)

from workflows.symbol_universe import market_waves
# 终报 → PG debates 写库(方案 a): 用 codex venv(有 psycopg2)跑 pg_store_debate.py。
# cc_loop 自身可能被系统 python 跑(无 psycopg2), 故显式走 venv 解释器。
_PG_PY = f"{EQUIPOISE}/venv/bin/python"
_PG_STORE = os.path.join(EQUIPOISE, "scripts", "pg_store_debate.py")

# 市场分组只用于选择标的；运行时会展平，GENERAL/ETF/个股没有调度依赖。
WAVES = market_waves()
# (旧 NEEDS_SEARCH/--search 机制已删, 2026-07-02: run.md 从未定义该 flag; parent 搜索是 Step 3
#  内建职责, 欧日新闻由备餐的 fetch_eu_jp_news 腿灌 PG, 不再靠 flag。)
# 权限(2026-06-29 改): root 下 --dangerously-skip-permissions 被硬拒, 改走白名单 =
# ~/.claude/settings.json 的 dontAsk + 工具级 allow(Bash/Read/Write/Edit/Glob/Grep/Web*)。
# spawn 子 agent 的 Agent 工具在 dontAsk 下默认免授权, 无需列入白名单(已验证 GENERAL_JP 三人格 spawn 成功)。
# 子 agent(Z/R/F)frontmatter 限定 tools: Read,Glob(只主 agent 能 WebSearch)。


def _real_model_id(mu):
    """从 claude -p 回执 modelUsage 取真实干活模型 id = output token 最多那个。取不到回 None。"""
    if not mu:
        return None
    best = max(mu.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0), default=(None, None))
    return best[0]


def _parse(symbol, proc_stdout, proc_stderr, dur):
    try:
        out = json.loads(proc_stdout)
        ok = (not out.get("is_error", True)) and out.get("subtype") == "success"
        return {"symbol": symbol, "ok": ok, "receipt": out.get("result", (proc_stdout or "")[:400]),
                "cost": out.get("total_cost_usd", 0.0), "dur": dur,
                "model": _real_model_id(out.get("modelUsage"))}
    except (json.JSONDecodeError, ValueError):
        return {"symbol": symbol, "ok": False, "receipt": (proc_stdout or proc_stderr or "")[:400],
                "cost": 0.0, "dur": dur, "model": None}


def _cmd(symbol):
    # 白名单路线(root 下 dangerous 被拒): 靠 ~/.claude/settings.json 的 dontAsk + 工具级 allow。
    # --permission-mode dontAsk 显式兜底, 即便 settings 被改也不弹窗/不挂起。
    return ["claude", "-p", f"/run {symbol}", "--model", "opus",
            "--permission-mode", "dontAsk", "--output-format", "json"]


class IbkrDown(Exception):
    """备餐报盈透掉线(exit 10) → 中止整批。"""


def prepare(symbol, timeout=900, force=False):
    """备餐(2026-07-02): fire 前拉行情+新闻→PG→水合临时文件。
    返回 (status, receipt), status ∈ ready / not-ready / ibkr-down / fresh-exists / error。
    盈透不单独探测 —— 备餐建连拉数本身就是探针; 数据 1h 内鲜则连盈透都不碰。
    force=True 透传 --force 给备餐(绕过 STALE-ANCHOR/FRESH-EXISTS 两道闸, 重跑用)。"""
    env = dict(os.environ)
    env.setdefault("EQUIPOISE_IBKR_PORT", "4001")   # IB Gateway 端口; 外部已 export 则尊重
    t0 = time.monotonic()
    try:
        p = subprocess.run([_PG_PY, os.path.join(EQUIPOISE, "prepare_debate_data.py"), symbol]
                           + (["--force"] if force else []),
                           cwd=EQUIPOISE, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "error", f"prepare TIMEOUT after {timeout}s"
    tail = [ln.strip() for ln in ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-3:]]
    receipt = " | ".join(tail) + f" ({time.monotonic() - t0:.0f}s)"
    return {0: "ready", 3: "not-ready", 10: "ibkr-down",
            12: "fresh-exists"}.get(p.returncode, "error"), receipt


def fire(symbol, timeout):
    """一个全新 claude -p '/run <SYM>' 子进程, 阻塞到完成。返回回执 dict。"""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(_cmd(symbol), cwd=EQUIPOISE,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"symbol": symbol, "ok": False, "receipt": f"TIMEOUT after {timeout}s",
                "cost": 0.0, "dur": time.monotonic() - t0, "model": None}
    return _parse(symbol, proc.stdout, proc.stderr, time.monotonic() - t0)


def _prep_or_skip(symbol, force=False):
    """跑备餐并分诊。ready → 返回 None(继续 fire); 掉线 → raise IbkrDown;
    其他 → 返回一个 FAIL/跳过回执 dict(跳过该标的)。"""
    st, rc = prepare(symbol, force=force)
    if st == "ready":
        return None
    if st == "ibkr-down":
        print(f"  ⛔ {symbol}: 备餐报盈透掉线 — 中止整批(盈透是全局的, 找用户处理)。{rc}", flush=True)
        raise IbkrDown(symbol)
    if st == "fresh-exists":
        # 该锚点已有报告(半死盈透/市场休市给旧bar的正常情况) — 跳过=成功语义, 别 fire 免覆盖历史
        print(f"  ⏭️ {symbol}: skipped-fresh-exists(该锚点已有报告, 正常跳过)。{rc}", flush=True)
        return {"symbol": symbol, "ok": True, "receipt": f"skipped-fresh-exists: {rc}",
                "cost": 0.0, "dur": 0.0, "model": None}
    print(f"  SKIP {symbol}: 备餐未就绪({st})。{rc}", flush=True)
    return {"symbol": symbol, "ok": False, "receipt": f"prep {st}: {rc}",
            "cost": 0.0, "dur": 0.0, "model": None}


def write_pg(symbol):
    """OK 后跑唯一写库脚本(2026-07-09 文件为界: /run 子进程不碰 PG, 写库全在这层)。
    pg_store_debate.py 一条命令双写 debates+evidence。best-effort, 返回末两行回执。"""
    if not os.path.exists(_PG_STORE) or not os.path.exists(_PG_PY):
        return f"[pg_store] 跳过(缺 {'脚本' if not os.path.exists(_PG_STORE) else 'venv'})"
    try:
        p = subprocess.run([_PG_PY, _PG_STORE, symbol], cwd=EQUIPOISE,
                           capture_output=True, text=True, timeout=30)
        msg = ((p.stdout or "") + (p.stderr or "")).strip()
        rc = " | ".join(msg.splitlines()[-2:]) if msg else "[pg_store] 无回执"
        if "[pg_store] OK" in msg:
            rc += " | " + load_translations(symbol)
        return rc
    except Exception as e:
        return f"[pg_store] 异常: {type(e).__name__}: {e}"


def load_translations(symbol):
    """翻译是辩论的最后一步(库主 10-25): 主会话的 Translator 已把 data/i18n_staging/debates_<SYM>_<TS>_<lang>.json 填好,
    写库成功后由这层入库(translate_reports.py --load-report); 原文已在库, 译文失败只报不挡。"""
    tr = os.path.join(EQUIPOISE, "scripts", "translate_reports.py")
    try:
        p = subprocess.run([_PG_PY, tr, "--load-report", symbol], cwd=EQUIPOISE, capture_output=True, text=True, timeout=120)
        msg = ((p.stdout or "") + (p.stderr or "")).strip()
        return "[i18n] " + (msg.splitlines()[-1] if msg else f"exit={p.returncode}")
    except Exception as e:
        return f"[i18n] 异常: {type(e).__name__}: {e}"


def _report_and_save(r):
    """打印单标的回执 + OK 则**立刻**写 PG(2026-06-30 第2点: 跑完一个保住一个)。返回 r 供累计。"""
    mdl = f" model={r['model']}" if r["model"] else ""
    print(f"  {'OK' if r['ok'] else 'FAIL'} {r['symbol']:8} {r['dur']:.0f}s ${r['cost']:.2f}{mdl}", flush=True)
    print("  " + (r["receipt"] or "").strip().replace("\n", "\n  "), flush=True)
    if r["ok"]:
        print("  " + write_pg(r["symbol"]), flush=True)
    return r


def prepare_all(symbols, force=False):
    """整批串行备餐；返回跳过回执和 READY 标的。"""
    res, ready = [], []
    for s in symbols:
        skip = _prep_or_skip(s, force)
        if skip is None:
            ready.append(s)
        else:
            res.append(skip)
    return res, ready


def run_ready(symbols, timeout, maxpar, stagger_seconds):
    """只 fire 已备好的标的；限流并在相邻启动之间错峰。"""
    import concurrent.futures
    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, maxpar)) as ex:
        futs = []
        for index, s in enumerate(symbols):
            futs.append(ex.submit(fire, s, timeout))
            if index + 1 < len(symbols):
                time.sleep(stagger_seconds)
        for fut in concurrent.futures.as_completed(futs):
            res.append(_report_and_save(fut.result()))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=list(WAVES), help="按市场预设选择标的")
    ap.add_argument("--symbols", help="显式标的清单(逗号分隔), 覆盖 --market")
    ap.add_argument("--parallel", action="store_true", help="并发子进程；不指定则 maxpar=1")
    ap.add_argument("--maxpar", type=int, default=1, help="并发时同时最多几个 claude -p(2026-06-30 默认 1=实质串行; 仅 --parallel 生效)")
    ap.add_argument("--stagger-seconds", type=float, default=5.0, help="相邻 fire 的启动间隔秒数(默认 5)")
    ap.add_argument("--timeout", type=int, default=5400, help="单标的辩论子进程超时秒(2026-06-29实测: 每轮~10min, 6-12轮要60-130min; 默认 90min)")
    ap.add_argument("--force-prepare", action="store_true",
                    help="透传 --force 给备餐(绕过 STALE-ANCHOR/FRESH-EXISTS 闸; 重跑用, 会覆盖同锚点报告)")
    ap.add_argument("--calc-check", choices=["remind", "off", "always"], default=None,
                    help="算术提示的档位: remind(默认, 只提醒) / off(不额外干预) / always(推导数全部用命令算)")
    ap.add_argument("--dry-run", action="store_true", help="只列波次编排, 不真跑")
    ap.add_argument("--base", default=None, help="增量场: 同一标的上一场的 TS(只能配 --symbols 单标的); 经 EQUIPOISE_DELTA_BASE 交给 run_step open")
    ap.add_argument("--why", default=None, help="增量场的一句理由(EQUIPOISE_DELTA_WHY)")
    a = ap.parse_args()
    if a.calc_check:
        os.environ["EQUIPOISE_CALC_CHECK"] = a.calc_check  # 子进程继承; run_step open 读它
    if a.base:
        _syms = [s for s in (a.symbols or "").split(",") if s.strip()]
        if a.market or len(_syms) != 1:
            ap.error("--base 只能配 --symbols 单标的, 不能配 --market")
        os.environ["EQUIPOISE_DELTA_BASE"] = a.base.strip()
        os.environ["EQUIPOISE_DELTA_WHY"] = (a.why or "").strip()

    if a.symbols:
        syms = [s.strip() if s.strip().isdigit() else s.strip().upper()
                for s in a.symbols.split(",") if s.strip()]
        plan = [("explicit", syms)]
    elif a.market:
        plan = WAVES[a.market]
    else:
        ap.error("需要 --market 或 --symbols 其一")

    total = sum(len(s) for _, s in plan)
    tag = f"market={a.market}" if a.market else "symbols"
    par = f"{a.parallel}(maxpar={a.maxpar})" if a.parallel else "False(串行)"
    print(f"# cc_loop(辩论 v2)  {tag}  波次={len(plan)}  标的={total}  parallel={par}")
    for label, ss in plan:
        print(f"  [{label}] {' '.join(ss)}")
    if a.dry_run:
        print("# dry-run, 不真跑。")
        print("# 注: 整批串行备餐完成后才错峰 fire; 备餐掉线中止整批; 终报完成即 upsert PG。")
        return

    print("\n# 开跑(整批串行备餐 → READY 标的错峰进入限流池 → 完成即写 PG)...\n", flush=True)
    ok_n = n = 0
    tcost = 0.0
    try:
        symbols = [s for _, group in plan for s in group]
        prep_results, ready = prepare_all(symbols, a.force_prepare)
        print(f"# PREPARE COMPLETE: {len(ready)} ready; starting debate queue", flush=True)
        pool_size = a.maxpar if a.parallel else 1
        results = prep_results + run_ready(ready, a.timeout, pool_size, a.stagger_seconds)
        for r in results:
            n += 1; ok_n += int(r["ok"]); tcost += r["cost"]
    except IbkrDown as e:
        print(f"\n# ⛔ 中止: 盈透掉线(在 {e} 备餐时发现)。已完成 {ok_n}/{n} 个(已落库不丢), "
              f"剩余待盈透恢复后续跑。", flush=True)
        sys.exit(10)

    print(f"# 完成: {ok_n}/{n} 成功, 累计 ${tcost:.2f}")


if __name__ == "__main__":
    main()
