#!/usr/bin/env python3
"""rebuild_technical.py — 用沙箱里冻结的四段 K 线, 以当前生成器重建本场要用的技术快照(只用 as_of 之前的 bar)。

    ./venv/bin/python scripts/drill/rebuild_technical.py --root <沙箱根> --as-of <TS>

为什么: 演习用当前语料 + 代码打历史节点, 原场留下的技术快照是当时的旧格式(如 10-09 的 V15 键
`<S>_daily_technicals_atr_pct_14_daily`), 当前 open 读的是 V16 键, 对不上。做法同 scripts/snapshot_v16_offline.py 的 --as-of:
四段(1m / 5m / 1h / 1d)各取沙箱里那份, 截到 bar 起点 < as_of, `snapshot_v16.build_payload` 后写辩手视图(与 hydrate 同形),
文件名不变; 旧文件挪到 drill/old_technical/ 备查。只重建 build_manifest 指到的技术快照(本标的 / 基准 / 板块 / 镜头)。
在沙箱根里跑(导入的是沙箱里的代码拷贝)。打印一行 JSON: rebuilt / skipped(带原因)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SEGS = (("1m", "2d_1m"), ("5m", "7d_5m"), ("1h", "2m_1h"), ("1d", "drill_1d"))


def _flatten(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _flatten(x)
    elif isinstance(v, list):
        for x in v:
            yield from _flatten(x)


def technical_paths(root: Path, symbol: str) -> list[str]:
    from workflows.data_manifest import build_manifest
    m = build_manifest(symbol, root)
    return sorted({p for p in _flatten(m.get("data_files")) if p.startswith("data/technical/")})


def _bar_file(root: Path, sym: str, seg: str) -> Path | None:
    fs = sorted((root / "data" / "market_data" / sym).glob(f"{sym}_{seg}_*.csv"))
    return fs[-1] if fs else None


def rebuild_one(root: Path, rel: str, as_of: str, backup_dir: Path | None = None) -> tuple[bool, str]:
    import pandas as pd
    from analysis import exchange_clock as ec
    from analysis import snapshot_v16 as sv
    from analysis import technical_snapshot_builder as tsb
    path = root / rel
    sym = path.parent.name
    a = pd.Timestamp(as_of[:8] + "T" + as_of[9:15], tz="UTC")
    files = {k: _bar_file(root, sym, seg) for k, seg in SEGS}
    if any(v is None for v in files.values()):
        return False, f"missing bar files {[k for k, v in files.items() if v is None]}"
    frames = {k: tsb.load_ohlcv(v) for k, v in files.items()}
    frames = {k: (df[df.index < a] if df is not None else None) for k, df in frames.items()}
    if any(df is None or len(df) == 0 for df in frames.values()):
        return False, f"no bars before as_of in {[k for k, df in frames.items() if df is None or len(df) == 0]}"
    bench = tsb.resolve_benchmark_symbol(sym)
    bench_df = None
    if bench:
        bf = _bar_file(root, bench, "drill_1d")
        bench_df = tsb.load_ohlcv(bf) if bf else None
        if bench_df is not None:
            bench_df = bench_df[bench_df.index < a]
    clock = ec.resolve_exchange_clock(sym)
    p = sv.build_payload(sym, frames, clock, as_of, benchmark_symbol=bench, bench_frames=bench_df,
                         instrument=tsb.load_instrument_metadata(sym, clock=clock), generated_at=a)
    if not p:
        return False, "build_payload returned nothing"
    keep = (backup_dir or root / "drill" / "old_technical") / rel
    keep.parent.mkdir(parents=True, exist_ok=True)
    if not keep.exists():
        shutil.copy2(path, keep)
    path.write_text(json.dumps(sv.debater_view(p), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True, f"{files['1m'].name} {files['5m'].name} {files['1h'].name} {files['1d'].name}"


def rebuild(root: Path, symbol: str, as_of: str, backup_dir: Path | None = None) -> dict:
    out = {"rebuilt": {}, "skipped": {}}
    paths = set(technical_paths(root, symbol))
    if backup_dir is not None:
        if backup_dir.resolve().is_relative_to(root.resolve()):
            raise ValueError("strict snapshot backups must be outside the drill root")
        # Peers can share the opening timestamp without appearing in the primary manifest.
        paths.update(p.relative_to(root).as_posix() for p in
                     (root / "data" / "technical").glob(f"*/*_{as_of}.json"))
    for rel in sorted(paths):
        try:
            ok, why = rebuild_one(root, rel, as_of, backup_dir)
        except Exception as e:  # noqa: BLE001 — 一个标的重建失败不拦整包, 但要报出来
            ok, why = False, f"{type(e).__name__}: {e}"
        out["rebuilt" if ok else "skipped"][rel] = why
    out["ok"] = not out["skipped"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--strict-backup-dir", type=Path,
                    help="Back up original snapshots outside a strict pack built from closed bars")
    a = ap.parse_args(argv)
    r = rebuild(Path(a.root), a.symbol, a.as_of, a.strict_backup_dir)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
