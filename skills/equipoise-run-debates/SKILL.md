---
name: equipoise-run-debates
description: >
  Run a single-symbol or regional GENERAL debate, a batch of independent debates, or a fixed same-region sector-group drill.
  Production runs prepare each symbol, run its arbiter, pass the release gate and store to PG. Sector-group trials use frozen sandbox inputs.
  Use when the user says "run NVDA / run these / run this batch" and the symbols are already chosen; which symbols to pick and whether IBKR is up are in equipoise-dispatch.
  Also holds the only entry-point notes for historical replay (the retired API version).
---

# equipoise-run-debates — how to run debates

Working directory `/opt/equipoise`, python is always `./venv/bin/python`, commands that touch IBKR must carry `EQUIPOISE_IBKR_PORT=4001`.
**Which to run, when to run, and how to judge IBKR: the authority is `equipoise-dispatch`**; this document only covers how to drive.

**The engine is bound to the current harness**: if you are Codex read only `references/codex.md`; if you are Claude Code read only
`references/cc.md`. The two sides share the flow below, the content layer and the deterministic scripts, not the executors; neither launches the other.
The Codex version is the current mainstay; the CC version is maintained in parallel.

## Choose the unit of judgment

An explicit formal group uses `codex_loop.py --group GROUP_KEY`; the [group adapter](references/group-production.md) drives one independent arbiter through shared rounds and member signoff.

For a frontend example from one real fixed group, follow the [operator-led group hearing](references/group-demo.md).

- Use the [group dispatch contract and deployment status](../../orchestration/duty.md#dispatch-by-group) to identify the unit before selecting a runner.
- **One-member group.** Run the production flow below for its symbol. An explicitly independent batch runs once per selected unit.
- **Regional GENERAL.** Run its configured GENERAL symbol through the same flow. Its index lenses retain the existing per-lens results and weighted market summary.
- **Fixed sector group (sandbox).** Use this mode for an explicitly grouped drill. Freeze the named roster in one trading region at one cutoff. The sector ETF and market index supply context. Overseas issuer disclosures can inform a member when available by the cutoff. Give the roles the following group instructions with the frozen materials.

  > Establish shared facts once and cite the same sources wherever they apply. Use members as mutual references. Let the evidence and live disagreements determine which comparisons deserve more attention as the debate develops. For each member, explain the transmission to its own price and what could overturn it. Each side supplies a separate scenario per member. The arbiter resolves the relevant claims and chooses the mixture separately for each member. Apply the existing single-symbol ruler to each resulting distribution to derive that member's score and execution card. Return one result per member. Omit group scores, portfolio weights and group actions.

  Keep drill artifacts in the sandbox. The production batch launcher starts independent debates and the GENERAL finalizer builds a weighted summary. Neither implements this grouped drill.

## Per-symbol flow (identical for both engines)

1. **Data preparation** `EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py <SYM>`: connects to IBKR to pull market data, pulls news,
   and hydrates from PG the temporary files this session will read. Exit codes:
   `0` READY · `3` required data missing (skip only this one; the output says what is missing) · `10` IBKR down (**abort the whole batch**, find the user) ·
   `12` a report already exists at this anchor (skip, counts as success; add `--force` if a re-debate is really wanted).
   **Non-US news (EU / JP / KR: the region's market news and each single name's own news) is fetched by you**: data preparation only checks freshness (12 hours) and prints
   `NEWS: … <EU, SIE, TM>` for what is missing. For each item run the news leg in this session — CC: `/refresh-news EU|JP|KR|<SYM>` (WebSearch); Codex: `.codex/refresh-news.md`
   with REGION or SYMBOL — a region up to twenty items, a single name about ten (home language plus English; a sketch of what happened and what to check — tracing cause and
   effect is the debate's job), load the staging file with `fetch_eu_jp_news.py --load`, then prepare again. Warning-level: do not block the fire over it.
2. **fire**: start a brand-new subprocess as the main session. **Whatever you write to it (a spawn message, a prompt, a task line) is in English**, even
   if you are talking to the owner in Chinese: the corpus, the skills and the materials are English, and a Chinese launch line is the one thing that drags a whole
   debate into Chinese (2026-10-26, several sessions). Quoted source passages keep their original language. It is the arbiter; it runs `scripts/run_step.py` step by step per `orchestration/run-protocol.md`,
   and itself spawns the two debaters Zealot and Reaper and the two kinds of neutral executors, Scout and Runner.
   The subprocess **only reads** the data-preparation artifacts; it does not connect to PG, does not connect to IBKR, and does not rerun data preparation.
3. **Artifacts**: `data/debate/<SYM>/<SYM>_Analysis_<TS>.json` and `data/evidence/<SYM>/..._Evidence_<TS>.json`.
   Assembly and the release gate are both done mechanically inside `run_step.py finalize`.
4. **Store**: `./venv/bin/python scripts/pg_store_debate.py <SYM> [Analysis path]`. Before writing it runs the same release gate again,
   and does not write if the gate fails; one command writes `debates` and the evidence tables. In the batch drivers this step is built in: store each one as it finishes.
5. **Translate (last step)**: the main session has already produced `data/i18n_staging/debates_<SYM>_<TS>_<lang>.json` per language (protocol §4, Translator subagent);
   after the store run `./venv/bin/python scripts/translate_reports.py --load-report <SYM | Analysis path>` (loads every staging file of that report; `--load <file>` does one).
   `cc_loop.py` / `codex_loop.py` do this themselves right after `[pg_store] OK` (receipt line `[i18n] {"ok": true, "loaded": [...]}`; 2026-10-26 fix — before that
   they stored the original and stopped, and the zh file sat in `data/i18n_staging/` unloaded). `debate_driver.py` does extract → `--fill` with the engine's cheap
   model → `--load` itself. The original is never rewritten; a failed translation is reported, not retried forever. If a report shows `lang_available: ["en"]` only,
   run `--load-report <SYM>` by hand.

The debate rules are not in this document: roles, principles, manuals and formats are in `prompts/agent/`, the flow is in `orchestration/run-protocol.md`,
and the engine adapters are `.claude/commands/run.md` and `.codex/run.md`.

## The three disciplines

1. **Data preparation is always serial.** It is the only stage that crowds IBKR; prepare the first pool serially, then prepare the next one only as each slot is released. The market-data leg additionally has
   the `/run/lock/equipoise-ibkr-prepare.lock` cross-process lock as a backstop. Prepared data counts as fresh for one hour; fire does not touch IBKR again.
2. **Stagger adjacent fires by a few seconds**, spreading the subprocesses' search and evidence requests apart in time.
3. **Concurrency depends on quota, not on dependencies.** There is no execution order between symbols: the region's general, sector ETFs and single names do not queue behind one another. Failed symbols go to the back of the queue,
   with at most one retry after the first round is fed.

Do not run debates on stale data: inventory for which no new request was sent does not count as data. When the same symbol already has a running process or service, do not start it again.

## Acceptance and resuming after a break

- **Proof of completion** = the Analysis and Evidence files + the store receipt. The process exit code alone does not count.
- If the session or network broke, first check the process, the object store `data/store/<SYM>/<TS>/_session.json` and the receipts:
  still running → keep waiting; exited with a qualified final report → done (if not stored, add one store command); exited with only the object store and no final report →
  rerun the same symbol: the main session's `run_step.py open` returns a resume receipt for a session with existing objects and continues from the last archived object
  without reopening the session (protocol section 6).
- **Schema (v3.4, cohort C12)**: `run_step.py open` writes the session's ruler snapshot (`_session.json.odds`) once, with the schema taken from `config/odds.yaml` (`"3.4"`); there is no schema flag. A session opened before the deploy has no snapshot and cannot be resumed (`session opened before v3.4; cannot resume`): prepare data again and open a new session (deploy only when no debate is running). The snapshot's `provenance.commit` comes from git or, on the server (not a git checkout), from the one-line `/opt/equipoise/DEPLOY_COMMIT` that the deploy writes; details in `docs/source-snapshot.md`.
- **Do not restart a task merely because the user disconnected.** When the release gate fails, do not patch the report; let that session resubmit by itself or be judged a failure truthfully.
- Only when two or more different symbols in the same wave hit data-preparation `exit 10` in a row, or the probe fails, judge IBKR down; a single `exit 10` may just mean no new bar.

## Boundaries

- The data truth is in PG; files under `data/` are only transport items after data preparation and this session's artifacts.
- Storing is done serially by the trusted parent layer; subprocesses and roles in the sandbox never connect to PG or IBKR directly.
- Batch drivers always load market membership from `config/symbols.yaml` via `workflows/symbol_universe.py`; no hand-written lists in scripts or in this document.

## See also

- `references/codex.md` / `references/cc.md`: this engine's launch commands, parameters, permissions.
- `references/replay.md`: historical replay (the only use of the retired API version).
