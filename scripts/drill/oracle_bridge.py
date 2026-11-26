"""Serve one Hetzner strict-pack queue with the local Gemini CLI via verified SSH.

python scripts/drill/oracle_bridge.py --host HOST --pack NAME [--once]
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tarfile
import tempfile
import time
from uuid import uuid4

import paramiko

import oracle_daemon as daemon


BASE = PurePosixPath("/opt/equipoise_drill")
STATIC = ("prompts/drill/oracle.md", "prompts/agent", ".claude/agents/scout.md",
          ".claude/agents/runner.md", "drill/archive.jsonl")


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"invalid pack or job name: {value!r}")
    return value


def read_text(sftp, path):
    with sftp.open(str(path), "rb") as stream:
        return stream.read().decode("utf-8")


def write_text(sftp, path, content, replace=False):
    tmp = str(path) + "." + uuid4().hex + ".tmp"
    with sftp.open(tmp, "wb") as stream:
        stream.write(content.encode("utf-8"))
    try:
        (sftp.posix_rename if replace else sftp.rename)(tmp, str(path))
    except OSError:
        sftp.remove(tmp)
        raise


def ensure_dir(sftp, path):
    try:
        sftp.mkdir(str(path), mode=0o700)
    except OSError:
        sftp.stat(str(path))
    if sftp.normalize(str(path)) != str(path):
        raise ValueError(f"unexpected directory target: {path}")


def mirror(client, root, pack, local):
    """Copy frozen files and role manuals. Never transfer store, queue, runs or sealed outputs."""
    files = pack["files"]
    for name in files:
        parts = PurePosixPath(name).parts
        if (len(parts) < 3 or parts[0] != "data" or parts[1] not in
                {"economic", "evidence_prep", "fear_greed", "fundamentals", "market_data", "news",
                 "nikkei_vi", "technical", "vkospi", "vstoxx"} or
                not all(re.fullmatch(r"[\w.+@^=-]+", part) and part not in {".", ".."} for part in parts)):
            raise ValueError(f"unexpected manifest input: {name}")
    # Keep the SSH command short and validate each returned data file against the manifest.
    data_dirs = sorted({"/".join(PurePosixPath(name).parts[:2]) for name in files})
    paths = shlex.join([*STATIC, *data_dirs])
    cmd = f"tar -C {shlex.quote(str(root))} -czf - -- {paths}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    stdin.channel.shutdown_write()
    seen = set()
    with tarfile.open(fileobj=stdout, mode="r|gz") as archive:
        for entry in archive:
            if entry.isdir():
                continue
            name = entry.name
            if (not entry.isfile() or (name not in files and name not in STATIC and
                                       not name.startswith("prompts/agent/"))):
                raise ValueError(f"unexpected archive entry: {name}")
            path = (local / name).resolve()
            path.relative_to(local.resolve())
            body = archive.extractfile(entry).read()
            if name in files and hashlib.sha256(body).hexdigest() != files[name]:
                raise ValueError(f"frozen hash mismatch: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            seen.add(name)
    if stdout.channel.recv_exit_status() or set(files) - seen:
        raise ValueError("incomplete pack mirror: " + stderr.read().decode(errors="replace")[-400:])


def retrieve(cfg, job, material, as_of, notes_dir):
    notes = notes_dir / (job.stem + ".md")
    answer, run = daemon._agy_job(cfg, job, material, as_of, notes)
    return {"model": cfg.model, "engine": "agy", "as_of": as_of, "ended": daemon._now(),
            "run": run, "answer": answer, "notes": notes.read_text(encoding="utf-8") if notes.exists() else ""}


def job_workspace(cache, local, job_rel, material, files):
    """Expose the requested frozen data and reference manuals to this job alone."""
    work = local / "material"
    for name in STATIC:
        source = cache / name
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
    for name in files:
        if name in material:
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / name, target)
    job = work / job_rel
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(material, encoding="utf-8")
    return job


def serve(client, sftp, root, local, once, timeout, idle, check_only=False, until_round=None):
    pack = json.loads(read_text(sftp, root / "drill/pack.json"))
    if not pack.get("strict_as_of") or pack["sealed"] != str(BASE / "_sealed" / root.name):
        raise ValueError("expected a strict drill pack and its matching external audit directory")
    store = root / "data/store" / component(pack["symbol"]) / component(pack["as_of"])
    queue = root / "drill/queue"
    audit = BASE / "_sealed" / root.name / "audit"
    cache = local / "frozen"
    cache.mkdir()
    mirror(client, root, pack, cache)
    if check_only:
        print(json.dumps({"pack": root.name, "as_of": pack["as_of"], "verified_files": len(pack["files"])}))
        return 0
    ensure_dir(sftp, queue)
    ensure_dir(sftp, audit)
    print(json.dumps({"pack": root.name, "queue_ready": True}), flush=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        last_job = time.monotonic()
        while time.monotonic() - last_job < idle:
            if until_round is not None:
                try:
                    state = json.loads(read_text(sftp, root / "drill/director.json"))
                except FileNotFoundError:
                    state = {}
                if (state.get("status") in {"done", "stopped"} or
                        (state.get("status") == "paused" and int(state["round"]) >= until_round)):
                    return 0
            write_text(sftp, queue / ".daemon_alive", daemon._now(), replace=True)
            names = set(sftp.listdir(str(queue)))
            for name in sorted(n for n in names if n.endswith(".req.json")):
                jid = component(name[:-9])
                if jid + ".done" in names:
                    continue
                if until_round is not None:
                    tagged = re.fullmatch(r"R(\d+)B\d+-\d+", jid)
                    if not tagged or int(tagged[1]) > until_round:
                        raise ValueError(f"request is outside the configured round limit: {jid}")
                req = json.loads(read_text(sftp, queue / name))
                job, out = store / f"_job_{jid}.md", store / f"_out_job_{jid}.json"
                if (req["job_id"], req["material"], req["output"]) != (jid, str(job), str(out)):
                    raise ValueError(f"queue request does not match its store paths: {jid}")
                if sftp.normalize(str(job)) != str(job) or sftp.normalize(str(store)) != str(store):
                    raise ValueError(f"unexpected job path: {jid}")
                receipt = audit / (jid + ".bridge.json")
                # Do not spend a model call on a request already settled by its timeout handler.
                if out.name in sftp.listdir(str(store)):
                    write_text(sftp, queue / (jid + ".done"), daemon._now())
                    continue
                try:
                    result = json.loads(read_text(sftp, receipt))
                except FileNotFoundError:
                    if jid + ".claimed" in names:
                        continue
                    try:
                        with sftp.open(str(queue / (jid + ".claimed")), "xw"):
                            pass
                    except OSError:
                        if jid + ".claimed" in sftp.listdir(str(queue)):
                            continue
                        raise
                    material = read_text(sftp, job)
                    with tempfile.TemporaryDirectory(prefix="equipoise-scout-") as temp:
                        job_dir = Path(temp)
                        job_local = job_workspace(cache, job_dir, job.relative_to(root), material, pack["files"])
                        cfg = daemon.Config(root=job_dir / "material", cmd=["agy"], model=daemon.NEON_MODEL,
                                            engine="agy", timeout=timeout)
                        future = executor.submit(retrieve, cfg, job_local, material, pack["as_of"], job_dir)
                        while not future.done():
                            write_text(sftp, queue / ".daemon_alive", daemon._now(), replace=True)
                            time.sleep(3)
                        result = future.result()
                    write_text(sftp, receipt, json.dumps(result, ensure_ascii=False))
                # A requester can time out while the provider runs. Its existing result wins.
                try:
                    write_text(sftp, out, json.dumps(result["answer"], ensure_ascii=False))
                except OSError:
                    sftp.stat(str(out))
                if jid + ".done" not in sftp.listdir(str(queue)):
                    write_text(sftp, queue / (jid + ".done"), daemon._now())
                actual = json.loads(read_text(sftp, out))
                print(json.dumps({"job": jid, "model": result["model"], "rc": result["run"]["rc"],
                                  "status": actual.get("status", "answered")}), flush=True)
                if once:
                    return 1 if actual.get("status") == "failed" else 0
                last_job = time.monotonic()
            time.sleep(3)
    return 2 if once else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true", help="Verify the local material mirror and exit before serving jobs")
    parser.add_argument("--until-round", type=int, help="Exit once the director pauses after this round")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--idle", type=int, default=300)
    args = parser.parse_args()
    root = BASE / component(args.pack)
    with paramiko.SSHClient() as client:
        client.load_system_host_keys(str(Path.home() / ".ssh/known_hosts"))
        client.connect(args.host, username=args.user, timeout=15, banner_timeout=15, auth_timeout=15)
        with client.open_sftp() as sftp, tempfile.TemporaryDirectory(prefix="equipoise-oracle-") as tmp:
            return serve(client, sftp, root, Path(tmp), args.once, args.timeout, args.idle, args.check, args.until_round)


if __name__ == "__main__":
    raise SystemExit(main())
