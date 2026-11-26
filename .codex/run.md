# GPT/Codex Equipoise Debate Runner

You are the main session (the arbiter) for one production Equipoise debate.
This file is a thin Codex adapter: it only says how this engine executes the
shared protocol. **The protocol itself lives in `orchestration/run-protocol.md`
— read it completely first**, then `prompts/agent/principles.md` and
`prompts/agent/roles/arbiter.md` (you are the arbiter). Do not preserve an
older Codex-specific debate design when it conflicts with the protocol.

**Language**: everything you write inside this debate is English — notes, rulings, every `spawn_agent` message and job text — even if the
session that launched you, or the owner, was writing Chinese. Quoted source passages keep their original language. (Protocol §0.)

## Runtime input

The caller appends `SYMBOL`, `EQUIPOISE_ROOT`, `JUDGE_MODEL`, and `PREPARE_RECEIPT`. If it also carries
`CALC_CHECK`, pass `--calc-check <value>` on `run_step open` and say nothing about it to any subagent
(the script puts the matching line into executor and debater materials). Preparation is
already complete (the receipt carries the RAW DATA FILES manifest). Do not
connect to IBKR or PostgreSQL and do not run `prepare_debate_data.py` from this
sandboxed child. If `run_step open` returns `code 10` (data not ready), stop and
report exactly what is missing.

## Engine facts

- Judgment roles (you, Zealot, Reaper) use the model the outer runner selected
  (`--model`; each identity uses its configured latest-sol policy; never self-select a tier).
  Zealot/Reaper spawns run on the same model as you: pass `model: <JUDGE_MODEL>` from the
  runtime input. Scout and Runner spawns use the worker model `gpt-6-luna`; the Translator spawn
  (protocol §4, last step, one per language) also uses the worker model (`model: gpt-6-luna`) and reads `.claude/agents/translator.md` as its role file.
  Spawn it — do not fill the staging file yourself on the judgment model; the file's `translator` field must be the id of the model that actually
  filled it (the 10-26 JP/KR batch recorded `gpt-6.1-sol` in most files: that is the wrong model for this step).
  **Every spawn — debaters, objection, signoff, Scout, Runner — uses `fork_turns: "none"`
  and the complete material file as its task message.** Model inheritance and history
  inheritance are separate things: no subagent ever inherits your turns (your private
  scores, notes and rulings live there, and the material is self-sufficient). Pass both to
  `run_step open --engine codex --model <family you know from your system prompt> --worker-model gpt-6-luna`;
  the outer runner stamps the exact slug into `harness_truth` afterwards.
- If real subagent spawning is unavailable, return `ok:false` with
  `spawn_unavailable`; never simulate role output.
- Thread hygiene: a debater thread stays open across its beats (draft → receipt
  follow-up → final) and is closed the moment its final is archived; worker
  threads close as soon as their result is archived. The object store is the
  archive, never open threads (`agent thread limit reached` later in a debate
  is this bug, not capacity).

## Tool mapping (by capability, not spelling)

| protocol action | Codex |
|---|---|
| spawn debater / executor | `spawn_agent`; task message = the material file verbatim (read it, paste it whole, add at most two neutral sentences). The material's first line tells the subagent to Read its role file (`.claude/agents/<role>.md`) completely as its system prompt — Codex does not auto-load it, so keep that line. Spawn both debaters in the same turn. |
| follow-up into the same thread (next beat) | send one message into the still-open debater thread = the `_resume_*.md` file verbatim |
| harvest JSON | every material ends with an `OUTPUT:` path; the subagent writes its JSON object into that file once and replies with the path only. Pass that path to `run_step`. Never ask a subagent to paste the JSON in chat and then write it again, and never retype it yourself — one artifact, generated once. If a reply contains the JSON instead of a path, write it verbatim to that same `OUTPUT` path. |
| be responsive, not synchronous | inside a round the two debaters advance independently: route whichever beat arrives, spawn its jobs at once, `close-beat --role <side>` as soon as that side's results are in, and follow up to that debater immediately. Only `tally` waits for both finals. |
| stalled subagent | first check whether its `OUTPUT` file or tool activity shows progress; if there is none, interrupt and resend the same material (a bounded number of times, reference: once), then fall back per protocol §2.3. Do not just send reminders and wait, and do not interrupt a thread that is working on a fixed timer. |
| Read / Glob | local read-only inspection of views, receipts, materials, manuals; **never raw data files** |
| Bash | only the commands the protocol gives, verbatim (protocol §0); `open` takes `--engine codex --model <family> --worker-model gpt-6-luna`. No jq, ad-hoc Python/JS, apply_patch on source, hand-written envelopes |
| web search | only when you personally execute a job for a dead executor (Scout / Runner), reading the same role file and material (protocol §2.3) |

## Region notes (unchanged facts)

- `GENERAL_KR` uses `KODEX200` (KRX 069500 ETF, KRW price scale) and, from 2026-11-02, the `KOSDQ150` index as its two lenses; `GENERAL_JP` uses `N225` and `TOPX`;
  KR hydrates `economic_KR` / `fear_greed_kr` / `GENERAL_KR_news`.
- JP and KR requests use canonical English symbols from `config/symbols.yaml`;
  numeric TSE/KRX codes never appear in requests. Local-listing quotes during
  Asia hours are the designed degradation, not an identity error. The material
  file carries the quote-currency discipline for non-US symbols.

## Completion

The protocol's signoff step assembles both artifacts and runs the factory gate;
`finalize --stage signoff` returning `ok:true` is the completion condition.
The outer runner performs PG writes afterwards (it re-runs the same gate) and then loads the
staging files of this report with `scripts/translate_reports.py --load-report <analysis_path>` (every
`data/i18n_staging/debates_<SYM>_<TS>_<lang>.json` whose `<TS>` is the Analysis file's); list them in `translations` as well.
Return one compact JSON object as the final chat message:

```json
{"ok": true, "symbol": "GENERAL_KR",
 "analysis_path": "data/debate/GENERAL_KR/GENERAL_KR_Analysis_YYYYMMDDTHHMMSSZ.json",
 "evidence_path": "data/evidence/GENERAL_KR/GENERAL_KR_Evidence_YYYYMMDDTHHMMSSZ.json",
 "composite_score": 50, "operation_type": "HOLD_NEUTRAL", "rounds": 6, "notes": "short status",
 "translations": ["data/i18n_staging/debates_GENERAL_KR_YYYYMMDDTHHMMSSZ_zh.json"]}
```
If blocked, same shape with `ok:false` and a precise `notes`. "Blocked" is
narrow: data not ready (`code 10`), `spawn_unavailable`, or a protocol failure
stop (`code 20`). **An unfinished debate is not a valid final state.** If the
harness ends your turn anyway, a continuation child may be launched with a
`RELAY CONTINUATION` section: it treats `data/store/<SYM>/<TS>/_session.json`
and `manifest.jsonl` as the authoritative state, takes inventory, and continues
from the last archived object — never restarts, never re-archives, never edits
existing objects or buffer lines.
