#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_debate_data.py — CC 版辩论"备餐"入口 (2026-07-02 设计, 调度五步闭环的第 3/4 步)。

对话 CC 在 fire `claude -p "/run <SYM>"` **之前**跑这一条, 保证 /run 是纯辩论编排器不碰数据:
  1) 盈透 CONNECT 探测          不通 → exit 10, **停下与用户交互**(IB Gateway 可能要手机确认/挑战码)
  2) 拉行情 (自己+benchmark+sector; 1h 内鲜则跳)  → PG technical (+临时 CSV)
  3) 新闻: 美股大区 → Finn (宏观+自己+sector, 每请求随机 key) → PG news
          欧/日/韩 → 区域大盘与个股都走 agent 腿 /refresh-news <EU|JP|KR|SYM>(主会话自带搜索; Linkup 10-25 起不用),
                     备餐只判 12h 鲜度并在回执 NEWS: 行列出还缺的 → PG news(由 fetch_eu_jp_news --load 灌)
  4) hydrate: technical(V16 快照, 含趋势结构数字) + news + sentiment + economic + fundamentals(不取别场报告: 标的互相独立, 不继承结论),
     全从 PG dump 成 /run 要的临时文件(文件=运输载体不是真相, 权威判定在 PG;
     2026-07-09 起 economic/fundamentals 也水合 —— codex sandbox 连不上 PG/api, 两引擎统一读文件)
  5) 汇报每标的 READY / MISSING(STOP级) 清单

用法 (一次可备多个, 但 fire /run 仍一次一个):
    EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py NVDA
    EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py GENERAL_JP NTDOY
    EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py NVDA --force   # 绕过锚点去重

退出码: 0 全部 READY / 3 有 STOP 级缺失或 STALE-ANCHOR(别 fire, 输出里写了原因) / 10 盈透不通
       / 12 全部 FRESH-EXISTS(该锚点已有报告, 别 fire 免得 upsert 覆盖历史; --force 绕过) / 2 用法错

锚点时效闸(2026-07-09): 开市中(US 含盘前盘后)锚点比市场时钟旧超过 config
market_data_freshness.<region> 分钟 → STALE-ANCHOR(exit 3)。盈透半死时 mtime/存在性检查
全会被骗, 这道闸按市场时钟兜底。US 付费实时 10min; EU/JP 延迟源 30min; 闭市按上一收盘算不误伤。

锚点去重(2026-07-09, 复用 API 版积木): 半死盈透只给旧 bar 时, CSV mtime 会被刷新 → 鲜度检查
被骗 → 水合出旧锚点数据。API 版有 _check_analysis_exists 兜底, CC/codex 版的兜底就在这一闸:
data_ts(锚点) 已有 >= 它的报告 → FRESH-EXISTS 别 fire (/run 没有报告去重, pg_store 会覆盖同锚点行)。
"""
from __future__ import annotations
import sys
import asyncio
import argparse
import logging
import subprocess
import fcntl
import signal
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from hydrate_pg_to_files import (  # noqa: E402
    hydrate_technical, hydrate_news, hydrate_sentiment,
    hydrate_economic, hydrate_fundamentals, _load_symbols_cfg,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("prepare")

_REGION_GENERAL = {"EU": "GENERAL_EU", "JP": "GENERAL_JP", "KR": "GENERAL_KR"}
_IBKR_PREPARE_LOCK = Path("/run/lock/equipoise-ibkr-prepare.lock")


def _region_of(symbol: str, sy_cfg: dict) -> str:
    if symbol == "GENERAL_EU":
        return "EU"
    if symbol == "GENERAL_JP":
        return "JP"
    if symbol == "GENERAL_KR":
        return "KR"
    info = (sy_cfg.get("symbol_contracts") or {}).get(symbol) or {}
    return str(info.get("region") or "US").upper()


_REGION_CAL = {"US": "NYSE", "EU": "XETR", "JP": "JPX", "KR": "XKRX"}
_US_PRE_H, _US_POST_H = 5.5, 4.0   # US 付费数据含延长时段: 盘前 04:00 ET(=09:30-5.5h), 盘后至 20:00 ET


def _expected_latest_bar(region: str, now):
    """session-aware 期望的最新 bar 时刻: 开市中(US 含延长时段)=now; 闭市=上一 session 收盘(含延长)。
    日历拿不到 → None(跳过时效验证, 不误伤)。"""
    try:
        from datetime import timedelta
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar(_REGION_CAL.get(region, "NYSE"))
        sched = cal.schedule(start_date=(now - timedelta(days=10)).date(), end_date=now.date())
        if sched.empty:
            return None
        pre = timedelta(hours=_US_PRE_H) if region == "US" else timedelta(0)
        post = timedelta(hours=_US_POST_H) if region == "US" else timedelta(0)
        for _, session in sched.iloc[::-1].iterrows():
            o = session["market_open"].to_pydatetime() - pre
            c = session["market_close"].to_pydatetime() + post
            if now >= c:
                return c        # 该 session 已收: 期望锚点 = 收盘(含延长)
            if now >= o:
                # JPX stops trading during lunch; its latest possible bar stays
                # at the morning close. Do not apply XKRX's discontinued breaks.
                if region == "JP":
                    break_start = session.get("break_start")
                    break_end = session.get("break_end")
                    if break_start is not None and break_end is not None and break_start <= now < break_end:
                        return break_start.to_pydatetime()
                return now      # 盘中(含延长): 期望锚点 ≈ 现在
        return None
    except Exception:
        return None


def _anchor_stale_minutes(region: str, data_ts, data_config: dict):
    """锚点时效闸(2026-07-09): 返回超出允许滞后的分钟数(>0 = 太旧), 或 None = 合格/无法判定。
    机制: 期望最新 bar 时刻(分时段) - 实际锚点 > 区域允许滞后(config market_data_freshness)。
    盈透半死(socket 活着 feed 断)时 mtime/存在性检查全会被骗, 只有这道闸能按市场时钟兜底。"""
    from datetime import datetime, timezone
    if data_ts is None:
        return None
    now = datetime.now(timezone.utc)
    expected = _expected_latest_bar(region, now)
    if expected is None:
        return None
    allowed = float(((data_config.get("data_sources") or {})
                     .get("market_data_freshness") or {}).get(region, 30))
    lag_min = (expected - data_ts).total_seconds() / 60.0
    if region == "JP" and lag_min > allowed:
        # Delayed data may still end at the morning close just after reopening.
        # Count only the overlap with today's lunch, not that closed hour, as lag.
        try:
            import pandas_market_calendars as mcal
            sched = mcal.get_calendar("JPX").schedule(
                start_date=expected.date(), end_date=expected.date())
            if not sched.empty:
                session = sched.iloc[0]
                start, end = session.get("break_start"), session.get("break_end")
                if start is not None and end is not None:
                    overlap = (min(expected, end) - max(data_ts, start)).total_seconds() / 60.0
                    lag_min -= max(0.0, overlap)
        except Exception:
            pass  # Calendar failure retains the conservative wall-clock gate.
    return lag_min - allowed if lag_min > allowed else None


def _news_fresh(symbol: str, max_age_sec: int = 3600) -> bool:
    """PG news 里该 symbol 最新一批是否在 max_age_sec 内 (欧日区域新闻腿的跳闸判据)。"""
    try:
        from datetime import datetime, timezone
        from datalayer.pg.lowfreq_reader import get_news_snapshot
        t = (get_news_snapshot(symbol) or {}).get("fetched_at")
        if not t:
            return False
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() <= max_age_sec
    except Exception:
        return False


def _ctx_type(symbol: str, sy_cfg: dict) -> str:
    if symbol.startswith("GENERAL"):
        return "general"
    # 合约级覆盖(2026-09-30, DAX 案): 非美标的借框架不入 analysis_targets 列表
    # (列表兼作美股分组依据, 入列会被前端挂进美股堆)
    info = (sy_cfg.get("symbol_contracts") or {}).get(symbol) or {}
    if info.get("context_type"):
        return str(info["context_type"])
    if symbol in (((sy_cfg.get("analysis_targets") or {}).get("etfs")) or []):
        return "etf"
    return "stock"


async def _fetch_market(all_needs: list, data_config, symbols_config,
                        force_refresh: bool = False) -> bool:
    """盈透探测 + 拉行情。返回 False = 盈透不通 (上层 exit 10)。"""
    from horizon_sentinel import IbkrFetcher, is_symbol_data_fresh, DATA_FRESHNESS_SECONDS
    ta_dir = data_config.get('data_sources', {}).get('technical_analysis', {}).get('output_dir')
    fetcher = IbkrFetcher(symbols_config, ta_dir, DATA_FRESHNESS_SECONDS)
    currency_region = {"USD": "US", "EUR": "EU", "JPY": "JP", "KRW": "KR"}

    def anchor_stale(symbol: str) -> bool:
        contract = (symbols_config.get("symbol_contracts") or {}).get(symbol) or {}
        region = contract.get("region") or currency_region.get(str(contract.get("currency", "USD")).upper(), "US")
        data_ts = fetcher._latest_csv_bar_ts(symbol)
        if isinstance(data_ts, str):
            try:
                from datetime import datetime, timezone
                data_ts = datetime.strptime(data_ts, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                data_ts = None
        return _anchor_stale_minutes(
            region, data_ts, data_config
        ) is not None

    refresh = {
        s: (force_refresh
            or not is_symbol_data_fresh(s, ta_dir, DATA_FRESHNESS_SECONDS)
            or anchor_stale(s))
        for s in all_needs
    }
    if not any(refresh.values()):
        log.info("行情数据均新鲜, 跳过 IBKR 连接探测")
        return True
    try:
        if not fetcher._ensure_connected_sync():
            return False
        for s in all_needs:
            if refresh[s]:
                await fetcher.fetch_symbol(s, force_refresh=True)
        return True
    finally:
        await fetcher.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="prepare_debate_data.py",
                                 description="CC 版辩论备餐: 查盈透→拉行情+新闻→PG→hydrate→汇报")
    ap.add_argument("symbols", nargs="*", help="标的, 如 NVDA GENERAL_JP")
    ap.add_argument("--active-timeout", type=int, metavar="SECONDS",
                    help="Bound active preparation starting when the IBKR queue grants the batch its lock.")
    ap.add_argument("--force", action="store_true",
                    help="绕过锚点去重与时效闸(FRESH-EXISTS/STALE-ANCHOR 也放行; 慎用)")
    args = ap.parse_args()
    if args.active_timeout is not None and args.active_timeout <= 0:
        ap.error("--active-timeout must be positive")
    syms = [s.strip().upper().replace(" ", ".") for s in args.symbols if s.strip()]
    if not syms:
        ap.print_usage()
        return 2

    from horizon_sentinel import load_helpers
    from run_one_debate import _fetch_needs, _fetch_us_news
    data_config, symbols_config, _ = load_helpers()
    sy_cfg = _load_symbols_cfg()

    # ── 1+2) 盈透探测 + 拉行情 (所有标的的 needs 合并去重) ──────────────────
    all_needs = []
    for sym in syms:
        all_needs.extend(_fetch_needs(sym, symbols_config))
    all_needs = list(dict.fromkeys(all_needs))
    # 议会 v2 (B4 同业横向): 证据地图 peers 一并拉新鲜 —— 沙箱盘中拉不了, 备餐补齐。
    # 只加 symbol_contracts 里有合约的; 地图读不到是警告级, 不拦备餐。
    try:
        import yaml as _yaml
        emap = _yaml.safe_load((ROOT / "config" / "evidence_map.yaml")
                               .read_text(encoding="utf-8")) or {}
        contracts = symbols_config.get("symbol_contracts") or {}
        peer_needs = list(dict.fromkeys(
            p for sym in syms for p in (emap.get(sym) or {}).get("peers", [])
            if p in contracts and p not in all_needs))
        if peer_needs:
            log.info(f"行情 needs +peers(证据地图): {', '.join(peer_needs)}")
            all_needs.extend(peer_needs)
    except Exception as e:
        log.warning(f"[peers] 证据地图读取失败 (警告级, 不拦备餐): {e}")
    log.info(f"行情 needs: {', '.join(all_needs)}")
    # Cross-process guard: Codex/CC/manual dispatchers may coexist, while the
    # asyncio.Lock inside IbkrFetcher only serializes requests in one process.
    # Hold this lock only for the IBKR leg; news/hydration can proceed normally.
    _IBKR_PREPARE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _IBKR_PREPARE_LOCK.open("a+") as lock_fd:
        queued = time.monotonic()
        log.info(f"等待 IBKR 全局备餐锁: {_IBKR_PREPARE_LOCK}")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        acquired = time.monotonic()
        if args.active_timeout:
            # Process-wide wall clock, including news and hydration after the
            # market leg. SIGALRM terminates blocked native calls as well.
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(args.active_timeout)
        log.info("取得 IBKR 全局备餐锁: waited=%.1fs members=%d contracts=%d active_timeout=%s",
                 acquired - queued, len(syms), len(all_needs), args.active_timeout)
        try:
            market_ok = asyncio.run(_fetch_market(
                all_needs, data_config, symbols_config,
                force_refresh=args.force,
            ))
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            log.info("Released IBKR prepare lock after %.1fs of market preparation",
                     time.monotonic() - acquired)
    if not market_ok:
        print("STATUS: ibkr-down")
        print("盈透连不上 —— 停。IB Gateway 可能没登录/要手机确认/挑战码, 找用户处理后再备餐。")
        return 10

    # ── 3) 新闻: 美股 Finn; 欧日韩(区域与个股)只判鲜度, 缺的列进 NEWS: 行由主会话的 agent 腿搜 ──
    regions = {_region_of(s, sy_cfg) for s in syms}
    for sym in syms:
        _fetch_us_news(sym, symbols_config)          # 内部自动跳过 EU/JP
    # 区域大盘新闻(GENERAL_EU/JP/KR): 库主 10-25 晚——Linkup 彻底不用, 区域新闻也走 agent 腿(主会话自带搜索: CC=opus / GPT=sol)。
    # 这里只判 12h 鲜度, 缺的和个股一起列进回执 NEWS: 行; 警告级, 不拦 fire。
    needs_agent_news = []
    for rg in sorted(regions - {"US"}):
        gsym = _REGION_GENERAL[rg]
        if _news_fresh(gsym, max_age_sec=12 * 3600):
            log.info(f"[news] {gsym} 区域新闻 PG 内 12h 鲜")
            continue
        needs_agent_news.append(rg)
        log.warning(f"[news] {gsym} 无 12h 内区域新闻 — 主会话请跑 `/refresh-news {rg}` 后重新备餐 (警告级, 不拦 fire)")
    # v3.3 (2026-10-26): 欧日韩**个股**也要有自己的新闻(以前只有区域大盘有)。主路径是 **agent 腿**: 跑辩论的主会话自己用联网搜索
    # (`/refresh-news <SYM>`, 十条左右, 本国语 + 英文); 库主 10-25: Linkup 与 API 版是上古残余, 不再给它们加路径。
    # 这里只判鲜度(12h), 把还缺的个股列进回执 `NEWS:` 行; 警告级, 不拦 fire(辩论里 Scout 搜索兜底)。
    for sym in syms:
        if _region_of(sym, sy_cfg) == "US" or _ctx_type(sym, sy_cfg) != "stock":
            continue
        if _news_fresh(sym, max_age_sec=12 * 3600):
            log.info(f"[news] {sym} 个股新闻 PG 内 12h 鲜")
            continue
        needs_agent_news.append(sym)
        log.warning(f"[news] {sym} 无 12h 内个股新闻 — 主会话请跑 `/refresh-news {sym}` 后重新备餐 (警告级, 不拦 fire)")
    if needs_agent_news:
        print(f"NEWS: non-US news missing → run `/refresh-news <EU|JP|KR|SYM>` (your own web search: region ≤20 items, single name ~10) then prepare again: {', '.join(needs_agent_news)}")

    # ── 4) hydrate (全从 PG) ─────────────────────────────────────────────────
    for rg in sorted(regions | {"US"}):
        sf = hydrate_sentiment(rg)
        log.info(f"[hydrate] sentiment/{rg}: {sf.name if sf else '(PG空)'}")
        ef = hydrate_economic(rg)
        log.info(f"[hydrate] economic/{rg}: {ef.name if ef else '(PG空 — 警告级, agent 可搜索兜底)'}")
    for sym in syms:
        if _ctx_type(sym, sy_cfg) == "stock":
            ff = hydrate_fundamentals(sym)
            log.info(f"[hydrate] fundamentals/{sym}: {ff.name if ff else '(PG无 — 警告级, agent 可申报搜索补)'}")
    tech_ok: dict = {}
    for s in all_needs:
        tech_ok[s] = hydrate_technical(s)
    news_syms = {"GENERAL"}                            # 宏观新闻所有场景共用必需
    for sym in syms:
        rg = _region_of(sym, sy_cfg)
        if rg in _REGION_GENERAL:
            news_syms.add(_REGION_GENERAL[rg])
        if not sym.startswith("GENERAL"):
            news_syms.add(sym)
            sector = ((sy_cfg.get("symbol_contracts") or {}).get(sym) or {}).get("sector")
            if sector:
                news_syms.add(sector)
    news_ok = {s: hydrate_news(s) for s in sorted(news_syms)}

    # ── 4b) 议会 v2: 同业横向预算 (B4) → data/evidence_prep/<SYM>/ (Scout/仲裁者盘中只读) ──
    # 无地图标的也落盘 (内容即 ok:false "无证据地图条目"), 让辩论侧看得见"没有地图"这个事实。
    try:
        import json as _json
        from datetime import datetime as _dtm, timezone as _tz
        from evidence_quant import handle as _eq_handle
        ts_tag = _dtm.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        for sym in syms:
            pc = _eq_handle({"mode": "peer_compare", "symbol": sym, "window_days": 20})
            outdir = ROOT / "data" / "evidence_prep" / sym
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / f"{sym}_peer_compare_{ts_tag}.json").write_text(
                _json.dumps(pc, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info(f"[evidence_prep] {sym}: peer_compare "
                     f"{'ok' if pc.get('ok') else '— ' + str(pc.get('error'))}")
    except Exception as e:
        log.warning(f"[evidence_prep] 跳过 (警告级): {e}")

    # ── 4c) (V16, 2026-10-25) 趋势结构数字已并入技术快照(trend_signal_inputs / 双梯); 触发信号由 data_manifest 从快照算, 不再落 _trend_context_*.json ──

    # ── 4d) dealer gamma 定位剖面 (GEX/ZGL/双墙; 只 SPY/QQQ/GENERAL, CBOE 免费延迟) ──
    # 市况分类与挂点层参考: 正 gamma=均值回归机制在场, 负 gamma=趋势易加速。
    # 外部免费源, 失败警告级不拦备餐; 个股不算(dealer 方向假设不成立)。
    try:
        import json as _json3
        from datetime import datetime as _dtm3, timezone as _tz3
        from workflows.dealer_gamma import build_dealer_gamma
        _dg_targets = {"GENERAL": ["SPY", "QQQ"], "SPY": ["SPY"], "QQQ": ["QQQ"]}
        ts_tag = _dtm3.now(_tz3.utc).strftime("%Y%m%dT%H%M%SZ")
        for sym in syms:
            for und in _dg_targets.get(sym, []):
                try:
                    dg = build_dealer_gamma(und)
                    outdir = ROOT / "data" / "evidence_prep" / sym
                    outdir.mkdir(parents=True, exist_ok=True)
                    (outdir / f"{sym}_dealer_gamma_{und}_{ts_tag}.json").write_text(
                        _json3.dumps(dg, ensure_ascii=False, indent=1), encoding="utf-8")
                    log.info(f"[dealer_gamma] {sym}/{und}: "
                             f"{'ok, ' + dg['trigger_signal'][:60] if dg.get('ok') else '— ' + str(dg.get('error'))}")
                except Exception as e:
                    log.warning(f"[dealer_gamma] {sym}/{und} 跳过 (警告级): {e}")
    except Exception as e:
        log.warning(f"[dealer_gamma] 整段跳过 (警告级): {e}")

    # ── 5) 汇报: STOP 级缺失清单 (对齐 run.md Step 0e) + 锚点去重闸 ──────────
    from horizon_sentinel import _pg_data_ts, _check_analysis_exists
    any_missing = False
    fresh_exists_n = 0
    for sym in syms:
        rg = _region_of(sym, sy_cfg)
        ctx = _ctx_type(sym, sy_cfg)
        missing = []
        for n in _fetch_needs(sym, symbols_config):
            if not tech_ok.get(n):
                missing.append(f"technical/{n}")
        if not news_ok.get("GENERAL"):
            missing.append("news/GENERAL(宏观)")
        if ctx == "stock" and rg == "US" and not news_ok.get(sym):
            missing.append(f"news/{sym}(个股, US应有Finn)")
        if not (ROOT / "data" / "fear_greed").exists() or not list(
                (ROOT / "data" / "fear_greed").glob("fear_greed_latest_*.json")):
            missing.append("sentiment/US(fear_greed)")

        if missing:
            status = "MISSING: " + ", ".join(missing)
            any_missing = True
        else:
            try:
                data_ts = _pg_data_ts(sym, ctx)
            except Exception:
                data_ts = None
            # 锚点时效闸: 开市中锚点比市场时钟旧超过区域允许滞后 → 盈透半死/延迟超标, 挡下
            over = None if args.force else _anchor_stale_minutes(rg, data_ts, data_config)
            if over is not None:
                status = (f"STALE-ANCHOR (锚点 {data_ts:%Y%m%dT%H%M%SZ} 超允许滞后 {over:.0f} 分钟"
                          f" — 盈透可能半死: 先 ibkr_probe.py, 必要时 ibkr_restart.sh; 硬跑加 --force)")
                any_missing = True
            # 锚点去重: 该 data_ts 已有报告 → 别 fire (/run 无报告去重, 重跑会覆盖同锚点历史行)
            elif data_ts and _check_analysis_exists(sym, data_ts, data_config) and not args.force:
                status = f"FRESH-EXISTS (锚点 {data_ts:%Y%m%dT%H%M%SZ} 已有报告; 重辩加 --force)"
                fresh_exists_n += 1
            elif data_ts:
                status = f"READY (锚点 {data_ts:%Y%m%dT%H%M%SZ})"
            else:
                status = "READY (⚠️ PG 无锚点, 去重/时效闸未生效)"
        print(f"[{sym}] {status}")
        if status.startswith("READY"):
            # RAW DATA FILES 清单: 与 run_step open 同一份代码生成(路径 + 内容说明 + 缺料警告)
            try:
                from workflows.data_manifest import build_manifest, render_raw_data_files, render_trigger_signals
                mf = build_manifest(sym, ROOT, sy_cfg)
                print(f"  CONTEXT_TYPE: {mf['context_type']}  REGION: {mf['region']}  DATA_TIMESTAMP: {mf['ts']}")
                print("  CONTEXT MANUAL: " + mf["context_manual"])
                print("  RAW DATA FILES:")
                for line in render_raw_data_files(mf).splitlines():
                    print("  " + line)
                print("  TRIGGER SIGNALS: " + render_trigger_signals(mf).replace("\n", "\n    "))
            except Exception as e:  # 清单是回执附件, 不拦 READY
                log.warning(f"[manifest] {sym} 清单生成失败(警告级): {e}")

    if any_missing:
        print("\nSTATUS: not-ready  (缺 STOP 级数据, 别 fire /run; 缺啥上面写了)")
        return 3
    if fresh_exists_n == len(syms):
        print("\nSTATUS: fresh-exists  (全部标的该锚点已有报告 — 正常跳过, 别 fire; 重辩用 --force)")
        return 12
    print("\nSTATUS: ready  (可以 fire: claude -p \"/run <SYM>\", 一次一个)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
