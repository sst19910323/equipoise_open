"""
恐慌指数(sentiment)日更 —— 跑 3 个爬虫 → 解析最新输出 → upsert PG `sentiment` 表。

确定性爬虫, 不进 update_schedule.yaml。hermes 每交易日直接跑一条(乙案, 2026-07-02 定):
    cd /opt/equipoise && ./venv/bin/python datalayer/pg/sentiment_daily.py

给 hermes 的契约:
    exit 0  → 三区都齐, 完事。
    exit 1  → 有区挂了, stdout 有一行 `FAILED_REGIONS: US,EU` 之类 →
              喊 CC 补: claude -p "/refresh-sentiment" (CC 会 WebSearch 兜底补挂掉的区)。

口径: US = fear_greed(0-100) + vix ; EZ = vstoxx ; JP = nikkei_vi ; KR = vkospi。日频。
历史不强求(拉不到就日更往前累积); 周/月涨跌读侧从历史算。
依赖: cloudscraper / beautifulsoup4 / lxml / pyyaml / psycopg2(都在 ./venv)。
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402  复用 PG 连接 + 表名

DL_ROOT = Path(__file__).resolve().parent.parent     # datalayer/ (爬虫脚本在 datalayer/fetchers/)
ROOT = Path(__file__).resolve().parents[2]            # equipoise 根 (data/ 在这, 爬虫也写到这)
PY = sys.executable

# 爬虫 → 区域 → 该区最新输出文件 pattern (成败判定用)
SCRAPERS = [
    ("fetchers/fear_greed_fetcher.py", "US", "data/fear_greed/fear_greed_latest_*.json"),
    ("fetchers/vstoxx_fetcher.py",     "EU", "data/vstoxx/vstoxx_latest_*.json"),
    ("fetchers/nikkei_vi_fetcher.py",  "JP", "data/nikkei_vi/nikkei_vi_latest_*.json"),
    ("fetchers/vkospi_fetcher.py",     "KR", "data/vkospi/vkospi_latest_*.json"),
]
FRESH_SECONDS = 20 * 3600   # 输出文件比这旧 = 本次没爬出新东西(残留旧文件不算数)


def run_scrapers() -> set:
    """跑 3 个爬虫。返回挂掉的区域集合(进程失败 或 没产出 20h 内的新文件)。"""
    import time as _time
    failed = set()  # 跑 4 个爬虫(US/EU/JP/KR)
    for s, region, pattern in SCRAPERS:
        try:
            r = subprocess.run([PY, str(DL_ROOT / s)], cwd=str(ROOT), timeout=120,
                               capture_output=True)
            proc_ok = (r.returncode == 0)
            print(f"  ran {s} (rc={r.returncode})")
        except Exception as e:
            proc_ok = False
            print(f"  WARN {s}: {type(e).__name__}: {e}")
        f = _newest(pattern)
        fresh_ok = bool(f) and (_time.time() - Path(f).stat().st_mtime) <= FRESH_SECONDS
        if not (proc_ok and fresh_ok):
            failed.add(region)
    return failed


def _newest(pattern: str):
    files = sorted(glob.glob(str(ROOT / pattern)))
    return files[-1] if files else None


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_latest() -> list:
    rows = []
    # US: fear_greed(0-100) + vix
    f = _newest("data/fear_greed/fear_greed_latest_*.json")
    if f:
        d = _load(f); fg = d.get("fear_greed") or {}; vx = d.get("vix") or {}
        dt = fg.get("source_date") or str(d.get("filename_timestamp_basis", ""))[:10]
        if dt and fg.get("value") is not None:
            rows.append(("US", "fear_greed", float(fg["value"]), "0-100", dt))
        if dt and vx.get("value") is not None:
            rows.append(("US", "vix", float(vx["value"]), "index", dt))
    # EU: vstoxx
    f = _newest("data/vstoxx/vstoxx_latest_*.json")
    if f:
        d = _load(f); v = d.get("vstoxx") or {}
        dt = v.get("source_date") or str(d.get("filename_timestamp_basis", ""))[:10]
        if dt and v.get("value") is not None:
            rows.append(("EZ", "vstoxx", float(v["value"]), "index", dt))
    # JP: nikkei_vi
    f = _newest("data/nikkei_vi/nikkei_vi_latest_*.json")
    if f:
        d = _load(f); v = d.get("nikkei_vi") or {}
        dt = str(d.get("filename_timestamp_basis", ""))[:10]
        if dt and v.get("value") is not None:
            rows.append(("JP", "nikkei_vi", float(v["value"]), "index", dt))
    # KR: vkospi
    f = _newest("data/vkospi/vkospi_latest_*.json")
    if f:
        d = _load(f); v = d.get("vkospi") or {}
        dt = str(d.get("filename_timestamp_basis", ""))[:10]
        if dt and v.get("value") is not None:
            rows.append(("KR", "vkospi", float(v["value"]), "index", dt))
    return rows


def upsert(rows: list) -> None:
    if not rows:
        print("  (no sentiment rows parsed)"); return
    sql = (f"INSERT INTO {db.T_SENT} (region,indicator,value,unit,date) "
           f"VALUES (%s,%s,%s,%s,%s) "
           f"ON CONFLICT (region,indicator,date) "
           f"DO UPDATE SET value=EXCLUDED.value, unit=EXCLUDED.unit")
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
    print(f"  upserted {len(rows)} sentiment rows: " +
          ", ".join(f"{r[0]}/{r[1]}={r[2]}@{r[4]}" for r in rows))


def main() -> int:
    print("[sentiment_daily] scraping...")
    failed = run_scrapers()
    print("[sentiment_daily] parsing + upserting...")
    upsert(parse_latest())
    if failed:
        print(f"FAILED_REGIONS: {','.join(sorted(failed))}")
        print("[sentiment_daily] done (partial — 上面区域要 CC 兜底).")
        return 1
    print("[sentiment_daily] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
