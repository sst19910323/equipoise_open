#!/usr/bin/env python3
"""
报告正文翻译 → debates.report_i18n (2026-10-24, 说明见 datalayer/pg/i18n.py / docs/handoff/I18N-API-2026-10-24.md)。

只翻 i18n.TRANSLATABLE_FIELDS 里的字符串叶子; KEEP_PATHS(枚举/机器字段)、数字、本来就是目标语言的串原样。
做法: 抽出待翻字符串编号 → 一次发给最便宜的模型 → 按编号放回原位, 结构不交给模型, 坏不了。

引擎绑当前 harness(库主定): --engine claude → claude -p --model haiku;
                            --engine codex  → codex exec -m gpt-6-luna (CODEX_HOME 决定 codex1/codex2/codex3);
                            --engine auto   → 按出这一行的引擎(debates 看 signatures.zealot, postcheck 看 reckoner; claude* → claude, 其余 → codex), 各走各的账。
--table postcheck(2026-10-25 主工程): 复盘行只翻 verdict 一句(i18n.POSTCHECK_TRANSLATABLE_FIELDS), 写 postcheck.report_i18n。

  ./venv/bin/python scripts/translate_reports.py --engine claude --to en --latest 2            # 空跑: 只列待翻
  ./venv/bin/python scripts/translate_reports.py --engine claude --to en --latest 2 --apply
  ./venv/bin/python scripts/translate_reports.py --engine claude --to en --symbol NVDA --as-of 20261023T135700Z --apply
  选项: 默认跳过已有该语言译文的; --force 重翻; --shard i/n 分片并行
  老数据批量(后台): for i in 0 1 2; do setsid nohup ./venv/bin/python -u scripts/translate_reports.py \
        --engine claude --to en --since 2026-10-01 --shard $i/3 --apply > logs/translate_en_$i.log 2>&1 & done
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
from psycopg2.extras import Json, RealDictCursor  # noqa: E402

from datalayer.pg import i18n  # noqa: E402

ENGINES = {
    "claude": {"model": "haiku"},
    "codex": {"model": "gpt-6-luna"},
}
LANG_NAMES = {"en": "English", "zh": "Simplified Chinese"}
_KEEP_RE = [re.compile("^" + re.escape(p).replace(r"\*", r"\d+") + r"(\.|$)") for p in i18n.KEEP_PATHS]


def _connect():
    con = psycopg2.connect(
        host=os.environ.get("EQUIPOISE_PG_HOST", "localhost"),
        port=int(os.environ.get("EQUIPOISE_PG_PORT", "5432")),
        dbname=os.environ.get("EQUIPOISE_PG_DB", "equipoise"),
        user=os.environ.get("EQUIPOISE_PG_USER", "postgres"),
        password=os.environ.get("EQUIPOISE_PG_PASSWORD", "postgres"),
        client_encoding="UTF8",
    )
    con.autocommit = True
    return con


def _kept(path: str) -> bool:
    return any(r.match(path) for r in _KEEP_RE)


def _needs(s: str, src: str) -> bool:
    """这个串要不要翻: 中文原文 → 含汉字; 英文原文 → 像句子(有字母且有空格), 排除代码/枚举样的串。"""
    if src == "zh":
        return i18n.has_cjk(s)
    return bool(re.search(r"[A-Za-z]", s)) and " " in s.strip()


def _auto_engine(row: dict, table: str) -> str:
    """出这一行的引擎: claude / codex。"""
    if table == "postcheck":
        tag = str(row.get("reckoner") or "")
    else:
        sig = ((row.get("report") or {}).get("signatures") or {}) if isinstance(row.get("report"), dict) else {}
        tag = str(sig.get("zealot") or sig.get("arbiter") or "")
    return "claude" if tag.lower().startswith("claude") else "codex"


def extract(report: dict, src: str, fields=None):
    """→ (子树 dict, [(path_tuple, text)])。debates 行: 顶层键整棵子树(TRANSLATABLE_FIELDS)+ 叶子路径白名单(i18n.LEAF_PATHS,
    2026-11-03: 执行卡一句话 / 节点标签 / 裁决解释 / 未让步主张); 叶子在提取前先用 ensure_display_fields 补齐展示字段,
    所以老报告也有执行卡可翻, 且与接口现算的一模一样(同一函数、确定性)。"""
    fields = tuple(fields or i18n.TRANSLATABLE_FIELDS)
    leaves = []
    if fields == tuple(i18n.TRANSLATABLE_FIELDS):
        try:
            from workflows.execution_card import ensure_display_fields
            report = ensure_display_fields(json.loads(json.dumps(report, ensure_ascii=False, default=str)), source="translate")
        except Exception:
            report = report
        leaves = [(p, t) for p, t in i18n.collect_leaves(report) if re.search(r"[A-Za-z]", t) or i18n.has_cjk(t)]
        leaves = [(p, t) for p, t in leaves if (i18n.has_cjk(t) if src == "zh" else not i18n.has_cjk(t))]
    sub = {k: report[k] for k in fields if k in report}
    items = []

    def walk(o, path):
        dotted = ".".join(str(p) for p in path)
        if path and _kept(dotted):
            return
        if isinstance(o, str):
            if _needs(o, src):
                items.append((tuple(path), o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + [k])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + [i])

    walk(sub, [])
    return sub, items + leaves


def put_back(sub: dict, items, out: dict) -> dict:
    """子树项写回子树; 叶子项(路径顶层键不在子树里)写进 `_leaves` = {点分路径: 译文}, 读时 i18n.apply_leaves 按路径覆盖。"""
    doc = json.loads(json.dumps(sub, ensure_ascii=False))
    leaves, leaf_src = {}, {}
    for idx, (path, orig) in enumerate(items):
        if path[0] not in doc:
            dotted = ".".join(str(p) for p in path)
            leaves[dotted] = out[str(idx)]
            leaf_src[dotted] = i18n.leaf_src_hash(orig)   # 2026-11-03: 叶子译文带原文指纹, 原文改了译文就不再套用
            continue
        node = doc
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = out[str(idx)]
    if leaves:
        doc["_leaves"] = leaves
        doc["_leaf_src"] = leaf_src
    return doc


PROMPT = """You are translating an equity research report from {src} to {dst}.
Input: a JSON object mapping ids to text snippets. Output: a JSON object with exactly the same ids, each value translated to {dst}.
Rules:
- Translate faithfully and concisely in a professional sell-side research tone. Do not add, drop, or soften any claim.
- Keep numbers, prices, percentages, dates, tickers, and codes exactly as written (e.g. 1281.37, 56%, 2026-10-29, Z2, R.EV1, OBJ1.Z, A.MC1, Stage 2).
- Keep these protocol words verbatim if they appear: ENTRY, MARKET_ENTRY, LIMIT_ENTRY, PILOT, HOLD, HOLD_NEUTRAL, WAITING, Zealot, Reaper, action.
- Output ONLY the JSON object, no markdown fences, no commentary.

{payload}"""


def call_model(engine: str, prompt: str) -> str:
    model = ENGINES[engine]["model"]
    with tempfile.TemporaryDirectory() as d:   # 空目录起, 不加载工程 CLAUDE.md/AGENTS.md, 省 token
        if engine == "claude":
            cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", ""]
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=d, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(f"claude -p 失败: {r.stderr[-500:]}")
            return json.loads(r.stdout)["result"]
        out = Path(d) / "last.txt"
        import shutil
        codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex")
        if not codex_bin:   # 非登录 shell(cron / ssh 一句话)的 PATH 没有 /root/.local/bin: 按 harness_resolver 取默认身份的可执行文件
            try:
                from workflows.harness_resolver import resolve_codex
                codex_bin = resolve_codex().executable
            except Exception:
                codex_bin = "codex"
        cmd = [codex_bin, "exec", "-m", model, "-c", "model_reasoning_effort=low", "--skip-git-repo-check",
               "--sandbox", "read-only", "-o", str(out), "-"]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=d, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"codex exec 失败: {r.stderr[-500:]}")
        return out.read_text(encoding="utf-8")


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    return json.loads(t[a:b + 1])


def translate_one(engine: str, report: dict, src: str, dst: str, chunk: int = 60, fields=None):
    sub, items = extract(report, src, fields)
    out = _translate_items(engine, items, src, dst, chunk)
    doc = put_back(sub, items, out)
    doc["_meta"] = {"engine": engine, "model": ENGINES[engine]["model"], "from": src, "strings": len(items),
                    "translated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return doc, len(items)


def _translate_items(engine: str, items, src: str, dst: str, chunk: int = 60) -> dict:
    """[(path, text)] → {str(idx): translation}, chunked; raises RuntimeError when a chunk fails twice."""
    out = {}
    for start in range(0, len(items), chunk):
        part = {str(i): items[i][1] for i in range(start, min(start + chunk, len(items)))}
        prompt = PROMPT.format(src=LANG_NAMES[src], dst=LANG_NAMES[dst],
                               payload=json.dumps(part, ensure_ascii=False, indent=0))
        got = None
        for attempt in range(2):
            try:
                got = parse_json(call_model(engine, prompt))
                missing = [k for k in part if not isinstance(got.get(k), str) or not got[k].strip()]
                if missing:
                    raise ValueError(f"缺 id {missing[:5]}")
                if dst == "en":
                    left = [k for k in part if i18n.has_cjk(got[k])]
                    if left:
                        raise ValueError(f"译文仍含汉字 id {left[:5]}")
                break
            except Exception as e:
                print(f"    第 {attempt + 1} 次失败: {type(e).__name__}: {e}")
                got = None
        if got is None:
            raise RuntimeError("翻译失败")
        out.update({k: got[k] for k in part})
    return out


# ───────────────────────── last-step mode: extract → (subagent | --fill) → load ───────────────────────── #
# The debate's / postcheck's last step (owner, 2026-10-25): the original is stored first; then the translator — the cheapest
# model, one subagent per language — fills a staging JSON written by --extract; --load puts it into report_i18n. The batch mode
# below (--engine … --apply) stays only for backfilling old rows.

STAGING_DIR = ROOT / "data" / "i18n_staging"


def _hash(doc) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def _src_hash(doc_src: dict, src: str, fields) -> str:
    """Hash of the translatable strings only (what the Translator actually saw). Hashing the whole report broke --load for every
    staging file extracted from the local Analysis file: PG jsonb re-serializes numbers (50.0 vs 50), so the whole-document hash of
    the file never equals the hash of the stored row even though nothing changed (2026-10-26, 13 JP/KR reports)."""
    _, items = extract(doc_src, src, fields)
    return _hash({".".join(str(x) for x in p): t for p, t in items})


def apply_staging(doc_src: dict, st: dict, fields) -> tuple:
    """Put the staging file's translations into the translatable subtree of doc_src (the row as PG holds it) → (doc, None) or (None, error).
    Matching is by path, never by id: ids are enumeration order, and the nested key order of the local Analysis file differs from
    PG jsonb (which sorts keys), so an id-based put-back would move translations into the wrong fields. The by-path source texts must be
    exactly what the Translator saw; that is the real "original unchanged" check (the whole-document hash also tripped on the harness-truth
    stamp codex_loop writes into the Analysis file after the session)."""
    sub_, items = extract(doc_src, st["from"], fields)
    dotted = [".".join(str(x) for x in p) for p, _ in items]
    src_by_path = dict(zip(dotted, (t for _, t in items)))
    st_by_path = {st["paths"][i]: st["items"][i] for i in st["items"]}
    if src_by_path != st_by_path:
        missing = sorted(set(st_by_path) ^ set(src_by_path))[:5]
        changed = [p for p in st_by_path if p in src_by_path and st_by_path[p] != src_by_path[p]][:5]
        return None, f"original changed since extraction; re-extract (paths differ: {missing}; text differs: {changed})"
    tr_by_path = {st["paths"][i]: st["translations"][i] for i in st["items"]}
    out = {str(idx): tr_by_path[d] for idx, d in enumerate(dotted)}
    return put_back(sub_, items, out), None


def _ts_tag(as_of) -> str:
    if not isinstance(as_of, datetime):
        as_of = datetime.fromisoformat(as_of)
    if as_of.utcoffset() is not None:
        as_of = as_of.astimezone(timezone.utc)
    return as_of.strftime("%Y%m%dT%H%M%SZ")


def build_extract(doc_src: dict, src: str, dst: str, table: str, symbol: str, as_of, dt=None, ruler=None,
                  source: str = "pg") -> dict:
    """The staging file the translator fills: items {id: source text}; translations {id: text} left for the translator."""
    fields = i18n.POSTCHECK_TRANSLATABLE_FIELDS if table == "postcheck" else i18n.TRANSLATABLE_FIELDS
    _, items = extract(doc_src, src, fields)
    return {
        "table": table, "symbol": symbol.upper(), "as_of": _ts_tag(as_of), "dt": dt, "ruler": ruler,
        "from": src, "to": dst, "src_hash": _src_hash(doc_src, src, fields), "source": source,
        "instructions": (f"Translate every value of `items` from {LANG_NAMES.get(src, src)} to {LANG_NAMES.get(dst, dst)} and write the results "
                         "into `translations` under the same ids. Keep numbers, prices, dates, symbols/tickers, enum values, field names, "
                         "markdown and line breaks exactly; do not add, drop or merge ids; do not translate anything else in this file. "
                         "Put your exact model id in `translator`."),
        "items": {str(i): text for i, (_, text) in enumerate(items)},
        "paths": {str(i): ".".join(str(x) for x in path) for i, (path, _) in enumerate(items)},
        "translations": {},
        "translator": None,
    }


def validate_translations(staging: dict) -> list:
    """Problems with the filled staging file (empty list = good)."""
    probs = []
    items, tr = staging.get("items") or {}, staging.get("translations") or {}
    missing = [k for k in items if not isinstance(tr.get(k), str) or not tr[k].strip()]
    if missing:
        probs.append(f"untranslated ids: {missing[:8]}{'…' if len(missing) > 8 else ''}")
    extra = [k for k in tr if k not in items]
    if extra:
        probs.append(f"unknown ids: {extra[:8]}")
    if staging.get("to") == "en":
        left = [k for k in items if isinstance(tr.get(k), str) and i18n.has_cjk(tr[k])]
        if left:
            probs.append(f"CJK left in ids: {left[:8]}")
    return probs


def _load_local_analysis(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fetch_row(cur, table: str, symbol: str, as_of_tag: str, dt=None, ruler=None):
    as_of = datetime.strptime(as_of_tag, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if table == "postcheck":
        cur.execute("SELECT symbol, as_of, dt, ruler, lang, verdict, reckoner FROM postcheck WHERE symbol=%s AND as_of=%s AND dt=%s AND ruler=%s",
                    (symbol.upper(), as_of, int(dt), ruler))
    else:
        cur.execute("SELECT symbol, as_of, lang, report FROM debates WHERE symbol=%s AND as_of=%s", (symbol.upper(), as_of))
    return cur.fetchone()


def cmd_extract(a) -> int:
    if a.table == "debates" and a.analysis:
        rep_ = _load_local_analysis(Path(a.analysis))
        symbol = str(rep_.get("symbol") or a.symbol or "").upper()
        as_of = rep_.get("market_data_timestamp_utc") or a.as_of
        src = rep_.get("lang") or i18n.detect_lang(rep_)
        doc_src, source, dt, ruler = rep_, str(a.analysis), None, None
    else:
        con = _connect(); cur = con.cursor(cursor_factory=RealDictCursor)
        row = _fetch_row(cur, a.table, a.symbol, a.as_of, a.dt, a.ruler)
        if not row:
            print(json.dumps({"ok": False, "error": "row not found"})); return 1
        symbol, as_of, src = row["symbol"], row["as_of"], row["lang"]
        doc_src = row["report"] if a.table == "debates" else {"verdict": row.get("verdict") or ""}
        source, dt, ruler = "pg", (row.get("dt") if a.table == "postcheck" else None), (row.get("ruler") if a.table == "postcheck" else None)
    if src == a.to:
        print(json.dumps({"ok": False, "error": f"source language is already {a.to}"})); return 1
    st = build_extract(doc_src, src, a.to, a.table, symbol, as_of, dt=dt, ruler=ruler, source=source)
    out = Path(a.out) if a.out else STAGING_DIR / f"{a.table}_{st['symbol']}_{st['as_of']}"         f"{('_' + str(dt) + '_' + str(ruler)) if a.table == 'postcheck' else ''}_{a.to}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "staging": str(out), "strings": len(st["items"]), "from": src, "to": a.to}, ensure_ascii=False))
    return 0


def cmd_fill(a) -> int:
    """Fill the staging file with this script's cheap model (batch drivers; the conversational main session spawns a translator subagent instead)."""
    path = Path(a.fill)
    st = json.loads(path.read_text(encoding="utf-8"))
    engine = a.engine if a.engine and a.engine != "auto" else "claude"
    items = st.get("items") or {}
    out = {}
    keys = list(items.keys())
    for start in range(0, len(keys), 60):
        part = {k: items[k] for k in keys[start:start + 60]}
        prompt = PROMPT.format(src=LANG_NAMES[st["from"]], dst=LANG_NAMES[st["to"]], payload=json.dumps(part, ensure_ascii=False, indent=0))
        got = parse_json(call_model(engine, prompt))
        out.update({k: got.get(k, "") for k in part})
    st["translations"] = out
    st["translator"] = f"{engine}/{ENGINES[engine]['model']}"
    path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    probs = validate_translations(st)
    print(json.dumps({"ok": not probs, "staging": str(path), "strings": len(items), "problems": probs}, ensure_ascii=False))
    return 0 if not probs else 1


def load_staging(path: Path) -> dict:
    """Put one filled staging file into report_i18n. Returns the receipt dict (ok=False with the reason; never raises on data problems)."""
    st = json.loads(path.read_text(encoding="utf-8"))
    probs = validate_translations(st)
    if probs:
        return {"ok": False, "staging": str(path), "problems": probs}
    con = _connect(); cur = con.cursor(cursor_factory=RealDictCursor)
    row = _fetch_row(cur, st["table"], st["symbol"], st["as_of"], st.get("dt"), st.get("ruler"))
    if not row:
        return {"ok": False, "staging": str(path), "error": "row not in PG yet — store the original first"}
    doc_src = row["report"] if st["table"] == "debates" else {"verdict": row.get("verdict") or ""}
    fields = i18n.POSTCHECK_TRANSLATABLE_FIELDS if st["table"] == "postcheck" else i18n.TRANSLATABLE_FIELDS
    doc, err = apply_staging(doc_src, st, fields)
    if err:
        return {"ok": False, "staging": str(path), "error": err}
    items = st["items"]
    doc["_meta"] = {"engine": "subagent", "model": st.get("translator") or "unknown", "from": st["from"], "strings": len(items),
                    "translated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "staging": path.name}
    if st["table"] == "postcheck":
        cur.execute("UPDATE postcheck SET report_i18n = report_i18n || jsonb_build_object(%s, %s::jsonb) "
                    "WHERE symbol=%s AND as_of=%s AND dt=%s AND ruler=%s AND verdict = %s",
                    (st["to"], Json(doc), row["symbol"], row["as_of"], row["dt"], row["ruler"], row.get("verdict") or ""))
    else:
        cur.execute("UPDATE debates SET report_i18n = report_i18n || jsonb_build_object(%s, %s::jsonb) "
                    "WHERE symbol=%s AND as_of=%s AND report = %s::jsonb",
                    (st["to"], Json(doc), row["symbol"], row["as_of"], Json(row["report"])))
    con.commit()
    ok = bool(cur.rowcount)
    return {"ok": ok, "table": st["table"], "symbol": st["symbol"], "as_of": st["as_of"], "lang": st["to"],
            "strings": len(items), "by": st.get("translator"), "staging": path.name}


def cmd_load(a) -> int:
    rc = load_staging(Path(a.load))
    print(json.dumps(rc, ensure_ascii=False))
    return 0 if rc.get("ok") else 1


def staging_files_for_report(symbol: str, analysis_path: Optional[Path] = None) -> tuple:
    """(Analysis path, [staging files]) for one debate report: the Translator wrote data/i18n_staging/debates_<SYM>_<TS>_<lang>.json
    per language; <TS> is the Analysis file's own timestamp. With no path, the symbol's newest Analysis file."""
    sym = symbol.upper()
    if analysis_path is None:
        cands = sorted((ROOT / "data" / "debate" / sym).glob(f"{sym}_Analysis_*.json"))
        if not cands:
            return None, []
        analysis_path = cands[-1]
    m = re.search(r"_Analysis_(\d{8}T\d{6}Z)\.json$", analysis_path.name)
    if not m:
        return analysis_path, []
    return analysis_path, sorted(STAGING_DIR.glob(f"debates_{sym}_{m.group(1)}_*.json"))


def cmd_load_report(a) -> int:
    """Outer-runner hook (cc_loop / codex_loop, after pg_store_debate.py): load every staging file the main session's Translator
    left for this report. Argument = symbol (newest Analysis) or an Analysis path. Idempotent; a missing or unfilled file is reported, not fatal."""
    arg = a.load_report
    ap_ = Path(arg)
    if ap_.suffix == ".json" and ap_.exists():
        sym = re.sub(r"_Analysis_.*$", "", ap_.name).upper()
        analysis, files = staging_files_for_report(sym, ap_)
    else:
        sym = arg.upper()
        analysis, files = staging_files_for_report(sym)
    if analysis is None:
        print(json.dumps({"ok": False, "symbol": sym, "error": "no Analysis file"})); return 1
    if not files:
        print(json.dumps({"ok": False, "symbol": sym, "report": analysis.name, "error": "no staging file for this report (Translator step missing?)",
                          "expected": f"data/i18n_staging/debates_{sym}_<TS>_<lang>.json"}, ensure_ascii=False)); return 1
    loaded = [load_staging(f) for f in files]
    ok = all(r.get("ok") for r in loaded)
    print(json.dumps({"ok": ok, "symbol": sym, "report": analysis.name, "loaded": loaded}, ensure_ascii=False))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true", help="last-step mode: write the staging JSON for one report/verdict (then a translator subagent fills `translations`)")
    ap.add_argument("--analysis", help="with --extract --table debates: the local Analysis JSON (before/without PG)")
    ap.add_argument("--dt", type=int); ap.add_argument("--ruler")
    ap.add_argument("--out", help="with --extract: staging path (default data/i18n_staging/<table>_<SYM>_<TS>[_dt_ruler]_<to>.json)")
    ap.add_argument("--fill", metavar="STAGING", help="fill a staging file with this script's cheap model (batch drivers)")
    ap.add_argument("--load", metavar="STAGING", help="last-step mode: put a filled staging file into report_i18n (original must already be in PG and unchanged)")
    ap.add_argument("--load-report", metavar="SYM|ANALYSIS", help="outer-runner hook after pg_store_debate.py: load every staging file for that report (symbol = its newest Analysis)")
    ap.add_argument("--engine", choices=list(ENGINES) + ["auto"])
    ap.add_argument("--table", default="debates", choices=["debates", "postcheck"])
    ap.add_argument("--to", choices=list(i18n.SUPPORTED_LANGS))
    ap.add_argument("--latest", type=int, help="全库最新 N 份")
    ap.add_argument("--symbol")
    ap.add_argument("--as-of")
    ap.add_argument("--since", help="只做 as_of >= 这天的(YYYY-MM-DD, UTC); 库主 10-24: 老数据先补 10 月起, 太老的不急")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--leaves-only", action="store_true",
                    help="补译模式(2026-11-03): 只给已有该语言覆盖层的报告补缺的叶子(执行卡一句话 / 节点标签 / 裁决解释 / 未让步主张), 其余不动")
    ap.add_argument("--shard", help="i/n: 只做第 i 片(0 起), 多进程并行用, 如 0/3 1/3 2/3")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.extract:
        if not a.to:
            ap.error("--extract needs --to")
        sys.exit(cmd_extract(a))
    if a.fill:
        sys.exit(cmd_fill(a))
    if a.load:
        sys.exit(cmd_load(a))
    if a.load_report:
        sys.exit(cmd_load_report(a))
    if not a.engine or not a.to:
        ap.error("batch mode needs --engine and --to (or use --extract / --fill / --load)")

    con = _connect()
    cur = con.cursor(cursor_factory=RealDictCursor)
    where, params = ["lang <> %s"], [a.to]
    if a.leaves_only:
        where.append("(report_i18n ? %s)")   # 只补已有覆盖层的; 没有覆盖层的走普通模式整份翻(那时叶子一起进)
        params.append(a.to)
    elif not a.force:
        where.append("NOT (report_i18n ? %s)")
        params.append(a.to)
    if a.symbol:
        where.append("symbol = %s")
        params.append(a.symbol.upper())
    if a.as_of:
        where.append("as_of = %s")
        params.append(datetime.strptime(a.as_of, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
    if a.since:
        where.append("as_of >= %s")
        params.append(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc))
    if a.table == "postcheck":
        where.append("score IS NOT NULL")
        sql = f"SELECT symbol, as_of, dt, ruler, lang, verdict, reckoner FROM postcheck WHERE {' AND '.join(where)} ORDER BY as_of DESC, dt, ruler DESC"
        fields = i18n.POSTCHECK_TRANSLATABLE_FIELDS
    else:
        sql = f"SELECT symbol, as_of, lang, report, report_i18n FROM debates WHERE {' AND '.join(where)} ORDER BY as_of DESC"
        fields = i18n.TRANSLATABLE_FIELDS
    if a.latest:
        sql += f" LIMIT {int(a.latest)}"
    cur.execute(sql, params)
    rows = cur.fetchall()
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        rows = rows[i::n]
    print(f"待翻 {len(rows)} 份 ({a.table}; engine={a.engine}, → {a.to})")
    ok = 0
    for r in rows:
        doc_src = r["report"] if a.table == "debates" else {"verdict": r.get("verdict") or ""}
        engine = a.engine if a.engine != "auto" else _auto_engine(r, a.table)
        _, items = extract(doc_src, r["lang"], fields)
        tag = f"{r['symbol']} @ {r['as_of'].strftime('%Y%m%dT%H%M%SZ')}" + (f" dt={r['dt']} {r['ruler']}" if a.table == "postcheck" else "")
        if a.leaves_only and a.table == "debates":
            # 只补缺的叶子: 路径不在 TRANSLATABLE_FIELDS 子树里、且覆盖层 _leaves 还没有的, 或已有但原文指纹变了(过期, 读时也不套用)
            ov = ((r.get("report_i18n") or {}).get(a.to) or {})
            have = ov.get("_leaves") or {}
            src_map = ov.get("_leaf_src") or {}
            miss = [(p, t) for p, t in items if p[0] not in fields
                    and (".".join(str(x) for x in p) not in have
                         or i18n.leaf_is_stale(".".join(str(x) for x in p), t, src_map))]
            if not a.apply:
                print(f"  {tag}: 缺或过期 {len(miss)} 个叶子串 (已有 {len(have)})")
                continue
            if not miss:
                continue
            try:
                out = _translate_items(engine, miss, r["lang"], a.to)
            except Exception as e:
                print(f"  {tag}: 失败 {e}")
                continue
            new_leaves = {".".join(str(x) for x in p): out[str(i)] for i, (p, _t) in enumerate(miss)}
            new_src = {".".join(str(x) for x in p): i18n.leaf_src_hash(t) for p, t in miss}
            cur.execute("UPDATE debates SET report_i18n = jsonb_set(jsonb_set(report_i18n, %s::text[], "
                        "coalesce(report_i18n #> %s::text[], '{}'::jsonb) || %s::jsonb, true), %s::text[], "
                        "coalesce(report_i18n #> %s::text[], '{}'::jsonb) || %s::jsonb, true) "
                        "WHERE symbol=%s AND as_of=%s AND report = %s::jsonb",
                        ([a.to, "_leaves"], [a.to, "_leaves"], Json(new_leaves),
                         [a.to, "_leaf_src"], [a.to, "_leaf_src"], Json(new_src),
                         r["symbol"], r["as_of"], Json(r["report"])))
            print(f"  {tag}: 补 {len(miss)} 个叶子 ({engine}) → {'已写入' if cur.rowcount else '原文已变, 未写'}")
            ok += bool(cur.rowcount)
            continue
        if not a.apply:
            print(f"  {tag}: {len(items)} 个串, {sum(len(t) for _, t in items)} 字 ({engine}/{ENGINES[engine]['model']})")
            continue
        if not items:
            continue
        try:
            doc, n = translate_one(engine, doc_src, r["lang"], a.to, fields=fields)
        except Exception as e:
            print(f"  {tag}: 失败 {e}")
            continue
        if a.table == "postcheck":
            cur.execute("UPDATE postcheck SET report_i18n = report_i18n || jsonb_build_object(%s, %s::jsonb) "
                        "WHERE symbol=%s AND as_of=%s AND dt=%s AND ruler=%s AND verdict = %s",
                        (a.to, Json(doc), r["symbol"], r["as_of"], r["dt"], r["ruler"], r.get("verdict") or ""))
        else:
            cur.execute("UPDATE debates SET report_i18n = report_i18n || jsonb_build_object(%s, %s::jsonb) "
                        "WHERE symbol=%s AND as_of=%s AND report = %s::jsonb",
                        (a.to, Json(doc), r["symbol"], r["as_of"], Json(r["report"])))
        print(f"  {tag}: {n} 个串 ({engine}) → {'已写入' if cur.rowcount else '原文已变, 未写'}")
        ok += bool(cur.rowcount)
    if a.apply:
        print(f"完成 {ok}/{len(rows)}")
    con.close()


if __name__ == "__main__":
    main()
