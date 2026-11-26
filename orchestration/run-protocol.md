# Debate orchestration protocol (shared by both engines; rebuild batch 3, 2026-09-30)

Reader: the main session (the arbiter) — Claude Code's `/run <SYM>` or Codex's `run.md` child process. The two engine files only say
how this engine spawns, how it follows up, and which model it uses; the flow, the judgment points, the materials, the failure handling and **every command** are in this one document.
Flow goes into code, judgment stays with the model: each step first runs `scripts/run_step.py <subcommand>`, and the receipt (one line of JSON) gives the next step and the paths to
Read; you act only at the judgment points — spawning debaters and executors, verifying evidence, writing notes and private records, two passes of the final ruling, then the redraft.

## 0. Constants and constraints

- Working directory = project root; python is always `./venv/bin/python` (use `python` on a machine without a venv). `$S` below means
  `--symbol <SYM> --ts <DATA_TIMESTAMP>` (the timestamp comes from the `open` receipt, not the wall clock).
- Commands: run only the `run_step.py` and `debate_store.py` commands given in the sections of this document, verbatim; no jq, no ad-hoc scripts, no hand-written envelopes, no
  source edits. The receipt is one line of JSON: `next` is the next step; on `ok:false` read `error` or `gate.fatal`.
- You are the arbiter (`prompts/agent/roles/arbiter.md`): at the start, Read `prompts/agent/principles.md` and that role file;
  load each step's manuals per the receipt's `reads`. **You do not Read any raw data file** (context minimization); judgment material = the views,
  the ledger's mechanical lines, and returned results.
- Silence during the exchange: notes make procedural rulings only and publish no acceptance, score, operation or mentality; a private note is recorded every round (which inputs moved, what stays open; invisible to the debaters).
- Effort references are calibrated on one symbol. For GENERAL state the lens count and names; for a group state the frozen member count and names to both sides. Shared research counts once. Use the remaining material disagreements to judge further work.
- Numbers (set by the owner, configurable, see the `run_step open` parameters): **only the hard round cap of 20 is a safety stop** — when reached, the session is force-closed and open claims are recorded
  as held unresolved; the arbiter knows this number and winds down as it approaches. The session closes when the key disagreements have been verified or named as stalemates, not at a round count. Everything else is **guidance**, which the script does not block: per round, per side,
  the reference beat count is 3 (exceeding it must be explained); the model-call reference budget is 8; objection rounds are 1 by default (a second only when the redraft introduced a substantive new error, an omission, or a change of the adopted distribution, or when new evidence was requested); the arbiter's
  neutral supplementary checks per round, reference 2. The reference session length is 5 rounds (a session usually carries several topics: a small topic closes in one round, a large one takes several, and several run side by side when none is complex):
  three or four rounds is normal, two is not; closing before the reference is allowed, and the notes then say in one sentence why the topic list is already complete.
- Failure means halt, never placeholder numbers: a side's final fails validation twice / the redraft fails the gate twice / the gate fails after signoff → receipt `code 20`,
  session marked `failed`, report to the user. Data not ready → `code 10`, pointing back to data preparation.
- **Language: English, everywhere inside the session** — your notes and rulings, every spawn message and job you write for a debater, Scout or Runner, every
  filing and every JSON value. The session that launched you may have been talking to the owner in Chinese; that does not carry into the debate. Only a
  quotation from a source keeps its original language (the surrounding text stays English). A debater speech or executor result in another language is a
  format failure: send it back for a rewrite in English before archiving.
- The object store `data/store/<SYM>/<TS>/` is the only source of historical truth within the session: ids are minted only by the script; you and the debaters only cite them; when resuming, first read
  `manifest.jsonl` and `_session.json`, continue from the last minted object, and never re-archive.

## 1. Opening

```
./venv/bin/python scripts/run_step.py open --symbol <SYM> --engine cc|codex --model <your model id> [--worker-model <executor model id>]
```
Add `--calc-check off|always` only when the user has chosen an arithmetic-check level (if not chosen it is the default remind level, no need to write it); the script splices the one sentence matching that level
into the executor and debater task materials, and you do not relay the level to any role.
Precondition: data preparation `prepare_debate_data.py <SYM>` has returned READY (its receipt carries the RAW DATA FILES manifest).
Receipt: `ts`, `context_type`, `region`, `store`, `warnings`, `caps`, `arbiter_reads`,
`spawn: {Zealot: material path, Reaper: material path}`. The material file = the complete spawn message (meta information, role file, a conditional required extension — `prompts/agent/formats/lenses.md`, multi-lens sessions only — context
manual, RAW DATA FILES manifest, trigger signals, non-US event calendar and quote discipline, budget, beat rules, view path, focus); you use it **whole and verbatim**
as the spawn message, with nothing added, removed or paraphrased; the only permitted addition is at most two neutral sentences of session status.

## 2. Each round: spawn → each side walks its own beats → close the round

The two debaters write simultaneously, without waiting for each other. Within a round you are a **responder**: handle whichever side's beat has arrived, and give the receipt to whichever
side's job results are complete; only closing the round waits for both finals. The same artifact is generated only once: the subagent writes the JSON into the `OUTPUT`
file given in the material and replies with the path only; you hand the path to the script — do not have it paste the JSON in the message and then write it again, and do not retype it yourself.

### 2.1 spawn (one new thread per debater per round, both in one concurrent message)
Spawn with the full text of the material file from the `open` (first round) or `close-round` (later rounds) receipt. The last line of the material gives the `OUTPUT`
path reserved for that beat: the debater writes one JSON (`formats/debate.md`) into this file once and replies with the path only (on engines where the debater has no file-writing tool: the debater
outputs the JSON directly, and you land it verbatim at that path). The JSON's `beat.status` is `need_more` (with filings) or `final` (the final).
**The thread stays open** until that side's final is archived.

### 2.2 route (archive this side's beat + route its outcalls)
```
./venv/bin/python scripts/run_step.py route $S --round <n> --beat <k> --zealot <OUTPUT path>   # or --reaper; if both arrive together, pass both at once
```
Route whichever side's beat has arrived immediately, without waiting for the other; the two sides' beat numbers count separately. The script: a final passes `validate_role_record`
and is minted `R<n>.Z`; an interim beat is minted `R<n>.Z.B<k>`; aggregate filings are deduplicated literally, pass the duplicate gate (own-symbol daily bars / own-vs-own-benchmark relative strength /
own-symbol trend fit are rejected as duplicates) and the manual-existence check (the model-call budget only counts, it does not block); **data calls are executed mechanically on the spot and archived**
(`ok:true` is verified as `verified`, `ok:false` is verified as `not_found` and **downgraded to one evidence-search job**); evidence searches and model calls
are written as job materials `_job_<id>.md`; when the same beat is routed twice in succession, the earlier side's in-flight jobs are kept and ids continue in sequence; in a non-US first-round first beat, if
the calendar is empty and nobody has filed for events, one neutral arbiter event check is added automatically.
Receipt: `archived`, `finals`, `pending` (the side still checking), `data_calls`, `skipped` (with reasons), `jobs` (each with
`material` and `output`), `open_jobs`.
- `ok:false, retry:true`: a side's final failed validation — relay the `rejected` text verbatim back to **that debater's same thread**, have it rewrite the same
  `OUTPUT` file, and on receipt `route` again for the same beat (passing that side only); if it fails again, the script halts (`code 20`).
- `need_more` with an empty `request_evidence` → the script refuses it (either file or deliver a final). The beat count is only a reference; the script does not fail a side for exceeding it.

### 2.3 Execute jobs + verify
As soon as `jobs` returns, spawn them concurrently, one executor per job: `kind: scout` → one Scout per question (`.claude/agents/scout.md`);
`kind: runner` → one Runner per model call (`.claude/agents/runner.md`). Spawn message = the material file in full, using the executor
model (receipt `worker_model`). The executor writes the single-item JSON of `formats/evidence.md` into the `OUTPUT` file given in the material.
When one side's job results are complete (without waiting for the other side's), **verify** them one by one per `manuals/synthesis/arbiter-evidence-review.md`
: for each, write one sentence of `scope` (which part may be used as fact, which part is only inference or was not found) and its one-word heading `status`
(`verified` / `unverified` / `not_found`), into `results.json`: `[{"job_id", "status", "scope"} |
{"job_id", "failed": "<reason>"}]` — write only your verification; the script reads the result bodies from each job's `OUTPUT` file.
A child thread that is slow to deliver: first check whether its `OUTPUT` file and tool actions show progress; if there is truly no progress, interrupt and resend the same material, a bounded
number of times (reference: once); if it still fails, handle it per the next item and record one sentence in the notes. Do not just send reminders and wait, and do not repeatedly interrupt a working
thread on a fixed number of minutes.
The executor is a neutral call, and it does not matter who executes it: if it died or cannot be spawned, restart it once first; if that still fails, the main session reads the same role file and material
and executes it by the book itself (equally neutral, same output shape). Report `failed` only when execution is truly impossible (the script records `failed`: dropped out, not nothing found; the debater
may refile), and never fabricate; every job must have a result, none may be silently dropped.

### 2.4 close-beat (archive results by debater + receipt and re-wake)
```
./venv/bin/python scripts/run_step.py close-beat $S --round <n> --beat <k> --role <zealot|reaper|arbiter> --results results.json
```
Close only this side's jobs (the arbiter's own supplementary checks: if the result is already in, close it together with either side, otherwise close it later with `--role arbiter`; omitting `--role`
= close all in-flight jobs of this beat). The script archives each result (a model call is minted `Z.MC1` and its heavy-detail grandchild `Z.MC1.EV1` is split out; an evidence search
is minted `Z.EV1`; an arbiter supplementary check is minted `A.EV1`), assembles for that debater the receipt `_receipt_R<n>_B<k>_<role>.jsonl` (the full text of that side's outcall results for this beat
+ arbiter supplementary checks already returned) and the resume material `_resume_R<n>_B<k+1>_<role>.md`. Receipt `pending: {role: {material,
receipt}}`: immediately **follow up in the same thread** of that debater with the full text of the material file (paths and instructions only); it writes the next beat to the new `OUTPUT` path
→ back to 2.2 (`--beat k+1`).
When both finals are archived and no jobs are in flight → close both debater threads and enter 2.5 (`tally` refuses while jobs are in flight).
Downgrade exit (the only one): the harness cannot follow up into a thread that has already replied → that side's previous beat is archived verbatim as its final (pass it to `route --beat k+1`,
changing `beat.status` to `final` is enough, content untouched), and the notes record one sentence "beat continuation downgraded".

### 2.5 tally → notes → close-round
```
./venv/bin/python scripts/run_step.py tally $S --round <n>
```
Receipt: `tally_brief` (mechanical lines: each side's scenario ruler — `S_ref`, the best strategy, `q*`, `x*` per strategy, the session cost, warnings; in a multi-lens session one ruler line per lens per side plus a default-weighted summary line — OPEN claims with each claim's `claim_rev`, `deadlock requested by …` lines, suggested focus, model-call filings; from round 3 (round 2 in a follow-up session), when neither side's `S_ref` moved by a point on any tree, a `[Stall reference]` line — guidance to write every ready closing record in this round's notes and to name what is pending on any claim kept open; it is on your copy only),
the arbiter view path, `reads` (the notes manual, the evidence verification manual), `hard_stop` (open claims, round count, cap).
Read the view and the manuals, write `notes.json`:
```json
{"arbiter_notes": "≤200-character procedural notes (settlement rulings / consistency call-outs / case-closing gatekeeping / focus announcement / repetition call-outs / supplementary-check filings; with no ruling, write 'no ruling this round')",
 "private_note": "one sentence, invisible to the debaters: which scenario inputs moved this round and on what, what stays open (a score is optional: {\"private_score\": {\"composite_score\": <int>, \"note\": \"...\"}} is also accepted)",
 "focus": "suggested topic for this round <id> (default: cite the mechanical lines' suggested focus; attach one sentence of reason when changing focus)",
 "proactive_requests": [<at most 2 neutral supplementary-check filings, same shape as request_evidence elements>],
 "terminal_open": {"<claim id>": {"state": "deadlocked", "claim_rev": "<the claim's current claim_rev>", "remaining": "<the atom / probability / price still disputed>",
                                   "tried": [{"evidence": "<returned evidence or model-call id targeting this claim>", "engaged_by": ["<opposing record ids that engaged it>"]}],
                                   "next": "filed:<request id> | none: <why no route has positive expected value>"}},
 "dispositions": [{"claim": "<claim id>", "claim_rev": "<current claim_rev>", "evidence": ["<evidence id>"], "ruling": "one line"}],
 "decision": "CONTINUE | TERMINATE"}
```
`dispositions` is optional: your procedural ruling on returned evidence that a side did not engage; it counts as engagement for that evidence on that claim revision.
```
./venv/bin/python scripts/run_step.py close-round $S --round <n> --notes notes.json
```
The script archives the notes (`NOTES.R<n>`, mechanical lines prepended) and the private record (`PRIVATE.R<n>`), and enforces the **hard termination conditions** with one closure check (`scripts/closure_check.py`, re-run on the archived notes at release): an open claim — a debater's `deadlocked` is only a request and stays open — without a closing record that passes
(`tried` non-empty; each evidence returned, targeting the claim and engaged by the opposing side or disposed by you; `claim_rev` current; `next: filed:<id>` keeps it open) while the round count has not reached the cap → `TERMINATE` is overridden to `CONTINUE` (receipt `override_reason`); an evidence-backed challenge not engaged blocks `deadlocked` on its target;
`unresolved` is never accepted from you: only the script writes it, when the round count reaches the cap (forced `TERMINATE`, open claims recorded `unresolved`). Engagement is causal: every spawn material records `view_seq` (the last archived object it contained), and a record counts as engaging evidence only if it was filed after the evidence from a material that contained it. `CONTINUE` → both views are rebuilt, the receipt's `spawn` gives the next round's
materials, back to 2.1. `TERMINATE` → enter 3.
Whether the debate has been talked through is your call: whether each side has understood the other's claims, whether the key grounds have been answered, which node the remaining disagreement sits on, what kind of information could change
the operation; a stalemate is not granted while an unanswered verification challenge is on the table.

## 3. Two passes of the final ruling → proposed final ruling → objection → redraft → signoff

Every step in this section is the same command with a different `--stage` (prefix omitted below):
```
./venv/bin/python scripts/run_step.py finalize $S --stage provisional|audit|proposed|objection|ruling|signoff [--input x.json] [--round k] [--zealot <OUTPUT path> --reaper <OUTPUT path>]
```

1. `finalize $S --stage provisional` → the receipt gives the view and the manuals (final ruling and review, execution lines, final report format). **First pass**:
   the ruling sheet → the weight → the voids (do not run the ledger at this point), write `prov.json` (`claims_review[{claim, claim_rev, verdict, reason}]`,
   `mixture {w, reason}`, `voided {effects, candidates}`, `mentality` (one decision reason), `mentality_basis`, `rationale`); do not write `scenario`, `operation_*` or `composite_score` — the script replays Z′ / R′, mixes A, compares the strategies and writes them →
   `finalize $S --stage provisional --input prov.json` (mints `PROV` with its derivation, not polished afterwards).
2. `finalize $S --stage audit` → the full tally file + mechanical lines + each side's scenario ruler. **Second pass**: the three audit questions checked against
   the first-pass ruling — did every adopted claim land in a node, did any effect enter a node twice, was a settled topic overturned (the audit reads the replayed Z′ / R′ in `PROV`; on inconsistency go back to the specific claim); write the proposed final ruling
   `proposed.json` = the full set of arbiter-written fields in `formats/final.md` (the script derives A, `operation_*`, `composite_score`, `s_ref` and `scenario.derived` again) →
   `finalize $S --stage proposed --input proposed.json` (mints `PROPOSED`, receipt `spawn` gives two objection materials).
3. Objection round: **newly spawn** one debater each (material in full); the two `formats/objection.md` JSONs are each written into the `OUTPUT` file given in the material
   → `finalize $S --stage objection --round 1 --zealot <path> --reaper <path>`. One round by default; the receipt offers a second only when the first round's objections would change the adopted distribution or request new evidence
   (archived the same way with `--round 2`); otherwise the receipt gives `next: redraft`
   and `needs_new_evidence` (if any: handle as held unresolved and write it into `debate_summary`, no silent topping-up of material).
4. Redraft: Read the arbiter view (including `PROPOSED` and all `OBJ`), dispose of the objections one by one, write `ruling.json` = the full set of proposed-final-ruling fields
   (as revised) + `objection_dispositions[{id, index, disposition: adopted|rejected|deferred, reason}]` →
   `finalize $S --stage ruling --input ruling.json`. A debater's objection-stage `scenario_revision` is folded into the replay only when the redraft changes `claims_review` or `mixture`; a signoff revision never changes A. The script mints `RULING.v1`, **assembles + runs the gate (pre-signoff mode)**;
   `ok:false` → fix only the judgment fields per `gate.fatal`, submit once more (mints `RULING.v2`); failing twice → halt. Gate passed →
   the receipt's `spawn` gives two signoff materials.
5. Signoff: newly spawn each; the two `formats/signoff.md` JSONs are each written into the `OUTPUT` file given in the material →
   `finalize $S --stage signoff --zealot <path> --reaper <path>`. The script mints `SIGN<v>.Z/R`, injects `dissent` (the redraft version the signoff targets is traceable), reassembles, runs
   **the same release gate**, renames the buffer to `.complete.jsonl`, session `done`. Each side's endorsed score is derived from its endorsed scenario (last-round scenario + its revisions). The receipt gives `analysis_path`, `evidence_path`,
   `composite_score`, `operation_type`, `rounds`, `dissent`, `unresolved`.

## 4. Artifacts and PG write

- `data/debate/<SYM>/<SYM>_Analysis_<TS>.json` (the final report; all mechanical fields are injected by the assembler: `debate_history`,
  `evidence`, `evidence_added`, `model_calls`, `arbiter_log`, `arbiter_private_scores`, `provisional_ruling`,
  `scenario` (= A, with `scenario.mixture`), `operation_type`, `operation_target`, `operation_size`, `operation_execution`, `composite_score`, `s_ref`, `s_ref_z/r`, `scenario.derived` (`q_star`, `x_star`, `x_opt`, `candidates`, `cost_bps`, `unit_basis`, `display`),
  `odds` (the session's ruler snapshot), `spawns`, a store `seq` on every history / evidence / model-call / arbiter-log record, `schema_version`, `objections`, `dissent` (incl. `*_endorsed_scenario`), `signatures`, `data_files`, timestamps, engine lineage,
  `search_rounds_used`) and `data/evidence/<SYM>/<SYM>_Evidence_<TS>.json` (the evidence side file, verbatim equal to the report's `evidence`).
- The main session **does not write PG**. The orchestration layer (cc_loop / codex_loop / conversational CC), after receiving the ok receipt, runs
  `./venv/bin/python scripts/pg_store_debate.py <SYM> <Analysis path>`, which before writing
  runs the same gate again (`./venv/bin/python scripts/run_step.py gate --analysis <path>`) and does not write if the gate fails.
- **Translation is the last step of the session, after signoff** (owner, 2026-10-25): the original is stored first, then one Translator per language in `config/i18n.yaml` `translate_to`
  (the cheapest model) fills a staging file, and a script loads it. You do it like this, once per language:
  1. `./venv/bin/python scripts/translate_reports.py --extract --table debates --analysis <Analysis path> --to <lang>` → receipt `{"ok":true,"staging":"data/i18n_staging/…","strings":N}`
     (`strings: 0` → nothing to translate, skip the rest).
  2. Spawn the Translator (role file `.claude/agents/translator.md`; CC: Agent tool `subagent_type: translator`; Codex: `spawn_agent` on the worker model) with one line:
     `Translate the staging file <path> in place; reply with the path and the count.` It writes the file back.
  3. Hand the staging path(s) to the orchestration layer together with the Analysis path: the layer runs `scripts/pg_store_debate.py` (original) and then
     `./venv/bin/python scripts/translate_reports.py --load-report <Analysis path>` (loads every `data/i18n_staging/debates_<SYM>_<TS>_<lang>.json`
     whose `<TS>` is the Analysis file's; `--load <staging>` does one file). cc_loop / codex_loop do this themselves right after a successful store
     (2026-10-26 fix: before that they stored the original and stopped). It refuses a file if the original is not in PG yet or has changed.
     Keep the default staging path from `--extract` (no `--out`), otherwise the layer cannot find the file.
  The Translator never sees the report, only the strings; nothing in the report is rewritten. If the Translator fails, say so in the report — the original still stands.
- The short report to the user (≤ 250 characters): round count, composite score and operation, one sentence of core reasoning, signoff dissent, number of outcalls, file paths,
  held unresolved (if any), and the staging path(s) of the translations.

## 5. Views and visibility (enforced by the script, not by self-discipline)

- The debater view `_view_<role>.jsonl` = the full text of both sides' prior-round finals + each final's appendix (that side's outcall conclusion sections for the round, with verification status) +
  the arbiter's procedural announcements (mechanical lines + notes) + the full text of outcalls the debater itself called for + the conclusion sections of the arbiter's neutral supplementary checks. Interim beats, private records, the first-pass ruling,
  the proposed final ruling, objections, the redraft, signoffs, and model calls' heavy-detail grandchildren never enter a debater view; the opponent's outcalls appear only as conclusion sections.
- The detail exception channel: a debater names "detail <id>" in its speech, and you attach **the path of that one object file** after the resume material of the next beat
  (`data/store/<SYM>/<TS>/R.MC1.EV1.json` etc.); attach the path, do not transcribe.
- Objection view = own last-round final + the proposed-final-ruling side file + both sides' objections from the previous round; signoff view = own last-round final + own objections +
  the redraft side file (including the objection disposition record).
- Debaters do not read the previous session's report: continuity must be earned, not inherited (the parent-layer report hydration channel is closed to debaters).

## 6. Resuming and breakpoints

If any step dies, first run `./venv/bin/python scripts/run_step.py status $S` (or run `open` once more; for a session with existing objects it returns
the same resume receipt):
the receipt gives which step it stopped at, the next command, and the material file to use; receipts during the debate are listed per debater (`roles`: whether each is waiting for a beat, has jobs in flight,
or has delivered its final, and each one's next command). `_session.json` records the round, beat counts and focus; `manifest.jsonl` records the minted
objects; an already-minted final is not re-spawned, an already-archived job is not re-dispatched. Redraft archived but the gate did not finish (script crash or interruption) →
`finalize $S --stage ruling` **without `--input`**: reassembles and gates the latest archived redraft, mints no new version, consumes no retry.
A "validator exception" in the gate is an engineering defect, not a report problem: stop and report to the user; do not edit archived objects.
Health check before resuming: `./venv/bin/python scripts/debate_store.py verify $S` exposes double minting, overwrites and ledger misalignment alike; a misaligned session is treated as
poisoned — redo data preparation and start a new session. Views are rebuilt automatically by the script; when a manual rebuild is truly needed, use
`./venv/bin/python scripts/debate_store.py view $S --role zealot|reaper|arbiter`.

## 7. Follow-up (delta) sessions

A follow-up session is a **new session** on the same symbol — new data preparation, new `ts`, new reference price and odds
snapshot, the same rounds / beats / outcalls / closure check / ruling chain / gate as §1-§5 — opened with
`run_step.py open … --base <previous TS> [--why "<one line>"]` (the launcher relays the dispatcher's choice through
`EQUIPOISE_DELTA_BASE` / `EQUIPOISE_DELTA_WHY`, so your commands are the same). Two things differ:
- **Standing evidence.** The previous session's *verified Scout evidence* is re-minted at open as the arbiter's `A.EV…`
  objects (`meta.reused`, `origin_session`, `origin_id`, `last_verified_at` — never reset) and listed in
  `_standing_evidence.md`, which the round-1 materials point to. It is a **follow-up assessment**: debaters reuse these facts
  as previously verified within their recorded scope and as-of date, without routine re-verification; they re-judge
  applicability at today's price and date by reasoning first and re-request only on a specific gap in freshness, scope or
  source; inquiry is focused on changes since the base and their affected premises, and any material coverage gap or
  contradiction reopens it. **Claim admission**: every claim opened names the change since the base it rests on (or the
  specific gap / contradiction found), the fact or premise it affects, and what answer would change the scenario or the
  execution — "the price moved" alone is not a basis. You admit disputes on that footing, require a specific reason for
  repeating a covered check, merge duplicate disputes at registration, and hold both sides to the same closure standard;
  inherited evidence is neither a prior verdict nor proof of present applicability. An old `supports` or claim number does
  not carry over. Data-preparation figures and model calls (P07 / P06 / P01) are never imported: they read the new bars.
- **Reference length** is `config/delta.yaml` `round_ref` (3) instead of 5. It is guidance, not a stopping target: a
  decision-relevant gap reopens the inquiry whatever the round; closing early still needs the one sentence in the notes.
Nothing of the previous ruling, scores, operation, levels, mixture, finals or settled claims is passed — not to the
debaters, not to you (continuity must be earned, not inherited). The machine refuses a base that is not done, not
earlier, older than `max_base_age_days`, or deeper than `max_chain` in a chain of follow-ups. The report carries
`lineage` (`mode`, `base_as_of`, `depth`, `why`, `price_move`, imported ids); the postcheck scores it as any report.
