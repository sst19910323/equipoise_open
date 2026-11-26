"""
VSTOXX (EURO STOXX 50 Volatility Index, V2TX) Fetcher

EU 版 VIX,对位 fear_greed_fetcher / nikkei_vi_fetcher。
源:STOXX 官方页 stoxx.com/index/v2tx/(实时,9:15-17:30 CET 工作日)。
fallback:tradingeconomics.com/vstoxx:ind(缓慢但常活)。

输出:data/vstoxx/vstoxx_latest_<TS>.json,schema 仿 fear_greed_latest_* / nikkei_vi_latest_*。

VSTOXX 解读参考(欧洲市场,与 Nikkei VI 阈值略不同):
- 平静期 (calm)      <18
- 正常期 (normal)    18-25
- 紧张期 (elevated)  25-32
- 危机期 (stressed)  32-45
- 恐慌期 (crisis)    45+
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml
import cloudscraper

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]   # datalayer/fetchers/<this> → equipoise root
CONFIG_PATH = PROJECT_ROOT / "config" / "data_sources.yaml"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "vstoxx"
FILE_PREFIX = "vstoxx_latest_"
PRIMARY_URL = "https://stoxx.com/index/v2tx/"
FALLBACK_URL = "https://tradingeconomics.com/vstoxx:ind"
BROWSER_ARGS = {"browser": "chrome", "platform": "windows", "desktop": True}
DISSEMINATION_WINDOW_CET = "09:15-17:30"


@dataclass
class VstoxxConfig:
    cache_dir: Path


def load_config(override_path: Optional[Path] = None) -> VstoxxConfig:
    path = override_path or CONFIG_PATH
    user_cfg = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = yaml.safe_load(fp) or {}
                user_cfg = data.get("data_sources", {}).get("vstoxx", {})
        except Exception:
            pass

    raw_path_str = user_cfg.get("cache_dir")
    if raw_path_str:
        p = Path(raw_path_str)
        final_path = p if p.is_absolute() else (PROJECT_ROOT / p)
    else:
        final_path = DEFAULT_CACHE_DIR
    return VstoxxConfig(cache_dir=final_path)


# ─────────────────────────── 解析 ─────────────────────────── #

def _to_float(s: str) -> Optional[float]:
    s = s.strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in ("--", "-", "n/a", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _classify_regime(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 18:
        return "calm"
    if v < 25:
        return "normal"
    if v < 32:
        return "elevated"
    if v < 45:
        return "stressed"
    return "crisis"


_REGIME_LEGEND = "calm:<18 / normal:18-25 / elevated:25-32 / stressed:32-45 / crisis:45+"


def _parse_cet_label_to_utc(label: str, now_utc: datetime) -> datetime:
    """
    解析 stoxx.com 上的时间标签。常见形式:
      "5:30 PM CET" / "17:30 CET" / "Last update: 14:23 CET"
    无法解析时 fallback 到 dissemination 窗口收盘(17:30 CET = 16:30 UTC summer DST 时 15:30 UTC)。
    保守做法:用上一交易日的 17:30 CET 作为 close,即 UTC 15:30 或 16:30(CEST)。
    这里取保守 UTC 15:30(冬令)。
    """
    text = label.strip().lower()
    # try "HH:MM (am|pm)? cet" pattern
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*cet", text)
    if m:
        hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        # CET = UTC+1 (winter), CEST = UTC+2 (summer DST, late Mar - late Oct)
        # 简化:不严格判断 DST,按 CET=UTC+1 算,若用户在夏令时取数会差 1h(可接受 — debate 不依赖秒级)
        utc_minute_total = hh * 60 + mm - 60
        if utc_minute_total < 0:
            utc_minute_total += 24 * 60
            base = now_utc - timedelta(days=1)
        else:
            base = now_utc
        cand = base.replace(
            hour=utc_minute_total // 60,
            minute=utc_minute_total % 60,
            second=0,
            microsecond=0,
        )
        # 若解析时间在未来(典型场景:凌晨 fetch 取的是上一交易日收盘),退一天
        if cand > now_utc:
            cand -= timedelta(days=1)
        return cand
    # fallback:今天的 dissemination 收盘 17:30 CET = 16:30 UTC(夏令时为 15:30)
    return now_utc.replace(hour=16, minute=30, second=0, microsecond=0)


def _fetch_primary() -> Tuple[Optional[Dict], Optional[datetime]]:
    """从 stoxx.com 抓取(具体 class/id 选择器)。"""
    scraper = cloudscraper.create_scraper(browser=BROWSER_ARGS)
    print(f"[vstoxx] fetching primary {PRIMARY_URL}")
    try:
        r = scraper.get(PRIMARY_URL, timeout=25)
    except Exception as e:
        print(f"[vstoxx] primary request failed: {e}")
        return None, None
    if r.status_code != 200:
        print(f"[vstoxx] primary HTTP {r.status_code}")
        return None, None
    if "Just a moment" in r.text:
        print("[vstoxx] primary cloudflare-challenged")
        return None, None

    html = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    html = re.sub(r"<style[\s\S]*?</style>", "", html)

    # STOXX 实际 HTML 用确定的 class/id 选择器(2026-05 抓页验证):
    #   <span id="overview-last-value">21.89</span>                 ← current value
    #   <span class="icon-loss" or "icon-gain"></span>              ← direction
    #   <span class="data-daily-change">-1.45</span>                ← change absolute
    #   <span class="data-daily-change-percent">-6.20</span>        ← change percent
    #   <span class="data-daily-change-timestamp">05:30 pm</span>   ← time CET
    #   <span class="data-week-performance ...">-0.69%</span>       ← week change
    #   <span class="data-year-performance ...">27.23%</span>       ← 52w change
    #   <span class="data-ytd-performance ...">43.71%</span>        ← YTD change
    #   <span class="data-daily-low">21.8889</span>                 ← daily low
    #   <span class="data-daily-high">22.6177</span>                ← daily high
    #   <span class="data-year-low">14.0473</span>                  ← 52w low

    def _pick(selector_pattern: str) -> Optional[str]:
        m = re.search(selector_pattern, html, re.IGNORECASE)
        return m.group(1).strip() if m else None

    value = _to_float(_pick(r'id="overview-last-value"[^>]*>([^<]+)<') or "")
    change_abs = _to_float(_pick(r'class="[^"]*\bdata-daily-change\b[^"]*"[^>]*>([^<]+)<') or "")
    change_pct = _to_float(_pick(r'class="[^"]*\bdata-daily-change-percent\b[^"]*"[^>]*>([^<]+)<') or "")
    source_label_raw = _pick(r'class="[^"]*\bdata-daily-change-timestamp\b[^"]*"[^>]*>([^<]+)<') or ""

    # direction 来自 icon-loss / icon-gain class
    if re.search(r'class="[^"]*\bicon-loss\b', html, re.IGNORECASE):
        direction = "down"
    elif re.search(r'class="[^"]*\bicon-gain\b', html, re.IGNORECASE):
        direction = "up"
    else:
        direction = "flat"

    # 附加性能字段(占用空间小,前端可选展示)
    week_pct = _to_float((_pick(r'class="[^"]*\bdata-week-performance\b[^"]*"[^>]*>([^<]+)<') or "").rstrip("%"))
    year_pct = _to_float((_pick(r'class="[^"]*\bdata-year-performance\b[^"]*"[^>]*>([^<]+)<') or "").rstrip("%"))
    ytd_pct = _to_float((_pick(r'class="[^"]*\bdata-ytd-performance\b[^"]*"[^>]*>([^<]+)<') or "").rstrip("%"))
    daily_low = _to_float(_pick(r'class="[^"]*\bdata-daily-low\b[^"]*"[^>]*>([^<]+)<') or "")
    daily_high = _to_float(_pick(r'class="[^"]*\bdata-daily-high\b[^"]*"[^>]*>([^<]+)<') or "")
    year_low = _to_float(_pick(r'class="[^"]*\bdata-year-low\b[^"]*"[^>]*>([^<]+)<') or "")
    year_high = _to_float(_pick(r'class="[^"]*\bdata-year-high\b[^"]*"[^>]*>([^<]+)<') or "")

    if value is None or not (3 < value < 200):
        print(f"[vstoxx] primary parse failed: value={value} out of range")
        return None, None

    # 时间标签 "05:30 pm" → 附加 " CET"(stoxx.com 旁边渲染了 CET 文字)
    source_label = (source_label_raw + " CET").strip() if source_label_raw else "Unknown CET"

    now_utc = datetime.now(timezone.utc)
    source_dt = _parse_cet_label_to_utc(source_label, now_utc)

    previous_close = round(value - change_abs, 4) if change_abs is not None else None

    vstoxx = {
        "name": "EURO STOXX 50 Volatility (VSTOXX)",
        "value": value,
        "previous_close": previous_close,
        "change_absolute": change_abs,
        "change_percent": change_pct,
        "direction": direction,
        "source_label": source_label,
        "data_timestamp_utc": source_dt.isoformat(),
        "source_date": source_dt.date().isoformat(),
        "dissemination_window_cet": DISSEMINATION_WINDOW_CET,
        "regime_tier": _classify_regime(value),
        "regime_legend": _REGIME_LEGEND,
        "daily_low": daily_low,
        "daily_high": daily_high,
        "week_performance_pct": week_pct,
        "year_performance_pct": year_pct,
        "ytd_performance_pct": ytd_pct,
        "fifty_two_week_low": year_low,
        "fifty_two_week_high": year_high,
    }

    payload = {
        "filename_timestamp_basis": source_dt.isoformat(),
        "fetched_at_utc": now_utc.isoformat(),
        "source": "stoxx.com",
        "source_url": PRIMARY_URL,
        "vstoxx": vstoxx,
    }
    return payload, source_dt


def _fetch_fallback() -> Tuple[Optional[Dict], Optional[datetime]]:
    """从 tradingeconomics.com 抓取(缓慢但常活)。"""
    scraper = cloudscraper.create_scraper(browser=BROWSER_ARGS)
    print(f"[vstoxx] fetching fallback {FALLBACK_URL}")
    try:
        r = scraper.get(FALLBACK_URL, timeout=25)
    except Exception as e:
        print(f"[vstoxx] fallback request failed: {e}")
        return None, None
    if r.status_code != 200:
        print(f"[vstoxx] fallback HTTP {r.status_code}")
        return None, None

    html = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    html = re.sub(r"<style[\s\S]*?</style>", "", html)

    # Trading Economics 用 id="ctl00_ContentPlaceHolder1_ctl02_lblLast" 类的结构
    value = None
    m = re.search(r'id="[^"]*[Ll]ast[^"]*"[^>]*>\s*([\d.]+)\s*<', html)
    if m:
        value = _to_float(m.group(1))
    if value is None:
        # 兜底:首个合理范围浮点
        for cand in re.finditer(r'>\s*(\d{1,3}\.\d{1,2})\s*<', html):
            v = _to_float(cand.group(1))
            if v and 5 < v < 200:
                value = v
                break

    if value is None:
        print("[vstoxx] fallback parse failed: could not locate value")
        return None, None

    now_utc = datetime.now(timezone.utc)
    # Trading Economics 不显式给 timestamp,用 fetch 时间反推上一交易日 close
    source_dt = now_utc.replace(hour=16, minute=30, second=0, microsecond=0)
    if source_dt > now_utc:
        source_dt -= timedelta(days=1)

    vstoxx = {
        "name": "EURO STOXX 50 Volatility (VSTOXX)",
        "value": value,
        "previous_close": None,
        "change_absolute": None,
        "change_percent": None,
        "direction": "unknown",
        "source_label": "TradingEconomics latest",
        "data_timestamp_utc": source_dt.isoformat(),
        "source_date": source_dt.date().isoformat(),
        "dissemination_window_cet": DISSEMINATION_WINDOW_CET,
        "regime_tier": _classify_regime(value),
        "regime_legend": _REGIME_LEGEND,
    }

    payload = {
        "filename_timestamp_basis": source_dt.isoformat(),
        "fetched_at_utc": now_utc.isoformat(),
        "source": "tradingeconomics.com",
        "source_url": FALLBACK_URL,
        "vstoxx": vstoxx,
    }
    return payload, source_dt


def _fetch_and_parse() -> Tuple[Optional[Dict], Optional[datetime]]:
    """先试主源,失败 fallback。"""
    payload, dt = _fetch_primary()
    if payload is not None:
        return payload, dt
    print("[vstoxx] primary failed, trying fallback")
    return _fetch_fallback()


def download_latest(config_path: Optional[Path] = None) -> Optional[Path]:
    print("[vstoxx] download_latest()")
    cfg = load_config(config_path)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    payload, source_dt = _fetch_and_parse()
    if payload is None or source_dt is None:
        print("[vstoxx] failed to fetch from all sources")
        return None
    try:
        ts = source_dt.strftime("%Y%m%dT%H%M%SZ")
        filename = f"{FILE_PREFIX}{ts}.json"
        target = cfg.cache_dir / filename
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[vstoxx] saved: {target}")
        return target
    except Exception as e:
        print(f"[vstoxx] save failed: {e}")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    download_latest()
