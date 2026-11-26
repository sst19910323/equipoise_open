"""
Nikkei Volatility Index (NK225VI) Fetcher

JP 版 VIX,对位 fear_greed_fetcher。
源:Nikkei 官方 Current Values 页(indexes.nikkei.co.jp),Cloudflare 保护,用 cloudscraper 绕。
本次顺手把 Nikkei 225 VI Futures Index 也一起拉(同页 sibling 节点)。

输出:data/nikkei_vi/nikkei_vi_latest_<TS>.json,schema 仿 fear_greed_latest_*。

Nikkei VI 解读参考:
- 平静期      ~18-22
- 正常期      22-28
- 紧张期      28-35
- 危机期      35-60+
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml
import cloudscraper

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]   # datalayer/fetchers/<this> → equipoise root
CONFIG_PATH = PROJECT_ROOT / "config" / "data_sources.yaml"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "nikkei_vi"
FILE_PREFIX = "nikkei_vi_latest_"
SOURCE_URL = "https://indexes.nikkei.co.jp/en/nkave/index?idx=nk225vi"
BROWSER_ARGS = {"browser": "chrome", "platform": "windows", "desktop": True}


@dataclass
class NikkeiVIConfig:
    cache_dir: Path


def load_config(override_path: Optional[Path] = None) -> NikkeiVIConfig:
    path = override_path or CONFIG_PATH
    user_cfg = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = yaml.safe_load(fp) or {}
                user_cfg = data.get("data_sources", {}).get("nikkei_vi", {})
        except Exception:
            pass

    raw_path_str = user_cfg.get("cache_dir")
    if raw_path_str:
        p = Path(raw_path_str)
        final_path = p if p.is_absolute() else (PROJECT_ROOT / p)
    else:
        final_path = DEFAULT_CACHE_DIR
    return NikkeiVIConfig(cache_dir=final_path)


_ITEM_RE = re.compile(
    r'<div class="idx-indexlist-item">\s*'
    r'<div class="name">\s*'
    r'<a[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<name>[^<]+)</a>\s*</a>?\s*'
    r'</div>\s*'
    r'<div class="value">(?P<value>[^<]+)</div>\s*'
    r'<div class="daily-change(?P<dir>[^"]*)">\s*'
    r'<span class="change">(?P<change>[^<]+)</span>\s*'
    r'<span class="range">(?P<range>[^<]+)</span>\s*'
    r'</div>\s*'
    r'<div class="date">(?P<date>[^<]+)</div>',
    re.IGNORECASE | re.DOTALL,
)


def _to_float(s: str) -> Optional[float]:
    s = s.strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in ("--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_source_date(label: str) -> Optional[datetime]:
    """
    'May/14(11:06)' or 'May/13(Close)' -> datetime UTC.
    Close = 15:00 JST (东京收盘) → 06:00 UTC.
    Intraday timestamp = JST → UTC (-9h).
    Year is implicit (Nikkei 不写年),取最近的合理年份。
    """
    m = re.match(r"^([A-Za-z]+)/(\d+)\((.+)\)$", label.strip())
    if not m:
        return None
    mon_str, day_str, ts_str = m.groups()
    month_map = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    mon = month_map.get(mon_str[:3])
    if not mon:
        return None
    day = int(day_str)
    # Determine year: if month is in next 2 months ahead of now, treat as past
    now = datetime.now(timezone.utc)
    year = now.year
    cand = datetime(year, mon, day, tzinfo=timezone.utc)
    # If candidate is more than 30 days in the future, it's actually last year
    if (cand - now).days > 30:
        cand = datetime(year - 1, mon, day, tzinfo=timezone.utc)

    # JST time within day
    if ts_str.lower() == "close":
        # 东京收盘 15:00 JST = 06:00 UTC
        jst_h, jst_m = 15, 0
    else:
        m2 = re.match(r"^(\d{1,2}):(\d{2})$", ts_str)
        if m2:
            jst_h, jst_m = int(m2.group(1)), int(m2.group(2))
        else:
            jst_h, jst_m = 6, 0  # default to close UTC
    # JST = UTC + 9
    utc_h_total = jst_h * 60 + jst_m - 9 * 60
    if utc_h_total < 0:
        utc_h_total += 24 * 60
        cand = cand - __import__("datetime").timedelta(days=1) if False else cand
    cand = cand.replace(hour=utc_h_total // 60, minute=utc_h_total % 60)
    return cand


def _fetch_and_parse() -> Tuple[Optional[Dict], Optional[datetime]]:
    scraper = cloudscraper.create_scraper(browser=BROWSER_ARGS)
    print(f"[nikkei_vi] fetching {SOURCE_URL}")
    try:
        r = scraper.get(SOURCE_URL, timeout=25)
    except Exception as e:
        print(f"[nikkei_vi] request failed: {e}")
        return None, None
    if r.status_code != 200:
        print(f"[nikkei_vi] HTTP {r.status_code}")
        return None, None
    if "Just a moment" in r.text:
        print("[nikkei_vi] still cloudflare-challenged")
        return None, None

    html = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    html = re.sub(r"<style[\s\S]*?</style>", "", html)

    # Find the Volatility Index section
    vol_idx = html.find('<h3 class="idx-indexlist-subtitle">Volatility Index</h3>')
    if vol_idx < 0:
        print("[nikkei_vi] Volatility Index section not found")
        return None, None
    # Restrict search window to the next ~2.5k chars (covers both VI + VI Futures)
    section = html[vol_idx: vol_idx + 3000]

    nikkei_vi = None
    nikkei_vi_futures = None
    for m in _ITEM_RE.finditer(section):
        name = m.group("name").strip()
        item = {
            "name": name,
            "href": m.group("href"),
            "value": _to_float(m.group("value")),
            "change_absolute": _to_float(m.group("change")),
            "change_pct": _to_float(m.group("range")),
            "direction": m.group("dir").strip() or "flat",
            "source_label": m.group("date").strip(),
        }
        if "Volatility Index" in name and "Futures" not in name:
            nikkei_vi = item
        elif "VI Futures" in name:
            nikkei_vi_futures = item

    if nikkei_vi is None:
        print("[nikkei_vi] failed to locate Nikkei VI item in Volatility section")
        return None, None

    source_dt = _parse_source_date(nikkei_vi["source_label"])
    if source_dt is None:
        source_dt = datetime.now(timezone.utc)

    # interpretation tier
    v = nikkei_vi.get("value")
    if v is None:
        tier = "unknown"
    elif v < 22:
        tier = "calm"
    elif v < 28:
        tier = "normal"
    elif v < 35:
        tier = "elevated"
    elif v < 50:
        tier = "stressed"
    else:
        tier = "crisis"
    nikkei_vi["regime_tier"] = tier
    nikkei_vi["regime_legend"] = "calm:<22 / normal:22-28 / elevated:28-35 / stressed:35-50 / crisis:50+"

    payload = {
        "filename_timestamp_basis": source_dt.isoformat(),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "indexes.nikkei.co.jp",
        "source_url": SOURCE_URL,
        "nikkei_vi": nikkei_vi,
        "nikkei_vi_futures": nikkei_vi_futures or "N/A",
    }
    return payload, source_dt


def download_latest(config_path: Optional[Path] = None) -> Optional[Path]:
    print(f"[nikkei_vi] download_latest()")
    cfg = load_config(config_path)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    payload, source_dt = _fetch_and_parse()
    if payload is None or source_dt is None:
        print("[nikkei_vi] failed to fetch")
        return None
    try:
        ts = source_dt.strftime("%Y%m%dT%H%M%SZ")
        filename = f"{FILE_PREFIX}{ts}.json"
        target = cfg.cache_dir / filename
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[nikkei_vi] saved: {target}")
        return target
    except Exception as e:
        print(f"[nikkei_vi] save failed: {e}")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    download_latest()
