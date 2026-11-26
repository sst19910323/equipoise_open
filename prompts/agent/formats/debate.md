# Debate-round output format (shared by Zealot and Reaper; Read as the first action every round)

Only field shapes and enumerations are given here; field semantics are in `principles.md`. Field order is generation order: reasoning first, then settlement, then claims, then path, mentality, operation, and finally the filings (the composite score is written by the script).

## Character discipline for the body text

The speech text is stored verbatim inside a JSON string: quote the other side's words and news headlines with 「」, not ASCII double quotes; wrap numbers and field names individually in single backticks, no triple-backtick code blocks; do not paste sets of structured data fragments into the body; backslash is used only for paragraph breaks `\n`; small markdown tables are allowed, large tables are split into items. Ordinary paragraphs, `##` headings, bullets, and bold are allowed.

## Strict JSON (no explanatory text, markdown markup, or code fences)

When a LENSES block is present, first read the rules file it names; its field overrides apply to this format.

```json
{
  "analysis_text": "the report-form speech body (organized by the judgment chain; in non-opening rounds, first dispose of the topics in debate, then state, then digest returned evidence, finally file)",
  "stance_ledger": {
    "held": [
      {"claim": "the other side's claim in one sentence", "evidence_tier": "T3", "response": "rebut | evidence_requested | contingent | settled_prior", "note": "why this consideration is not enough to move me"}
    ],
    "contingent": [
      {"id": "Z-C1", "if": "a falsifiable hard condition (level, comparator, number of closes, volume-ratio threshold, calendar event; no AND with fundamental verification or another symbol's break)", "then": "action once triggered"}
    ],
    "reclaims": [
      {"of": "number of the original ledger entry", "reason": "what new evidence overturned that entry's basis"}
    ]
  },
  "claims": [
    {"id": "Z1", "kind": "own", "claim": "one-sentence claim", "evidence_tier": "<T1|T2|T3>", "status": "open", "answers": [],
     "effect": {"if_true": [{"op": "p_move", "from": "C", "to": "A", "amount": "<0..p(C)>"}], "if_false": []}},
    {"id": "Z3", "kind": "challenge", "target": "R2", "claim": "answering the other side's claim", "evidence_tier": "<T1|T2|T3>", "status": "open",
     "answers": ["R.EV2"], "effect": {"if_true": [{"op": "p_move", "from": "A", "to": "C", "amount": "<amount>"}], "if_false": []}},
    {"id": "Z2", "kind": "own", "revises": "<claim_rev of the text being replaced, e.g. R2.Z; only when the claim text changed>", "claim": "the narrowed claim", "evidence_tier": "<T1|T2|T3>", "status": "open",
     "answers": ["R.MC1"], "effect": {"if_true": [{"op": "split", "node": "A", "new_node": {"id": "A2", "label": "short name", "P_T": "<price>", "events": []}, "amount": "<0..p(A)>"}], "if_false": []}}
  ],
  "scenario": {
    "schema": "3.4", "T_days": 15, "P0": "<opening reference price, copied>",
    "nodes": [
      {"id": "A", "label": "short name", "p": "<0..1>", "P_T": "<end price on the valuation day>", "refs": ["Z1", "Z.EV2"],
       "events": [{"t": "<trading day 1..T>", "kind": "gap_open | open | touch | close", "price": "<price>"}]}
    ],
    "event": {"name": "<scheduled event or omit>", "date": "YYYY-MM-DD", "folded_into": ["A"], "implied_move_pct": "<from P01>", "source": "P01"},
    "execution": {"overweight": {"stop": {"level": "<price below P0>", "semantics": "close | touch"},
                                 "conditional": [{"name": "...", "observe": "<close | touch | open | gap_open> <> | >= | < | <=> <level>", "fill": "next_open | at_level"}]},
                  "underweight": {"stop": {"level": "<price above P0>", "semantics": "close | touch"}, "conditional": []}}
  },
  "scenario_updates": [
    {"claim": "R2", "applies": "R2#true | null", "ops": [{"op": "p_move", "from": "A", "to": "C", "amount": "<amount>"}], "refs": ["R.EV2"], "why": "one sentence naming the evidence"}
  ],
  "short_term_path": {
    "direction": "上行 | 震荡 | 下行",
    "target": "touch level; for 震荡 fill the range \"lower-upper\"",
    "invalid": "reverse invalidation level (required; for 震荡 copy the range verbatim)",
    "horizon_days": "<int, trading days — your own window, independent of the ruler's T_days>",
    "rationale": "one sentence, within forty words"
  },
  "mentality": "with_trend | against_trend | realizing | thesis_exit | exposure_adjustment | continuing | waiting",
  "mentality_basis": "the situational fact behind the decision reason, plus any material reinforcing or restraining reason (may say two are co-equal), each citing the atom or event that carries it (waiting: what, how many days, what if it does not come)",
  "operation_type": "ENTRY | HOLD_NEUTRAL | TRIM_POSITION | LIQUIDATE_NOW",
  "operation_target": "operation reference level, only two shapes: a number, or for 震荡 lower-upper (acting at the current price → fill the current price; waiting for a level → fill that level; market vs limit is not an option; HOLD also fills a level as anchor, never N/A); tranches, fill conditions, and invalidation lines go in stance_ledger.contingent and the body, not in this field",
  "summary_statement": "one-sentence core view",
  "request_evidence": [],
  "beat": {"status": "final | need_more", "next_is_final": true}
}
```

## Field notes (shape only)

- The JSON above is a shape illustration with placeholders in angle brackets, not a parseable document: every value you output is concrete.
- `stance_ledger`: output every round; keys with no content get `[]`; `held.evidence_tier` takes `T1` to `T4`. There are no point moves: a settlement is a `scenario_updates` entry.
- `claims`: output in full every round. `id` is the numbered address (Zealot uses `Z1/Z2/…`, Reaper uses `R1/R2/…`, sub-claims `Z2a`), unique in the session; references always use the number. `kind`: `own` / `challenge` (must carry `target` = the other side's claim number) / `reinforce` (a new finding or a doubt now excluded). `evidence_tier` is self-reported `T1` to `T3` (T4 is not fit to be a claim; T1 and T2 must be backed by a machine number on the table or an outcall number; the arbiter may overrule). `effect` registers, at first filing, the ops (grammar below) that land on **your own** scenario's node ids if the claim holds (`if_true`) or fails (`if_false`); `[]` means "already inside": the claim's paragraph names the existing node(s), probability and channel that already carry that outcome — for either outcome, on an own claim or a challenge alike; accepting a claim you challenged under an empty effect is a no-change that needs that reason. It is frozen with the claim text: a changed `effect` under unchanged text is rejected; to change it, revise the claim — new text, `"revises": "<claim_rev>"` and a new `effect`; a claim's `claim_rev` is the id of the final record in which its current text first appeared (e.g. `R2.Z`, the id on every record in your view). An effect your `applies` has already carried cannot be changed by revising the claim (rejected: "effect already carried"): file the further change as a new claim with a new id — on an opponent's claim that new claim is a `reinforce`, never a second challenge. Your registered effect for an opponent's claim is the `effect` on your `challenge` that targets it (no challenge = empty effect); on a `challenge`, `if_true` / `if_false` mean the *target* claim holds / fails, and `applies` always names the target claim's id (`R2#true`), never the challenge's; one challenge per target per side — while its effect is not yet carried, change it by revising it (`revises`), never by filing a second challenge on the same target; once carried, add a `reinforce` claim as above. `answers`: the evidence / model-call ids this claim responds to (default `[]`); only a challenge, update or status change that names the evidence id here counts as engaging it — a mention in prose does not. `status`: `open` / `settled_for` / `settled_against` / `conceded` / `deadlocked` / `withdrawn` / `uncontested`; your `deadlocked` is a request, shown as `deadlock_requested` until the arbiter's closing record passes; likewise your own `settled_for` / `uncontested` does not close your claim — it stays open until the other side concedes it or the arbiter rules on it (`uncontested` closes by itself only when no challenge ever targeted it), and a challenge you mark `settled_for` stays open until its target's owner concedes or withdraws the target or the arbiter rules on it. `conceded` / `withdrawn` on your own challenge concedes its target: the target claim settles as holding, and both sides' registered `if_true` effects for it apply once. `withdrawn` on your own claim that a challenge ever targeted counts as `conceded`: the claim settles as failing and both sides' registered `if_false` effects for it apply once (an unchallenged claim is simply withdrawn). The opening produces two to four `own` claims.
- Op grammar (one grammar for `effect`, `scenario_updates[].ops` and `scenario_revision.ops`): `{"op": "p_move", "from", "to", "amount"}` (both nodes exist; a node whose p reaches 0 is removed); `{"op": "P_T_set", "node", "value"}` (rewrites the node's last `close` event too; it may not move `P_T` across `P0` — a node above `P0` stays above, below stays below: to put weight on the other side, `split`); `{"op": "path_set", "node", "events"}`; `{"op": "split", "node", "new_node": {"id", "label", "P_T", "events"}, "amount"}` (the new id must not exist). Sub-branches are addressed `"<node id>/<sub id>"`; a move across parents is not expressible — restructure with `split`. A list of ops is atomic and never renormalized.
- `scenario`: the one ruler (principles section 5). Mutually exclusive nodes, probabilities summing to one (rounding within 0.01 is normalized and recorded; anything else is invalid), `P_T` on the common valuation day (`T_days` fixed by the orchestration; `P0` copied from the opening reference price), `refs` to the claims or evidence each node rests on, and `events`: the ordered execution events that let the machine compute every strategy's return on that node — a `close` stop triggers only on a `close` beyond it and fills at the next day's first `open`/`gap_open`; a `touch` stop fills at the touching event; a conditional order triggers on its `observe` event (`close`, `open`, `gap_open` match that event kind; `touch` matches any event at or beyond the level, like a touch stop) and fills per its `fill`: `next_open` = the next day's first `open`/`gap_open`, `at_level` = the triggering event's price. A node may carry `sub` branches (`p_cond` summing to one, each with its own `events` and `P_T`). The short-term reading of the tree (day 3 / day 5 on the tally) is taken from `close` events only — a `touch` or `gap_open` is not a closing position; write a `close` where you mean one. Both sides of P0 need a node unless you argue why one side has zero probability. The machine shows each node's `z` (end move in units of ATR·√T): three or more is allowed and needs a transmission path. Emit the full `scenario` every round, with `schema: "3.4"`.
- `scenario.execution`: one contract per direction — `overweight` (stop below the fill) and `underweight` (stop above, a buy-back); either may be `{}` or absent; `conditional[].fill` is `next_open` or `at_level`. The cost is not written: it is computed from observable measures (volatility, traded value, venue) at the opening and shown on the tally lines; dispute it only by disputing its inputs. Your last-round contracts join the other side's as the candidates of the final comparison; an unfiled direction has no conditional candidate.
- `scenario_updates`: `[{claim, applies, ops, refs, why}]`, every change to your scenario since your previous round, each bound to the claim or evidence that caused it. The script replays this round's `ops`, in order, onto your previous round's scenario and must reproduce this round's `scenario` exactly — keep node ids stable, append new nodes last, change prices and paths only through ops. `applies: "<claim id>#true|#false"` when the update carries a registered effect (its `ops` must equal that effect's list exactly, once per effect and outcome; for an opponent's claim, the list on your challenge of it — in the example, the update `R2#true` carries exactly what `Z3` registers under `if_true`), otherwise `null`. An update that carries a registered effect must name it in `applies`; `applies: null` is only for an independent increment (an update with `applies: null` whose ops equal any registered effect of your side (either outcome), whatever `claim` it names, is rejected — write `applies` for it; `applies: null` is only for an independent increment with its own ops). The opening round has `scenario_updates: []`; the opening scenario is frozen as your commitment.
- `short_term_path`: five fields, independent each round. `horizon_days` is an integer in trading days — your own window for the path, not the ruler's `T_days`.
- `mentality`: one of the seven decision reasons in the mentality manual; `mentality_basis` is its situational fact, with any material reinforcing or restraining reason (or two co-equal reasons), each citing its atom or event.
- Size is not written: the assembler derives it from the adopted scenario and the chosen strategy (principles section 4). Argue size by changing inputs.
- `composite_score`: written by the script (`round(S_ref)` of your scenario); you may omit it, and an authored value is kept only as `authored_score`.
- Visibility has one layer: each party sees only its own direct results; the other side and the arbiter understand the evidence through cited speech. All snapshot fields are prefixed with the symbol's code; when citing across symbols do not strip the prefix.
- `request_evidence`: the list of outcall filings; `[]` when none this round. Three kinds of element:
  - data call: `{"type": "data", "mode": "peer_compare | bars | relative_strength | econ_series | event_calendar | pair_link | trend_fit", "symbol": "NVDA", ...mode parameters}`
  - evidence search: `{"type": "search", "target_claim": "which claim it targets (write 'self-check' for a self-check)", "question": "what to look up", "would_change_my_mind": "what result would change your position"}`
  - model call: `{"type": "model_call", "model": "P01 | P02 | P03 | P04 | P05 | P06 | P07 | P08 | P09 | P10 | P11", "issue": "topic number (required)", "params": {"...": "the target the manual asks for, one-sentence level"}, "refs": ["optional: paths of files on the table or numbers of returned evidence for the executor to check against"]}`
- `beat`: this beat's declaration. When `status` is `need_more`, `request_evidence` must not be empty, and `next_is_final` declares whether the next beat is the final; when `status` is `final`, this speech enters the record.
