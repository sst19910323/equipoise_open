# -*- coding: utf-8 -*-
"""
backfill_marketdata_pg.py — 把散落的技术快照 + 新闻 json 回填进 PG (technical / news 表)。

一次性脚本: 在**服务器**(research.example.invalid, 真数据所在)上跑, 把这几天的:
  - data/technical/<SYMBOL>/<SYMBOL>_technical_<ts>.json  → technical 表 (as_of=market_data_timestamp_utc)
  - data/news/<SYMBOL>/<SYMBOL>_news_<ts>.json            → news 表      (fetched_at=文件名时间戳)
upsert 进 PG。幂等(主键冲突即覆盖), 可重复跑。

用法:
    python backfill_marketdata_pg.py                 # 全量回填
    python backfill_marketdata_pg.py --days 5        # 只回填近 5 天(按文件 mtime)
    python backfill_marketdata_pg.py --tech-dir data/technical --news-dir data/news
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datalayer.pg.marketdata_pg_store import save_technical, save_news

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backfill")

_TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


def _recent(p: Path, cutoff: float) -> bool:
    if cutoff <= 0:
        return True
    try:
        return p.stat().st_mtime >= cutoff
    except Exception:
        return True


def backfill_technical(tech_dir: Path, cutoff: float) -> tuple[int, int]:
    ok = fail = 0
    files = sorted(tech_dir.glob("*/*_technical_*.json"))
    log.info(f"[technical] 扫描到 {len(files)} 个文件")
    for f in files:
        if not _recent(f, cutoff):
            continue
        symbol = f.parent.name.upper()
        try:
            payload = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            log.warning(f"[technical] 读 {f.name} 失败: {e}")
            fail += 1
            continue
        if save_technical(payload, symbol=symbol):
            ok += 1
        else:
            fail += 1
    return ok, fail


def backfill_news(news_dir: Path, cutoff: float) -> tuple[int, int]:
    ok = fail = 0
    files = sorted(news_dir.glob("*/*_news_*.json"))
    log.info(f"[news] 扫描到 {len(files)} 个文件")
    for f in files:
        if not _recent(f, cutoff):
            continue
        symbol = f.parent.name.upper()
        m = _TS_RE.search(f.name)
        fetched_at = m.group(1) if m else None
        try:
            items = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            log.warning(f"[news] 读 {f.name} 失败: {e}")
            fail += 1
            continue
        if not isinstance(items, list) or not items:
            continue
        if save_news(symbol, items, fetched_at):
            ok += 1
        else:
            fail += 1
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tech-dir", default="data/technical")
    ap.add_argument("--news-dir", default="data/news")
    ap.add_argument("--days", type=float, default=0, help="只回填近 N 天(按文件 mtime); 0=全量")
    args = ap.parse_args()

    cutoff = (time.time() - args.days * 86400) if args.days > 0 else 0
    tech_dir = (PROJECT_ROOT / args.tech_dir).resolve()
    news_dir = (PROJECT_ROOT / args.news_dir).resolve()

    log.info(f"回填范围: {'近 %.0f 天' % args.days if args.days > 0 else '全量'}")
    t_ok = t_fail = n_ok = n_fail = 0
    if tech_dir.exists():
        t_ok, t_fail = backfill_technical(tech_dir, cutoff)
    else:
        log.warning(f"technical 目录不存在: {tech_dir}")
    if news_dir.exists():
        n_ok, n_fail = backfill_news(news_dir, cutoff)
    else:
        log.warning(f"news 目录不存在: {news_dir}")

    log.info(f"✅ 完成。technical: {t_ok} 成功 / {t_fail} 失败; news: {n_ok} 成功 / {n_fail} 失败")


if __name__ == "__main__":
    main()
