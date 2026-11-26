# How the Codex engine runs postcheck

For the Codex harness only. The flow and disciplines are in the parent `SKILL.md`; the scoring spec is in `orchestration/postcheck-protocol.md`.
Never launch Claude or the CC driver; inside each report's reckoner, spawn no subagents.

## Signature (required)

The judgment JSON's `reckoner` **must be the model version the current process is actually running**: the parent layer has saved this command line's startup output to a temporary file and given its path in
the prompt; the `model:` field in the startup header is the exact value. Do not copy the example, do not use a fixed default, do not guess; if it cannot be confirmed, state plainly in the receipt that
the signature cannot be confirmed and **do not store**. It must never be omitted — the default in `postcheck_store.py` is a `claude*` signature, so omitting it records your work as
done by Claude and pollutes the worklist's attribution.

## Single report

One independent Codex main session is the sole reckoner, directly executing `prep` → judgment → `save`. If running the script directly hits the known empty
`OperationalError`, run the same entry point with `runpy` — this is only an entry-point detour; it does not skip `prep`, the digest, the raw-material review or `save`.

## Batch

`skills/equipoise-postcheck/scripts/codex_loop.py` starts one brand-new, stateless `codex exec` main session per report; the parent only maintains the rolling pool and takes no part in judgment.
Formal batches do not run bare in the conversation foreground; carry them in a transient service:

```bash
systemd-run --unit=equipoise-postcheck-codex-d3-<UTCSTAMP> \
  --property=WorkingDirectory=/opt/equipoise \
  --setenv=EQUIPOISE_IBKR_PORT=4001 \
  /opt/equipoise/venv/bin/python \
  /opt/equipoise/skills/equipoise-postcheck/scripts/codex_loop.py \
  --dt 3 --universe all --limit 16 --jobs 16 --harness codex1
```
Default universe `all`, `--limit 16`, `--jobs 16` (the debate pool defaults to 8, postcheck 16; set by the owner 2026-10-08). The user saying "run N" maps to `--limit N`, a specified concurrency maps to `--jobs N`,
"run all" uses `--limit 0`. The pool refills one as one finishes; a single failure does not block the rest; after it ends, re-take the remaining list from PG or the worklist as the authority.
`--harness codex1|codex2|codex3` selects the identity (by default reads `EQUIPOISE_HARNESS`, failing that codex1; do not guess from `CODEX_HOME`).

## Completion check

After `save`, confirm via the worklist or PG that the `(symbol, time, tier)` has been covered by an agent reckoner (signature prefix `gpt*` / `codex*`
or `claude*`).

## Trust boundary

The parent dispatch layer and the independent sessions started by `codex_loop.py` both run in the trusted execution environment and can connect to PG and IBKR. The runner must use an execution mode with network and database write access;
do not feed a single-report task into an offline or read-only sandbox.
