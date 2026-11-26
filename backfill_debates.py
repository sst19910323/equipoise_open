#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_debates.py — 把已有报告 JSON 回填进 PG equipoise.debates 表 (让它别空着)。

用法:
    python backfill_debates.py [天数]      # 默认 3 天内的报告; 传 0 = 全部

读 data/debate/{SYMBOL}/{SYMBOL}_Analysis_*.json, 逐份 save_debate 进库。
幂等(upsert): 重复跑不会灌重。
"""
import sys
import json
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from datalayer.pg.debate_pg_store import save_debate


def main():
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    debate_dir = PROJECT_ROOT / "data" / "debate"
    if not debate_dir.exists():
        print(f"目录不存在: {debate_dir}")
        return
    cutoff = 0 if days <= 0 else time.time() - days * 86400

    ok = skip = fail = 0
    for sym_dir in sorted(debate_dir.iterdir()):
        if not sym_dir.is_dir():
            continue
        symbol = sym_dir.name
        for f in sorted(sym_dir.glob(f"{symbol}_Analysis_*.json")):
            if f.stat().st_mtime < cutoff:
                skip += 1
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  ✗ 读取失败 {f.name}: {e}")
                fail += 1
                continue
            if save_debate(symbol, data):
                ok += 1
            else:
                fail += 1

    print(f"\n回填完成: 写入 {ok}, 跳过(超{days}天) {skip}, 失败 {fail}")


if __name__ == "__main__":
    main()
