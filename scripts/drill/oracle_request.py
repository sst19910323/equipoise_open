#!/usr/bin/env python3
"""oracle_request.py — 演习里 Scout / Runner 任务的替身: 把任务排进队列, 等沙箱外的 oracle 守护进程写回结果。

    ./venv/bin/python scripts/drill/oracle_request.py --job <_job_*.md> --out <_out_job_*.json>

在沙箱根里跑(沙箱里有 drill/pack.json 时 run_step 的回执给出这条命令)。阻塞到守护进程写下 <id>.done; 超时就按
formats/evidence.md 的失败形状写 OUTPUT(协议 §2.3: 执行者掉线 = failed, 不编造), 退出码都是 0, 最后打印 OUTPUT 路径。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 1500


def _abs(root: Path, p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else root / p


def job_id_of(job: Path) -> str:
    m = re.match(r"_job_(.+)\.md$", job.name)
    if not m:
        raise ValueError(f"not a job material file: {job.name}")
    return m.group(1)


def queue_dir(root: Path) -> Path:
    return root / "drill" / "queue"


def failed_shape(material: str, reason: str) -> dict:
    if "TASK: model_call" in material:
        call = re.search(r"^MODEL_CALL_ID:\s*(\S+)", material, re.M)
        model = re.search(r'"model"\s*:\s*"([^"]+)"', material)
        return {"model_call_id": call.group(1) if call else "", "model": model.group(1) if model else "",
                "status": "failed", "reason": reason}
    return {"status": "failed", "reason": reason}


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def enqueue(root: Path, job: Path, out: Path) -> Path:
    job, out = _abs(root, job), _abs(root, out)
    q = queue_dir(root)
    q.mkdir(parents=True, exist_ok=True)
    jid = job_id_of(job)
    req = q / f"{jid}.req.json"
    if not req.exists():
        _atomic_write(req, json.dumps({"job_id": jid, "material": str(job), "output": str(out),
                                       "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    return req


def request(root: Path, job: Path, out: Path, timeout: float = TIMEOUT_S, poll: float = 2.0) -> int:
    job, out = _abs(root, job), _abs(root, out)
    enqueue(root, job, out)
    done = queue_dir(root) / f"{job_id_of(job)}.done"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if done.exists() and out.exists():
            print(out)
            return 0
        time.sleep(poll)
    _atomic_write(out, json.dumps(failed_shape(job.read_text(encoding="utf-8"),
                                               f"drill oracle did not answer within {int(timeout)} s"), ensure_ascii=False))
    print(out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    a = ap.parse_args(argv)
    return request(ROOT, Path(a.job), Path(a.out), a.timeout)


if __name__ == "__main__":
    sys.exit(main())
