#!/usr/bin/env python3
"""Backfill a used evidence_quant transport file into Analysis/Evidence artifacts.

The transport path is lineage only.  The exact UTF-8 file content is copied to
payload_text so the subsequent PG archive is self-contained.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--analysis", required=True, type=Path)
    ap.add_argument("--evidence", required=True, type=Path)
    ap.add_argument("--source-file", required=True, type=Path)
    ap.add_argument("--mode", default="peer_compare")
    args = ap.parse_args()

    symbol = args.symbol.upper()
    analysis_path = args.analysis if args.analysis.is_absolute() else ROOT / args.analysis
    evidence_path = args.evidence if args.evidence.is_absolute() else ROOT / args.evidence
    source_path = args.source_file if args.source_file.is_absolute() else ROOT / args.source_file
    payload_text = source_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    report = json.loads(analysis_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, list) or report.get("evidence") != evidence:
        raise SystemExit("Analysis.evidence and Evidence file are not identical before backfill")

    source_file = _rel(source_path)
    if any(item.get("source_file") == source_file for item in evidence if isinstance(item, dict)):
        print(f"SKIP {symbol}: {source_file} already archived")
        return 0

    request = {"mode": args.mode, "symbol": symbol}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("window_days") is not None:
        request["window_days"] = data["window_days"]
    ok = bool(payload.get("ok")) if isinstance(payload, dict) else False
    item = {
        "task": f"归档并核验 {symbol} 的 {args.mode} 固定模式数据",
        "simple": (f"{symbol} 的 {args.mode} 数据已由角色在本场读取并用于分析；"
                   "完整原文随本 Evidence 项归档。"),
        "detailed": ("【结论】固定模式数据的完整响应已归档，路径仅作来源追溯。\n"
                     f"【原文】见本项 payload_text（{len(payload_text)} UTF-8 characters）。\n"
                     "【来源】equipoise evidence_quant / IBKR-derived prepared data"),
        "source": [f"data:evidence_quant:{args.mode}:{symbol}"],
        "kind": "data",
        "mode": args.mode,
        "status": "verified" if ok else "not_found",
        "request": request,
        "payload_text": payload_text,
        "source_file": source_file,
    }
    evidence.append(item)
    report["evidence"] = evidence
    rendered_evidence = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    rendered_report = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    evidence_path.write_text(rendered_evidence, encoding="utf-8")
    analysis_path.write_text(rendered_report, encoding="utf-8")
    print(f"OK {symbol}: archived {len(payload_text)} chars from {source_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
