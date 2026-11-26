---
description: Run the complete debate flow for a single symbol (main session = arbiter, calling run_step.py step by step per orchestration/run-protocol.md). Usage: /run NVDA
---

Execute a complete debate for symbol `$ARGUMENTS`. **Run only one symbol at a time** (multiple symbols in parallel = multiple sessions).
`$ARGUMENTS` empty → prompt the user: "Tell me which symbol to run, e.g. `/run NVDA`".

This file only says how the Claude Code engine executes the protocol; **the flow, judgment points, materials and failure handling are all in
`orchestration/run-protocol.md`; Read it completely first**, then Read `prompts/agent/principles.md` and
`prompts/agent/roles/arbiter.md` (you are the arbiter).

**Language**: everything you write inside this debate is English — notes, rulings, every Agent-tool message and job text — even if the conversation
with the owner was in Chinese. Quoted source passages keep their original language. (Protocol §0.)

## Engine facts

- Runs inside the single `equipoise` project (fired by `skills/equipoise-run-debates/scripts/cc_loop.py`, or `claude -p "/run <SYM>"`).
  Data completeness is decided by the orchestration layer before firing (`prepare_debate_data.py` returns READY); you do not connect to IBKR, do not connect to PG,
  and do not sync across machines. If `run_step open` reports `code 10` (data not ready), STOP and tell the user what is missing and the data-preparation command.
- Model split: debaters (zealot / reaper) use opus, executors (scout / runner) use sonnet — already set in the frontmatter of `.claude/agents/*.md`,
  taking effect automatically on spawn; you (the main session) stay on opus. `open --engine cc --model <your exact model id>`
  (copy the "The exact model ID is ..." sentence from your system prompt if it has one; otherwise use the Opus 5.5 ID from the
  Claude 5 family list in your system prompt — cc_loop launches you with `--model opus` — do not search; verified 11-06: in
  `claude -p` mode the system prompt lists the family IDs but has no "exact model ID" sentence).
- Artifacts land in local `data/`; **the main session does not write PG** (the PG write is run by the orchestration layer via `scripts/pg_store_debate.py`, which has the release gate built in).

## Tool mapping

| protocol action | Claude Code practice |
|---|---|
| spawn debater / executor | Agent tool, `subagent_type` = zealot / reaper / scout / runner; message = the material file in full (Read it and paste verbatim, nothing added, removed or paraphrased); both debaters in one concurrent message |
| translate (last step, protocol §4) | Agent tool, `subagent_type: translator` (haiku), one per language; message = `Translate the staging file <path> in place; reply with the path and the count.`; then include the staging path in your final report; the orchestration layer (cc_loop, or you in a conversational run) runs `translate_reports.py --load-report <Analysis path>` after the PG write |
| follow up in the same thread (next beat) | SendMessage / resume to the **same** Agent thread, message = `_resume_*.md` in full; close the thread once that side's final is archived |
| harvest debater / executor JSON | the last line of every material gives the `OUTPUT` path. Debaters have no file-writing tool: write the JSON they return verbatim to that path (not a character changed) and pass the path to `run_step`; executors have Bash and write into the `OUTPUT` file themselves, replying with the path only. The same artifact is generated only once: do not have the subagent rewrite it, and do not paraphrase it yourself |
| responsive within a round | the two debaters each walk their own beats: route whichever side's beat has arrived and start its jobs immediately; when that side's results are complete, `close-beat --role <that side>` and follow up immediately; only `tally` waits for both finals |
| child thread slow to deliver | first check whether its `OUTPUT` file and tool actions show progress; only when there is truly none, interrupt and resend the same material (a bounded number of times, reference: once); if that still fails, handle per protocol §2.3 |
| Read / Glob | read only views, receipts, materials, manuals; **do not read raw data files** |
| Bash | run only the commands given in the protocol's sections (protocol §0), verbatim; `open` takes `--engine cc --model <your exact model id>` |
| WebSearch / WebFetch | only when you personally execute by the book in place of a dead executor (Scout / Runner) (protocol §2.3) |

## Permissions and threads

- Server root uses `--permission-mode dontAsk`; Agent needs no authorization under dontAsk.
- Thread hygiene: a debater thread stays open across the beats of this round and is closed as soon as its final is archived; an executor thread is closed as soon as its result is archived. The object store is the archive;
  do not rely on open threads to remember things.

## Finishing

Give the user a ≤ 250-character report per protocol §4 (with the translation staging path); remind the orchestration layer to run the PG write and then `translate_reports.py --load-report <Analysis path>` (cc_loop does both itself). **An unfinished debate is not a valid final state** — "in progress / remaining rounds not run"
is not a status, it is a violation; only the protocol's failure halts (`code 10/20`) may end early.
