"""Finish or repair Codex translations for the exact Analysis filenames supplied on the command line."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import re
from pathlib import Path

import yaml
from psycopg2.extras import Json, RealDictCursor

import translate_reports as tr


def ensure_overlay(cur, symbol, as_of, lang):
    stamp = as_of.strftime("%Y%m%dT%H%M%SZ")
    # Serialize the cheap-model call as well as the write for concurrent repairs.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"report_i18n/{symbol}/{stamp}/{lang}",))
    cur.execute("SELECT lang, report, report_i18n ? %s AS present FROM debates WHERE symbol=%s AND as_of=%s",
                (lang, symbol, as_of))
    row = cur.fetchone()
    if row is None:
        raise ValueError("original not in PG")
    if row["lang"] == lang or row["present"]:
        return "already available"

    src, report = row["lang"], row["report"]
    stage = tr.STAGING_DIR / f"debates_{symbol}_{stamp}_{lang}.json"
    doc = None
    model = tr.ENGINES["codex"]["model"]
    if stage.is_file():
        try:
            st = json.loads(stage.read_text(encoding="utf-8"))
            identity = (st["table"], st["symbol"], st["as_of"], st["from"], st["to"])
            if (identity == ("debates", symbol, stamp, src, lang)
                    and st.get("translator") in (model, f"codex/{model}")
                    and not tr.validate_translations(st)):
                doc, _ = tr.apply_staging(report, st, tr.i18n.TRANSLATABLE_FIELDS)
                if doc is not None:
                    doc["_meta"] = {"engine": "subagent", "model": st["translator"], "from": src,
                                    "strings": len(st["items"]), "translated_at": datetime.now(timezone.utc).isoformat(),
                                    "staging": stage.name}
        except (ValueError, KeyError, TypeError, AttributeError):
            # A malformed or outdated artifact can be regenerated from the stored original.
            pass
    if doc is None:
        doc, _ = tr.translate_one("codex", report, src, lang)
        method = f"translated by {model}"
    else:
        method = "loaded verified staging"
    cur.execute("UPDATE debates SET report_i18n = report_i18n || jsonb_build_object(%s, %s::jsonb) "
                "WHERE symbol=%s AND as_of=%s AND lang=%s AND report=%s::jsonb AND NOT (report_i18n ? %s)",
                (lang, Json(doc), symbol, as_of, src, Json(report), lang))
    if cur.rowcount == 0:
        cur.execute("SELECT report_i18n ? %s AS present FROM debates WHERE symbol=%s AND as_of=%s",
                    (lang, symbol, as_of))
        current = cur.fetchone()
        if current and current["present"]:
            return "already available"
        raise ValueError("source changed during translation; retry on current original")
    return method


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    langs = yaml.safe_load((tr.ROOT / "config/i18n.yaml").read_text(encoding="utf-8"))["translate_to"]
    results = []
    with closing(tr._connect()) as con:
        con.autocommit = False
        for path in args.reports:
            match = re.fullmatch(r"(.+)_Analysis_(\d{8}T\d{6}Z)\.json", path.name)
            if not match:
                parser.error(f"expected an exact Analysis filename: {path.name}")
            symbol, stamp = match.groups()
            as_of = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            for lang in langs:
                receipt = {"symbol": symbol, "as_of": stamp, "lang": lang}
                try:
                    with con, con.cursor(cursor_factory=RealDictCursor) as cur:
                        receipt["result"] = ensure_overlay(cur, symbol, as_of, lang)
                except Exception as exc:
                    receipt["error"] = str(exc)
                results.append(receipt)
    ok = all("error" not in r for r in results)
    print(json.dumps({"ok": ok, "reports": results}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
