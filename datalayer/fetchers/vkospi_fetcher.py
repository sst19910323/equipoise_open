"""
VKOSPI (V-KOSPI 200, 코스피200 변동성지수) Fetcher

KR 版 VIX,对位 nikkei_vi_fetcher / vstoxx_fetcher / fear_greed_fetcher。
KRX 官方基于 KOSPI200 期权的隐含波动率指数(韩国的恐慌指数)。
源:Investing.com quote 页(cloudscraper 绕 CDN;与 nikkei_vi 同款抓法),
    值取自 data-test="instrument-price-last" 等确定性属性。

输出:data/vkospi/vkospi_latest_<TS>.json,schema 仿 nikkei_vi_latest_*(顶层键 vkospi)。

VKOSPI 解读参考(隐含波动率, 历史区间比 VIX 略高):
- 平静期   <20
- 正常期   20-30
- 紧张期   30-45
- 承压期   45-60
- 危机期   60+
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml
import cloudscraper

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]   # datalayer/fetchers/<this> → equipoise root
CONFIG_PATH = PROJECT_ROOT / "config" / "data_sources.yaml"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "vkospi"
FILE_PREFIX = "vkospi_latest_"
SOURCE_URL = "https://www.investing.com/indices/kospi-volatility"
BROWSER_ARGS = {"browser": "chrome", "platform": "windows", "desktop": True}
KST = timezone(timedelta(hours=9))   # Asia/Seoul, 无夏令时


@dataclass
class VKospiConfig:
    cache_dir: Path


def load_config(override_path: Optional[Path] = None) -> VKospiConfig:
    path = override_path or CONFIG_PATH
    user_cfg = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = yaml.safe_load(fp) or {}
                user_cfg = data.get("data_sources", {}).get("vkospi", {})
        except Exception:
            pass

    raw_path_str = user_cfg.get("cache_dir")
    if raw_path_str:
        p = Path(raw_path_str)
        final_path = p if p.is_absolute() else (PROJECT_ROOT / p)
    else:
        final_path = DEFAULT_CACHE_DIR
    return VKospiConfig(cache_dir=final_path)


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("%", "").replace("+", "").replace("(", "").replace(")", "")
    if not s or s in ("--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _search(pat: str, html: str) -> Optional[str]:
    m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def _fetch_and_parse() -> Tuple[Optional[Dict], Optional[datetime]]:
    scraper = cloudscraper.create_scraper(browser=BROWSER_ARGS)
    print(f"[vkospi] fetching {SOURCE_URL}")
    try:
        r = scraper.get(SOURCE_URL, timeout=25)
    except Exception as e:
        print(f"[vkospi] request failed: {e}")
        return None, None
    if r.status_code != 200:
        print(f"[vkospi] HTTP {r.status_code}")
        return None, None
    if "Just a moment" in r.text:
        print("[vkospi] still CDN-challenged")
        return None, None

    t = r.text
    value = _to_float(_search(r'data-test="instrument-price-last"[^>]*>([^<]+)<', t))
    if value is None:
        print("[vkospi] failed to locate instrument-price-last")
        return None, None
    change_abs = _to_float(_search(r'data-test="instrument-price-change"[^>]*>([^<]+)<', t))
    change_pct = _to_float(_search(r'data-test="instrument-price-change-percent"[^>]*>([^<]+)<', t))
    status_time = _search(r'data-test="trading-time-label"[^>]*>([^<]+)<', t)

    # 无独立日期标签 → 用 KST 当日日期打戳 (sentiment_daily 只在交易日跑; PG date 列做 point-in-time)。
    now_kst = datetime.now(timezone.utc).astimezone(KST)
    source_dt_kst = now_kst.replace(hour=15, minute=30, second=0, microsecond=0)  # KRX 收盘 15:30 KST
    source_dt = source_dt_kst.astimezone(timezone.utc)

    direction = "flat"
    if change_abs is not None:
        direction = "up" if change_abs > 0 else ("down" if change_abs < 0 else "flat")

    if value < 20:
        tier = "calm"
    elif value < 30:
        tier = "normal"
    elif value < 45:
        tier = "elevated"
    elif value < 60:
        tier = "stressed"
    else:
        tier = "crisis"

    vkospi = {
        "name": "KOSPI 200 Volatility Index (VKOSPI)",
        "value": value,
        "change_absolute": change_abs,
        "change_pct": change_pct,
        "direction": direction,
        "source_status": status_time or "N/A",
        "regime_tier": tier,
        "regime_legend": "calm:<20 / normal:20-30 / elevated:30-45 / stressed:45-60 / crisis:60+",
    }

    payload = {
        "filename_timestamp_basis": source_dt.isoformat(),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "investing.com",
        "source_url": SOURCE_URL,
        "vkospi": vkospi,
    }
    return payload, source_dt


def download_latest(config_path: Optional[Path] = None) -> Optional[Path]:
    print("[vkospi] download_latest()")
    cfg = load_config(config_path)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    payload, source_dt = _fetch_and_parse()
    if payload is None or source_dt is None:
        print("[vkospi] failed to fetch")
        return None
    try:
        ts = source_dt.strftime("%Y%m%dT%H%M%SZ")
        filename = f"{FILE_PREFIX}{ts}.json"
        target = cfg.cache_dir / filename
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[vkospi] saved: {target}  (value={payload['vkospi']['value']} tier={payload['vkospi']['regime_tier']})")
        return target
    except Exception as e:
        print(f"[vkospi] save failed: {e}")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    download_latest()
