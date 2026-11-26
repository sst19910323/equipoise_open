---
name: equipoise-dispatch
description: >
  The dispatch entry point for Equipoise debates and postchecks. Use when the user expresses the intent to run, update, take a look at, or start a stock market region
  (US / Europe / Japan / Korea / Japan-Korea), or names symbols, asks for a region's full set, or asks for a postcheck.
  Does not depend on "update" or any other fixed command word; infer the mode from the action intent, the region and the quantity modifiers.
  This document covers three things: which symbols to pick, whether IBKR is up, and how to report once done. How to launch is in equipoise-run-debates; postcheck is in equipoise-postcheck.
---

# equipoise-dispatch — the current conversation is the orchestration layer

Working directory `/opt/equipoise`, python is always `./venv/bin/python`, and every command that touches IBKR **must carry `EQUIPOISE_IBKR_PORT=4001`**.

- A human can request a run directly. During automatic duty, Hermes wakes the duty officer, who selects eligible work using the same dispatch rules.
- **The data truth lives only in PG**. Files under `data/` are temporary transport items and this session's artifacts; do not read them as a data source.
- **The engine is bound to the current harness**: if you run inside Codex use only the Codex driver; inside Claude Code use only the CC driver; neither launches
  the other. The two sides share the rule corpus, formats and deterministic scripts, not the executors. The Codex version is the current mainstay.
- The order is fixed: pick symbols → (when needed) probe IBKR → serial data preparation → staggered fire → store each to PG one by one → report. The commands and discipline for the last four steps are in
  `equipoise-run-debates` (`skills/equipoise-run-debates/SKILL.md` plus this engine's page).

## Step 1: select dispatch units

Use the [group dispatch contract and current rollout status](../../orchestration/duty.md#dispatch-by-group). Resolve ownership before counting candidates. In a grouped run keep the member that triggered it in the reason. An explicit request for one group means one shared hearing. For a singleton, the symbol remains its execution key.

On the Codex engine, before reporting the worklist, glance at the quota reset window (it only affects how many to pick; it does not replace the region, event and freshness judgment):
`references/quota-window.md`.

- **Judge intent first; do not match fixed command words**: "run", "update", "take a look", "do a pass", "market's open",
  "let's start" and the like can all express execution intent. As long as the context clearly means running Equipoise stock analysis
  and a region is given, enter the corresponding region mode on your own; do not ask for a command word because the user did not say "update".
- **Named symbols** ("run NVDA" / "run GENERAL_JP" / "Europe: run RHM and BESI") → run exactly the named set;
  named symbols take precedence over the default smart selection, but still check that they do not contradict the region the user stated.
- **Region-level execution intent** ("run US" / "do a pass on Europe" / "Japan-Korea just opened" / "do Korea" /
  "look at Japan" / "run a few US names" / "update Europe") →
  generate the worklist yourself per "smart region update"; **this does not mean every symbol in the region**.
- **Region resolution**: US stocks / United States = `US`; Europe / European stocks = `EU`; Japan / Japanese stocks = `JP`;
  Korea / Korean stocks = `KR`; Japan-Korea = the union of `JP` + `KR` as two independent regions; do not treat Korea as a subgroup of Japan.
- **Full coverage** ("run all US names" / "update all of Europe" / "run everything in Japan-Korea" / "run everything") →
  cover the region's configured actionable stocks, ETFs and GENERAL. For enabled fixed groups, count each owning group once and keep background-only references out of the launch list;
  only explicit full-coverage wording expands to the full set. "Run everything" inherits the region of the current instruction / context
  and does not automatically expand to global; only when the user explicitly says "global / all regions" do you run all four regions' full sets.
- **Quantity-word resolution**: "run a few / do a pass / take a look" means smart selection, not an exact count;
  only "all / every / run them all / none left out" means full coverage. Phrases like "run them all then" that refer back to a worklist already shown
  mean, by default, confirming that worklist, not suddenly expanding to the region's full set.

### Smart region update (the semi-manual default)

This is the default meaning of "perform one stock analysis pass on <region>"; the user is not required to say "update".
Select the symbols in that region worth updating this time;
do not rerun the whole universe every time. Market opening hours are the fundamental reason the user issues instructions by region;
every selection and execution must keep that region boundary.

1. **First read the current update table in full**:
   `/opt/equipoise/config/update_schedule.yaml` is the authoritative update table under active maintenance. Every time the user asks to run
   a region, you must re-read it on the spot; do not use the list or memory from a previous session. Focus on
   `fundamentals.<SYMBOL>.last/next` and `next.time.reason` for that region's symbols, and also look at the corresponding macro section:
   US → `economic.US`; EU → `economic.EZ` + `economic.UK`; JP → `economic.JP`;
   if KR currently has no independent economic section, treat it honestly as a signal gap; do not borrow JP macro as a substitute.
2. **Then take the real full set**: read `config/symbols.yaml`; select the region by `symbol_contracts.<SYM>.region`;
   only a missing region counts as US. Symbols in the update table must be intersected with the currently runnable universe;
   those not yet added to the universe are only noted as a gap, never run quietly. The order of `analysis_targets` is not a priority.
3. **Then take freshness**: PG is the truth for reports. Run
   `./venv/bin/python skills/equipoise-dispatch/scripts/pre_round_brief.py --market us|eu|jp|kr` (or `--symbols A,B`):
   for each symbol, the time of its most recent session, composite score and operation, the one-line statement of the last session, the `tree (divergence)` column and the `due` column.
   **The reference value you read first is the divergence** (2026-11-06, owner: "divergence first, then events"; `scripts/divergence.py`): the report's own scenario tree, as a mixture of its
   paths, against the latest price — `dead` (the price sits outside everything the report foresaw; `tree dead:` is the only due reason that is hard), `drift` (a candidate, weigh it with
   what happened), `inside` (the report already covers this price: a crossed level is not news), with the move in ATR and the trading days since the report (`-3.6σ/1d`). The `due`
   column then lists the mechanical reasons: a dead or drifting tree; `earnings landed` (the company reported after the report — by itself only a reason when the tree did not price
   the print, or when the release changed something the price does not show); the re-evaluation time the report wrote has passed; an entry order derived as a limit order passed its
   validity (default 3 trading days, or as the report explicitly wrote) without touching — a touch within the validity is recorded as "simulated fill", which is a state, not a due.
   Reports without a v3.4 tree (older ones) still show the legacy `line hit:` from their trigger lines; sector-line cohorts are listed as "needs manual". Due = goes into this round's
   candidates; the choice, the count and the order are your judgment (the duty loop's Codex reads the same values from the status JSON). Cancelling or renewing orders is not handled here but in the rebalance plan.
   The divergence and the order state need market data: the normal usage is with `EQUIPOISE_IBKR_PORT=4001 … --fetch`,
   and the script first refreshes these symbols' daily bars along the same path as postcheck (skipping those fresh within 1 hour) before judging; without `--fetch` it only reads the leftover CSVs in data/market_data
   and marks "no market data" when there are none (when this column is missing do not guess; only ① can be judged). File mtime may serve as a degraded clue only when PG is unreadable.
4. **Use the update table as the main axis of selection, but do not mechanically treat "time reached = run"**: if the symbol's earnings / major macro has just landed,
   or it is about to enter a window that requires a position decision,
   it may be upgraded even if it is normally a low-frequency type. Do not upgrade merely because of a `next` that is still far away.
5. **Set the baseline cadence by type; events may override**:
   - The region's `GENERAL`: included by default in every region-level execution unless the user explicitly says not to run macro;
     US=`GENERAL`, EU=`GENERAL_EU`, JP=`GENERAL_JP`, KR=`GENERAL_KR`;
     "update Japan-Korea" includes both `GENERAL_JP` and `GENERAL_KR` by default. GENERAL has high priority,
     but it is not a completion barrier for the other symbols.
   - The region's broad-market / market lens plus high-beta, policy/commodity-sensitive, fast-narrative single names:
     a regular re-check scale of 1–2 trading days of that market.
   - Sector ETFs, large-cap growth / ordinary cyclicals: a regular scale of 2–3 trading days.
   - Mature low-volatility, defensive / consumer staples, slow-moving value names: a regular scale of about 5 trading days;
     for example `KO`/`MCD` if added in future should not normally share the cadence of high-beta tech names.
   These are selection scales, not a hard cron: earnings, major policy / litigation, breaking news, abnormal price-volume, or an old report's
   trigger/invalidation coming true upgrade immediately; with no new information, do not lower the bar to pad the count.
6. **The count is decided by the current table and the evidence; no fixed cap or target number**: first rank "update-table events / old-report
   triggers already fired", then those past their type's baseline cadence. If the table supports only 3 names right now, run 3;
   if it supports 20 at once, list 20 and let the execution layer feed them in waves by the concurrency pool. Do not add symbols to pad the count,
   nor remove symbols that genuinely should be updated to fit an artificial quota.
7. **Report the worklist before launching**: briefly list the selected symbols and the reasons (event / overdue / market lens),
   and also mention a few representative names that look easy to mis-select but are still fresh this time. This is the visible semi-manual checkpoint;
   if the user does not object, continue executing; no need to stop and wait for another confirmation.

`config/debate_schedule.yaml` is an artifact of the retired API version: the current engine does not write back to it, and the times in it expired long ago; **do not read it to pick symbols**.
The re-check time a report itself suggests is in PG `debates.next_review`, which the brief above already brings out.

- **There is no execution-order dependency whatsoever between symbols**. The debaters can fetch data and search same-sector and peer information themselves; they do not rely on a parent-layer report to feed context
  — GENERAL_x / sector ETFs / single names **do not queue behind one another; all can run in parallel**.
  The `symbol_contracts.<SYM>` sector/region in `config/symbols.yaml` is used only to decide **which benchmark / which region's data to pair with**
  (region defaults to US; TSM/ASML count as US) and does not imply ordering.
- **Concurrency depends only on usage, not on dependencies**. All stock batch runners uniformly use a rolling list/pool: pool default 8, and when the user explicitly specifies a value, the user's value prevails;
  when one finishes, feed the next; failed symbols go to the back of the queue and get at most one retry after the first-round feed completes.
  More symbols are fed in batches.

## Step 2: IBKR judgment (debates and postchecks both pass through this one gate)

**When to probe**: the first run of this session, or more than ~1 hour since the last success. Just probed / just ran successfully → skip.

```bash
cd /opt/equipoise && EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python ibkr_probe.py   # probe a real SPY daily bar
```
Layered probe TCP → API handshake → **actually pull one SPY daily bar** (the Gateway has a half-dead state: the socket is alive but the data farm is disconnected,
and a connect-only test cannot detect it). Exit codes `0` up / `1` port dead (not logged in / process dead) / `2` half-dead (data not flowing).

**Non-zero (both 1 and 2 count) → go through the single restart procedure; do not invent your own kill commands**:
```bash
bash ibkr_restart.sh
```
Kills the whole tree (a partial refresh does not push 2FA; if the user's phone gets nothing you wait in vain) → fresh start → wait for 4001 to listen → automatically re-run the probe.
**Before launching it, say in the conversation "go check your phone to approve 2FA"**. The user approving 2FA is itself the approval to restart; no separate request is needed.
**Try only once**: a second failure = IBKR offline; stop and report truthfully. Never force a run, never loop retries (prevents 2FA bombing / account lockout).

**Do not re-verify repeatedly**: the explicit probe is done once for the whole batch. Do not probe separately within the batch — data preparation pulling data is itself the probe (when data is fresh within 1h
it does not even touch IBKR). Data preparation exit 10 = disconnection signal; abort the remaining batch and report truthfully (those already finished are already in PG and are not lost).

Never run debates on stale data. US is paid real-time, Europe/Japan 15-min delayed; both are acceptable; "stale data" means inventory for which no new request was sent.

## Step 3: run

How to prepare data, how to fire, how to store, how to resume after a break — all in `equipoise-run-debates`: first read `skills/equipoise-run-debates/SKILL.md`
(the flow shared by both engines, exit codes, the three disciplines, acceptance), then this engine's page (`references/codex.md` or `references/cc.md`).
The dispatch layer only needs to remember: **data preparation serial, fire staggered, concurrency by quota not by dependency**; pool default 8, and the user's value prevails when specified.

### Non-US news leg

Since v3.3 (2026-10-26) non-US news — the region's market news (GENERAL_EU / JP / KR) **and** each single name's own news — is searched by the main session itself (CC: `/refresh-news EU|JP|KR|<SYM>` with WebSearch; Codex via `.codex/refresh-news.md`; spec shared in `.claude/commands/refresh-news.md`). Data preparation only checks freshness (12 hours) and prints `NEWS: …` with what is missing; run the leg for each item, load the staging file (`fetch_eu_jp_news.py --load <file> --region <RG>` or `--symbol <SYM>`), then prepare again. The Linkup leg is retired (the owner, 2026-10-25). Non-US news is **warning-level**: failing to produce it does not block the fire (debaters can file a search as fallback).
A single name gets about ten items from the last 3 days, home language plus English — a sketch of what happened and what to check; tracing cause and effect is the debate's job. A region gets up to twenty.

### Postcheck

Goes through `equipoise-postcheck` (`skills/equipoise-postcheck/SKILL.md` plus this engine's page). Same IBKR gate: postcheck's outcome prices must also be newly requested.
If the window is not mature or no outcome price was obtained, skip and report it; do not force-fill.

### Historical replay

Only the retired API version can replay at a historical moment; the entry point and caveats are in `skills/equipoise-run-debates/references/replay.md`.
Do not use it unless the user names "API version / code version / replay".

## Step 4: report

Report the execution unit, its members, the completion or skip reason and the report location. If a quota limit broke the run midway → say truthfully which ones finished
(everything finished is in PG and not lost), and the rest continue once the quota recovers.

Human-facing outputs for the owner — the round summaries addressed to the owner — are written in `owner_lang` from `config/i18n.yaml` (currently zh). Machine artifacts (JSON, receipts, PG rows) stay in the core language.

## Boundaries

- Do not change the scheduling-table logic; do not touch Hermes's low-frequency refreshes.
- The authoritative update table `/opt/equipoise/config/update_schedule.yaml` is a plan maintained by the user and the low-frequency refresh tasks; during dispatch it is read-only,
  do not change it on your own initiative.
