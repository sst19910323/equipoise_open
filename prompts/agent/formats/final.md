# Final report output format (the arbiter writes the judgment fields; the distribution, the operation, the score and the mechanical fields are derived and injected by the script)

Only field shapes and enumerations are given here; semantics are in `principles.md`, steps in `manuals/synthesis/`. Field order is generation order, following the judgment chain: evidence → reading → mentality → ruling sheet → weight → voids; the operation and the composite score are derived from them.

When the material carries a LENSES block, or your reads list `prompts/agent/formats/lenses.md`, read that file first; its field overrides apply to this format.

## Fields written by the arbiter (strict JSON, no fences)

In the proposed and final ruling, assess downside protection independently for each investable symbol or lens, including each investable GENERAL lens. Supply `protection_by_symbol`, keyed by the exact symbol, with `{"phase":"quiet|emerging|persistent|easing|insufficient","basis":"...","review":"..."}` for each. Omit observation-only indices. In a group, each member ruling covers its own symbol. Use English. Never assume holdings, name an account size, or turn protection into an outright short.

Classify the market phase from the frozen reference price and name the horizon. The machine maps each phase to one neutral hedge signal, like a spot sell signal. Judge the market without assuming any investor exposure, previous trade or instrument. The portfolio layer determines applicability, product, sizing and execution.

- `quiet`: no material downside episode is evident on this horizon. A long-term uptrend does not rule out a near-term event.
- `emerging`: new evidence of worsening transmission, severity or probability opens a material downside episode from today's price. One red bar alone is insufficient.
- `persistent`: the shock still transmits and substantial adverse outcomes remain ahead at current prices. Previously realized losses do not establish further downside.
- `easing`: identify the prior adverse episode in the market data and show why its residual downside is now sufficiently limited relative to plausible recovery to retire this episode's hedge signal. One green bar, a large past fall, or a minor improvement alone is insufficient. A subsequent adverse repricing can start a new episode.
- `insufficient` is an assessment status: name the missing evidence when the forward regime cannot be distinguished.

In `basis`, cite a dated or quantified observation from the frozen materials and compare the residual stress and recovery paths. For a supported stress price S and current reference P, remaining downside is max(0, 1 - S/P); weigh the scenario probability and horizon. Separate company and shared market risk; overlapping indices do not independently confirm a thesis. In `review`, name the observable falsifier and next review time. Phases may skip or reverse; there is no required four-step cycle. The spot score and both spot cards are independent.

```json
{
  "protection_by_symbol": {"<investable symbol>": {"phase": "quiet | emerging | persistent | easing | insufficient", "basis": "Current observable downside versus rebound within the horizon (max 500 characters)", "review": "Falsifier and next review time (max 240 characters)"}},
  "reasoning": {
    "key_drivers": [
      {"direction": "bullish | bearish", "category": "technical | fundamental | macro | news",
       "weight": "high | medium | low", "factor": "name of the factor", "evidence": ["the fact supporting it, one short sentence with the key number; one or two items, no claim numbers"]}
    ],
    "risks": [
      {"risk": "short phrase", "probability": "high | medium | low", "impact": "high | medium | low",
       "mitigation": "mitigating measure", "trigger": "monitoring trigger condition"}
    ]
  },
  "asset_tier": "quality_large | mid | small_spec | na",
  "short_term_path": {
    "direction": "上行 | 震荡 | 下行",
    "target": "touch level; for 震荡 fill \"lower-upper\"",
    "invalid": "reverse invalidation level (required; for 震荡 copy the range verbatim)",
    "horizon_days": 7,
    "rationale": "within forty words"
  },
  "mentality": "with_trend | against_trend | realizing | thesis_exit | exposure_adjustment | continuing | waiting",
  "mentality_basis": "the decision reason's situational fact, plus any material reinforcing or restraining reason (may say two are co-equal), each citing the atom or event that carries it (a verified claim on the table, a trigger signal, or the snapshot; waiting must answer what it waits for, how many trading days, and what happens if it does not come)",
  "claims_review": [
    {"claim": "<claim id>", "claim_rev": "<the claim's current claim_rev: the id of the final record where its current text first appeared, e.g. R2.Z; shown on the tally>", "verdict": "adopted | partly | rejected", "scale": "<only with partly: 0..1, the share of the claim's registered if_false effect that lands — on the owner's tree and on the challenger's alike>", "reason": "one line; with partly: what was hit (the fact, the size, or one part of a compound claim) and what still stands"}
  ],
  "mixture": {"w": "<0..1, the weight of the Zealot's replayed scenario>", "reason": "<required when |2w−1| > 0.5: the disputed claim ids and the evidence id behind the tilt; else null>"},
  "voided": {
    "effects": [{"claim": "<claim id>", "side": "Z | R", "outcome": "true | false", "reason": "documented invalidity: why this registered effect cannot apply"}],
    "candidates": [{"id": "<candidate id from the tally>", "reason": "unparseable | stop_wrong_side | level_unreachable | unknown_event | stop_too_near (a stop the provisional ruling's warnings report as nearer than one ATR to its entry reference; on a `_now` candidate it drops that stop, on a conditional it excludes the contract)"}]
  },
  "risk_management": {
    "stop_loss": "specific level + trigger semantics (touch / close confirmation / N days without reclaim / time stop); only hard conditions judgeable on daily bars (level, comparator, number of closes, volume-ratio threshold, calendar event); no AND with fundamental verification or another symbol's break",
    "take_profit": "specific level or tranche table",
    "max_drawdown_tolerance": "8%",
    "review_triggers": ["re-evaluation triggers to monitor"]
  },
  "known_flaws": [
    {"flaw": "known flaw", "disposition": "refuted | priced", "how": "who refuted it (cite number) or which node / path carries it", "refs": ["<node id when priced>"]}
  ],
  "next_review": {
    "recommend_after": "UTC time: when the duty loop should call for a re-evaluation. It must point to one of the events below (its date, or the window start when the hour or day is uncertain — write it early, never late) or to the horizon end; a call time with no event behind it is not written",
    "events": [
      {"id": "E1", "name": "what is expected (the issuer's Q3 report / CPI / FOMC / a product event / a peer's or customer's report)",
       "who": "issuer | peer | customer | macro | sector", "date": "YYYY-MM-DD", "date_precision": "exact | date | window | tbd",
       "window_end": "YYYY-MM-DD (only with date_precision window)", "source": "where the date comes from",
       "tests": ["<claim ids and node ids this event tests, e.g. Z1, M3>"], "coverage": "full | partial | none (which outcomes the scenario already carries)",
       "check": "what to look at when it lands (the number, the guidance line, the decision)",
       "if_compatible": "what to do when the outcome fits the carried nodes", "if_broken": "what to do when it breaks the premise (cite the exit / trim clause)",
       "if_unknown": "what to do while the outcome cannot be read yet"}
    ],
    "notes": "the arrangement between now and the events: which position and order constraints stay in force; an order's validity if it differs from the default"
  },
  "residual_adjustment": {
    "basis": [
      {"kind": "voided entry", "issue": "the claim id or candidate id voided", "reason": "..."},
      {"kind": "audit correction", "issue": "what changed from the first ruling to the final", "reason": "cite claim or evidence numbers"}
    ]
  },
  "statement": "one sentence summarizing the core reason for the decision",
  "debate_summary": "evolution of each side's position, the topic settlement list (one line per topic: number / how closed / ownership and settled amount), consensus, retained disagreements, the final overall reasoning (the weight cites numbers); when the derived composite score and the short-term path point in opposite directions, say why"
}
```

## Field notes (shape only)

- `reasoning.key_drivers[].evidence` may only contain content that appeared in the debate, the ledger, and verified evidence; pure hindsight description is not a main driver. This column is for dashboard readers: summarize into short sentences, write facts and numbers, do not write claim or evidence numbers (numbers stay in the ruling list, residual adjustment, and summary, where traceability is needed).
- `asset_tier` per the size-and-quality tiering manual.
- The JSON above is a shape illustration with placeholders in angle brackets, not a parseable document.
- `mentality`: one of the seven decision reasons (mentality manual); `mentality_basis` cites the atom or event that carries it (principles section 3).
- `claims_review`: one entry per verified executable claim, written at every stage (first ruling, draft, redraft); `adopted` settles the claim for its owner, `rejected` against, and each side's registered effects for it are replayed onto that side's scenario before the mixture. `partly` settles it against its owner in part: each side's registered `if_false` effect lands at `scale` of its size (probability moves and splits by amount, an end price that share of the way), whether the side still held the effect or had already applied it whole in a round — the replay keeps only the ruled share, so an over-concession is taken back and a refusal to move is charged by the same entry. `scale` is argued in `reason` from what was hit (the fact, the size, one part of a compound claim) and what still stands; it is not a default half. An effect that has no share (a `path_set`) is ruled whole or voided. `claim_rev` is the claim's current revision as shown on the tally. Rule on the target claim, never on a challenge: a challenge's verdict is its target's (write the target's id; both sides' registered effects follow from that one verdict); an id that is not a filed claim, a challenge id or a stale `claim_rev` is refused.
- `mixture.w`: one weight per session; the session's distribution is `w · Z′ + (1 − w) · R′`, where Z′ and R′ are the two sides' scenarios replayed by the script (frozen opening + accepted updates + settled effects). The weight is a judgment over both complete replayed scenarios, including any material cross-lens trade-off; favoring a side does not mean favoring the same market direction in every lens. Up to |2w−1| = 0.5 no reason is written; above it `reason` names the disputed claims and the evidence (by id: at least one claim id and one returned evidence or model-call id of this session); at `w` 0 or 1 it also says why every atom of the discarded side that still changes the chosen strategy is discarded, naming each claim whose effect was applied in the discarded side's replay, or writing "no decision-relevant atoms discarded" when there is none. The script checks the ids at the ruling and again at release.
- `voided`: only documented invalidity — an effect whose ops cannot apply to the replayed state (node missing, amount above the node's p; the script attempts the ops and a void on an effect that does apply is rejected: "void rejected: effect is executable"; name the `side` whose replay fails), an effect a side CARRIED through an `applies` update whose outcome contradicts your settlement of that claim (both sides conceded R2 in opposite directions, say: the Zealot carried `R2#true`, you rule R2 `rejected` → void `{claim: "R2", side: "Z", outcome: "true"}`; the replay skips that update before the settled effect applies — this is the only repair, the replay cannot reverse a carried update), a candidate that does not parse, has its stop on the wrong side, cannot reach its level under any atom, or names an event not in the scenario. Disagreement about profitability is not a ground: it belongs in the weight and the comparison. You may not add a candidate.
- An objection-stage `scenario_revision` from a debater enters the replay only when the redraft changes `claims_review` or `mixture`; a redraft that changes only `voided` leaves it as an endorsement.
- You do not write `scenario`, `operation_type`, `operation_target` or `operation_size` (rejected as derived fields authored), nor `composite_score` (an authored value is kept as `authored_score`). The script compares five strategies (neutral, overweight now / on a condition, underweight now / on a condition) on the mixture over the union of both sides' last-round contracts and maps the winner: overweight → `ENTRY` (market / limit derived from the level), underweight now → `TRIM_POSITION` or `LIQUIDATE_NOW` (the whole unit), underweight on a condition → `TRIM_POSITION` with the condition, neutral → `HOLD_NEUTRAL` ("neutral; reference target exposure 50% of the standard unit").
- `known_flaws`: the main counter-arguments and their effect; a `priced` flaw names the node or path that carries it in `refs`.
- `next_review` (2026-11-06): **events are the reasons, `recommend_after` is the call time.** Price trigger lines are retired — the machine measures how far the price has left this report's own scenario tree every ten minutes (divergence), and a level with no story is not news; the levels with meaning already live in the tree's events, in `operation_execution.conditional` and in `risk_management`. Write every expected event inside the horizon that tests this judgement, and the one future fact that would adjudicate the residual disagreement (cite the claim); whether an event is worth writing is your judgement — under- and over-estimating one are both errors the postcheck grades. The machine adds `expires_at` (the horizon end) and `known_events` (what it could see on its own: the issuer's expected report from quarterly cadence, scheduled macro releases); a known event you neither list nor dismiss with `coverage: none` raises a gate warning, not a failure. Unsure of the hour or the day: `date_precision` window with `window_end`, and `recommend_after` at the window start.
- A limit order is not required to state a validity period: if written, as written; if not, 3 trading days counted from the trading day after the base date (inclusive of the last day). `short_term_path.horizon_days` is not the order's expiry, and `next_review`'s re-evaluation time does not automatically cancel the order. Fill, exit, and trim conditions are written only as hard conditions judgeable on daily bars (level, comparator, number of closes, volume-ratio threshold, calendar event).
- `residual_adjustment.basis`: each item is one of two kinds (`voided entry` / `audit correction`); when the first ruling and the final differ, every change needs an audit item.
- `statement` is the one-sentence core reason for the decision; it does not express confidence.

## Fields injected by the assembler (the arbiter does not write them)

`scenario` (the mixture A, with `scenario.mixture = {w, t, z_prime, r_prime, reason}`), `operation_type`, `operation_target`, `operation_size`, `operation_execution` (the conditional contract, when one was chosen), `composite_score` (`round(S_ref)` of A), `scenario.derived` (`q_star`, `x_star`, `x_opt`, `candidates[]` with provenance, `chosen`, the session cost in bps, `unit_basis`, `s_ref`, `display`, `replay`), `provisional_ruling` (the first ruling archived with its derivation: `claims_review[{claim, claim_rev, verdict: adopted|rejected, reason}]`, `mixture`, `voided`, `mentality`, `rationale`, and the derived `scenario`, `operation_*`, `composite_score`), `s_ref`, `s_ref_z`, `s_ref_r` (the machine score of A and of each side's endorsed scenario), `odds` (the session's ruler snapshot: γ, σ_ref, cost constants, the computed cost, `unit_basis`, provenance), `spawns`, `schema_version`, `objections` (the objection rounds' original text and handling record), `dissent` (signoff: `zealot_composite_score` / `zealot_reservation` / `zealot_endorsed_scenario` / `reaper_composite_score` / `reaper_reservation` / `reaper_endorsed_scenario`, and the version signed), `signatures` (the actual model identifiers of the three parties), `evidence`, `evidence_added`, `model_calls`, `evidence_generated_at`, `search_rounds_used`, `debate_history`, `arbiter_log`, `arbiter_private_scores`, `data_files`, `market_data_timestamp_utc`, `report_generated_at`, `reference_prices` (this session's reference prices: the latest price in the opening technical snapshot, each with its own quote time. It does not change the meaning of `operation_target`).
