---
name: equipoise-postcheck
description: >
  Postcheck (shared by both engines). Use when the user says "run postcheck / run the postcheck / postcheck NVDA / postcheck US / continue postcheck".
  The main session itself acts as the reckoner: judges whether each mature debate report was wrong-footed, missed or hit, gives one composite score, writes the attribution, and stores it in PG postcheck.
  Does not spawn subagents; resumable across sessions via the worklist. The scoring spec is in orchestration/postcheck-protocol.md.
---

# equipoise-postcheck — postcheck

Entirely in `/opt/equipoise`, python is always `./venv/bin/python`. PG `debates` is read-only; `postcheck` is one table shared by all engines.

A postcheck is not a backtest, nor a re-debate; it is an after-the-fact settlement of **one matured debate report**:
`prep` reads that old report from PG → connects to IBKR to **newly request** the outcome window's market data → the script compresses the window into deterministic price-path facts (digest) →
you read the digest and the report → score and attribute → write the judgment JSON → `save` into PG `postcheck`.

**The sole authority on scoring criteria, the three attribution questions and the JSON shape is `orchestration/postcheck-protocol.md`; this document never restates it.**
Read it in full before the first one in each session; no need to re-read for the later ones in the same session.

**The engine is bound to the current harness**: if you are Codex read `references/codex.md`; if you are Claude Code read `references/cc.md`
(how to sign, the batch driver, how many to run per session). Neither launches the other.

## Three unchanging disciplines

- **A single main session executes linearly and spawns no subagents.** When verification is needed, search yourself.
- **Price and path facts come only from the digest; do not make up numbers.** Outcome prices must be newly requested this time; do not consume leftover old files.
- **The hindsight rule**: you stand where the outcome is known; attribution admits only facts, no empty hindsight talk; a black swan is a conclusion reached after checking, not a reason to skip the check.
- **`path_fit` is a diagnostic layer, not points** (2026-11-06): the digest carries how the realised path sat inside the tree (PIT by day, coverage, breach persistence, path CRPS skill); cite it in `diagnosis` when informative, never move a band on it. Protocol Step 2, R25 paragraph.
- **Read the original, write English.** `prep` hands you the report as stored in `debates.report`, in its source language (English from 2026-10-26, Chinese before); translations (`report_i18n`) are never read for scoring. Verdict and diagnosis are written in English; the Chinese verdict shown to the owner is produced afterwards by the translation step, not by you.

## Flow

**A. See what is left** (the first step of every new session; not from memory):
```bash
./venv/bin/python skills/equipoise-postcheck/scripts/worklist.py --dt 3 --start 2026-06-18 --universe us_all
```
Lists the `(symbol, time)` pairs that are mature but not yet postchecked by an agent: "pending (no row)" and "to replace (old-engine rows)".
`--dt 3|20` (the 50-day tier was retired 2026-09-30). `--universe` derives from `config/symbols.yaml`:
`<market>_generals | _stocks | _etfs | _all` (market = us / eu / jp / kr) and cross-market `all`; `us_all` includes GENERAL. Lens-wise GENERAL reports (digest `lenswise`) settle every lens separately and still get one composite score weighed by `weight`: see the lens-wise paragraph in `orchestration/postcheck-protocol.md`.

**B. Postcheck one by one**:
```bash
EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python datalayer/pg/postcheck_store.py prep --symbol <SYM> --dt <dt> --as_of <TS>
```
Receipt `status`: `ok` → take `staging_path` and continue; `immature` (including no outcome price obtained this time) or `no_report` → skip and report it;
do not force-fill. **Several symbols in a row immature with the reason mentioning IBKR → IBKR may be offline; stop and report to the user** (probe and restart are in `equipoise-dispatch` step 2).

Then follow the spec: Read `staging_path` → settle first, then score → judge `review` → (when needed) read the full debate for attribution → write the judgment JSON →
```bash
./venv/bin/python datalayer/pg/postcheck_store.py save <judgment.json path>
```
`status:saved` with `by=<your model id>` = success. **Then translate the verdict as the tier's last step** (protocol Step 4): `translate_reports.py --extract --table postcheck …`
→ spawn the Translator subagent on the staging file → `translate_reports.py --load <path>`; one language per `config/i18n.yaml` `translate_to`.

**C. Wrap up**: rerun A to confirm the numbers went down; tell the user which ones this session postchecked (symbols and scores) and how many remain. All state is in PG; no handoff needed:
a new session just says "continue postcheck".

## Default scope (unless otherwise specified)

Tier `--dt 3`; start `--start 2026-06-18`; universe `us_all`; cover every period in the full window. When the user wants only "the latest period per symbol",
`prep` omits `--as_of`.

## See also

- The retired code version `run_postcheck.py` is used only when the user names "the ds version / code-version postcheck"; the old rows it left are naturally overwritten when the same key is rerun.
- Private postcheck booklets and the deployment chronicle are not included in this public snapshot. See `docs/source-snapshot.md` for its scope and provenance.
