#!/usr/bin/env python3
"""build_pack.py — 演习(PTR)冻结包: 一个历史节点一个沙箱根目录, 只装那一刻已知的东西。

    ./venv/bin/python scripts/drill/build_pack.py --symbol GENERAL --as-of 20261009T133700Z --run a

沙箱根 <dest>/<SYM>_<AS_OF>_<run>/:
  - 代码是**拷贝**(run_step / data_manifest 用 Path(__file__).resolve() 定 ROOT, 软链会把对象仓写回生产); venv 软链回 src。
  - data/: 原场 _meta.json.data_files 原样; 其余数据目录每个文件族(文件名去掉时刻戳)取不晚于 as_of 的最新一份
    (技术快照按 as_of 整点, 其它留 PREP_SLACK_MIN 备餐余量; 原场已用到的族只要原场那份); 日线每标的一份, 截到 as_of 前一天;
    盘中文件不晚于 as_of; store / debate / evidence 为空。
  - drill/pack.json: 清单与 sha256; drill/archive.jsonl: oracle 用的当时材料(PG: 近 N 天同区域辩论里的证据原文 + 新闻, 都不晚于 as_of)。
结果封存在 <dest>/_sealed/<同名>/outcome.json(沙箱根之外, 只给校准方): as_of 起的日线 + 原场的 postcheck 行。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

STAMP = re.compile(r"(\d{8}T\d{6}Z)")
PREP_SLACK_MIN = 15          # 备餐文件比开场时刻晚几分钟落盘(与 evidence_quant.PREP_SLACK_MIN 同值)
DATA_DIRS = ("economic", "evidence_prep", "fear_greed", "fundamentals", "market_data", "news",
             "nikkei_vi", "technical", "vkospi", "vstoxx")
EXACT_DIRS = ("technical",)  # 技术快照的时刻戳 = 行情时刻: 晚一分钟也是别的场的, 不给余量
CODE_EXCLUDE = {"data", "venv", ".venv", "logs", "pids", "results", ".git", "council", "docs", "reports",
                "node_modules", ".aws", ".ssh", ".pytest_cache", ".local", "drill_sealed", "tmp", ".tmp", "output"}
EMPTY_DATA = ("store", "debate", "evidence", "i18n_staging", "status")
REGION_GENERAL = {"us": "GENERAL", "eu": "GENERAL_EU", "jp": "GENERAL_JP", "kr": "GENERAL_KR"}
ARCHIVE_DAYS = 14
OUTCOME_BARS = 30


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def stamp_of(name: str) -> datetime | None:
    hits = STAMP.findall(name)
    return parse_ts(hits[-1]) if hits else None


def family_of(rel: str) -> str:
    head, _, tail = rel.rpartition("/")
    hits = list(STAMP.finditer(tail))
    if not hits:
        return rel
    m = hits[-1]
    return f"{head}/{tail[:m.start()]}*{tail[m.end():]}" if head else f"{tail[:m.start()]}*{tail[m.end():]}"


def flatten_paths(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [p for v in node.values() for p in flatten_paths(v)]
    if isinstance(node, list):
        return [p for v in node for p in flatten_paths(v)]
    return []


def _rel(p: Path, base: Path) -> str:
    return str(p.relative_to(base)).replace(os.sep, "/")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_code(src: Path, root: Path, strict: bool = False) -> dict:
    def ignore(d, names):
        top = Path(d).resolve() == src.resolve()
        skip = {n for n in names if n == "__pycache__" or n.endswith(".pyc")
                or (top and (n.strip() in CODE_EXCLUDE or n.startswith((".env.", ".tmp-")) or n == ".env"))}
        if strict:
            rel = Path(d).relative_to(src)
            if top:
                roots = {"analysis", "config", "datalayer", "orchestration", "prompts", "scripts", "skills",
                         "workflows", ".claude", ".codex", "AGENTS.md", "CLAUDE.md"}
                skip.update(n for n in names if n not in roots and not (n.endswith(".py") and not n.startswith("_")))
            if rel == Path(".claude"):
                skip.update(set(names) - {"agents"})
            if rel == Path(".codex"):
                skip.update(set(names) - {"run.md"})
            skip.update(n for n in names if (Path(d) / n).is_symlink())
        return skip
    shutil.copytree(src, root, ignore=ignore, symlinks=False)
    venv = "linked"
    if (src / "venv").exists():
        try:
            os.symlink(src / "venv", root / "venv", target_is_directory=True)
        except OSError:          # Windows 开发机没有软链权限: 只影响本机单测, 服务器上必须是 linked
            venv = "missing"
    else:
        venv = "absent in src"
    return {"venv": venv}


def select_data(src: Path, as_of: datetime, exact: list[str], strict: bool = False) -> list[str]:
    """返回要拷进沙箱的 data/ 相对路径(日线另算)。"""
    chosen = {p for p in exact if not strict or (stamp_of(Path(p).name) and stamp_of(Path(p).name) <= as_of
              and p.split("/")[1] in DATA_DIRS and ".." not in Path(p).parts and "_1d_" not in Path(p).name)}
    covered = {family_of(p) for p in chosen}
    slack = as_of if strict else as_of + timedelta(minutes=PREP_SLACK_MIN)
    best: dict[str, tuple[datetime, str]] = {}
    for d in DATA_DIRS:
        base = src / "data" / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = _rel(p, src)
            if d == "market_data" and "_1d_" in p.name:
                continue
            ts = stamp_of(p.name)
            if ts is None:
                continue
            limit = as_of if (d in EXACT_DIRS or d == "market_data") else slack
            fam = family_of(rel)
            if ts > limit or fam in covered:
                continue
            if fam not in best or ts > best[fam][0]:
                best[fam] = (ts, rel)
    chosen.update(rel for _, rel in best.values())
    return sorted(chosen)


def _read_daily(p: Path) -> tuple[list[str], list[dict]]:
    with open(p, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def copy_closed_intraday(src: Path, dst: Path, as_of: datetime) -> int:
    """A CSV is tagged with its last bar's start. A row becomes usable at its end."""
    match = re.search(r"_(\d+)([mh])_\d{8}T\d{6}Z\.csv$", src.name)
    if not match or int(match[1]) < 1:
        raise ValueError(f"unknown intraday interval: {src.name}")
    duration = timedelta(minutes=int(match[1]) * (60 if match[2] == "h" else 1))
    with src.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        names = {c.lower(): c for c in columns}
        date_key = names.get("datetime") or names.get("date")
        if not date_key:
            raise ValueError(f"missing bar timestamp: {src.name}")
        kept = []
        for row in reader:
            start = datetime.fromisoformat(row[date_key].replace("Z", "+00:00"))
            if start.tzinfo is None:
                raise ValueError(f"ambiguous bar timezone: {src.name} line {reader.line_num}")
            if start + duration <= as_of:
                kept.append(row)
    if kept:
        with dst.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(kept)
    return len(kept)


def _row_date(row: dict) -> str:
    k = {c.lower(): c for c in row}
    return str(row.get(k.get("date", "date"), "")).strip()[:10]


def _latest_daily(sym_dir: Path) -> Path | None:
    fs = list(sym_dir.glob(f"{sym_dir.name}_*_1d_*.csv"))
    return max(fs, key=os.path.getmtime) if fs else None


def truncate_daily(src: Path, root: Path, as_of: datetime, strict: bool = False) -> list[str]:
    cut, out = as_of.date().isoformat(), []
    base = src / "data" / "market_data"
    if not base.exists():
        return out
    for sym_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if strict:
            vintages = [(stamp_of(f.name), f) for f in sym_dir.glob(f"{sym_dir.name}_*_1d_*.csv")]
            p = max((item for item in vintages if item[0] and item[0] <= as_of), default=(None, None))[1]
        else:
            p = _latest_daily(sym_dir)
        if p is None:
            continue
        cols, rows = _read_daily(p)
        keep = [r for r in rows if _row_date(r) and _row_date(r) < cut]
        if not keep:
            continue
        dst = root / "data" / "market_data" / sym_dir.name / f"{sym_dir.name}_drill_1d_{as_of:%Y%m%dT%H%M%SZ}.csv"
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(keep)
        out.append(_rel(dst, root))
    return out


def forward_bars(src: Path, symbols: list[str], as_of: datetime, n: int = OUTCOME_BARS) -> dict:
    cut, out = as_of.date().isoformat(), {}
    for s in symbols:
        p = _latest_daily(src / "data" / "market_data" / s)
        if p is None:
            continue
        _, rows = _read_daily(p)
        fwd = [r for r in rows if _row_date(r) >= cut][:n]
        out[s] = [{"date": _row_date(r), **{k.lower(): r[k] for k in r if k.lower() != "date"}} for r in fwd]
    return out


# ---------------- PG(只读, 只在构建时; 沙箱里没有 PG) ----------------

def _psql(sql: str) -> list[dict]:
    cmd = ["sudo", "-u", "postgres", "psql", "-d", "equipoise", "-At", "-c", sql]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def news_items(rows: list[dict], as_of: datetime) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        for it in row.get("items") or []:
            try:
                pub = datetime.fromtimestamp(int(it.get("datetime")), tz=timezone.utc)
            except (TypeError, ValueError):
                continue
            key = it.get("id") or it.get("headline")
            if pub >= as_of or key in seen:
                continue
            seen.add(key)
            out.append({"source": "pg:news", "symbol": row.get("symbol"), "published_at": pub.isoformat(),
                        "title": it.get("headline"), "text": it.get("summary"), "url": it.get("url"),
                        "publisher": it.get("source")})
    return sorted(out, key=lambda x: x["published_at"])


def evidence_items(rows: list[dict], symbol: str, as_of: datetime) -> list[dict]:
    out = []
    for row in rows:
        e = row.get("ev") or {}
        same = row.get("symbol") == symbol and parse_iso(row.get("as_of")) == as_of
        text = e.get("payload_text") or e.get("detailed") or e.get("finding") or e.get("simple")
        if not text:
            continue
        out.append({"source": "pg:debates.evidence", "session": f"{row.get('symbol')}@{row.get('as_of')}",
                    "published_at": row.get("as_of"),
                    "origin": "same_session (collected during the session, up to about an hour after its as_of)"
                    if same else "earlier_session",
                    "kind": e.get("kind"), "question": e.get("question") or e.get("request"),
                    "status": e.get("status"), "verified_scope": e.get("verified_scope"), "text": text})
    return out


def parse_iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)).astimezone(timezone.utc)
    except ValueError:
        return None


def region_symbols(cfg: dict, symbol: str) -> tuple[str, list[str]]:
    sc = (cfg or {}).get("symbol_contracts") or {}
    region = str((sc.get(symbol) or {}).get("region") or "us").lower()
    members = sorted(s for s, v in sc.items() if str((v or {}).get("region") or "us").lower() == region)
    return region, members


def pg_archive(symbol: str, as_of: datetime, members: list[str], region: str, days: int = ARCHIVE_DAYS) -> list[dict]:
    lo, hi = (as_of - timedelta(days=days)).isoformat(), as_of.isoformat()
    arr = "array[" + ",".join("'" + s.replace("'", "''") + "'" for s in members) + "]::text[]"
    ev = _psql("select row_to_json(t) from (select d.symbol, d.as_of, e as ev from debates d, "
               "jsonb_array_elements(d.report->'evidence') e "
               f"where d.as_of <= '{hi}' and d.as_of > '{lo}' and d.symbol = any({arr})) t")
    news_syms = sorted({symbol, REGION_GENERAL.get(region, "GENERAL")})
    narr = "array[" + ",".join(f"'{s}'" for s in news_syms) + "]::text[]"
    slack = (as_of + timedelta(minutes=PREP_SLACK_MIN)).isoformat()
    nw = _psql("select row_to_json(t) from (select symbol, fetched_at, items from news "
               f"where fetched_at <= '{slack}' and fetched_at > '{lo}' and symbol = any({narr})) t")
    return evidence_items(ev, symbol, as_of) + news_items(nw, as_of)


def pg_postcheck(symbol: str, as_of: datetime) -> list[dict]:
    return _psql("select row_to_json(p) from postcheck p "
                 f"where symbol = '{symbol}' and as_of = '{as_of.isoformat()}' order by dt, checked_at")


# ---------------- 自检: 沙箱里的 data_manifest 必须认出这个节点 ----------------

def self_check(root: Path, symbol: str) -> dict:
    py = root / "venv" / "bin" / "python"
    code = ("import json,sys; sys.path.insert(0,'.'); from workflows.data_manifest import build_manifest; "
            f"m=build_manifest({symbol!r}); print(json.dumps({{'ok':m['ok'],'ts':m.get('ts'),'missing':m.get('missing')}}))")
    p = subprocess.run([str(py) if py.exists() else sys.executable, "-c", code], cwd=root,
                       capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"ok": False, "error": (p.stderr or p.stdout)[-2000:]}


def localize_paths(src: Path, root: Path) -> list[str]:
    """拷贝里的 markdown 写死了生产路径(AGENTS.md / CLAUDE.md 的 `cd /opt/equipoise` 等): 换成沙箱根, 免得 sol 照着去读生产目录
    (11-07 首场: 仲裁者 cat 了 /opt/equipoise/AGENTS.md 与 docs/handoff)。只动 .md; 代码里的常量不动(演习不经那些入口)。"""
    pat = re.compile(re.escape(str(src)) + r"(?![\w-])")
    changed = []
    for p in root.rglob("*.md"):
        if "data" in p.relative_to(root).parts[:1] or p.is_symlink():
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = pat.sub(lambda _m: str(root), t)
        if n != t:
            p.write_text(n, encoding="utf-8")
            changed.append(_rel(p, root))
    return changed


def rebuild_technical(root: Path, symbol: str, as_of: str, sealed: Path | None = None) -> dict:
    """原场的技术快照是当时的旧格式: 用沙箱里冻结的四段 K 线、当前生成器重建(scripts/drill/rebuild_technical.py, 在沙箱里跑)。"""
    py = root / "venv" / "bin" / "python"
    p = subprocess.run([str(py) if py.exists() else sys.executable, "scripts/drill/rebuild_technical.py", "--root", str(root),
                        "--symbol", symbol, "--as-of", as_of,
                        *(["--strict-backup-dir", str(sealed / "old_technical")] if sealed else [])],
                       cwd=root, capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"ok": False, "error": (p.stderr or p.stdout)[-2000:]}


class OverlayError(Exception):
    pass


def _sha_lf(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r", b"")).hexdigest()


def apply_overlays(root: Path, names: list[str]) -> list[dict]:
    """PTR 补丁: scripts/drill/overlay/<name>/ = overlay.json(被替换文件的基线 sha256, 去掉 CR 后算; null = 补丁新增的文件)+ files/<rel> 替换件。
    沙箱拷贝里的文件与基线不符 = 生产已改过这份文件, 补丁过期 → 拒绝构建(要按生产新版重做补丁), 不硬套。"""
    done = []
    for name in names:
        od = root / "scripts" / "drill" / "overlay" / name
        man_p = od / "overlay.json"
        if not man_p.exists():
            raise OverlayError(f"overlay {name}: {man_p} missing")
        man = json.loads(man_p.read_text(encoding="utf-8"))
        for rel, base in man["base_sha256_lf"].items():
            tgt, rep = root / rel, od / "files" / rel
            if not rep.exists():
                raise OverlayError(f"overlay {name}: replacement {rel} missing")
            if base is None:  # a new file the overlay adds: production must not have it yet
                if tgt.exists():
                    raise OverlayError(f"overlay {name}: {rel} is new in the overlay but production now has it; "
                                       "rebuild the overlay on the current file")
                continue
            if not tgt.exists() or _sha_lf(tgt) != base:
                raise OverlayError(f"overlay {name}: {rel} differs from the overlay's base — production changed it; "
                                   "rebuild the overlay on the current file")
        for rel in man["base_sha256_lf"]:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(od / "files" / rel, root / rel)
        done.append({"name": name, "files": sorted(man["base_sha256_lf"]), "description": man.get("description", "")})
    return done


def build_pack(symbol: str, as_of: str, run: str, src: Path = Path("/opt/equipoise"),
               dest_base: Path = Path("/opt/equipoise_drill"), pg: bool = True, check: bool = True,
               archive_days: int = ARCHIVE_DAYS, overlays: list[str] | None = None, strict: bool = False) -> Path:
    src, dest_base = Path(src), Path(dest_base)
    a = parse_ts(as_of)
    origin = src / "data" / "store" / symbol / as_of
    meta_p = origin / "_meta.json"
    if not meta_p.exists():
        raise FileNotFoundError(f"no original session at {origin} (drill v0 only freezes nodes that had a real session)")
    name = f"{symbol}_{as_of}_{run}"
    root, sealed = dest_base / name, dest_base / "_sealed" / name
    if root.exists() or sealed.exists():
        raise FileExistsError(f"{root} already exists: pick another --run")
    dest_base.mkdir(parents=True, exist_ok=True)
    code = copy_code(src, root, strict)
    try:
        applied = apply_overlays(root, overlays or [])
    except OverlayError:
        shutil.rmtree(root, ignore_errors=True)
        raise

    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    exact = [p for p in flatten_paths(meta.get("data_files")) if p.startswith("data/")]
    missing = [p for p in exact if not (src / p).exists()]
    files = {}
    for rel in select_data(src, a, exact, strict):
        if not (src / rel).exists():
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if strict and rel.startswith("data/market_data/") and dst.suffix.lower() == ".csv":
            copy_closed_intraday(src / rel, dst, a)
        else:
            shutil.copy2(src / rel, dst)
    files_daily = truncate_daily(src, root, a, strict)
    for d in EMPTY_DATA:
        (root / "data" / d).mkdir(parents=True, exist_ok=True)
    localized = localize_paths(src, root)
    sealed.mkdir(parents=True, mode=0o700)
    tech = rebuild_technical(root, symbol, as_of, sealed if strict else None) if check else {"ok": True, "skipped": "check off"}
    for p in sorted((root / "data").rglob("*")):
        if p.is_file():
            files[_rel(p, root)] = _sha(p)

    cfg = yaml.safe_load((src / "config" / "symbols.yaml").read_text(encoding="utf-8")) or {}
    region, members = region_symbols(cfg, symbol)
    lenses = [l["symbol"] for l in ((cfg.get("symbol_contracts") or {}).get(symbol) or {}).get("lenses") or []]

    drill = root / "drill"
    drill.mkdir(exist_ok=True)
    archive = pg_archive(symbol, a, members, region, archive_days) if pg else []
    if strict:
        # A debate's opening timestamp does not date the evidence collected during it.
        archive = [item for item in archive if item["source"] == "pg:news"]
    with open(drill / "archive.jsonl", "w", encoding="utf-8") as f:
        for item in archive:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    os.chmod(sealed, 0o700)
    outcome = {"symbol": symbol, "as_of": as_of, "bars": forward_bars(src, [symbol] + lenses, a),
               "postcheck": pg_postcheck(symbol, a) if pg else [],
               "origin_report": f"data/debate/{symbol}/{symbol}_Analysis_{as_of}.json"}
    (sealed / "outcome.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=1), encoding="utf-8")

    pack = {"symbol": symbol, "as_of": as_of, "run": run, "region": region,
            "origin_store": str(origin), "sealed": str(sealed), "src": str(src),
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code": code, "data_files_missing_in_src": missing, "daily_files": len(files_daily),
            "archive_items": len(archive), "archive_days": archive_days, "strict_as_of": strict, "localized_md": localized,
            "technical_rebuild": tech, "overlays": applied, "files": files}
    if check:
        pack["self_check"] = self_check(root, symbol)
    (drill / "pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--as-of", dest="as_of", required=True, help="the original session's TS, e.g. 20261009T133700Z")
    ap.add_argument("--run", required=True, help="run label, e.g. a")
    ap.add_argument("--src", default="/opt/equipoise")
    ap.add_argument("--dest", default="/opt/equipoise_drill")
    ap.add_argument("--archive-days", dest="archive_days", type=int, default=ARCHIVE_DAYS)
    ap.add_argument("--no-pg", dest="pg", action="store_false")
    ap.add_argument("--strict-as-of", action="store_true", help="Use pre-cutoff file vintages and publication-dated news archive")
    ap.add_argument("--overlay", default="", help="comma-separated PTR overlays under scripts/drill/overlay/")
    args = ap.parse_args(argv)
    root = build_pack(args.symbol.upper(), args.as_of, args.run, Path(args.src), Path(args.dest), args.pg,
                      archive_days=args.archive_days, overlays=[o for o in args.overlay.split(",") if o], strict=args.strict_as_of)
    pack = json.loads((root / "drill" / "pack.json").read_text(encoding="utf-8"))
    chk = pack.get("self_check") or {}
    ok = (bool(chk.get("ok")) and chk.get("ts") == args.as_of and pack["code"]["venv"] == "linked"
          and bool(pack["technical_rebuild"].get("ok")))
    print(json.dumps({"ok": ok, "root": str(root), "sealed": pack["sealed"], "self_check": chk,
                      "venv": pack["code"]["venv"], "data_files": len(pack["files"]),
                      "daily_files": pack["daily_files"], "archive_items": pack["archive_items"],
                      "technical_rebuild": pack["technical_rebuild"], "localized_md": len(pack["localized_md"]),
                      "overlays": [o["name"] for o in pack["overlays"]],
                      "data_files_missing_in_src": pack["data_files_missing_in_src"]}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
