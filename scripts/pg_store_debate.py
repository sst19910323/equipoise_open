# -*- coding: utf-8 -*-
"""
pg_store_debate.py — agent 版辩论(CC/codex 共用)的**唯一 PG 写库脚本**。

🔀 2026-07-09 统一"文件为界"架构: agent 子进程(claude -p / codex exec)只读水合文件、只写
Analysis/Evidence JSON —— **不碰 PG**(codex sandbox 网络隔离直连不了, CC 为对称也不写)。
写库全在沙盒外由本脚本一条命令完成: ① debates(upsert) ② evidence(best-effort, 取报告
`evidence` @ `evidence_generated_at`, 契约见 prompts/agent/formats/evidence.md)。
2026-09-30 重建批 3: 写库前先跑同一条出厂闸(scripts/run_step.py gate), 不过闸不写(X6);
`--skip-gate` 只给库主迁移旧报告用。
调用方: cc_loop / codex_loop 每标的 OK 后自动跑; 手动 fire 的编排层(对话 CC)也跑同一条。

把终报 Analysis JSON 写进 PG equipoise.debates:

定位: 这是 SYNC 里定的方案 (a) 的 codex 落地 —— 照搬 equipoise
workflows/debate_pg_store.save_debate 的"胖 jsonb + 瘦决策列, 主键(symbol, as_of)"契约,
让 CC 版终报和 deepseek 代码版终报落进**同一张 debates 表、同一套列**, 前端(/api/reports)
和 postcheck 只认 PG 一个口, 不会"前端看着像没跑"。

边界:
- 只写 debates 表(upsert on symbol+as_of), 不碰别的表。
- best-effort: 写失败只返回 False / 打日志, **绝不抛**, 不能搞挂辩论(flat JSON 仍是 source of truth)。
- 连接配置走环境变量 EQUIPOISE_PG_*, 缺省 = equipoise data_sources.yaml 的 lowfreq_pg(localhost/equipoise/postgres)。

CC 终报 → debates 列映射(字段名与 equipoise 终报一致, 几乎直读):
  symbol               ← 入参 symbol(大写)
  as_of (PK)           ← market_data_timestamp_utc (复现锚, 形如 20260622T025300Z)
  report_generated_at  ← report_generated_at
  action               ← action(int)
  operation_type/target/volume ← 同名字段
  next_review (jsonb)  ← next_review, 经 _normalize 补 reason(←notes), 其余原样
  report (jsonb)       ← 整份终报

用法:
  python pg_store_debate.py <SYMBOL> [analysis_json_path]
      省略 path → 在 data/debate/<SYMBOL>/ 下取最新的 <SYMBOL>_Analysis_*.json
  退出码 0 = 写成功, 1 = 失败/跳过(打印单行回执)。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 工程根 = 本文件上溯 3 层 (.../equipoise/scripts/pg_store_debate.py)
EQUIPOISE_ROOT = Path(__file__).resolve().parents[1]
if str(EQUIPOISE_ROOT) not in sys.path:
    sys.path.insert(0, str(EQUIPOISE_ROOT))   # datalayer 可导入(evidence 写入用)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS debates (
    symbol               text        NOT NULL,
    as_of                timestamptz NOT NULL,
    report_generated_at  timestamptz,
    composite_score      integer,
    operation_type       text,
    operation_target     text,
    operation_size       text,
    next_review          jsonb,
    report               jsonb        NOT NULL,
    lang                 text         NOT NULL DEFAULT 'zh',
    report_i18n          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (symbol, as_of)
);
"""

def _upsert_sql(n: dict, *, member=False) -> str:
    """n = names.debates(cur): 评分列与规模列在库里的实际名字(迁移前旧名, 迁移后新名)。"""
    score, size = n["composite_score"], n["operation_size"]
    owner_col = ", group_symbol" if member else ""
    owner_val = ", %s" if member else ""
    insert = f"""
INSERT INTO debates (symbol, as_of, report_generated_at, {score}, operation_type,
                     operation_target, {size}, next_review, report, lang{owner_col})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s{owner_val})
"""
    if member:
        return insert + "ON CONFLICT (symbol, as_of) DO NOTHING"
    return insert + f"""
ON CONFLICT (symbol, as_of) DO UPDATE SET
    report_generated_at = EXCLUDED.report_generated_at,
    {score}             = EXCLUDED.{score},
    operation_type      = EXCLUDED.operation_type,
    operation_target    = EXCLUDED.operation_target,
    {size}              = EXCLUDED.{size},
    next_review         = EXCLUDED.next_review,
    -- 2026-10-24 多语言: 原文变了旧译文作废(清空等翻译任务重翻); 同一份重存不丢译文
    report_i18n         = CASE WHEN debates.report IS DISTINCT FROM EXCLUDED.report
                               THEN '{{}}'::jsonb ELSE debates.report_i18n END,
    report              = EXCLUDED.report,
    lang                = EXCLUDED.lang
WHERE NOT (debates.report ? 'debate_ref')
"""


def _pg_cfg() -> dict:
    return {
        "host": os.environ.get("EQUIPOISE_PG_HOST", "localhost"),
        "port": int(os.environ.get("EQUIPOISE_PG_PORT", "5432")),
        "dbname": os.environ.get("EQUIPOISE_PG_DB", "equipoise"),
        "user": os.environ.get("EQUIPOISE_PG_USER", "postgres"),
        "password": os.environ.get("EQUIPOISE_PG_PASSWORD", "postgres"),
    }


def _parse_ts(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _safe_int(v):
    try:
        return int(float(str(v)))
    except Exception:
        return None


def _normalize_next_review(nr):
    """CC 版 next_review 与 deepseek 版对齐: 缺 reason 时用 notes 兜底(前端按 reason 显示)。
    recommend_after 的 date-only 不动 —— schedule_writeback 已兼容纯日期。其余字段原样保留。"""
    if not isinstance(nr, dict):
        return nr
    out = dict(nr)
    if not out.get("reason"):
        notes = out.get("notes")
        if notes:
            out["reason"] = notes
    return out


def find_latest_analysis(symbol: str, root: Path = EQUIPOISE_ROOT):
    pat = str(root / "data" / "debate" / symbol / f"{symbol}_Analysis_*.json")
    hits = glob.glob(pat)
    if not hits:
        return None
    return max(hits, key=lambda p: os.path.getmtime(p))


def _decision_values(symbol, as_of, report, lang):
    from psycopg2.extras import Json
    nr = _normalize_next_review(report.get("next_review"))
    target = report.get("operation_target")
    return (symbol, as_of, _parse_ts(report.get("report_generated_at")),
            _safe_int(report.get("composite_score", report.get("action"))),
            report.get("operation_type"), str(target) if target is not None else None,
            report.get("operation_size", report.get("operation_volume")),
            Json(nr) if nr is not None else None, Json(report), lang)


def _publish_group(cur, symbol, report, names):
    from psycopg2.extras import Json
    from workflows.group_report_view import project_member_report, SHARED_FIELDS

    cutoff = _parse_ts(report.get("market_data_timestamp_utc"))
    if (not cutoff or cutoff.utcoffset() is None or symbol != report.get("symbol")
            or not isinstance(report.get("members"), list) or not report["members"]):
        raise ValueError("group publication needs its own key, an aware cutoff and frozen members")
    members = {m["symbol"] for m in report["members"]}
    for quote in report.get("reference_prices", []):
        stamp = _parse_ts(quote.get("as_of"))
        if quote.get("symbol") not in members or not stamp or stamp.utcoffset() is None or stamp > cutoff:
            raise ValueError("opening quote needs a frozen member and a timestamp at or before the cutoff")
    values = []
    for member in report["members"]:
        view = project_member_report(report, member["symbol"])
        score = view.get("composite_score")
        if type(score) is not int or not 0 <= score <= 100:
            raise ValueError(f"invalid composite score for {view['symbol']}")
        for field in SHARED_FIELDS - {"market_data_timestamp_utc", "report_generated_at"}:
            view.pop(field, None)
        view.pop("debate_members")  # Hydrated from the archive on read, including for older rows.
        values.append(_decision_values(view["symbol"], cutoff, view, "en"))
    cur.execute("INSERT INTO debate_group_sessions (symbol, as_of, report) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING", (symbol, cutoff, Json(report)))
    inserted = bool(cur.rowcount)
    if not inserted:
        cur.execute("SELECT report = %s FROM debate_group_sessions WHERE symbol=%s AND as_of=%s",
                    (Json(report), symbol, cutoff))
        if not cur.fetchone()[0]:
            raise ValueError(f"conflicting frozen archive for {symbol} at {cutoff}")
    for row in sorted(values, key=lambda r: r[0]):
        cur.execute(_upsert_sql(names, member=True), (*row, symbol))
        if not cur.rowcount:
            cur.execute("SELECT report = %s FROM debates WHERE symbol=%s AND as_of=%s",
                        (row[8], row[0], cutoff))
            if not cur.fetchone()[0]:
                raise ValueError(f"conflicting decision for {row[0]} at {cutoff}")
    return inserted


def save_debate(symbol: str, final_json: dict) -> bool:
    """把一份 CC 终报 upsert 进 debates。best-effort, 失败返回 False 但绝不抛。"""
    if not symbol or not isinstance(final_json, dict) or "debate_ref" in final_json or "group_session" in final_json:
        return False
    try:
        import psycopg2
        from psycopg2.extras import Json
    except Exception as e:
        print(f"[pg_store] psycopg2 缺失, 跳过: {e}", file=sys.stderr)
        return False
    cfg = _pg_cfg()
    con = None
    try:
        con = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"],
            connect_timeout=4, client_encoding="UTF8",
        )
        group = final_json.get("report_kind") == "sector_group"
        con.autocommit = not group
        as_of = (_parse_ts(final_json.get("market_data_timestamp_utc"))
                 or _parse_ts(final_json.get("report_generated_at"))
                 or datetime.now(timezone.utc))
        with con.cursor() as cur:
            cur.execute(_CREATE_SQL)
            from datalayer.pg import names as _names
            from datalayer.pg.i18n import detect_lang
            names = _names.debates(cur)
            if group:
                inserted = _publish_group(cur, symbol.upper(), final_json, names)
                if inserted:
                    save_task_evidence_rows(symbol, final_json, connection=con)
                con.commit()
            else:
                cur.execute(_upsert_sql(names), _decision_values(symbol.upper(), as_of, final_json, detect_lang(final_json)))
                if not cur.rowcount:
                    raise ValueError("a frozen group decision already occupies this member and cutoff")
        print(f"[pg_store] OK {symbol.upper()} @ {as_of.isoformat()} → debates")
        return True
    except Exception as e:
        print(f"[pg_store] FAIL {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def save_report_evidence(symbol: str, final_json: dict) -> None:
    """②证据存档 —— **已停写(2026-08-19, 用户拍板)**。

    老 `evidence` 表是"整场一坨 blob", 无逐条行、与 debates 靠 generated_at 松耦合(同刻两场
    还会主键互撞)。三层嵌套上线后, 逐条归档由 evidence_entity + evidence_items 承担, 老表
    没有第二个写者的价值 —— 表原地冻结当遗留归档(不 drop, `/api/evidence` 快照端点照旧能读
    历史), 新场只走 save_task_evidence_rows。本函数保留空壳只为回执连续 + 随时可回滚。"""
    print(f"[evidence_pg] FROZEN {symbol}: 老 evidence blob 表已停写(逐条归档走 evidence_items)")
    return


def _save_report_evidence_legacy(symbol: str, final_json: dict) -> None:
    """停写前的原实现, 留档备回滚(2026-08-19)。"""
    try:
        ev = final_json.get("evidence") or []
        if not isinstance(ev, list) or not ev:
            print(f"[evidence_pg] SKIP {symbol}: 报告无 evidence 字段(旧版报告正常)")
            return
        ts = final_json.get("evidence_generated_at") or final_json.get("report_generated_at")
        from datalayer.pg.evidence_pg_store import save_evidence
        ok = save_evidence(symbol, ev, ts)
        print(f"[evidence_pg] {'OK' if ok else 'FAIL'} {symbol} @ {ts} ({len(ev)} items)")
    except Exception as e:
        print(f"[evidence_pg] EXC {symbol}: {type(e).__name__}: {e}", file=sys.stderr)


def _norm_ev_id(v) -> str:
    """场内证据号归一化(2026-08-19): 场内唯一形状 = `EV<n>`(无横杠), 与 evidence_items
    主键 f"EV{i}" 一致。契约历史写法 `EV-n` 与手写变体 `ev 4` 都归到这里——否则
    tasks.evidence_ids 里躺着指不到任何行的野指针, 前端点开必 404。"""
    s = str(v or "").strip().upper().replace(" ", "").replace("_", "")
    if s.startswith("EV-"):
        s = "EV" + s[3:]
    return s


def _content_eid(it: dict) -> str:
    """证据内容指纹(与 scripts/backfill_evidence_items.py 的 content_eid 同算法, 改必同改):
    同问题+同结论+同全文 = 同一份证据实体——跨场/跨 task 复用自动去重的键。"""
    import hashlib
    key = "\x1f".join([str(it.get("question") or it.get("task") or ""), str(it.get("simple") or ""),
                       str(it.get("detailed") or ""), str(it.get("payload_text") or "")])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def save_task_evidence_rows(symbol: str, final_json: dict, *, connection=None) -> None:
    """③task 体系双写(best-effort, 2026-08-17, v2 实体+挂载模型): 报告 evidence[] 逐条 →
    evidence_entity(内容本体, eid 指纹去重, 可被多场/多 task 引用) + evidence_items(挂载:
    本场谁在哪轮哪个 task 用了它); 带 task_id 的条目按 task_id 聚合 → tasks 台账。
    旧格式报告(条目无 task_id/round) → 直属挂载行, tasks 空——设计即如此。
    幂等: 先 DELETE 本场挂载/台账再插(entity 全局共享, 只增不删)。失败只打回执不抛。
    表结构: scripts/create_task_evidence_tables.py; 契约: prompts/agent/formats/evidence.md。"""
    ev = final_json.get("evidence") or []
    if not isinstance(ev, list) or not ev:
        return
    try:
        import psycopg2
        from psycopg2.extras import Json
    except Exception:
        if connection is not None:
            raise
        return
    cfg = _pg_cfg()
    con = None
    try:
        con = connection or psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"],
            connect_timeout=4, client_encoding="UTF8",
        )
        sym = symbol.upper()
        as_of = (_parse_ts(final_json.get("market_data_timestamp_utc"))
                 or _parse_ts(final_json.get("report_generated_at"))
                 or datetime.now(timezone.utc))
        gen_at = _parse_ts(final_json.get("evidence_generated_at"))
        cur = con.cursor()
        from datalayer.pg import names as _names
        mc = _names.model_calls(cur)      # 表名与三列的实际名字(迁移前 tasks / task_id / playbook / tier_eligible)
        evn = _names.evidence(cur)        # evidence_items 的挂载列名; evidence_entity 有没有 verified_scope
        cur.execute("DELETE FROM evidence_items WHERE symbol=%s AND as_of=%s", (sym, as_of))
        cur.execute(f"DELETE FROM {mc['table']} WHERE symbol=%s AND as_of=%s", (sym, as_of))
        # tasks 台账主源 = 报告 tasks[](v2.2, 完整三件套: 概述/详述(分析过程)/证据引用);
        # 旧报告无此字段 → 回退到 evidence 条目 task_id 聚合(概述级)。
        # 2026-09-30 改名: 报告字段 model_calls[model_call_id, model, suggested_evidence_tier]; 旧报告是 tasks[...]。
        report_tasks = final_json.get("model_calls")
        if report_tasks is None:
            report_tasks = final_json.get("tasks")
        tasks: dict = {}
        if isinstance(report_tasks, list):
            for t in report_tasks:
                tid = (t.get("model_call_id") or t.get("task_id")) if isinstance(t, dict) else None
                if tid:
                    tasks[tid] = {
                        "round": t.get("round"), "issue": t.get("issue"),
                        "kind": t.get("kind") or "model", "playbook": t.get("model") or t.get("playbook"),
                        "requested_by": t.get("requested_by"), "request": t.get("request"),
                        "status": t.get("status"),
                        "tier_eligible": t.get("suggested_evidence_tier") or t.get("tier_eligible"),
                        # 概述已升格"一小段"(结论+判据+关键数字, 2026-08-19) → 放到 800
                        "result_summary": (t.get("summary") or "")[:800],
                        "detail": t.get("detail"),
                        "evidence_ids": [_norm_ev_id(x) for x in (t.get("evidence_refs") or [])]}
        n_items = 0
        minted: set = set()          # 本场真实铸出的场内号, 用于末尾剔野指针
        for i, it in enumerate(ev, 1):
            if not isinstance(it, dict):
                continue
            src = it.get("source")
            if isinstance(src, str):
                src = [src]
            elif not isinstance(src, list):
                src = []
            eid = _content_eid(it)
            cur.execute(
                """INSERT INTO evidence_entity
                   (eid, kind, status, verdict, question, simple, detailed, payload_text,
                    source, generated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (eid) DO NOTHING""",
                [eid, it.get("kind") or it.get("type"), it.get("status") or it.get("verdict"),
                 it.get("verdict"),
                 it.get("question") or it.get("task") or "",   # 2026-09-30 改名: 研究问题 task → question
                 it.get("simple") or "", it.get("detailed") or "",
                 it.get("payload_text"), Json(src), gen_at])
            if evn["verified_scope"] and it.get("verified_scope"):
                # 核定范围是本场仲裁者写的; 同一份证据实体被多场引用时, 以最近一次核定为准。
                cur.execute("UPDATE evidence_entity SET verified_scope=%s WHERE eid=%s",
                            [it.get("verified_scope"), eid])
            tid = it.get("model_call_id") or it.get("task_id")   # 报告新名优先, 旧报告兜底
            cur.execute(
                f"""INSERT INTO evidence_items
                   (symbol, as_of, evidence_id, eid, {evn['model_call_id']}, round, requested_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [sym, as_of, f"EV{i}", eid, tid, it.get("round"), it.get("requested_by")])
            n_items += 1
            minted.add(f"EV{i}")
            if tid:
                t = tasks.setdefault(tid, {
                    "round": it.get("round"), "issue": it.get("issue"),
                    "kind": it.get("kind") or it.get("type"),
                    "playbook": it.get("model") or it.get("playbook"),
                    "requested_by": it.get("requested_by"), "request": it.get("request"),
                    "status": it.get("status") or it.get("verdict"),
                    "tier_eligible": it.get("suggested_evidence_tier") or it.get("tier_eligible"),
                    "result_summary": (it.get("simple") or "")[:300],
                    "detail": None, "evidence_ids": []})
                if f"EV{i}" not in t["evidence_ids"]:
                    t["evidence_ids"].append(f"EV{i}")
        n_dropped = 0
        for tid, t in tasks.items():
            # 报告 evidence_refs 引到本场没铸出的号(写错/幻觉) → 丢弃并回执, 不让野指针入库。
            keep = [x for x in (t.get("evidence_ids") or []) if x in minted]
            n_dropped += len(t.get("evidence_ids") or []) - len(keep)
            t["evidence_ids"] = keep
            cur.execute(
                f"""INSERT INTO {mc['table']}
                   (symbol, as_of, {mc['model_call_id']}, round, issue, kind, {mc['model']}, requested_by,
                    request, status, {mc['suggested_evidence_tier']}, result_summary, detail, evidence_ids)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                [sym, as_of, tid, t["round"], t["issue"], t["kind"], t["playbook"],
                 t["requested_by"], Json(t["request"]) if t["request"] is not None else None,
                 t["status"], t["tier_eligible"], t["result_summary"],
                 t.get("detail"), t.get("evidence_ids") or []])
        if connection is None:
            con.commit()
        print(f"[task_pg] OK {sym} @ {as_of.isoformat()} ({n_items} items, {len(tasks)} tasks"
              + (f", 剔除 {n_dropped} 个无效 evidence_ref" if n_dropped else "") + ")")
    except Exception as e:
        if connection is not None:
            raise
        try:
            con.rollback()
        except Exception:
            pass
        print(f"[task_pg] EXC {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        if connection is None and con is not None:
            try:
                con.close()
            except Exception:
                pass


def run_gate(path: str) -> dict:
    """同一条出厂闸(与主会话 finalize 用的一样): fatal 非空 = 不写库。"""
    try:
        sys.path.insert(0, str(EQUIPOISE_ROOT / "scripts"))
        from run_step import gate
        return gate(Path(path), EQUIPOISE_ROOT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "fatal": [f"gate crashed: {type(e).__name__}: {e}"], "warnings": []}


def main(argv):
    skip_gate = "--skip-gate" in argv
    argv = [a for a in argv if a != "--skip-gate"]
    if not argv:
        print("用法: python pg_store_debate.py <SYMBOL> [analysis_json_path] [--skip-gate]", file=sys.stderr)
        return 1
    symbol = argv[0].strip().upper() if not argv[0].strip().isdigit() else argv[0].strip()
    path = argv[1] if len(argv) > 1 else find_latest_analysis(symbol)
    if not path or not os.path.exists(path):
        print(f"[pg_store] FAIL {symbol}: 找不到 Analysis JSON ({path})", file=sys.stderr)
        return 1
    try:
        with open(path, encoding="utf-8") as fh:
            final_json = json.load(fh)
    except Exception as e:
        print(f"[pg_store] FAIL {symbol}: 读 JSON 失败 {e}", file=sys.stderr)
        return 1
    group = final_json.get("report_kind") == "sector_group"
    if group:
        try:
            from group_run import verify_envelope
            verify_envelope(Path(path), final_json, EQUIPOISE_ROOT)
        except Exception as exc:
            print(f"[pg_store] GROUP-GATE-FAIL {symbol}: {exc}", file=sys.stderr)
            return 1
    elif not skip_gate:
        verdict = run_gate(path)
        if not verdict.get("ok"):
            print(f"[pg_store] GATE-FAIL {symbol}: 不写库; fatal={json.dumps(verdict.get('fatal'), ensure_ascii=False)[:1500]}",
                  file=sys.stderr)
            return 1
        print(f"[pg_store] gate ok ({len(verdict.get('warnings') or [])} warnings)")
    ok = save_debate(symbol, final_json)
    if ok and not group:
        save_report_evidence(symbol, final_json)
        save_task_evidence_rows(symbol, final_json)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
