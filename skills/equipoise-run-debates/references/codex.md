# How the Codex engine runs debates

For the Codex harness only. The shared flow, exit codes, three disciplines and acceptance are in the parent `SKILL.md`; this page only states the facts of the Codex channel.
Never launch `claude`, `claude -p` or `cc_loop.py`.

## Formal runs always go to the background

- All formal debates must run in a background service detached from SSH and the interactive terminal. **Running a non-`--dry-run`
  `codex_loop.py` directly in the conversation foreground is forbidden**; the only exceptions are `--dry-run`, read-only status checks, and short smokes.
- Use `systemd-run` to start a transient service, set `WorkingDirectory=/opt/equipoise`, pass `EQUIPOISE_IBKR_PORT=4001`
  and `PATH` explicitly (the runner needs to call `/root/.local/bin/codex`). SSH, the conversation UI or the user's local network disconnecting must not terminate the task.
- **One independent service per symbol**: a single failure, timeout or rerun does not drag down the others, and logs are separate.
- Unit name `equipoise-codex-<symbol-lower>-<YYYYMMDD-HHMMSS>`. Before starting, check whether the same symbol has an active service or a running
  `codex_loop.py` process.

## Two identities

The server has three Codex identities (codex1 and codex3 are the main ones, codex2 is kept for postchecks; each with its own executable, `CODEX_HOME` and quota; never run at the same time). Specify explicitly with
`--harness codex1|codex2|codex3`; by default it reads the environment variable `EQUIPOISE_HARNESS`, and failing that codex1. **Do not guess the identity from `CODEX_HOME`.**

## Single symbol

```bash
EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py <SYM>     # prepare data first; feed only when READY
systemd-run --unit=equipoise-codex-<sym-lower>-<YYYYMMDD-HHMMSS> \
  --property=WorkingDirectory=/opt/equipoise \
  --setenv=EQUIPOISE_IBKR_PORT=4001 \
  --setenv=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /opt/equipoise/venv/bin/python \
  /opt/equipoise/skills/equipoise-run-debates/scripts/codex_loop.py --symbols <SYM> --harness codex1
  Follow-up (delta) session on an earlier session of the same symbol (orchestration/run-protocol.md §7; config/delta.yaml):
  add `--base <previous TS> --why "<one line>"` (one symbol only); the launcher relays it to `run_step open` through the environment.
```
Manual pooling: prepare data symbol by symbol serially, feed only the READY ones, stagger adjacent service starts by a few seconds, queue when the pool is full, feed one as one finishes.

## Batch: use the dispatcher

The dispatcher is itself designed to run as a detached service, with all three disciplines built in (prepare the first pool serially, prepare one as one slot is released, failures go to the
back of the queue with one retry, no synchronization barrier).

```bash
./venv/bin/python skills/equipoise-run-debates/scripts/us_batch_dispatcher.py --dry-run
./venv/bin/python skills/equipoise-run-debates/scripts/jpkr_batch_dispatcher.py --dry-run
./venv/bin/python skills/equipoise-run-debates/scripts/jpkr_batch_dispatcher.py --markets eu --dry-run
```
Common parameters: `--harness` · `--max-parallel` (default 8; the user's value prevails when specified) · `--prepare-stagger-seconds` (default 5) ·
`--stagger-seconds` (default 15) · `--timeout` · `--prepare-timeout` · `--force-prepare` · `--max-retries` ·
`--markets jp,kr|eu` (regional version) · `--symbols` (only for manual exception overrides).

## Models

The main session and the two debaters use the judgment model chosen by the runner (`--model`; each harness resolves its own model policy),
Scout and Runner use the executor model `gpt-6-luna`. Details in `.codex/run.md`.

## Acceptance (the Codex-specific part)

- After starting, report the unit name, and confirm it has entered the running state with `systemctl show <UNIT>` and `journalctl -u <UNIT>`.
- Completion is judged by `data/status/codex_run_<SYM>_*.json`, the Analysis and Evidence files, and the store receipt, not by the systemd exit code alone.
  A store failure does not make the session a failure; the receipt carries `pg_warnings`, and rerunning `scripts/pg_store_debate.py <SYM>` is enough.
- When the harness ends the main session early, the runner automatically starts a relay subprocess (`--relay-legs`), resuming from the object store without reopening the session.
- `--calc-check remind|off|always`: the level of the arithmetic prompt, one per batch. Default `remind` (only reminds that unit conversion is error-prone; does not require running commands);
  `off` adds no intervention; `always` computes every self-derived number with a local command down to the final result with units (steadier; each task costs a few dozen seconds more).
  Executors and debaters see only the sentence for the current level; the wording is in `prompts/agent/roles/calc-lines.md`.

## Smoke and probes

```bash
./venv/bin/python skills/equipoise-run-debates/scripts/codex_smoke.py --dry-run
./venv/bin/python skills/equipoise-run-debates/scripts/codex_smoke.py                  # can it spawn subagents
./venv/bin/python skills/equipoise-run-debates/scripts/codex_search_probe.py --dry-run # length and shape of search answers (drop --dry-run for a real run)
./venv/bin/python skills/equipoise-run-debates/scripts/codex_search_agent_smoke.py     # the executor's ability to dispatch an ad-hoc search agent
```
Output goes to `data/status/`. When the smoke returns `spawn_available=false`, do not launch a formal run; report to the user.

### Automatic version selection for codex1/codex2/codex3 (2026-11-18)

The owner authorized `latest:sol` for duty/debate and `latest:astra` for postcheck.
Each identity has `equipoise_models.json` in its own CODEX_HOME; resolution lives in
`workflows/codex_models.py`. All three identities use this policy (owner authorized 2026-11-18). Visible stable numeric
model slugs in its own `models_cache.json` are ordered numerically within the chosen
family. The cache must be no older than 24 hours; missing/stale/invalid data stops
launching, without an older-model fallback. This is the latest version known to the
local Codex cache, not a live release lookup. Codex is responsible for refreshing that cache.
Each batch resolves once and passes the exact model through to every child and relay.
Both dispatchers and both Codex runners accept `--model` to pin an explicit version.
Scout/Runner/Translator remain on `gpt-6-luna`. Running processes are not migrated.
