# How the Claude Code engine runs postcheck

For the Claude Code harness only. The flow and disciplines are in the parent `SKILL.md`; the scoring spec is in `orchestration/postcheck-protocol.md`.
Never launch Codex.

## Signature

The judgment JSON's `reckoner` = the value of the sentence "The exact model ID is …" in your runtime environment, with the bracketed suffix removed.

## Two ways to run

- **(A) Act as the reckoner yourself**: a single session runs linearly; suitable for small numbers when you want to chat while running. Subject to the session budget below. Each batch takes
  the next three to five from the top of the worklist; after finishing, look at the worklist again and take more.
- **(B) Headless loop**: one brand-new `claude -p "/postcheck …"` subprocess per report acts as the reckoner and exits when done; context does not accumulate;
  the parent only assigns work and writes back the signature using the real model usage from the subprocess receipt. Use it for backfilling history in bulk or when a single session may not hold up:
  ```bash
  cd /opt/equipoise && ./venv/bin/python skills/equipoise-postcheck/scripts/cc_loop.py \
    --dt 3 --start 2026-06-18 --universe us_all --limit 5      # add --dry-run first to see which ones would run
  ```
  `--limit` controls the batch size (about 0.7 to 1.7 USD per report); `--jobs N` concurrency; `--no-redo` runs only brand-new ones and does not touch the old-engine rows to replace.

## Session budget (way A)

Measured cost per report: a light one (no check needed, no search, no deep read) about six to eight thousand tokens; a heavy one (reading the full debate, searching, long attribution) about fifteen to eighteen thousand.
Tokens are not the bottleneck; the real limit is that judgment dulls as the context grows long. **Self-limit by quality**: twelve to fifteen per session is safe, the hard ceiling is about thirty
but quality slides in the later part. A normal day of US single names plus ETFs is about twenty-five to thirty reports, roughly two sessions. When stopping, tell the user how many were done and
how many remain; a reopened session just says "continue postcheck".
