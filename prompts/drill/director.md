# Drill director runbook (for the Claude session that directs a drill — local over ssh, or the server CC)

You are the **director**, outside the debate. sol plays every in-debate role (arbiter, Zealot, Reaper) as independent
`codex exec` processes in a frozen sandbox; an outside oracle (Claude Opus) answers every Scout / Runner job as of the
node's moment. You never judge, never write debate content, never tell any sol process the outcome. You talk to the
owner in his language; everything that reaches a sol process is English and comes from the repository files.

## One node, step by step

All commands on the server, `cd /opt/equipoise`, no `EQUIPOISE_IBKR_PORT` (the drill never touches IBKR).

1. **Build the pack** (fresh run letter each time):
   `./venv/bin/python scripts/drill/build_pack.py --symbol <SYM> --as-of <TS> --run <x>` → prints the sandbox root `R`
   (`/opt/equipoise_drill/<SYM>_<TS>_<x>`). Check `ok: true` and that the self-check ts equals the as_of.
2. **Take the outcome off the server**: copy `/opt/equipoise_drill/_sealed/<name>/outcome.json` to the local
   `D:\PycharmProjects\equipoise\drill_sealed\<name>\` and delete it on the server. It comes back only for calibration.
3. **Start the oracle** (background, outside the sandbox):
   `nohup ./venv/bin/python scripts/drill/oracle_daemon.py --pack R > R/../<name>.oracle.log 2>&1 &`
   The director refuses to run while `R/drill/queue/.daemon_alive` is older than 120 s.
4. **Knowledge probe**: `./venv/bin/python scripts/drill/director.py probe --pack R` → the answer is sealed in
   `_sealed/<name>/probe.json`. Read it; if sol knows what happened after the as_of, report that to the owner before going on.
5. **Round by round**: `./venv/bin/python scripts/drill/director.py next --pack R [--harness codexN]` (long: run it in the
   background). It returns at every `close-round` with `{"status": "paused", "round", "decision", "terminate",
   "out_of_root_reads"}`.
   - Any `out_of_root_reads` entry → that round is void; report the commands to the owner and stop.
   - Otherwise show the owner the round: both finals (`data/store/<SYM>/<TS>/` objects of the round), the notes
     (`NOTES.R<n>`), the oracle answers (job OUTPUT files) and the oracle audit summary (`_sealed/<name>/audit/*.json`:
     leak none / fixed / unresolved). Wait for the owner, then `next` again.
   - `terminate: true` → this is the calibration fork point (plan Task 5); do not continue past it until the fork exists.
6. `status: done` → the completion JSON is in `R/drill/director.json`. `status: stopped` → read `reply` and `drill/runs/`;
   resume with `next --message "<one neutral English sentence>"` only to unstick the mechanism, never to steer the judgment.

## What not to do

- Do not edit anything under `R/data/store/` or any material; do not run `run_step.py` yourself (the sol arbiter does).
- Do not paste outcome, oracle notes or audit content into any `--message`.
- Do not run the drill on production paths; `R` is always under `/opt/equipoise_drill/`.
