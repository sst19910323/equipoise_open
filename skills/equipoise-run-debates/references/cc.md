# How the Claude Code engine runs debates

For the Claude Code harness only. The shared flow, exit codes, three disciplines and acceptance are in the parent `SKILL.md`; this page only states the facts of the CC channel.
Never launch Codex.

## Single symbol

```bash
cd /opt/equipoise && EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py <SYM> \
  && claude -p "/run <SYM>" --model opus --permission-mode dontAsk --output-format json \
  && ./venv/bin/python scripts/pg_store_debate.py <SYM>
```
`claude -p "/run <SYM>"` is a complete Claude Code main session: it reads `.claude/commands/run.md`, runs the full session as the arbiter,
and exits when done; context does not accumulate. The debaters' only tools are Read and Glob; searching is done by the neutral executors.

## Batch: cc_loop

```bash
./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --market jp --dry-run          # orchestration only
./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --symbols A,B                  # serial
./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --symbols A,B,C --parallel --maxpar 3
./venv/bin/python skills/equipoise-run-debates/scripts/cc_loop.py --market us                    # the whole market
```
cc_loop has two stages: **the whole batch is prepared serially first, then the READY ones are fired rate-limited by `--maxpar` and staggered by `--stagger-seconds`**;
each symbol is stored as soon as it succeeds. Data-preparation `exit 10` aborts the whole batch (cc_loop itself also exits 10); those already finished are in the database.
The grouping of `--market` only selects symbols and does not imply ordering. Serial by default is for quota reasons (preventing several `claude -p` from burning through the rolling limit at once).
`--calc-check remind|off|always`: the level of the arithmetic prompt, one per batch, default `remind` (reminder only). For a manual single-symbol run, prefix the command with
`EQUIPOISE_CALC_CHECK=off` or `always`. This engine's debaters have no tool that can run commands; under `always` the debaters still get the reminder sentence,
while the executors follow `always`. The wording is in `prompts/agent/roles/calc-lines.md`.

## Overnight batch

When the user wants to close the session: pass the IBKR gate first (if needed, get the restart and phone 2FA done before the user goes to sleep), then nohup:
```bash
cd /opt/equipoise && nohup env EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python \
  skills/equipoise-run-debates/scripts/cc_loop.py --market us \
  > logs/cc_loop_us_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
```
IBKR goes down in the middle of the night → clean shutdown (no 2FA push to disturb anyone); check the log in the morning and finish the rest. Give the user the log path when reporting.

## Permissions

In the root environment use `--permission-mode dontAsk` (`--dangerously-skip-permissions` is hard-refused under root); the Agent tool needs no authorization by default under dontAsk.

## Disconnection

After a `claude -p` subprocess is interrupted, run `claude -p "/run <SYM>"` again: `run_step.py open` returns a resume receipt for a session with existing objects,
and the main session continues from the last archived object in the object store. If the final report exists but was not stored, add only the store command.
