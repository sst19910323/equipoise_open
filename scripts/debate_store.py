#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""debate_store.py — 对象仓: 一物一文件 + manifest + 编号脚本铸造 + 按可见性矩阵拼视图。

重建批 3(2026-09-30)改名与扩容; 四条机械宪法不变:
  ①可见性=编号前缀   — R.MC1.EV* 是孙辈, 永远只对跑该模型调用的 Runner 开放, 不进任何辩手视图;
  ②AI 间只流通编号+结论段 — 视图/回执由本脚本机械拼, 附录=结论段+verdict;
  ③编号脚本铸造       — 写入即编号(put 时 mint), 模型只引用不发明;
  ④IO 极简律          — 模型调用结果整包进来, 脚本机械劈成 MC(薄结论)+MC.EV(重细节);
                        拼接/信封/登记全脚本, 模型的手只碰自己新生成的字。

数据宪法: 对象仓是场内工作介质, PG 是唯一史实。每次 put 同步机械转写三份 buffer
(_inflight / _inflight_evidence / _inflight_private), ledger_tally / validate_debate /
assemble_debate_artifacts / pg_store 全链吃 buffer——流通受限≠归档受限: MC 的重细节在 buffer 行里
保持全文, 随 model_calls 台账进 PG。

布局: data/store/<SYM>/<TS>/
  manifest.jsonl                 追加审计件 {seq,id,kind,by,round,beat,ts,file}
  R1.Z.json / R2.R.json          辩手终稿(R1=Init, R2=Round 1, ...)
  R2.Z.B1.json                   中间拍("还要查"那一拍; 归档不进视图)
  Z.MC1.json / R.MC1.json        模型调用结论段(归属前缀命名空间, 各归属独立计数)
  R.MC1.EV1.json                 模型调用重细节(自动劈出, 父子链在名字里)
  Z.EV1.json / A.EV1.json        直属证据(Z/R=辩手, A=仲裁者中立补查; 与 PG 的 EV 位次号解耦)
  NOTES.R2.json                  仲裁者评注(程序性公告, 进视图)
  PRIVATE.R2.json                仲裁者私记评分(永不进辩手视图)
  PROV.json                      终裁第一判(裁决单)
  PROPOSED.json                  拟定终裁(公开给双方提异议)
  OBJ1.Z.json / OBJ2.R.json      异议回合原文
  RULING.v1.json                 再定稿(仲裁者判断字段 + 异议处理记录; 可再版)
  SIGN1.Z.json                   签章(编号里的数字 = 所签的再定稿版本)
  _view_*.jsonl / _receipt_*     衍生视图与回执(覆盖重生成, 不入档)

用法(主会话只用这一个写入口; 编排步骤见 orchestration/run-protocol.md):
  put   --symbol NVDA --ts <TS> --kind final --by Zealot --round 2 [--beat 2] <tmp.json>
        --kind beat|final|model_call|evidence|notes|private_score|provisional|proposed|objection|ruling|signoff
        model_call 另带 --status --tier --model --issue [--request <申报原文.json>];
        evidence 另带 --status [--ev-kind search|data]
  view  --symbol NVDA --ts <TS> --role zealot|reaper|arbiter            (每轮 spawn 前重生成)
  view  ... --role zealot --objection <k>                                (异议视图)
  view  ... --role zealot --signoff                                      (签章视图: 末轮终稿+自家异议+再定稿)
  receipt --symbol NVDA --ts <TS> --round 2 --beat 1 --by Zealot         (本拍回执: 该方本拍外呼的结果全文)
  verify --symbol NVDA --ts <TS>
  --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台防线
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

HEAVY_KEYS = ("payload_text", "detailed", "steps", "legs")   # 模型调用劈分线 + 附录瘦身线
ROLES = {"Zealot": "Z", "Reaper": "R"}
ARBITER = "Arbiter"
_LEGACY_ARBITER = {"Q-PARENT", "ARBITER", "PARENT"}
STRING_CAP, STRING_KEEP = 900, 800
TRUNCATION_MARK = " …[summary truncated; full detail is archived — name 'detail <id>' per protocol when needed]"
KINDS = ("beat", "final", "model_call", "model_call_evidence", "evidence", "notes",
         "private_score", "provisional", "proposed", "objection", "ruling", "signoff")
EVIDENCE_KINDS = ("model_call", "evidence")
ID_SHAPE = re.compile(
    r"^(R\d+\.(Z|R)(\.B\d+)?|(?:(?:Z|R|A)\.)MC\d+(\.EV\d+)?|(?:(?:Z|R|A)\.)EV\d+|"
    r"NOTES\.R\d+|PRIVATE\.R\d+|PROV|PROPOSED|OBJ\d+\.(Z|R)|RULING\.v\d+|SIGN\d+\.(Z|R))\.json$")


class StoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_name(n: int) -> str:
    """Round label written into views, envelopes and the report (English core, 2026-11-01; rows stored before that
    carry the legacy "回合k", which every parser still accepts)."""
    return "Init" if n == 1 else f"Round {n - 1}"


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    text = "\n".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) for l in lines)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(text + ("\n" if text else ""), encoding="utf-8")
    tmp.replace(path)


def _append_locked(path: Path, obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _truncate(value: Any) -> Any:
    if isinstance(value, str):
        return value[:STRING_KEEP] + TRUNCATION_MARK if len(value) > STRING_CAP else value
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


def _split_heavy(content: dict) -> tuple[dict, dict]:
    """模型调用整包机械劈分: 薄结论(去重键+截长串) / 重细节(孙辈)。"""
    slim = {k: _truncate(v) for k, v in content.items() if k not in HEAVY_KEYS}
    heavy = {k: content[k] for k in HEAVY_KEYS if k in content}
    return slim, heavy


def _owner(by: str | None) -> str:
    if by in ROLES:
        return by
    if by is None or str(by).upper() in _LEGACY_ARBITER or by == ARBITER:
        return ARBITER
    raise StoreError(f"unknown owner {by!r} (Zealot | Reaper | Arbiter)")


def _pfx(by: str | None) -> str:
    return ROLES.get(_owner(by), "A")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"_bad": True})
    return out


class Store:
    def __init__(self, symbol: str, ts: str, root: Path = ROOT):
        self.symbol, self.ts, self.root = symbol, ts, root
        self.dir = root / "data" / "store" / symbol / ts
        self.manifest = self.dir / "manifest.jsonl"
        self.debate_buf = root / "data" / "debate" / symbol / f"_inflight_{ts}.jsonl"
        self.evidence_buf = root / "data" / "evidence" / symbol / f"_inflight_evidence_{ts}.jsonl"
        self.private_buf = root / "data" / "evidence" / symbol / f"_inflight_private_{ts}.jsonl"

    def session_state(self) -> dict:
        """The run_step session state (`_session.json` in the store dir); {} when the store is used without a session."""
        p = self.dir / "_session.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

    def schema(self) -> str | None:
        """Same rule as run_step.Session.schema(): the odds snapshot's schema; None = no session or opened before v3.4."""
        return (self.session_state().get("odds") or {}).get("schema")

    # ---------- 互斥锁: 铸号→落对象→登记→转写必须是一个跨进程临界区 ----------
    from contextlib import contextmanager as _ctx

    def ensure_dirs(self) -> None:
        """对象仓目录与三条缓冲的父目录一起建好。缓冲父目录(data/debate/<SYM>, data/evidence/<SYM>)在新标的第一场之前
        不存在; 沙箱里的子进程建不了它, 若等到归档终稿时才发现, manifest 已登记而缓冲追加失败 → 账目错位判 poisoned
        (2026-10-16 KO 首场)。这里统一建, 建不了就在归档之前抛错。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        for buf in (self.debate_buf, self.evidence_buf, self.private_buf):
            buf.parent.mkdir(parents=True, exist_ok=True)

    @_ctx
    def _lock(self):
        self.ensure_dirs()
        lock_path = self.dir / "_store.lock"
        fh = open(lock_path, "a+b")
        try:
            if fh.seek(0, 2) == 0:
                fh.write(b"0")
                fh.flush()
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # 阻塞等锁; close 即释放
                yield
            except ImportError:  # Windows 本地测试路径
                import time as _time
                import msvcrt
                for _ in range(1200):
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        _time.sleep(0.05)
                else:
                    raise StoreError("store lock timeout (60s) — another put is stuck?")
                try:
                    yield
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()

    # ---------- manifest ----------
    def rows(self) -> list[dict]:
        if not self.manifest.exists():
            return []
        out = []
        for line in self.manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def ids(self) -> set[str]:
        return {r["id"] for r in self.rows()}

    def obj(self, oid: str) -> dict:
        path = self.dir / f"{oid}.json"
        if not path.exists():
            raise StoreError(f"object {oid} not found in store")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self, kind: str, by: str | None = None) -> dict | None:
        rows = [r for r in self.rows() if r["kind"] == kind and (by is None or r.get("by") == by)]
        return rows[-1] if rows else None

    def finals(self) -> list[dict]:
        return [r for r in self.rows() if r["kind"] == "final"]

    def last_final_round(self) -> int:
        return max((r["round"] for r in self.finals()), default=0)

    # ---------- 账目对齐守卫(铸号前必过; 调用方必须已持有 _lock) ----------
    def _alignment_guard(self) -> None:
        """manifest 与转写 buffer 逐类同数, 否则按 poisoned 处置: 宁停不歪。"""
        rows = self.rows()
        db, eb, pb = _read_jsonl(self.debate_buf), _read_jsonl(self.evidence_buf), _read_jsonl(self.private_buf)
        pairs = [
            ("finals", sum(1 for r in rows if r["kind"] == "final"),
             sum(1 for r in db if r.get("role") in ROLES)),
            ("evidence+model_call", sum(1 for r in rows if r["kind"] in EVIDENCE_KINDS),
             sum(1 for r in eb if not any(k in r for k in ("arbiter_notes", "provisional_ruling")))),
            ("notes", sum(1 for r in rows if r["kind"] == "notes"),
             sum(1 for r in eb if "arbiter_notes" in r)),
            ("provisional", sum(1 for r in rows if r["kind"] == "provisional"),
             sum(1 for r in eb if "provisional_ruling" in r)),
            ("private_score", sum(1 for r in rows if r["kind"] == "private_score"),
             sum(1 for r in pb if "arbiter_private_score" in r)),
        ]
        bad = [f"{name}: manifest {m} vs buffer {b}" for name, m, b in pairs if m != b]
        if bad:
            raise StoreError(
                "store/buffer ledger misaligned (" + "; ".join(bad) + ") — residue of an archive step killed midway; "
                "treat this session as poisoned_immutable_buffer: do not resume, re-prepare data and start a new session")

    # ---------- minting (③: 写入即编号; 调用方必须已持有 _lock) ----------
    def _mint(self, kind: str, by: str | None, round_: int | None, parent: str | None,
              beat: int | None = None, version: int | None = None) -> str:
        rows = self.rows()
        ids = {r["id"] for r in rows}

        def _blocked(oid: str) -> str | None:
            if oid in ids:
                return "minted"
            if (self.dir / f"{oid}.json").exists():
                return "orphan"  # 崩溃残留: 有对象文件无 manifest 行
            return None

        def _seq(prefix_fmt: str, start: int) -> str:
            # 顺序类编号 = 档案位次(PG 契约), 必须致密不许跳号——已铸号顺延; 孤儿挪走存证, 号照用
            n = start
            while True:
                oid = prefix_fmt.format(n)
                why = _blocked(oid)
                if why is None:
                    return oid
                if why == "orphan":
                    residue = self.dir / f"{oid}.json"
                    residue.rename(self.dir / f"_orphan_{oid}_{int(datetime.now(timezone.utc).timestamp())}.json")
                    return oid
                n += 1

        if kind == "final":
            oid = f"R{round_}.{ROLES[by]}"
        elif kind == "beat":
            oid = f"R{round_}.{ROLES[by]}.B{beat}"
        elif kind == "model_call":
            p = _pfx(by)
            k = 1 + sum(1 for r in rows if r["kind"] == "model_call" and _pfx(r.get("by")) == p)
            return _seq(p + ".MC{}", k)
        elif kind == "model_call_evidence":
            if not parent or parent not in ids:
                raise StoreError(f"model_call_evidence requires an existing parent MC id, got {parent!r}")
            j = 1 + sum(1 for r in rows if r["kind"] == "model_call_evidence" and r.get("parent") == parent)
            return _seq(parent + ".EV{}", j)
        elif kind == "evidence":
            p = _pfx(by)
            m = 1 + sum(1 for r in rows if r["kind"] == "evidence" and _pfx(r.get("by")) == p)
            return _seq(p + ".EV{}", m)
        elif kind == "notes":
            oid = f"NOTES.R{round_}"
        elif kind == "private_score":
            oid = f"PRIVATE.R{round_}"
        elif kind == "provisional":
            oid = "PROV"
        elif kind == "proposed":
            oid = "PROPOSED"
        elif kind == "objection":
            oid = f"OBJ{round_}.{ROLES[by]}"
        elif kind == "ruling":
            v = 1 + sum(1 for r in rows if r["kind"] == "ruling")
            oid = f"RULING.v{v}"
        elif kind == "signoff":
            oid = f"SIGN{version}.{ROLES[by]}"
        else:
            raise StoreError(f"unknown kind {kind!r}")
        why = _blocked(oid)
        if why == "minted":
            raise StoreError(f"id {oid} already minted — one object per id; a relay leg must "
                             "inventory the manifest instead of re-archiving")
        if why == "orphan":
            raise StoreError(f"orphan object file {oid}.json exists without a manifest row "
                             "(crashed write?) — inspect and remove it manually before re-archiving")
        return oid

    # ---------- 内容预检(拒收即什么都没写) ----------
    def _precheck(self, kind: str, content: Any, by: str | None, round_: int | None,
                  beat: int | None) -> Any:
        if kind in ("final", "beat", "objection"):
            if by not in ROLES or not round_:
                raise StoreError(f"{kind} requires --by Zealot|Reaper and --round N")
            if not isinstance(content, dict):
                raise StoreError(f"{kind} content must be a JSON object")
        if kind == "beat" and not beat:
            raise StoreError("beat requires --beat K")
        if kind == "final" and self.session_state():   # C2 / C3 verification against the archived finals; C8 derived score
            from validate_role_record import ingest
            from ruling import RulingError, final_records, session_cfg
            state = self.session_state()
            try:
                cfg = session_cfg(state)
            except RulingError as exc:
                raise StoreError(f"final cannot be verified (nothing minted): {exc}")
            from lens_views import lenses_of
            lenses = lenses_of(state)
            by_lens = (state.get("odds") or {}).get("by_lens") or {}
            errors, content = ingest(content, by, round_, final_records(self), cfg,
                                        ref_price=(state.get("odds") or {}).get("reference_price"),   # frozen at open
                                        lenses=lenses or None,
                                        ref_prices={L: (by_lens.get(L) or {}).get("reference_price") for L in lenses} or None,
                                        lens_weights=state.get("lens_weights_default") if lenses else None)
            if errors:
                raise StoreError("final failed validation (nothing minted): " + "; ".join(errors))
            b = content.get("beat")
            if isinstance(b, dict) and b.get("status") not in (None, "final"):
                raise StoreError(f"final content declares beat.status={b.get('status')!r}; archive it as --kind beat")
        elif kind == "final":  # 终稿在铸造前过全检: 写坏的审计件救不回来
            from validate_role_record import validate
            envelope = {"round": _round_name(round_), "role": by, "content": content}
            errors = validate(envelope, self.debate_buf if self.debate_buf.exists() else None)
            if errors:
                raise StoreError("final failed validation (nothing minted): " + "; ".join(errors))
            b = content.get("beat")
            if isinstance(b, dict) and b.get("status") not in (None, "final"):
                raise StoreError(f"final content declares beat.status={b.get('status')!r}; archive it as --kind beat")
        elif kind == "objection":
            if not isinstance(content.get("objections"), list):
                raise StoreError("objection content must carry objections[] (may be empty)")
        elif kind == "model_call":
            if not isinstance(content, dict):
                raise StoreError("model_call content must be the runner's full JSON return")
            if len(json.dumps(content, ensure_ascii=False)) < 50:
                raise StoreError("model_call content is empty/trivial — an empty worker result must be "
                                 "reported as a failure under the evidence protocol, never archived")
        elif kind == "evidence":
            if not isinstance(content, dict) or not any(
                    content.get(k) for k in ("question", "task", "target", "simple", "finding")):
                raise StoreError("evidence content must be a Scout/data-shaped object "
                                 "(question/simple/... keys) or the assembler will drop it")
        elif kind == "notes":
            if not round_:
                raise StoreError("notes requires --round N")
            if not isinstance(content, dict) or not (content.get("arbiter_notes") or "").strip():
                raise StoreError('notes content must carry {"tally", "arbiter_notes"}')
        elif kind == "private_score":
            if not round_:
                raise StoreError("private_score requires --round N")
            inner = content.get("arbiter_private_score") if isinstance(content, dict) else None
            content = inner if isinstance(inner, dict) else content
            if not isinstance(content, dict) or not (isinstance(content.get("composite_score"), int)
                                                        or str(content.get("note") or "").strip()):
                raise StoreError("private_score content must carry an integer composite_score or a non-empty note (inputs that moved, open points)")
        elif kind == "provisional":
            inner = content.get("provisional_ruling") if isinstance(content, dict) else None
            content = inner if isinstance(inner, dict) else content
            if not isinstance(content, dict) or "claims_review" not in content \
                    or not isinstance(content.get("composite_score"), int):
                raise StoreError("provisional content must carry claims_review and an integer composite_score")
        elif kind in ("proposed", "ruling"):
            if not isinstance(content, dict) or not isinstance(content.get("composite_score"), int) \
                    or not content.get("operation_type"):
                raise StoreError(f"{kind} content must carry integer composite_score and operation_type")
        elif kind == "signoff":
            if by not in ROLES:
                raise StoreError("signoff requires --by Zealot|Reaper")
            if not isinstance(content, dict) or not isinstance(content.get("endorsed_composite_score"), int):
                raise StoreError("signoff content must carry an integer endorsed_composite_score")
        elif kind not in KINDS:
            raise StoreError(f"unknown kind {kind!r}")
        return content

    # ---------- put (④: 唯一写入口, 铸造+落对象+登记+机械转写 buffer) ----------
    def put(self, kind: str, content: Any, by: str | None = None, round_: int | None = None,
            parent: str | None = None, meta: dict | None = None, beat: int | None = None) -> dict:
        meta = dict(meta or {})
        if by is not None and by not in ROLES:
            by = _owner(by)
        content = self._precheck(kind, content, by, round_, beat)
        version = None
        if kind == "signoff":
            version = int(meta.get("ruling_version") or sum(1 for r in self.rows() if r["kind"] == "ruling"))
            if version < 1:
                raise StoreError("signoff requires an archived ruling (RULING.v1) to sign")
            meta["ruling_version"] = version

        with self._lock():
            self._alignment_guard()
            oid = self._mint(kind, by, round_, parent, beat=beat, version=version)
            receipt: dict[str, Any] = {"ok": True, "id": oid, "kind": kind}

            heavy: dict = {}
            stored_content = content
            if kind == "model_call":
                stored_content, heavy = _split_heavy(content)

            record = {"id": oid, "kind": kind, "by": by, "round": round_, "ts": _now()}
            if beat:
                record["beat"] = beat
            if parent:
                record["parent"] = parent
            if meta:
                record["meta"] = meta
            record["content"] = stored_content
            _atomic_write(self.dir / f"{oid}.json", record)
            man = {"seq": len(self.rows()) + 1, "id": oid, "kind": kind, "by": by,
                   "round": round_, "ts": record["ts"], "file": f"{oid}.json"}
            if beat:
                man["beat"] = beat
            if parent:
                man["parent"] = parent
            _append_locked(self.manifest, man)
            receipt["path"] = str((self.dir / f"{oid}.json").relative_to(self.root))

            if kind == "model_call" and heavy:
                hid = self._mint("model_call_evidence", None, None, oid)
                hrec = {"id": hid, "kind": "model_call_evidence", "parent": oid, "ts": _now(), "content": heavy}
                _atomic_write(self.dir / f"{hid}.json", hrec)
                _append_locked(self.manifest, {"seq": len(self.rows()) + 1, "id": hid,
                                               "kind": "model_call_evidence", "parent": oid,
                                               "ts": hrec["ts"], "file": f"{hid}.json"})
                receipt["split_detail"] = hid

            # ---------- 机械转写 buffer(数据宪法: 归档链吃 buffer) ----------
            if kind == "final":
                _append_locked(self.debate_buf,
                               {"round": _round_name(round_), "role": by, "content": content})
                receipt["buffer"] = str(self.debate_buf.relative_to(self.root))
            elif kind in EVIDENCE_KINDS:
                row: dict[str, Any] = {"round": _round_name(round_ or 1), "requested_by": by}
                if beat:
                    row["beat"] = beat
                row.update({k: v for k, v in meta.items() if v is not None})
                if kind == "model_call":
                    row["model_call_id"] = oid
                elif self.schema():
                    row["evidence_id"] = oid   # ruler session: the release gate rebuilds closure records by store id (C5)
                row["content"] = content  # 全文归档(含重细节) — 流通受限≠归档受限
                _append_locked(self.evidence_buf, row)
                receipt["buffer"] = str(self.evidence_buf.relative_to(self.root))
            elif kind == "notes":
                row = {"round": _round_name(round_), "tally": content.get("tally", ""),
                       "arbiter_notes": content["arbiter_notes"]}
                for k in ("terminal_open", "dispositions"):   # 3.4 closing records travel to the report (C5)
                    if k in content:
                        row[k] = content[k]
                _append_locked(self.evidence_buf, row)
                receipt["buffer"] = str(self.evidence_buf.relative_to(self.root))
            elif kind == "private_score":
                row = {"round": _round_name(round_)} if "round" not in content else {}
                _append_locked(self.private_buf, {"arbiter_private_score": {**row, **content}})
                receipt["buffer"] = str(self.private_buf.relative_to(self.root))
            elif kind == "provisional":
                _append_locked(self.evidence_buf, {"provisional_ruling": content})
                receipt["buffer"] = str(self.evidence_buf.relative_to(self.root))
            return receipt

    # ---------- 结论段(②: 附录体量线 = 结论段 + verdict 一行) ----------
    def _segment(self, row: dict) -> dict:
        rec = self.obj(row["id"])
        c = rec.get("content") or {}
        m = rec.get("meta") or {}
        seg = {"id": row["id"], "kind": row["kind"], "by": rec.get("by"),
               "round": _round_name(rec["round"]) if rec.get("round") else None}
        if row["kind"] == "model_call":
            for k in ("model", "issue", "status", "verified_scope", "suggested_evidence_tier"):
                if m.get(k):
                    seg[k] = m[k]
            seg["summary"] = c.get("finding") or c.get("simple") or ""
        else:
            if m.get("status"):
                seg["status"] = m["status"]
            if m.get("verified_scope"):
                seg["verified_scope"] = m["verified_scope"]
            seg["summary"] = c.get("simple") or c.get("finding") or ""
            src = c.get("source")
            if src:
                seg["source"] = src if isinstance(src, list) else [src]
        if c.get("verdict"):
            seg["verdict"] = c["verdict"]
        return seg

    def _final_line(self, rows: list[dict], r: dict) -> dict:
        rec = self.obj(r["id"])
        appendix = [self._segment(x) for x in rows
                    if x["kind"] in EVIDENCE_KINDS and x.get("by") == r["by"] and x.get("round") == r["round"]]
        line = {"id": rec["id"], "kind": "final", "role": rec["by"],
                "round": _round_name(rec["round"]), "content": rec["content"]}
        if appendix:
            line["appendix"] = appendix
        return line

    def _notes_line(self, r: dict) -> dict:
        rec = self.obj(r["id"])
        return {"id": rec["id"], "kind": "notes", "round": _round_name(rec["round"]),
                "tally": rec["content"].get("tally", ""),
                "arbiter_notes": rec["content"].get("arbiter_notes", "")}

    def _side_file(self, name: str, value: Any) -> str:
        """大对象分行落侧文件, 视图行只放指针(2026-09-28 DAX 案: 单行 410KB 读不进)。"""
        out = self.dir / name
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out)
        return str(out.relative_to(self.root))

    # ---------- view (①②: 可见性矩阵的执行体) ----------
    def build_view(self, role: str, signoff: bool = False, objection_round: int | None = None) -> dict:
        role = role.capitalize()
        rows = self.rows()
        lines: list[dict] = []
        if role == "Arbiter":
            # 仲裁者视图(语境最小化): 终稿史+附录结论段+评注史+私记史+终裁链对象; 中立补查结论段
            for r in rows:
                if r["kind"] == "final":
                    lines.append(self._final_line(rows, r))
                elif r["kind"] == "notes":
                    lines.append(self._notes_line(r))
                elif r["kind"] in ("private_score", "provisional", "proposed", "objection", "ruling", "signoff"):
                    rec = self.obj(r["id"])
                    lines.append({"id": rec["id"], "kind": r["kind"], "by": rec.get("by"),
                                  "round": rec.get("round"), "content": rec["content"]})
                elif r["kind"] in EVIDENCE_KINDS and _owner(r.get("by")) == ARBITER:
                    lines.append(self._segment(r))
            out = self.dir / "_view_arbiter.jsonl"
            _write_jsonl(out, lines)
            return {"ok": True, "view": str(out.relative_to(self.root)), "lines": len(lines),
                    "bytes": out.stat().st_size}
        if role not in ROLES:
            raise StoreError("role must be zealot, reaper or arbiter")
        finals = [r for r in rows if r["kind"] == "final"]
        own = [r for r in finals if r["by"] == role]
        if signoff:
            # 签章视图: 自家末轮终稿 + 自家异议 + 再定稿(含异议处理记录)侧文件指针
            if own:
                rec = self.obj(own[-1]["id"])
                lines.append({"id": rec["id"], "kind": "final", "role": role,
                              "round": _round_name(rec["round"]), "content": rec["content"],
                              "note": "your final-round final"})
            for r in rows:
                if r["kind"] == "objection" and r.get("by") == role:
                    rec = self.obj(r["id"])
                    lines.append({"id": rec["id"], "kind": "objection", "round": rec["round"],
                                  "content": rec["content"], "note": "the objections you filed"})
            ruling = self.latest("ruling")
            if ruling is None:
                raise StoreError("signoff view requires an archived ruling")
            rec = self.obj(ruling["id"])
            f = self._side_file(f"_view_signoff_{role.lower()}_ruling.json", rec["content"])
            lines.append({"id": rec["id"], "kind": "ruling", "file": f,
                          "note": "full redraft in this file (multi-line JSON, includes objection_dispositions); Read it before signing off"})
            out = self.dir / f"_view_signoff_{role.lower()}.jsonl"
        elif objection_round:
            # 异议视图: 自家末轮终稿 + 拟定终裁侧文件 + 已有异议(双方, 第二回合可反驳对方)
            if own:
                rec = self.obj(own[-1]["id"])
                lines.append({"id": rec["id"], "kind": "final", "role": role,
                              "round": _round_name(rec["round"]), "content": rec["content"],
                              "note": "your final-round final"})
            proposed = self.latest("proposed")
            if proposed is None:
                raise StoreError("objection view requires an archived proposed ruling")
            rec = self.obj(proposed["id"])
            f = self._side_file(f"_view_objection_{role.lower()}_proposed.json", rec["content"])
            lines.append({"id": rec["id"], "kind": "proposed", "file": f,
                          "note": "full draft ruling in this file (multi-line JSON); Read it before filing objections"})
            for r in rows:
                if r["kind"] == "objection" and (r.get("round") or 0) < objection_round:
                    rec = self.obj(r["id"])
                    lines.append({"id": rec["id"], "kind": "objection", "role": rec["by"],
                                  "round": rec["round"], "content": rec["content"]})
            out = self.dir / f"_view_objection{objection_round}_{role.lower()}.jsonl"
        else:
            for r in rows:
                if r["kind"] == "final":
                    lines.append(self._final_line(rows, r))
                elif r["kind"] == "notes":
                    lines.append(self._notes_line(r))
                elif r["kind"] in EVIDENCE_KINDS and r.get("by") == role:
                    rec = self.obj(r["id"])  # 自己呼唤的孩子: 全文(MC 本就薄, 孙辈已劈走)
                    line = {"id": rec["id"], "kind": r["kind"], "by": role,
                            "round": _round_name(rec["round"]) if rec.get("round") else None,
                            "own": True, "content": rec["content"]}
                    if rec.get("meta"):
                        line["meta"] = rec["meta"]
                    lines.append(line)
                elif r["kind"] in EVIDENCE_KINDS and _owner(r.get("by")) == ARBITER:
                    lines.append(self._segment(r))  # 仲裁者中立补查: 双方共享结论段
                # beat/private_score/provisional/proposed/objection/ruling/signoff/孙辈/对手的孩子: 永不进辩手视图
            out = self.dir / f"_view_{role.lower()}.jsonl"
        _write_jsonl(out, lines)
        return {"ok": True, "view": str(out.relative_to(self.root)), "lines": len(lines),
                "bytes": out.stat().st_size}

    # ---------- receipt (本拍回执: 该方本拍外呼的结果全文, follow-up 只发路径) ----------
    def build_receipt(self, by: str, round_: int, beat: int | None = None) -> dict:
        by = by.capitalize()
        rows = [r for r in self.rows() if r["kind"] in EVIDENCE_KINDS
                and r.get("round") == round_
                and (beat is None or r.get("beat") == beat)
                and (r.get("by") == by or _owner(r.get("by")) == ARBITER)]
        lines = []
        for r in rows:
            rec = self.obj(r["id"])
            line = {"id": rec["id"], "kind": r["kind"], "content": rec["content"]}
            if rec.get("meta"):
                line["meta"] = rec["meta"]
            lines.append(line)
        tag = f"R{round_}" + (f"_B{beat}" if beat else "")
        out = self.dir / f"_receipt_{tag}_{by.lower()}.jsonl"
        _write_jsonl(out, lines)
        return {"ok": True, "receipt": str(out.relative_to(self.root)), "results": len(lines),
                "ids": [r["id"] for r in rows]}

    # ---------- verify (体检: 双铸/覆盖/错位一律现形) ----------
    def verify(self) -> dict:
        rows = self.rows()
        errors: list[str] = []
        warnings: list[str] = []
        from collections import Counter
        dup_ids = [i for i, c in Counter(r["id"] for r in rows).items() if c > 1]
        if dup_ids:
            errors.append(f"manifest duplicate ids (double-minted): {dup_ids}")
        dup_seq = [s for s, c in Counter(r.get("seq") for r in rows).items() if c > 1]
        if dup_seq:
            warnings.append(f"manifest seq duplicated (trace of a concurrency race; line order remains authoritative): {dup_seq}")
        for r in rows:
            p = self.dir / r["file"]
            if not p.exists():
                errors.append(f"{r['id']}: object file missing")
                continue
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"{r['id']}: object file unreadable ({exc})")
                continue
            if obj.get("id") != r["id"]:
                errors.append(f"{r['id']}: object file claims id {obj.get('id')!r} — overwritten")
            for key in ("by", "round"):
                if r.get(key) is not None and obj.get(key) != r.get(key):
                    errors.append(f"{r['id']}: object {key}={obj.get(key)!r} does not match manifest "
                                  f"{r.get(key)!r} — autopsy signature of a later write overwriting an earlier one under the same id")
        known = {r["file"] for r in rows}
        stray = [f.name for f in self.dir.glob("*.json")
                 if f.name not in known and not f.name.startswith("_")]
        orphans = [n for n in stray if ID_SHAPE.match(n)]
        junk = [n for n in stray if not ID_SHAPE.match(n)]
        if orphans:
            warnings.append(f"orphan object files (file without manifest entry, crash residue; ids already skipped automatically): {orphans}")
        if junk:
            warnings.append(f"non-object stray files (keep temporary files outside the store directory): {junk[:8]}"
                            + (f" …{len(junk)} in total" if len(junk) > 8 else ""))

        def resolve(path: Path) -> Path:
            for cand in (path,
                         path.with_name(path.name.replace(".jsonl", ".complete.jsonl")),
                         path.with_name(path.name.replace("_inflight", "_complete"))):
                if cand.exists():
                    return cand
            return path

        db = _read_jsonl(resolve(self.debate_buf))
        eb = _read_jsonl(resolve(self.evidence_buf))
        m_finals = sum(1 for r in rows if r["kind"] == "final")
        m_ev = sum(1 for r in rows if r["kind"] in EVIDENCE_KINDS)
        b_finals = sum(1 for r in db if r.get("role") in ROLES)
        b_ev = sum(1 for r in eb if not any(k in r for k in ("arbiter_notes", "provisional_ruling")))
        if m_finals != b_finals:
            errors.append(f"finals ledger misaligned: manifest {m_finals} vs debate buffer {b_finals}")
        if m_ev != b_ev:
            errors.append(f"evidence ledger misaligned: manifest {m_ev} vs evidence buffer {b_ev} — EV ordinal contract broken")
        return {"ok": not errors, "objects": len(rows), "errors": errors, "warnings": warnings}

    def complete_buffers(self) -> list[str]:
        """收尾: buffer 改名 .complete.jsonl(保留作复盘日志, 不删); 对象仓目录保留。"""
        done = []
        for buf in (self.debate_buf, self.evidence_buf, self.private_buf):
            if buf.exists():
                dst = buf.with_name(buf.name.replace(".jsonl", ".complete.jsonl"))
                buf.rename(dst)
                done.append(str(dst.relative_to(self.root)))
        return done


# ---------------- CLI ----------------

def _cli_store(args) -> Store:
    root = Path(args.root) if args.root else ROOT
    return Store(args.symbol, args.ts, root)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol")
    common.add_argument("--ts")
    common.add_argument("--root", default=None)

    p = sub.add_parser("put", parents=[common])
    p.add_argument("src", help="JSON 内容文件(- = stdin)")
    p.add_argument("--kind", required=True, choices=[k for k in KINDS if k != "model_call_evidence"])
    p.add_argument("--by", choices=["Zealot", "Reaper", "Arbiter"])
    p.add_argument("--round", dest="round_", type=int, help="1=Init, 2=Round 1, ...; objection 用异议回合号")
    p.add_argument("--beat", type=int, help="拍号(中间拍必带; 外呼结果带上便于回执分拍)")
    p.add_argument("--status", choices=["verified", "unverified", "not_found", "failed"])
    p.add_argument("--tier", dest="suggested_evidence_tier")
    p.add_argument("--model")
    p.add_argument("--issue")
    p.add_argument("--ev-kind", dest="ev_kind", choices=["search", "data"])
    p.add_argument("--request", help="模型调用申报原文 JSON 文件(辩手 request_evidence 那条, 原样)")
    p.add_argument("--ruling-version", dest="ruling_version", type=int, help="signoff 用: 所签再定稿版本")

    v = sub.add_parser("view", parents=[common])
    v.add_argument("--role", required=True)
    v.add_argument("--signoff", action="store_true")
    v.add_argument("--objection", type=int, default=None, help="异议回合号")

    c = sub.add_parser("receipt", parents=[common])
    c.add_argument("--by", required=True)
    c.add_argument("--round", dest="round_", type=int, required=True)
    c.add_argument("--beat", type=int, default=None)

    sub.add_parser("verify", parents=[common])

    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    if not args.cmd:
        ap.error("command required (put/view/receipt/verify) or --self-test")
    if not args.symbol or not args.ts:
        ap.error("--symbol and --ts are required")
    store = _cli_store(args)
    try:
        if args.cmd == "put":
            raw = sys.stdin.read() if args.src == "-" else Path(args.src).read_text(encoding="utf-8")
            content = json.loads(raw)
            meta = {k: getattr(args, k) for k in ("status", "suggested_evidence_tier", "model", "issue")
                    if getattr(args, k, None)}
            if args.ev_kind:
                meta["kind"] = args.ev_kind
            if args.request:
                meta["model_call_request"] = json.loads(Path(args.request).read_text(encoding="utf-8"))
            if args.ruling_version:
                meta["ruling_version"] = args.ruling_version
            receipt = store.put(args.kind, content, by=args.by, round_=args.round_, meta=meta, beat=args.beat)
        elif args.cmd == "view":
            receipt = store.build_view(args.role, signoff=args.signoff, objection_round=args.objection)
        elif args.cmd == "verify":
            receipt = store.verify()
        else:
            receipt = store.build_receipt(args.by, args.round_, args.beat)
    except (StoreError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(receipt, ensure_ascii=False))
    if receipt.get("ok") is False:
        raise SystemExit(1)


# ---------------- self test ----------------

def role_record(score: int, cid: str, stake: int = 4, moves=None, status: str = "final") -> dict:
    """新格式(formats/debate.md)的最小合法辩论轮记录; 测试与自检共用。"""
    return {"analysis_text": f"报告体 {cid}", "stance_ledger": {"moves": moves or [], "held": [],
                                                               "contingent": [], "reclaims": []},
            "claims": [{"id": cid, "kind": "own", "claim": "c", "evidence_tier": "T3", "stake": stake,
                        "status": "open"}],
            "short_term_path": {"direction": "上行", "target": "110", "invalid": "95", "horizon_days": 5,
                                "rationale": "r"},
            "mentality": [{"name": "buy_the_dip", "weight": 1.0}],
            "operation_type": "HOLD_NEUTRAL", "operation_target": "100", "operation_size": "N/A",
            "composite_score": score, "summary_statement": "s", "request_evidence": [],
            "beat": {"status": status, "next_is_final": True}}


def _self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        s = Store("TEST", "20260930T000000Z", root)
        # round 1: beat -> evidence/model_call -> finals -> notes/private
        s.put("beat", role_record(60, "Z1", status="need_more"), by="Zealot", round_=1, beat=1)
        r_ev = s.put("evidence", {"question": "查A", "simple": "A 成立 12.3%", "verdict": "supports",
                                  "detailed": "D" * 2000, "source": ["src"]},
                     by="Zealot", round_=1, beat=1, meta={"status": "verified", "kind": "search"})
        assert r_ev["id"] == "Z.EV1", r_ev
        r_mc = s.put("model_call", {"model_call_id": "R.MC1", "model": "P02", "issue": "Z1×R1",
                                    "finding": "拥挤度中位, 阈值 1.5 实测 1.2", "verdict": "refutes",
                                    "steps": [{"kind": "data", "what": "bars", "result": "L" * 3000}],
                                    "payload_text": "P" * 3000, "suggested_evidence_tier": "T2"},
                     by="Reaper", round_=1, beat=1,
                     meta={"status": "verified", "model": "P02", "issue": "Z1×R1",
                           "suggested_evidence_tier": "T2",
                           "model_call_request": {"type": "model_call", "model": "P02", "issue": "Z1×R1"}})
        assert r_mc["id"] == "R.MC1" and r_mc["split_detail"] == "R.MC1.EV1", r_mc
        mc_obj = s.obj("R.MC1")
        assert "steps" not in mc_obj["content"] and "payload_text" not in mc_obj["content"]
        assert s.obj("R.MC1.EV1")["content"]["payload_text"] == "P" * 3000
        a_ev = s.put("evidence", {"question": "事件兜底", "simple": "无", "detailed": "d", "source": ["s"],
                                  "verdict": "not_found"}, by="Arbiter", round_=1, beat=1,
                     meta={"status": "not_found", "kind": "search"})
        assert a_ev["id"] == "A.EV1", a_ev
        s.put("final", role_record(62, "Z1"), by="Zealot", round_=1)
        s.put("final", role_record(40, "R1"), by="Reaper", round_=1)
        s.put("notes", {"tally": "[账本整理机械行] ...", "arbiter_notes": "本轮无裁定"}, round_=1)
        s.put("private_score", {"composite_score": 55, "operation_type": "HOLD_NEUTRAL",
                                "mentality": "wait_for_confirmation", "moved_by": [], "note": "私有"}, round_=1)
        # duplicate id refused
        try:
            s.put("final", role_record(62, "Z1"), by="Zealot", round_=1)
            raise AssertionError("duplicate final must be refused")
        except StoreError:
            pass
        # invalid final (old field names) refused before minting
        try:
            s.put("final", {"action": 60, "analysis_text": "x", "pillars": []}, by="Reaper", round_=2)
            raise AssertionError("invalid final must be refused")
        except StoreError:
            pass
        # need_more content cannot be archived as final
        try:
            s.put("final", role_record(60, "R1", status="need_more"), by="Reaper", round_=2)
            raise AssertionError("need_more beat must not be archived as final")
        except StoreError:
            pass
        # round 2 + provisional/proposed/objection/ruling/signoff
        s.put("final", role_record(65, "Z1"), by="Zealot", round_=2)
        s.put("final", role_record(42, "R1"), by="Reaper", round_=2)
        s.put("notes", {"tally": "t2", "arbiter_notes": "n2"}, round_=2)
        s.put("private_score", {"composite_score": 56, "moved_by": []}, round_=2)
        s.put("provisional", {"claims_review": [], "composite_score": 58, "operation_type": "HOLD_NEUTRAL",
                              "mentality": "wait_for_confirmation", "rationale": "r"})
        s.put("proposed", {"composite_score": 58, "operation_type": "HOLD_NEUTRAL", "statement": "x"})
        # debater view: own EV full, MC conclusion in reaper's appendix, no grandchild/beat/private
        s.build_view("zealot")
        zlines = _read_jsonl(s.dir / "_view_zealot.jsonl")
        kinds = [(l.get("kind"), l.get("id")) for l in zlines]
        assert ("final", "R1.Z") in kinds and ("notes", "NOTES.R1") in kinds, kinds
        assert not any(l["kind"] in ("beat", "private_score", "provisional", "proposed",
                                     "model_call_evidence") for l in zlines), kinds
        own_ev = next(l for l in zlines if l.get("id") == "Z.EV1")
        assert own_ev["own"] and own_ev["content"]["detailed"] == "D" * 2000
        r1r = next(l for l in zlines if l.get("id") == "R1.R")
        assert r1r["appendix"][0]["id"] == "R.MC1" and "steps" not in json.dumps(r1r["appendix"])
        assert not any(l.get("id") == "R.MC1" and l.get("own") for l in zlines)  # 对手的孩子不单列
        assert any(l.get("id") == "A.EV1" for l in zlines)  # 仲裁者补查双方共享结论段
        assert not any("[ARB" in json.dumps(l, ensure_ascii=False) for l in zlines)
        # receipt per beat
        rc = s.build_receipt("Reaper", 1, 1)
        rlines = _read_jsonl(root / rc["receipt"])
        assert [l["id"] for l in rlines] == ["R.MC1", "A.EV1"] and "steps" not in json.dumps(rlines[0]), rlines
        # objection view + objections
        s.build_view("zealot", objection_round=1)
        ov = _read_jsonl(s.dir / "_view_objection1_zealot.jsonl")
        assert ov[0]["id"] == "R2.Z" and ov[1]["kind"] == "proposed" and "file" in ov[1], ov
        s.put("objection", {"objections": [{"target": "operation_target", "kind": "omission",
                                            "statement": "s", "impact": "i", "refs": ["Z1"]}],
                            "replies": [], "needs_new_evidence": []}, by="Zealot", round_=1)
        s.put("objection", {"objections": [], "replies": [], "needs_new_evidence": []}, by="Reaper", round_=1)
        s.build_view("reaper", objection_round=2)
        ov2 = _read_jsonl(s.dir / "_view_objection2_reaper.jsonl")
        assert [l["id"] for l in ov2 if l["kind"] == "objection"] == ["OBJ1.Z", "OBJ1.R"], ov2
        # signoff needs a ruling
        try:
            s.put("signoff", {"reservation": "x", "dissent_refs": [], "endorsed_composite_score": 63},
                  by="Zealot")
            raise AssertionError("signoff before ruling must be refused")
        except StoreError:
            pass
        rr = s.put("ruling", {"composite_score": 59, "operation_type": "HOLD_NEUTRAL",
                              "objection_dispositions": [{"id": "OBJ1.Z", "disposition": "rejected", "reason": "r"}]})
        assert rr["id"] == "RULING.v1", rr
        s.build_view("zealot", signoff=True)
        sv = _read_jsonl(s.dir / "_view_signoff_zealot.jsonl")
        assert sv[0]["id"] == "R2.Z" and sv[1]["id"] == "OBJ1.Z" and sv[2]["kind"] == "ruling", sv
        rf = json.loads((root / sv[2]["file"]).read_text(encoding="utf-8"))
        assert rf["composite_score"] == 59 and "\n" in (root / sv[2]["file"]).read_text(encoding="utf-8")
        sg = s.put("signoff", {"reservation": "z 保留", "dissent_refs": ["Z1"], "endorsed_composite_score": 63},
                   by="Zealot")
        assert sg["id"] == "SIGN1.Z" and s.obj("SIGN1.Z")["meta"]["ruling_version"] == 1, sg
        # legacy chain: derived buffers feed the assembler with new keys
        from assemble_debate_artifacts import assemble
        draft = root / "draft.json"
        draft.write_text(json.dumps({"composite_score": 59, "operation_type": "HOLD_NEUTRAL"}), encoding="utf-8")
        receipt = assemble(draft, s.debate_buf, s.evidence_buf, root / "A.json", root / "E.json",
                           private_buffer_path=s.private_buf)
        assert receipt["history_records"] == 4 and receipt["model_call_records"] == 1, receipt
        assert receipt["evidence_records"] == 3 and receipt["private_score_records"] == 2, receipt
        analysis = json.loads((root / "A.json").read_text(encoding="utf-8"))
        assert analysis["model_calls"][0]["model_call_id"] == "R.MC1"
        assert analysis["model_calls"][0]["evidence_refs"] == ["EV2"]  # 位次=铸造号, 语义对齐
        assert analysis["model_calls"][0]["request"]["type"] == "model_call"
        assert "P" * 3000 in json.dumps(analysis["evidence"])    # 重细节照旧全文进档
        assert analysis["arbiter_private_scores"][0]["composite_score"] == 55
        assert analysis["provisional_ruling"]["composite_score"] == 58
        assert analysis["ledger_score_z"] == 62 and analysis["ledger_score_r"] == 40  # 开局分, 无结算
        # verify: 干净场全绿
        v = s.verify()
        assert v["ok"], v
        # verify: 人为制造"同号覆盖"与"账目错位", 必须现形
        ev1 = json.loads((s.dir / "Z.EV1.json").read_text(encoding="utf-8"))
        ev1["by"] = "Reaper"
        (s.dir / "Z.EV1.json").write_text(json.dumps(ev1, ensure_ascii=False), encoding="utf-8")
        v2 = s.verify()
        assert not v2["ok"] and any("overwrit" in e for e in v2["errors"]), v2
        ev1["by"] = "Zealot"
        (s.dir / "Z.EV1.json").write_text(json.dumps(ev1, ensure_ascii=False), encoding="utf-8")
        lines = s.evidence_buf.read_text(encoding="utf-8").splitlines()
        s.evidence_buf.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        v3 = s.verify()
        assert not v3["ok"] and any("misaligned" in e for e in v3["errors"]), v3
        try:
            s.put("evidence", {"question": "x", "simple": "y", "detailed": "z", "source": ["s"]},
                  by="Zealot", round_=2, meta={"status": "verified"})
            raise AssertionError("alignment guard must refuse puts on a misaligned store")
        except StoreError:
            pass
        s.evidence_buf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert s.verify()["ok"]
        # 孤儿位次自愈
        n_rev = sum(1 for r in s.rows() if r["kind"] == "evidence" and r.get("by") == "Reaper")
        orphan_id = f"R.EV{n_rev + 1}"
        (s.dir / f"{orphan_id}.json").write_text('{"id": "%s", "kind": "evidence"}' % orphan_id, encoding="utf-8")
        r_heal = s.put("evidence", {"question": "heal", "simple": "复用号", "detailed": "d", "source": ["s"]},
                       by="Reaper", round_=2, meta={"status": "verified"})
        assert r_heal["id"] == orphan_id, r_heal
        assert list(s.dir.glob(f"_orphan_{orphan_id}_*.json")), "residue must be displaced, not overwritten"
        assert s.verify()["ok"]
        done = s.complete_buffers()
        assert len(done) == 3 and all(d.endswith(".complete.jsonl") for d in done), done
        assert s.verify()["ok"]
    # 并发锤测: 8 个真子进程同时 put evidence, 编号唯一连续, manifest 序与 buffer 序对齐
    import subprocess as _sp
    with tempfile.TemporaryDirectory() as td2:
        procs = []
        for i in range(8):
            src = Path(td2) / f"c{i}.json"
            src.write_text(json.dumps({"question": f"q{i}", "simple": f"r{i} 数值 {i}",
                                       "detailed": "d" * 60, "source": ["s"]}), encoding="utf-8")
            procs.append(_sp.Popen([sys.executable, str(Path(__file__).resolve()), "put",
                                    "--symbol", "RACE", "--ts", "20260918T000000Z",
                                    "--root", td2, "--kind", "evidence",
                                    "--by", "Zealot" if i % 2 else "Reaper", "--round", "1",
                                    "--status", "verified", str(src)],
                                   stdout=_sp.PIPE, stderr=_sp.PIPE))
        outs = [p.communicate(timeout=120) for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[1][:200] for o in outs]
        rs = Store("RACE", "20260918T000000Z", Path(td2))
        ids = [r["id"] for r in rs.rows()]
        seqs = [r["seq"] for r in rs.rows()]
        assert sorted(ids) == sorted([f"Z.EV{n}" for n in range(1, 5)] + [f"R.EV{n}" for n in range(1, 5)]), ids
        assert len(set(seqs)) == 8, seqs
        buf_rows = _read_jsonl(rs.evidence_buf)
        assert [json.loads((rs.dir / f"{i}.json").read_text(encoding="utf-8"))["content"]["question"]
                for i in ids] == [r["content"]["question"] for r in buf_rows]
    print(json.dumps({"ok": True, "self_test": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
