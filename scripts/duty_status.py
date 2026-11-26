#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""duty_status.py — 值班状态(确定性, 零 LLM): Hermes 每次呼吸跑它, 只看退出码。

把四件事拼成一份 JSON: 盈透在不在线 / 有没有在跑(systemd 单元 + 备餐锁) / 各 codex 身份(harness_resolver 列的)还剩多少额度与一场大约烧多少 /
哪些标的到期该跑(复核时刻过了、价位线撞到、挂单到期、从没跑过、轮空太多)。判断交给值班 Codex(.codex/duty.md), 这里不挑标的。

退出码: 0 可以叫人(盈透通 + 有空位 + 推荐身份有额度 + 有到期标的) · 1 没事干 · 2 盈透挂(什么都不做) · 3 没额度 · 4 池子满/有批在跑
用法(服务器): cd /opt/equipoise && ./venv/bin/python scripts/duty_status.py [--no-probe] [--json-only]
快照: data/status/duty_status_<ts>.json(stdout 同内容; 排障日志)。记忆在 PG(2026-11-04): 每次运行写一行 duty_events(kind=breath),
轮空计数、值班 Codex 的推迟(defer)与"上次叫醒"(防反复叫醒)都从 duty_events 读(datalayer/pg/duty_store.py); 前端走 /api/duty。
--no-record 不写 PG(测试用)。
"""
from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:   # Windows: the module must still import (tests)
    fcntl = None
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

from workflows.harness_resolver import CHOICES, resolve_codex  # noqa: E402
from scripts.duty_quota import live_quota, availability, unavailable_summary  # noqa: E402
from workflows.symbol_universe import MARKET_REGIONS, dispatch_jobs, load_symbol_config, market_waves  # noqa: E402

STATUS_DIR = ROOT / "data" / "status"
LOCK = Path("/run/lock/equipoise-ibkr-prepare.lock")
HARD_REASON_PREFIXES = ("tree dead", "tree expired")   # a defer or a dismissal never holds back these (11-06: a dead or an expired
                                                        # scenario tree; earnings the tree priced correctly are not a reason by themselves — owner)
DISMISSABLE_PREFIXES = ("review time passed", "earnings landed", "order expired")   # the authored / event reasons a duty Codex may strike out


def due_rank(x: dict) -> tuple:
    """Order of the due list (owner, 11-06): divergence first (dead tree, then drift, by severity), then an event landed,
    then everything else (expired order / review date / never_ran / backstop); ties by skips and overdue days."""
    rs = [str(r) for r in x.get("reasons") or []]
    cls = 0 if any(r.startswith(("tree dead", "tree expired")) for r in rs) else 1 if any(r.startswith("drift") for r in rs) \
        else 2 if any(r.startswith("earnings landed") for r in rs) else 3
    od = x.get("overdue_days")
    return (cls, -float(x.get("severity") or 0.0), -int(x.get("skips") or 0), -(od if od is not None and od != 9999 else 1e6))


def duty_memory(now) -> tuple[dict, dict, dict | None, str]:
    """(skips, active defers, last wake, note) from PG duty_events; PG unreachable → empty with the reason in the note."""
    try:
        from datalayer.pg import duty_store
        duty_store.ensure_table()
        return duty_store.latest_skips(), duty_store.active_defers(now), duty_store.latest_wake(), "pg"
    except Exception as exc:
        return {}, {}, None, f"pg unavailable ({type(exc).__name__}): skips, defers and last wake unknown this breath"


def duty_dismissals(now) -> dict:
    """symbol → the newest accepted dismissal still in force (PG duty_events); unreachable → {}."""
    try:
        from datalayer.pg import duty_store
        return duty_store.active_dismissals(now)
    except Exception:
        return {}


def apply_dismissals(items: list, dismissals: dict) -> tuple[list, list]:
    """Owner, 11-06: a duty Codex that judged a call point wrong may strike it out (`dismiss` in its decision): the
    report's authored / event reasons (DISMISSABLE_PREFIXES) stop counting for that report (matched by `last_run`; a
    newer report voids the dismissal). Hard reasons (dead / expired tree) and the measured drift are never dismissed.
    Returns (still due, dismissed)."""
    def _key(r):
        s = str(r or "")
        if s.startswith("review time passed"):
            return "review time passed"
        return s.strip() if s.startswith(DISMISSABLE_PREFIXES) else None
    due, dismissed = [], []
    for it in items:
        d = dismissals.get(it["symbol"])
        if not d or (d.get("last_run") and it.get("last_run") and str(d["last_run"]) != str(it["last_run"])):
            due.append(it); continue
        struck = set(d.get("reasons") or [])      # only what that decision struck out; a later event or order state is new
        keep = [r for r in it.get("reasons") or [] if _key(r) is None or (struck and _key(r) not in struck)]
        if keep:
            due.append(dict(it, reasons=keep, notes_duty=f"call point dismissed by {d.get('harness')} ({d.get('why')})"))
        else:
            dismissed.append({**it, "why": d.get("why"), "dismissed_by": d.get("harness"), "wake": d.get("wake")})
    return due, dismissed


def apply_defers(items: list, defers: dict, now) -> tuple[list, list]:
    """Split a market's due items into (still due, deferred). A defer in force (until > now) holds a symbol back unless
    one of its reasons is hard (HARD_REASON_PREFIXES); then the symbol stays due and says the defer was overridden."""
    due, deferred = [], []
    for it in items:
        d = defers.get(it["symbol"])
        until = None
        if d:
            try:
                until = datetime.fromisoformat(str(d.get("until")).replace("Z", "+00:00"))
            except Exception:
                until = None
        if not until or until <= now:
            due.append(it); continue
        if any(str(r).startswith(HARD_REASON_PREFIXES) for r in it.get("reasons") or []):
            it = dict(it, notes_duty=f"a hard reason overrides the defer until {d.get('until')} ({d.get('why')})")
            due.append(it); continue
        deferred.append({**it, "until": d.get("until"), "why": d.get("why"), "deferred_by": d.get("harness"), "wake": d.get("wake")})
    return due, deferred


def _load_brief_module():
    p = ROOT / "skills" / "equipoise-dispatch" / "scripts" / "pre_round_brief.py"
    spec = importlib.util.spec_from_file_location("pre_round_brief", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg():
    p = ROOT / "config" / "duty.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── 盈透 ──
def probe(cfg) -> int:
    env = dict(os.environ, EQUIPOISE_IBKR_PORT=str(cfg.get("ibkr_port", 4001)))
    try:
        r = subprocess.run([str(ROOT / "venv/bin/python"), str(ROOT / "ibkr_probe.py"), str(cfg.get("probe_symbol", "SPY"))],
                           env=env, cwd=ROOT, capture_output=True, text=True, timeout=90)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 2


# ── 在跑 ──
def running_units(jobs=None, *, strict=False) -> list[dict]:
    try:
        out = subprocess.run(["systemctl", "list-units", "equipoise-codex-*", "--state=active,activating",
                              "--no-legend", "--plain"], capture_output=True, text=True, timeout=15, check=True).stdout
    except Exception:
        if strict:
            raise
        return []
    units = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        unit, desc = parts[0], parts[4]
        m_sym = re.search(r"--symbols\s+(\S+)", desc)
        m_group = re.search(r"--group\s+(\S+)", desc)
        key = m_group.group(1) if m_group else None
        job = (jobs or {}).get(key, {})
        symbols = job.get("members", []) if key else (m_sym.group(1).split(",") if m_sym else [])
        m_h = re.search(r"--harness\s+(\S+)", desc)
        units.append({"unit": unit, "kind": "dispatcher" if "dispatcher.py" in desc else "loop",
                      "symbols": symbols, "group": key, "cost_units": job.get("cost_units", max(1, len(symbols))),
                      "harness": m_h.group(1) if m_h else None})
    return units


def pool_capacity(units, max_par):
    n_loop = sum(1 for u in units if u["kind"] == "loop")
    # dispatcher 带的标的要么已经是 loop 单元(算过了)要么马上要起: 占用取两者较大的那个(上界估计)
    dispatcher_syms = sum(len(u["symbols"]) for u in units if u["kind"] == "dispatcher")
    dispatcher_active = dispatcher_syms > 0
    occupied = max(n_loop, dispatcher_syms)
    free = max(0, max_par - occupied)

    return dict(max_parallel=max_par, running=n_loop, dispatcher_active=dispatcher_active,
                dispatcher_symbols=dispatcher_syms, occupied=occupied, free=free)


def lock_held() -> bool:
    if not LOCK.exists():
        return False
    try:
        with open(LOCK, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            except OSError:
                return True
    except OSError:
        return False


# ── 额度 ──
def _find_in(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_in(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_in(v, key)
            if r is not None:
                return r
    return None


def last_rate_limits(home: str, now) -> dict | None:
    """该身份会话日志里最新一条 rate_limits(从最近改动的几个 jsonl 倒着找)。"""
    files = sorted(Path(home, "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rl = _find_in(rec, "rate_limits")
            prim = (rl or {}).get("primary") if isinstance(rl, dict) else None
            if not prim:
                continue
            ts = rec.get("timestamp")
            sampled = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            resets = prim.get("resets_at")
            return {"used_percent": prim.get("used_percent"), "window_minutes": prim.get("window_minutes"),
                    "resets_at": datetime.fromtimestamp(resets, timezone.utc).isoformat() if resets else None,
                    "plan_type": (rl or {}).get("plan_type"), "sampled_at": sampled.isoformat(),
                    "stale_minutes": round((now - sampled).total_seconds() / 60, 1), "source": str(f)}
    return None


def debates_since(brief, harness: str, since) -> int:
    import psycopg2
    c = brief._pg_cfg()
    try:
        con = psycopg2.connect(host=c["host"], port=c["port"], dbname=c["dbname"], user=c["user"],
                               password=c["password"], connect_timeout=4)
        try:
            with con.cursor() as cur:
                cur.execute("SELECT count(*) FROM debates WHERE report->'harness_truth'->>'harness' = %s "
                            "AND report_generated_at >= %s", (harness, since))
                return int(cur.fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0


LEDGER = ROOT / "data" / "status" / "usage_ledger.jsonl"


def calibrated_cost(harness: str) -> dict | None:
    p = ROOT / "config" / "usage_calibration.yaml"
    try:
        entries = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("entries") or []
    except Exception:
        return None
    mine = [e for e in entries if e.get("harness") == harness and e.get("cost_pct")]
    pool = mine or [e for e in entries if e.get("cost_pct")]   # 该身份没量过就借用别的身份的(同一套语料)
    return pool[-1] if pool else None


def ledger_append(now, harness: str, u: dict) -> None:
    """每次呼吸给每个身份记一行: 消耗曲线 / 每场成本 / 剩余与每日配额, 日后校准用。"""
    row = {"at": now.isoformat(), "harness": harness,
           **{k: u.get(k) for k in ("used_percent", "resets_at", "sampled_at", "debates_since_reset", "cost_per_debate_pct",
                                    "stop_used_pct", "burn", "debates_left", "days_to_reset",
                                    "launched_today", "debates_last_24h", "debates_last_7d")}}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def reset_radar(cfg, now) -> dict:
    """tibo 雷达: 只信 tracker 自己的 signal.active(原帖明确宣布会 reset 且尚未落地)。拉不到 = 关着。"""
    if not cfg.get("reset_radar", True):
        return {"open": False, "why": "disabled"}
    try:
        import urllib.request
        req = urllib.request.Request(cfg.get("reset_feed_url", "https://codex-reset.com/api/feed"),
                                     headers={"User-Agent": "equipoise-duty/1.0 (+curl-compatible)", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"open": False, "why": f"feed unavailable ({type(e).__name__})"}
    sig = d.get("signal") or {}
    at = sig.get("at")
    fresh = False
    if at:
        try:
            fresh = (now - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() < 3 * 86400
        except ValueError:
            fresh = False
    open_ = bool(sig.get("active")) and fresh and not d.get("stale")
    return {"open": open_, "signal": {"summary": sig.get("summary"), "at": at, "url": sig.get("url"), "active": sig.get("active")},
            "feed_fetched_at": d.get("fetched_at"), "stale": d.get("stale")}


def effective_stop(base: float, days_to_reset: float | None, burn: bool, cfg) -> float:
    """有效停派线: 雷达开 → 100; 最后 stop_relax_days 天内从 base 线性放开到 final_day_stop_pct; 否则 base。"""
    final = float(cfg.get("final_day_stop_pct", 100))
    if burn:
        return final
    relax = float(cfg.get("stop_relax_days", 2))
    if days_to_reset is None or relax <= 0 or days_to_reset >= relax:
        return base
    return round(base + (final - base) * (relax - days_to_reset) / relax, 1)


def trading_days_between(start, end) -> float:
    """start→end 之间的交易日数(周一到周五, 不看节假日), 今天按剩余比例算; 最少 0.25。"""
    if end <= start:
        return 0.25
    total, t = 0.0, start
    while t < end:
        day_end = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seg_end = min(day_end, end)
        if t.weekday() < 5:
            total += (seg_end - t).total_seconds() / 86400
        t = seg_end
    return max(round(total, 2), 0.25)


def usage_block(brief, cfg, now, burn: bool = False, running=()) -> dict:
    out = {}
    stop = cfg.get("usage_stop_used_pct") or {}
    budgets = cfg.get("weekly_units_at_85", {})
    for name in cfg.get("harness_priority", CHOICES):
        account = resolve_codex(name)
        u = live_quota(account.executable, account.codex_home, name)
        if u.get("quota_state") == "unknown":
            out[name] = u
            continue
        used = float(u["used_percent"] or 0)
        win = int(u.get("window_minutes") or 0)
        if u.get("resets_at") and win:
            since = datetime.fromisoformat(u["resets_at"]) - timedelta(minutes=win)
            n = debates_since(brief, name, since)
            u["debates_since_reset"] = n
            u["cost_running_avg_pct"] = round(used / n, 2) if n and used > 0 else None
        # 每场成本: 优先库主校准过的(config/usage_calibration.yaml 该身份最新一条), 没有才用本周期全包平均
        cal = calibrated_cost(name)
        if cal:
            u["cost_per_debate_pct"], u["cost_source"] = cal["cost_pct"], f"calibrated {cal.get('version')} {cal.get('measured_at')}"
        else:
            u["cost_per_debate_pct"], u["cost_source"] = u.get("cost_running_avg_pct"), "running average since reset"
        # 11-04 库主: 脚本只给原始量, 每天起几场不再由脚本配额, 交给值班 codex 按"一周能烧多少 + tibo 雷达"自己定
        days = None
        if u.get("resets_at"):
            resets = datetime.fromisoformat(u["resets_at"])
            u["days_to_reset"] = days = round(max((resets - now).total_seconds() / 86400, 0), 1)
        base = float(stop.get(name, 70))
        s = effective_stop(base, days, burn, cfg)
        u["stop_used_pct"], u["burn"] = s, burn
        u["debates_left"] = (max(int((s - used) / u["cost_per_debate_pct"]), 0) if u.get("cost_per_debate_pct") else None)
        u["launched_today"] = debates_since(brief, name, now.replace(hour=0, minute=0, second=0, microsecond=0))
        u["debates_last_24h"] = debates_since(brief, name, now - timedelta(hours=24))
        u["debates_last_7d"] = debates_since(brief, name, now - timedelta(days=7))
        u["available"], u["unavailable_reason"] = availability(u, s)
        if name in budgets:
            u["weekly_units_at_85"] = budgets[name]
            u["reserved_units"] = sum(x["cost_units"] for x in running if x.get("harness") == name)
            u["units_left"] = max(0, (s - used) * budgets[name] / 85 - u["reserved_units"])
            u["units_per_trading_day"] = round(u["units_left"] / trading_days_between(now, resets), 2)
            from datalayer.pg.duty_store import recent
            launches = recent("launch", since=now.replace(hour=0, minute=0, second=0, microsecond=0), limit=1000)
            u["units_launched_today"] = sum(
                event["payload"].get("cost_units", len(event["payload"].get("symbols", [])))
                for event in launches if event.get("harness") == name)
            if u["units_left"] < 1:
                u.update(available=False, unavailable_reason="less than one unit remaining")
        ledger_append(now, name, u)
        out[name] = u
    out["claude"] = None
    return out


# ── 新鲜日线(撞线判定用) ──
class FreshBars:
    """值班撞线用的盈透新鲜日线(2026-11-04 补: 之前只读辩论备餐留下的 CSV, 最近没跑过的标的撞线判不出来)。
    一条连接, 按需对每个标的发一次"近 1 个月日线"请求(~1s/只), **不落 CSV**(落了会刷新 mtime, 骗过备餐的 1h 鲜度检查),
    再与缓存 CSV 合并(同日以新拉的为准)。美股 use_rth=0(买了盘前盘后, 盘后大事当天就能撞线), 非美股强制常规时段。
    拉不到(连不上 / 没合约 / 超时)就退回缓存 CSV, 不抛。备餐锁被占(有批在拉盈透)时整体不连, 只用缓存。"""

    def __init__(self, brief, cfg):
        self.brief, self.cfg = brief, cfg
        self.cache: dict = {}
        self.fetcher = None
        self.lock_fd = None
        self.dead = False
        self.stats = {"fetched": 0, "failed": [], "cache_only": [], "mode": "fresh"}
        try:
            self.contracts = load_symbol_config().get("symbol_contracts") or {}
        except Exception:
            self.contracts = {}

    def _connect(self) -> bool:
        if self.fetcher is not None:
            return True
        if self.dead:
            return False
        try:
            LOCK.parent.mkdir(parents=True, exist_ok=True)
            self.lock_fd = open(LOCK, "a+")
            try:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock_fd.close(); self.lock_fd = None
                self.dead, self.stats["mode"] = True, "cache (prepare lock held)"
                return False
            import logging
            import random
            logging.getLogger("ibapi").setLevel(logging.WARNING)   # 否则每次呼吸往 duty.log 灌 ~30KB 协议日志
            os.environ.setdefault("EQUIPOISE_IBKR_PORT", str(self.cfg.get("ibkr_port", 4001)))
            from datalayer.fetchers.interactive_stock_fetcher import MarketDataConfig, MarketDataFetcher
            f = MarketDataFetcher(MarketDataConfig.from_yaml(str(ROOT / "config" / "data_sources.yaml")))
            if not f.connect_and_start(host=os.environ.get("EQUIPOISE_IBKR_HOST", "127.0.0.1"),
                                       port=int(self.cfg.get("ibkr_port", 4001)), client_id=random.randint(30000, 39999)) \
                    or not f.isConnected():
                raise ConnectionError("connect failed")
            self.fetcher = f
            return True
        except Exception as e:  # noqa: BLE001
            self.dead, self.stats["mode"] = True, f"cache (connect failed: {type(e).__name__})"
            self.close()
            return False

    def _fresh(self, sym: str):
        c = self.contracts.get(sym)
        # GENERAL* 没有自己的合约(它的线写的是镜头标的, 由 df_lookup 各自去拉)
        if not c or not all(c.get(k) for k in ("symbol", "sec_type", "exchange", "currency")) or not self._connect():
            return None
        import pandas as pd
        from datalayer.fetchers.interactive_stock_fetcher import SymbolContract
        from datalayer.fetchers.session_filter import drop_off_exchange_bars
        f = self.fetcher
        sc = SymbolContract(symbol=c["symbol"], sec_type=c["sec_type"], exchange=c["exchange"],
                            currency=c["currency"], ibkr_ticker=c.get("ibkr_ticker"))
        rid = f._next_req_id()
        f._submit_request(req_id=rid, contract=sc.to_ib_contract(), duration="1 M", bar_size="1 day", end_datetime="",
                          what_to_show="TRADES", use_rth=0 if str(c["currency"]).upper() == "USD" else 1, format_date=2)
        if not f._wait_for_completion(rid, timeout=20.0):
            raise TimeoutError("hist request timed out")
        df = f._bars_to_dataframe(f._data_store.pop(rid, []) or [])
        if df is None or df.empty:
            return None
        df, _flt = drop_off_exchange_bars(df, sc.symbol, "1 day")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date")
        return df.sort_index()

    def __call__(self, symbol):
        sym = str(symbol).upper()
        if sym in self.cache:
            return self.cache[sym]
        base = self.brief._daily_df(sym)
        fresh = None
        try:
            fresh = self._fresh(sym)
        except Exception as e:  # noqa: BLE001
            self.stats["failed"].append(f"{sym}:{type(e).__name__}")
        if fresh is None or fresh.empty:
            if (self.contracts.get(sym) or {}).get("sec_type"):
                self.stats["cache_only"].append(sym)
            df = base
        else:
            import pandas as pd
            self.stats["fetched"] += 1
            cols = ["open", "high", "low", "close"]
            fresh = fresh[[c for c in cols if c in fresh.columns]]
            if base is not None and len(base):
                days = set(fresh.index.date)
                base = base[[c for c in cols if c in base.columns]]
                base = base[[d not in days for d in base.index.date]]
                df = pd.concat([base, fresh]).sort_index()
            else:
                df = fresh
        self.cache[sym] = df
        return df

    def close(self):
        if self.fetcher is not None:
            try:
                self.fetcher.disconnect_and_stop()
            except Exception:
                pass
            self.fetcher = None
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
                self.lock_fd.close()
            except Exception:
                pass
            self.lock_fd = None


# ── 财报落地(事件型到期) ──
_TASK_LOG = ROOT / "logs" / "hermes_task.log"


def earnings_events(brief, now, lookback_days: int = 10) -> dict:
    """{SYM: {"release": date, "loaded_at": datetime|None}}: PG fundamentals 最近 lookback_days 天内有新 release_date 的标的,
    配上 hermes_task.log 里该标的最近一次 refresh-fundamentals 成功的时刻(没有就 None, 退回按日期比)。"""
    out = {}
    try:
        import psycopg2
        c = brief._pg_cfg()
        con = psycopg2.connect(host=c["host"], port=c["port"], dbname=c["dbname"], user=c["user"],
                               password=c["password"], connect_timeout=4)
        try:
            with con.cursor() as cur:
                cur.execute("SELECT symbol, max(release_date) FROM fundamentals WHERE release_date >= %s "
                            "AND release_date <= %s GROUP BY symbol",
                            ((now - timedelta(days=lookback_days)).date(), now.date()))
                for sym, rel in cur.fetchall():
                    out[str(sym).upper()] = {"release": rel, "loaded_at": None}
        finally:
            con.close()
    except Exception:
        return {}
    try:
        pending = None
        pat_try = re.compile(r"refresh-fundamentals: try \S+: .*?(?:/refresh-fundamentals\s+|SYMBOL=)([A-Za-z0-9_.]+)")
        pat_done = re.compile(r"^\[(\S+?)Z?\] refresh-fundamentals: done by \S+(?:.*SYMBOL=([A-Za-z0-9_.]+))?")
        for line in _TASK_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            m = pat_try.search(line)
            if m:
                pending = m.group(1).upper()
                continue
            m = pat_done.search(line)
            if m:
                sym = (m.group(2) or pending or "").upper()
                if sym in out:
                    out[sym]["loaded_at"] = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                pending = None
    except Exception:
        pass
    return out


def _earnings_reason(ev, last_gen: str | None) -> str | None:
    if not ev or not last_gen:
        return None
    lg = datetime.fromisoformat(last_gen)
    if lg.tzinfo is None:
        lg = lg.replace(tzinfo=timezone.utc)
    rel, loaded = ev["release"], ev.get("loaded_at")
    if rel < lg.date():
        return None
    if loaded is not None:
        if loaded <= lg:
            return None
    elif rel <= lg.date():
        return None
    return f"earnings landed: release {rel.isoformat()[5:]}" + (f", fundamentals loaded {loaded.strftime('%m-%dT%H:%MZ')}" if loaded else "")


def pick_harness(usage: dict, running_h: set, priority: list) -> str | None:
    """Which identity to wake. Rules: the identities never run at the same time (one running → only it);
    only identities under their own stop line (`available`); among those, the one furthest below its own
    sustainable pace — today's launches ÷ (debates_left ÷ days to its own reset), each identity against its own
    reset clock (11-04: codex1 had run 20 that day while codex2 sat idle with 48 left) — ties by the priority list.
    No daily quota: this only orders the candidates, the duty Codex decides how many."""
    cands = []
    for i, h in enumerate(priority):
        if running_h and h not in running_h:
            continue
        u = usage.get(h) or {}
        if not u.get("available"):
            continue
        left = u.get("units_left", u.get("debates_left")); days = u.get("days_to_reset")
        slack = (float(left) / max(float(days), 0.5)) if (left is not None and days is not None) else None
        slack = u.get("units_per_trading_day", slack)
        pace = (float(u.get("units_launched_today", u.get("launched_today")) or 0) / slack) if slack else (0.0 if slack is None else float("inf"))
        cands.append((pace, i, h))
    return min(cands)[2] if cands else None


# ── 到期 ──
def market_state(region: str, now) -> dict:
    try:
        spec = importlib.util.spec_from_file_location("prepare_debate_data", ROOT / "prepare_debate_data.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        exp = m._expected_latest_bar(region, now)
    except Exception:
        exp = None
    if exp is None:
        return {"state": "unknown", "expected_latest_bar": None, "minutes_since_close": None}
    if abs((exp - now).total_seconds()) < 120:
        return {"state": "open", "expected_latest_bar": now.isoformat(), "minutes_since_close": 0}
    return {"state": "closed", "expected_latest_bar": exp.isoformat(),
            "minutes_since_close": round((now - exp).total_seconds() / 60)}


def _slot_not_yet(ra, region: str, cfg, now) -> bool:
    """recommend_after 只写了日期 → 当作该日区域缺省时刻; 还没到返回 True。"""
    s = str(ra or "").strip()
    if len(s) != 10:
        return False
    hhmm = str((cfg.get("default_slot_utc") or {}).get(region, "13:30"))
    try:
        slot = datetime.fromisoformat(f"{s}T{hhmm}:00+00:00")
    except ValueError:
        return False
    return now < slot


def _next_due(rows, region: str, cfg, now):
    """这个市场下一个会到期的时刻(只看 recommend_after; 日期型按缺省班次), 给人看扎堆情况。"""
    times = []
    for r in rows:
        s = str(r.get("recommend_after") or "").strip()
        if not s:
            continue
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00")) if len(s) > 10 else \
                datetime.fromisoformat(f"{s}T{(cfg.get('default_slot_utc') or {}).get(region, '13:30')}:00+00:00")
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t > now:
            times.append((t, r["symbol"]))
    times.sort()
    if not times:
        return None
    first = times[0][0]
    same = [s for t, s in times if abs((t - first).total_seconds()) < 1800]
    return {"at": first.isoformat(), "symbols_within_30min": same}


def _date_slot_not_yet(reason: str, region: str, cfg, now) -> bool:
    """"line hit: date reached MM-DD" 只到了日期、还没到该区缺省班次(今天的 default_slot_utc)→ 不算(之前 00:00 UTC 就触发)。"""
    m = re.match(r"line hit: date reached (\d\d-\d\d)$", reason)
    if not m or m.group(1) != now.strftime("%m-%d"):
        return False
    return _slot_not_yet(now.date().isoformat(), region, cfg, now)


RECORD_DIVERGENCE = False   # main() turns it on unless --no-record: every breath's divergence readings go to PG divergence_log


def _log_divergence(rows: list, now) -> str | None:
    """11-06 (owner): keep each breath's divergence readings (one day row per symbol / report / lens, PG divergence_log)
    so the dead / drift thresholds can be calibrated against postcheck outcomes after two weeks. Never fatal."""
    if not RECORD_DIVERGENCE:
        return None
    try:
        from datalayer.pg import divergence_log
        divergence_log.record(rows, now)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"divergence not logged ({type(exc).__name__})"


def latest_group_definitions(keys):
    """Only a published hearing can establish the baseline for a group."""
    from contextlib import closing
    from datalayer.pg.lowfreq_reader import _connect_ro, _load_pg_cfg
    con = _connect_ro(_load_pg_cfg())
    if con is None:
        raise RuntimeError("cannot read published group definitions")
    with closing(con), con.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (symbol) symbol, report->'group_definition' "
                    "FROM debate_group_sessions WHERE symbol = ANY(%s) ORDER BY symbol, as_of DESC",
                    [list(keys)])
        return dict(cur.fetchall())


def group_init_backlog(definitions, published):
    return {key: ("new group" if key not in published else
                  "previous definition unrecorded" if published[key] is None else "definition changed")
            for key, definition in definitions.items() if published.get(key) != definition}


def due_block(brief, cfg, now, running_syms: set, fresh: "FreshBars | None" = None, *, symcfg=None) -> tuple[dict, dict]:
    symcfg = load_symbol_config() if symcfg is None else symcfg
    groups_config = symcfg.get("research_groups", {})
    backlog = group_init_backlog(groups_config, latest_group_definitions(groups_config))
    init_owner = {sym: key for key in backlog for sym in groups_config[key]["members"]}
    waves = market_waves(symcfg)
    skips, defers, last_wake, mem_note = duty_memory(now)   # 只读; duty.sh(duty_record.py verify)在真叫醒后更新轮空与推迟
    dismissals = duty_dismissals(now)
    backstop = int(cfg.get("backstop_skips", 6))
    grace = float(cfg.get("after_close_grace_minutes", 90))
    events = earnings_events(brief, now)
    markets = {}
    for market, groups in waves.items():
        region = MARKET_REGIONS.get(market, "US")
        ms = market_state(region, now)
        # 数据窗口: 开盘中(美股含延长时段)或收盘后宽限内才算有正经数据; 日历拿不到就不拦
        in_window = ms["state"] == "open" or ms["state"] == "unknown" or \
            (ms["state"] == "closed" and (ms.get("minutes_since_close") or 0) <= grace)
        syms = [s for _l, ss in groups for s in ss if s not in running_syms]
        # 窗内的市场用盈透新鲜日线判撞线; 窗外的到期标的反正只进 waiting, 读缓存即可
        rows = brief.pull(syms, now, with_bars=True, fetch=False,
                          df_source=fresh if (fresh is not None and in_window) else None,
                          include_base_day=True,   # 报警口径: 报告当天那根(含盘中为止的部分 bar = 最新价)也算
                          div_cfg=cfg.get("divergence") or {})
        log_note = _log_divergence(rows, now)
        if log_note:
            mem_note = f"{mem_note}; {log_note}" if mem_note else log_note
        items, waiting, pending_init = [], [], []
        for r in rows:
            sym = r["symbol"]
            if sym in running_syms:
                continue
            reasons = []
            if not r.get("history"):
                reasons.append("never_ran")
                pending_init.append(sym)
            else:
                reasons += r.get("due") or []
                if r.get("has_scenario_baseline") is False:
                    pending_init.append(sym)
                    reasons.append("init: no scenario baseline for this symbol")
                # 日期型 recommend_after: 区域缺省班次还没到, "review time passed" 不算
                if _slot_not_yet(r.get("recommend_after"), region, cfg, now):
                    reasons = [x for x in reasons if not x.startswith("review time passed")]
                reasons = [x for x in reasons if not _date_slot_not_yet(x, region, cfg, now)]
                ev = _earnings_reason(events.get(sym.upper()), r.get("last_gen"))
                if ev:
                    reasons.append(ev)
            init_group = init_owner.get(sym)
            if init_group:
                reasons.append(f"group init: {init_group} ({backlog[init_group]})")
            if not reasons:
                continue
            last_gen = r.get("last_gen")
            # 区域没有比上一份报告更新的 K 线 → 现在跑只会撞 exit 12, 不算到期
            exp = ms.get("expected_latest_bar")
            if not init_group and exp and last_gen and datetime.fromisoformat(exp) <= datetime.fromisoformat(last_gen):
                continue
            if not in_window:
                waiting.append(sym)
                continue
            n_skip = int(skips.get(sym, 0))
            div = r.get("divergence") if isinstance(r.get("divergence"), dict) else None
            items.append({"symbol": sym, "region": region, "reasons": reasons, "skips": n_skip,
                          "last_run": last_gen, "overdue_days": r.get("overdue_days"),
                          "score": r.get("composite_score"), "op_type": r.get("op_type"),
                          "order": r.get("order"), "notes": r.get("notes"),
                          "severity": (div or {}).get("severity"), "divergence": div,
                          "events": r.get("events"), "events_source": r.get("events_source"),
                          "expires_at": r.get("expires_at")})
        items, dismissed = apply_dismissals(items, dismissals)   # before the backstop: a dismissed call point is not a skipped wake
        for it in items:
            if it["skips"] >= backstop:
                it["reasons"].append(f"backstop: skipped {it['skips']} wakes")
        items.sort(key=due_rank)
        items, deferred = apply_defers(items, defers, now)
        # 值班 codex 在沙箱里连不上 PG: 把该市场其余(没到期、没推迟、没在跑)标的的一行摘要也给它, 让它能"自己再看看"
        due_syms = {x["symbol"] for x in items} | {x["symbol"] for x in deferred}
        others = [{"symbol": r["symbol"], "last_run": r.get("last_gen"), "recommend_after": r.get("recommend_after"),
                   "score": r.get("composite_score"), "op_type": r.get("op_type"), "order": r.get("order"),
                   "bars_through": r.get("bars_through"), "notes": r.get("notes"),
                   "divergence": ({k: r["divergence"].get(k) for k in ("level", "severity", "tail", "move_atr", "t", "lens")}
                                  if isinstance(r.get("divergence"), dict) else None)}
                  for r in rows if r["symbol"] not in due_syms]
        markets[market] = {"region": region, "market": {**ms, "in_window": in_window}, "due": items, "n_due": len(items),
                           "others": others, "deferred": deferred, "dismissed": dismissed,
                           "pending_init_groups": {key: reason for key, reason in backlog.items()
                                                   if groups_config[key]["region"] == region},
                           "pending_init": pending_init, "waiting_for_window": waiting, "next_due": _next_due(rows, region, cfg, now)}
    return markets, {"skips": skips, "defers": defers, "last_wake": last_wake, "memory": mem_note, "at": now.isoformat()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true", help="跳过盈透探针(测试用)")
    ap.add_argument("--json-only", action="store_true", help="只打印 JSON")
    ap.add_argument("--no-record", action="store_true", help="不往 PG 写 breath 行(测试用)")
    a = ap.parse_args()
    global RECORD_DIVERGENCE
    RECORD_DIVERGENCE = not a.no_record
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    brief = _load_brief_module()

    symcfg = load_symbol_config()
    jobs = dispatch_jobs(symcfg)
    units = running_units(jobs)
    running_syms = {s for u in units for s in u["symbols"]}
    capacity = pool_capacity(units, int(cfg.get("max_parallel", 8)))
    free = capacity["free"]

    radar = reset_radar(cfg, now)
    usage = usage_block(brief, cfg, now, burn=bool(radar.get("open")), running=units)
    running_h = {u["harness"] for u in units if u["harness"]}
    rec = pick_harness(usage, running_h, cfg.get("harness_priority") or CHOICES)

    # 顺序(库主 11-04): 额度 → 盈透(不在就停, 不再往下算) → 池子 → 到期(能起才连盈透拉新鲜日线判撞线)
    probe_exit = None if a.no_probe else probe(cfg)
    # 11-04 库主: "撞线了但没额度 / 池满没跑" 和 "没判出撞线" 是两回事, 记录里必须分得开 → 只要盈透在, 每次呼吸都按新鲜日线(含最新价)判到期,
    # 到期清单照记进 breath 行; 没额度 / 池满只体现在退出码(exit_reason), 不再省掉这一步
    fresh = FreshBars(brief, cfg) if (probe_exit in (None, 0) and not a.no_probe and cfg.get("fresh_bars", True)) else None
    if probe_exit not in (None, 0):
        markets, state = {}, {"skips": {}, "defers": {}, "last_wake": None, "memory": "probe failed; memory not read", "at": now.isoformat()}
    else:
        import contextlib
        try:
            # 盈透取数层会 print 到 stdout, 而 duty.sh 要从 stdout 读 JSON → 全挪到 stderr
            with contextlib.redirect_stdout(sys.stderr):
                markets, state = due_block(brief, cfg, now, running_syms, fresh, symcfg=symcfg)
        finally:
            if fresh is not None:
                fresh.close()
    total_due = sum(m["n_due"] for m in markets.values())
    for key, job in jobs.items():
        due = markets.get(job["market"], {}).get("due", [])
        job["due_members"] = [row["symbol"] for row in due if row["symbol"] in job["members"]]

    if probe_exit not in (None, 0):
        code = 2
    elif free <= 0:
        code = 4   # 池满
    elif rec is None:
        code = 3
    elif total_due == 0:
        code = 1
    else:
        code = 0
    # 11-04 库主: 起几个由值班 codex 定, 脚本的硬顶只剩池子空位
    launch_cap = free if code == 0 else 0
    quiet = None
    if code == 0:
        # 防反复叫醒: 上次叫醒(PG 里最近一行 exit=0 的 breath)后到期集合没多出新标的、空位也没变多、且不到 wake_cooldown_minutes → 这轮不叫
        try:
            lw = state.get("last_wake") or {}
            if lw:
                due_now = {d["symbol"] for m in markets.values() for d in m["due"]}
                age = (now - lw["at"]).total_seconds() / 60
                if due_now <= set(lw.get("due") or []) and free <= int(lw.get("free") or 0) \
                        and age < float(cfg.get("wake_cooldown_minutes", 30)):
                    code, launch_cap = 1, 0
                    quiet = f"nothing new since the wake {age:.0f} min ago"
        except Exception:
            pass

    exit_reason = {0: "wake the duty codex", 1: ("quiet: " + quiet) if quiet else "nothing due", 2: "ibkr down: no dispatch",
                   3: (unavailable_summary(usage) if not any((u or {}).get("available") for u in usage.values())
                       else "eligible identity unavailable while another harness is running; none launched"),
                   4: "pool full: due names recorded, none launched"}.get(code, "error")
    init_counts = [f"{market}={len(m['pending_init'])}" for market, m in markets.items() if m["pending_init"]]
    init_groups = sum(len(m["pending_init_groups"]) for m in markets.values())
    if init_groups:
        init_counts.append(f"groups={init_groups}")
    if init_counts:
        if code == 1 and not quiet:
            exit_reason = "no launchable candidates this breath"
        exit_reason += "; pending initialization: " + ", ".join(init_counts)
    out = {"at": now.isoformat(), "exit": code, "exit_reason": exit_reason,
           "ibkr": {"probe_exit": probe_exit},
           "bars": (fresh.stats if fresh is not None else {"mode": "cache (ibkr down or probe skipped)"}),
           "running": units, "prepare_lock_held": lock_held(),
           "capacity": dict(capacity, launch_cap=launch_cap),
           "quiet": quiet,
           "usage": usage, "recommended_harness": rec, "dispatch_jobs": jobs,
           "markets": markets, "total_due": total_due,
           "reset_window": radar, "memory": state.get("memory")}
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    wake = now.strftime('%Y%m%dT%H%M%SZ')
    snap = STATUS_DIR / f"duty_status_{wake}.json"
    snap.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    out["snapshot"] = str(snap)
    if not a.no_record:
        try:
            from datalayer.pg import duty_store
            duty_store.ensure_table()
            duty_store.record("breath", wake, out, harness=rec)
            duty_store.prune_breaths()
        except Exception as exc:
            out["memory"] = f"{out.get('memory')}; breath not recorded ({type(exc).__name__})"
    if a.json_only:
        print(json.dumps(out, ensure_ascii=False, default=str))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
