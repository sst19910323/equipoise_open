# Postcheck Protocol (shared by both engines; the sole authority on scoring and attribution)

Reader: the main session acting as reckoner (the settler) — Claude Code's `/postcheck`, or Codex's postcheck subprocess. This document is the full protocol;
how the engine is launched and how batches are resumed are in `skills/equipoise-postcheck/`.

**Task**: settle, after the fact, an old debate report that has "matured". Fetch the report from PG `debates` → `postcheck_quant` computes
the deterministic price path over the [T, T+dt trading days] window → **you settle in person** how accurate this verdict was (wrong-footed / missed / hit) and the attribution worth digging into →
write PG `equipoise.postcheck`. The front end reads postcheck for display.

**Language**: the verdict, diagnosis and every string you write are English, whatever language the conversation that launched you used; quoted source passages keep their original language.

**You are the reckoner.** The deterministic facts are computed by scripts and fed to you; you only judge and attribute. The `reckoner` signature = **the model id you are actually running as**
(how to obtain it is on this engine's page), filled into the `reckoner` field of the judgment JSON; do not copy any example value from this document. If omitted it falls back to
the constant in `postcheck_store.py`, and that constant is `claude*` — **the Codex engine must never omit it**, or attribution gets scrambled. Whether "an agent
has postchecked this" is judged by prefix family: `claude*` = CC, `gpt*` / `codex*` = Codex, regardless of the specific version number; anything else (deepseek) = pending replacement.

> **Single main session, linear execution, no sub-agent spawned at any point** (a postcheck is not a debate; it needs no adversarial personas). From Step 0 to Step 5 you
> alone walk through it symbol by symbol, tier by tier; when verification is needed, search yourself. Do not launch Zealot, Reaper, or Scout for a postcheck, and do not use the debate's declaration mechanism.

Run on the server at `/opt/equipoise` (PG on localhost; `debates` read-only, `postcheck` is one table shared by all engines). python is always
`./venv/bin/python`.

## Step 0: Parameters

Parameters = `<SYMBOL> [--dt N] [--as_of <TS>]` (for CC passed in by `/postcheck`, for Codex given by the parent layer in the prompt):
- SYMBOL = the first token, upper-case (including `GENERAL` / `GENERAL_EU` / `GENERAL_JP` / `GENERAL_KR`). Recognize Chinese colloquial names yourself.
- `--dt N` = a single postcheck tier (trading days). **Omitted → run the standard tiers 3 / 20 in turn**, each as an independent postcheck row. (The 50d tier was retired 2026-09-30:
  not much valid information carries to 50 trading days; the evaluation method for 20d is still being refined.)
- `--as_of <TS>` = postcheck exactly that issue of the report; omitted = the **latest matured** report under that tier.
- Parse failure → output `{"error":"usage: <SYMBOL> [--dt N] [--as_of <TS>]"}` and stop.

## Step 1: Fetch the report + freshly request the result price + compute the window (once per tier)

For each target tier dt (internally, prep **connects to IBKR and freshly requests** the symbol's daily bars before computing swing lines — the result price must be freshly requested this run,
not eaten from a leftover old CSV; hence carry `EQUIPOISE_IBKR_PORT=4001`):
```bash
EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python datalayer/pg/postcheck_store.py prep --symbol <SYMBOL> --dt <dt> [--as_of <TS>]
```
Receipt `status`:
- `ok` → you get `staging_path` + a fact summary (`ret_pct`/`mfe_pct`/`mae_pct`/`touched_target`/`touched_stop`/`short_term_outcome`/`win_start~win_end`; GENERAL goes through `members[]`). The receipt carries `ruler` (the ruler version, recorded automatically by the digest; you do not fill it in): re-grading the same report with a different ruler is stored as a separate row in PG, and the old score is not overwritten. Proceed to Step 2.
- `immature` → the window has not run its course, or **the result price was not obtained this run** (IBKR down); **skip this tier** (normal; if the why mentions IBKR, say so clearly in the receipt so the upper layer knows).
- `no_report` → no matching report in debates; skip.
- No tier returned `ok` → output `{"symbol":...,"status":"nothing_mature"}` and stop.

## Step 2: Settle first, score second (you make the call — wrong-footed / missed / hit)

`Read` `staging_path` to get `digest` (the deterministic price-path facts) + `report` (the verdict as it stood at the time).

**Report = operation decision + market analysis; weigh both equally in assessing how well this verdict was realized**:
- **The operation decision matters equally**: real-money direction and size (`operation_*` / `risk_management.stop_loss` / the `short_term_path` (old name `short_term`) path) — was the operation right, was the target realized or did price prove it wrong, was the stop pierced. **The postcheck is driven mainly by the operation.**
- **The analysis matters equally**: `reasoning`/`mentality`/anticipated risks and catalysts/narrative theses — was the reading accurate, did the price path bear out the theses.
- **`composite_score` (old name `action`) is not a directional forecast**: the composite score is S_ref of the adopted distribution: upside weight over total weight, not an independent impression (near 50 usually corresponds to HOLD, but it is not hard-mapped). Only check whether it is **roughly in the same direction** as the actual move and consistent with the operation; do not judge its direction right or wrong by the 55/45 line on its own. The digest's `direction_call` is already taken in the order "operation family → `short_term_path` (old name `short_term`) direction → rough direction of the composite score"; it is not decided by the composite score alone. The old/new report field-name mapping is in `docs/design/reorg/naming-migration-map.md`; reports from 09-29 and earlier still use the old names, and digest reading is compatible with both sets.
- **Expectation-flow scoring (one principle)**: what is scored is whether this decision was worth making on the information available at the time — **a rule-abiding loss is not punished deeply, a rule-breaking win is not rewarded**; but the diagnosis still separates decision process from outcome: a compliant process is no shield for long-run ineffectiveness, and repeated compliance with repeated losses must have the method's failure called out in the diagnosis.

**One composite score + selective layered summary**:
- In the end still give only one `score`; **no sub-scores, no averaging of dimension scores**. What the user receives is one complete decision; the merits and
  faults of analysis and execution all go into the same composite score.
- When writing `verdict`/`diagnosis`, identify the four-segment chain as needed: ① data / fact reading; ② market mechanism, event pricing and path judgment;
  ③ composite score / operation / anchors / magnitude; ④ arbiter assembly (did the correct views make it into the final report).
- **Write only the layers that carry information**: name a layer only when it is especially accurate, clearly wrong, or shows an inter-layer break where "the upstream judgment was right but the downstream operation / arbitration lost it".
  Ordinary, middling layers with no explanatory power for the total score need no item-by-item comment; writing a running log for the sake of a complete format is forbidden.
- If all layers agree and the outcome is clear (e.g. data, judgment and operation all precise), one overall "complete hit" suffices, with no need to pull it apart;
  if only one layer stands out, say clearly whether it adds to, subtracts from, or is merely a system diagnosis relative to the total score, but **do not report a separate numeric sub-score**.
- "Thought of it in the debate" and "adopted and executed in the final report" must be kept apart: the former may count as reading ability and a limited contribution, only the latter is the final deliverable;
  comprehensive analysis cannot excuse a wrong operation, and an operation that happened to be right cannot claim deep understanding that does not exist.

**Downside protection at D+3 and D+20 (optional diagnostic, 0% of the score)**

- Read the original member's `downside_protection`, or the corresponding investable lens in `by_lens`. Count a mirrored primary lens once. If the field is absent, skip silently. `insufficient` is an abstention. The hedge signal cannot change the score, band or review waiver. Record a material hit or miss in the existing diagnosis, or briefly in the verdict if the waiver applies.
- At **D+3**, check timing. For `emerging`, did the stated risk develop while protection was called for, or did the call precede a recovery? For `easing`, did the market stabilize or rebound, or did the same risk promptly deepen? At **D+20**, give the main judgment on the sequence and duration of that risk episode. A late recovery can conceal timely protection and a late fall can conceal premature protection. Use only evidence available by the tier cutoff.
- Compare the underlying's observable downside and rebound after publication with the original `basis`, `review` conditions and any frozen levels. Use underlying prices in market direction even when the spot card says SELL. End attribution at expiry or the first verified superseding phase for the same symbol/lens, whichever comes first. A review trigger marks a point to reassess. If the path, order of moves or later signal is unknown, qualify the conclusion. Never infer a held hedge, an inverse-product return or a fill from the underlying.
- `persistent` continues the risk assessment without earning another opening credit. `quiet` warrants a miss only when the original evidence supported a material warning; a surprise alone does not establish one. Apply the same evidence standard to false alarms and premature removals. Attribute each transition once across horizons; preserve each group member's own path and count shared episodes once in aggregate.

**Iron rule of generation order: detailed account first, summary and score derived after**:
- In your head / working notes, first complete a full detailed settlement account, then decide `score` and `verdict`; you may not pick a score band first
  and then back-fill material to defend it.
- The detailed account follows the pattern prescribed by this protocol: first lay out the line-crossing and path facts, then judge the operation and the market reading, apply the event responsibility boundary,
  check the four-segment chain and inter-layer breaks when necessary; when `review=true`, write the parts worth keeping into `diagnosis`.
- `verdict` is the summary naturally derived from the detailed account, **written by you as information density warrants**: capture the one or two features that best explain the composite score;
  it need not copy the structure of the detailed account, and need not cover all four segments. For an obvious complete hit / wrong-footed call it can be very short.
- **The judgment JSON also orders its fields by generation order**: after the `staging_path` metadata, write `diagnosis` first, then
  `verdict`, and `score` last; do not put the score on top by human reading habit. Although JSON consumers do not depend on field order,
  autoregressive generation order is the structural insurance against fixing the score first and inventing reasons afterwards.

`digest` is the basis for judgment (**price facts may only come from the digest; inventing numbers yourself is strictly forbidden**), not a checklist. After the detailed account is complete, give a 0–100 composite score, with three anchors:
- **0 wrong-footed**: the operation direction / core reading was wrong as a whole (called it backward, proven wrong by price, thesis falsified).
- **50 missed**: the call was conservative / vague, the market produced an obvious opportunity that was not seized (e.g. `mfe` very large but direction neutral / target set too close).
- **100 hit**: operation right, reading accurate, fully realized (`touched_target` and `ret` in the direction called).

The anchors are only a scale; **the exact number is yours to weigh over the whole report** (e.g. how much to deduct for "touched the target then collapsed back to cost" is your call). Pair it with a one-sentence `verdict`.

**Weighting guidance (general dashboard; no pre-existing position assumed)**:
- **Primary operation quality first**: for an entry waiting at a level (recorded in the final report as `LIMIT_ENTRY`, derived from the level) the digest carries an `entry` field (`entry_price_reached` whether price reached the order level / `reached_date` / `post_reach_ret_pct` P&L from reaching the level to window end; **the R21 order-depth trio**: `entry_depth_pct` order depth % (negative = breakout order placed above) / `entry_depth_range_ratio` order depth ÷ the window's average daily range (>1 = the range cannot support this depth) / `entry_gap_pct` the gap % when the level was not reached) — for an at-a-level order, "reached the level and the move afterwards went its way" is the core evidence of a hit, "reached the level and immediately trapped" is wrong-footed. The digest of a TRIM report carries `trim_exec` (same-shaped fields with the exec_ prefix): whether the execution price sold / how high it was placed (negative depth) / how far short if not sold.
  **Conditional orders are judged by line-crossing arithmetic (settled 2026-10-17)**: touching the order level within the order validity = a touch; where a close back above is required, a close back on the line (inclusive) = a reclaim; every other attached condition is checked item by item as the hard condition the report wrote (level, comparator, number of closes, volume-ratio threshold, calendar event) against the digest's daily-bar table, with no guessing at "did it hold above". Order validity: if the report states it explicitly, follow the report; if not, 3 trading days counted from the trading day after the base date (last day inclusive); `short_term_path.horizon_days` is not the order expiry, and a touch after expiry does not count as a fill. Old reports whose conditions are vague or cannot be judged from daily bars ("stabilizing on shrinking volume", "order-book absorption", "verify company damage") are marked **provisional**: counted neither as filled nor as dodged, and where it affects the band, state so in the verdict. After a fill, settle mechanically by the report's own exit conditions (digest `simulated_exit_ret_pct`); do not invent a holding return.
- **Postcheck parity (master scoring-rate schedule, ruler line 2026-09-22; changed to side-by-side settlement on the evening of 2026-10-17)**: **under the same postcheck path, the score ceiling of "not executing" may not exceed that of "executing correctly"; exemption is bought only with path evidence, never with posture**.
  The method no longer sorts exemption classes by order depth or gap; instead **every action is settled side by side against the alternative strategies feasible at the time**: digest `alternatives` gives the mechanical result, over the same window and the same exit basis, of "enter at the current price at T / trim at the current price / do nothing", while `entry` / `trim_exec` give the actual result of the conditional action.
  - Conditional order filled: settle by the actual fill and the self-written exit (`simulated_exit_ret_pct`), side by side with "act at the current price at T" — whether it waited for a better price or caught the falling knife, both legs go into the verdict.
  - Conditional order not filled: record it as unfilled as it is (the reader was actually flat or still holding within the window), and at the same time state what was missed and what was avoided relative to "act at the current price at T"; if it judged itself bullish yet waiting missed a move in its own direction, record it as "judgment not carried into operation" and grant no posture credit for "having an operation"; if waiting avoided an adverse move, record it as loss avoided.
  - Action at the current price: side by side with "do nothing", look at whether the exit conditions after the fill were realized; no automatic high score for "just did it".
  - HOLD: compare with the adjacent operations reachable in the same state (the HOLD clause in the D+20 section; D+3 likewise).
  - Same law on the sell side: the trim execution price side by side with "trim at the current price at T"; not sold = continued to hold this leg of risk, and both the gain from continuing to hold on an up move and the loss on a down move enter the comparison; no separate "continued-holding winner" exemption.
  The reachability of alternative strategies and the exit basis are frozen at T: acting at the current price and doing nothing are always reachable (`alternatives.price_basis` states where the current price came from; fallback prices such as the previous close are proxy fill assumptions); state-transition-type adjacent operations are judged reachable per the HOLD clause of the D+20 section (structural levels registered at T + accepted states);
  the exit uses only the stop the report itself wrote (the stop simulation of `immediate_entry`); if none is written or applicable, mark `stop_not_applicable` and hold to window end; the reckoner does not invent an exit on its behalf. `no_action.market_ret_pct` is the market itself, `opportunity_cost_pct` is the opportunity cost relative to entering at the current price (including its stop); read the two separately.
  The single composite score is set jointly by the ex-ante basis (how the report compared at the time, and whether it compared correctly) and the ex-post realization; the leg with the highest ex-post return is not treated as the mandatory choice at the time, and ex-post gains are not back-fitted; order-depth and gap fields remain in the digest as facts only, not as exemption thresholds.
- **`touched_target`/`touched_stop` are only auxiliary facts**: with no position assumed, not hitting the stop ≠ excellent, and touching the target precisely every time may mean the target was set too close. Do not treat these two booleans as a checklist; use them to corroborate "whether the primary operation / directional judgment held up".
- **The three short_term directions share one settlement method (paired with the `invalid` field)**: 上行/下行 (up / down) = "target touched first = realized,
  invalid touched first = wrong call" (the order in touch_timeline decides; for D+20 also check `line_state` — an intraday-only touch, or a single-day cross that came back, is recorded as touched; only what is substantively accepted counts as a state change); **震荡 (range) = a range claim** — target is filled as "lower edge-upper edge",
  and the two edges are the two-sided invalidation levels: neither edge broken within the validity window = realized (this is a real judgment and deserves the score), either edge broken =
  wrong call (penalized the same as calling it backward). GENERAL's multi-level string is parsed as ranges the same way (`SPY=95-105` is a range, not 95), each member settled separately. **震荡 is no longer a free abstention**: only daring to state a range earns the name; old reports without a range are treated leniently under the old criterion,
  but a system improvement item is recorded.
- **short_term is settled softly by validity window (2026-09-30, digest `short_term` object)**: short_term is the AI's view of the price path under the current situation, rolling and updated every day or two, not a hard-dated forecast contract; the postcheck takes `horizon_days` as the main window and applies it softly:
  - `outcome_at_expiry` = the raw result on the expiry day itself (`target_first`/`invalid_first`/`untouched`; for 震荡 `band_held`/`band_broken`; `pending` if the window has not yet reached expiry);
  - `outcome_with_grace` = a line touched within `grace_days` on either side of expiry counts (the grace is **fixed in advance and symmetric on both sides** — the same grace for target and invalidation level, not chosen after seeing the path); both readings are kept, the grace reading is primary for realized / wrong call, the expiry reading is used to see "on time or by grace";
  - neither line touched by expiry: `progress_at_expiry_pct` = how far the expiry close moved toward the target (100 = exactly at target, negative = the other way); **give partial credit by progress**, neither as wrong call nor as realized; for 震荡 look at `expiry_inside_band`;
  - `expired_before_window_end=true` (a 5-day claim inside a 20d window) → the path after the grace period **is no longer graded for short_term**; the 20d grades only operation and overall; the line-crossing sequence remains a full-window fact, but touches after expiry do not count for or against short_term;
  - old reports missing `horizon_days`: the whole window is the main window with no grace (`horizon_days: null`); the verdict records a system improvement item, with no retroactive re-grading.
- **`touch_timeline` line-crossing sequence (computed mechanically; may be cited directly)**: the **order and elapsed time** of each line's first touch (`elapsed_td` = trading days elapsed since the report time: the rest of the report day counts as day 0, pro-rated by the remaining session (`day_T_fraction`), full trading days thereafter count from 1; the 3d tier at 5m intraday precision, the 20d tier inherits the 3d fine segment plus a coarse-resolution remainder, marked by the `resolution` field). **Line names are separated**: `entry`/`stop`/`target` are operation anchors (order level, stop, TRIM execution price / HOLD observation anchor — when not LIMIT_ENTRY, the `target` slot holds the operation anchor), `st_target` = short_term's real profit target, `invalid` = the invalidation line, **a 震荡 debate has the two edge lines `st_low`/`st_high`** (from R21 the two range edges are sequenced mechanically; the moment an edge breaks is ruled by the sequence, and the digest also carries `st_band`) — directional realized / wrong call is read from `st_target`/`invalid` (for 震荡 the two edges), operation execution quality from the operation anchors; do not settle the TRIM execution price as a profit target. Which line is hit first is decided by it; on top of that fact you evaluate freely (e.g. "pierced the stop the very day of the fill" vs "held steady for two days after the fill then reached the target" are entirely different verdict qualities); events flagged `same_bar_ambiguous` = order within the same bar is undecidable, do not force a sequence. Events may carry a short `session` field (`pre`/`rth`/`post`/`overnight` = pre-market / regular session / after-hours / overnight, divided by US exchange sessions, timestamps themselves still UTC; present only with US full-session data, absent = cannot be judged, not an omission): a pre-/after-market touch **is valid** (extended hours are tradable), but note the session when assigning responsibility — whether a thin pre-market lone spike should count as a fill opportunity is at your discretion under the existing event responsibility boundary; do not retroactively re-grade old debates for lacking this field.
- **Path facts (`max_drawdown_pct` max peak-to-trough drawdown within the window / `cross_T_count` number of times the close changed sides relative to the T-day anchor price)**: on long windows (20d) look at these especially when judging HOLD / wait-and-see — an "all right" that closes where it started but crossed through a deep drawdown midway and V-shaped back is not the same grade as an "all right" that truly held; deduct from the former at your discretion by holding experience and invalidation discipline, and name the drawdown depth in the verdict.

**Scoring criteria (event responsibility boundary comes first; do not look only at `recommend_after`)**:

1. **The event boundary is a state watershed, not a guillotine (soft ruling settled 2026-10-17)**: when the report, at T, in the final delivery layer (statement / debate_summary / `next_review` / an explicit branch) names
   a deterministic event knowable in advance (earnings, CPI, rate decision, corporate resolution, etc.) and states the premise it changes and the re-evaluation arrangement, then before the event grade the original-state judgment, the reason for waiting and the actions already taken; after the event grade the branches
   delivered in advance, the constraints still in force and the consequences left by earlier actions; the watershed changes the scope of the original judgment, it does not cut responsibility off with one stroke, nor is it split by the two segments' day counts or a fixed proportion. Merely registering a date or a mechanical re-evaluation
   does not automatically end responsibility. The event-day bar is listed separately: its touches and closes beyond the line must enter the verdict; when daily bars cannot settle the order, keep the attribution as undetermined, and pull intraday bars only when the order would change the main ruling. An honest wait for re-evaluation
   loses no directional points; a weakening of the still-valid range gets a limited correction; a bet on the event passing is counted as a judgment error. **Execution lines strict, judgment lines soft**: order level, trim price, stop and order validity are settled strictly by the original price, condition and term;
   short-term targets, invalidation, the two range edges, observation anchors and re-evaluation lines are judged, within their applicable period, by closing persistence and volatility scale as to whether the opposite state gained acceptance (digest `line_state`), and retire after expiry.
   Risk warnings and dual branches are assessed by specificity, actionability and realization; no automatic exemption or bonus. Old reports missing fields only get an improvement item recorded; an explicit wrong hold, trim or conditional design must still be assessed, not exempted for
   a formal gap, nor deducted twice. D+20 is an observation cross-section; a state not yet confirmed at window end is marked insufficiently verified, and favorable results outside the window are not picked.
2. **Mechanical re-evaluation is no exemption**: `frequency_hint=daily`, "look again tomorrow", "look again once the index breaks below / holds above X",
   `price_below`/`price_above`/`sector_drop` or some symbol reaching some value — if the event, causal change behind it,
   or why that state would change the original thesis is not explained, these are only risk-control / scheduling / price execution lines. Such reports are still settled over the full postcheck window;
   a mechanical `recommend_after` cannot cut the responsibility period short, nor does happening to write a number earn event-registration credit.
3. **Ex-ante branches and risk statements can only mitigate; they cannot turn a wrong call right**: if the final main direction went wrong, but the debate process explicitly discussed
   the other branch that later materialized, the risk mechanism and its causal reasons, and gave actionable identification conditions, that shows it did not miss the risk entirely,
   and it should be deducted one band less than a one-sided misjudgment with no awareness at all. Mitigation strength depends on specificity: the more complete the naming of the risk source, transmission mechanism, observable evidence and action,
   the stronger the mitigation. If the event / risk was only mentioned in passing in `risks`, `dissent` or `debate_history`, but was not
   absorbed by `debate_summary`/the final report's main conclusion, did not raise the weight or change the operation, that only shows "not entirely unconsidered",
   and it must bear the main responsibility for **identified but under-weighted / assembly failure**, with only limited mitigation, never event exemption.
   Listing only templated risks, generic "market risk", or a single price threshold with no reason — **no mitigation**.
4. **A post-event dual branch counts as due diligence**: for a named deterministic event, if the direction, confirmation conditions
   and actions for event outcomes A/B were given in advance, then even though the final report cannot foresee the event outcome, it is deemed to have done what it should in a state of uncertainty. A pure price up/down dual branch
   only proves execution discipline and cannot automatically exempt a wrong main direction.
5. **Deduct only for real judgment errors**: a one-sided bet before the event is decided / treating volatility (ATR, implied range) as directional evidence /
   calling it backward within the pre-event range it was truly responsible for / an irreversible action (TRIM, reduction) in the wrong direction.
6. **No deduction, only a system improvement item recorded** (formal gaps of old reports; from v3.2 the final report must write hard conditions and a post-event plan, and new reports missing them are assessed as judgment): missing the execution instruction "what to do once the condition holds" /
   `short_term.horizon_days` shorter than the postcheck window / no "sequential path" field (each side right about one segment, yet forced to pick one of 震荡·上行·下行).
7. **Architecture gaps are named separately and not counted as judgment deductions**: the report did not anchor a known in-window event as a re-evaluation point (`recommend_after`
   set outside the window), or `review_triggers` did not include same-sector heavyweight companies / demand-side (hyperscaler) earnings dates —
   this is a gap in the next_review scheduling mechanism; state it in `diagnosis` and give a system improvement suggestion.

**Event-driven capability ladder (higher-rung hits must earn extra credit)**:

The postcheck must not treat every "mentioned the event" as the same capability. For event-window reports, check the following five rungs in order; the higher the rung the scarcer it is,
and it must be an ex-ante judgment that **at T had already entered `statement`/`debate_summary`/the final report's operation**, not back-fitted from the ex-post path:

1. **Knows there is an event**: names the event and date, knows a re-evaluation is due then. This only establishes the responsibility boundary; it is not itself a directional forecast.
2. **Judges the event outcome**: judges in advance whether the earnings / data / policy itself leans good or bad, with grounds.
3. **Judges what is priced in**: distinguishes "the fundamental outcome" from "how much the market already expects", judging whether a good / bad outcome has already been digested by price.
4. **Judges the post-event price reaction**: explicitly forecasts the direction, magnitude or key path after the event lands, rather than merely listing A/B template branches.
5. **Counter-intuitive second-order judgment + correct operation**: e.g. judging in advance "earnings will be very good, but the good news is fully priced, so it will fall after release,
   therefore trim before / on earnings day", or "the data is very bad but pessimism is over-priced, so it rebounds after landing". If the event outcome,
   the post-event reaction and the operation timing are all realized, this is the highest quality of event-driven debate and should score markedly above a report that merely guessed the direction,
   usually entering **95–100**; it may not be pressed back to an ordinary hit score for an end-point drawdown or window noise.

Calibration principles:
- Only rung 1 reached → only the scope of applicability was identified; this is not itself a realization contribution (graded as "partially completed", see the D+20 section), with no extra reward for directional ability.
- Rung 2 or 3 reached and realized → an explicit bonus on top of the base path score.
- Rung 4 reached and realized → a high-quality event judgment even if the pre-event operation was ordinary.
- Rung 5 reached and realized → near full marks; if the timing of the irreversible operation was also precise, consider the full-marks anchor first.
- **Calling it right** after the event is of course different from merely being exempted: exemption only means no deduction for an unknown outcome; an active forecast that is realized must earn extra reward.
- If the higher-rung judgment appears only in some debater's `dissent`/`debate_history`, and the final report did not adopt it or operated the opposite way,
  only the limited contribution of "the council identified it" may be given; it cannot be scored high as rung 4/5.
  **Before writing "not adopted / omitted", check its standing (2026-09-12)**: first look for this judgment in `provisional_ruling.claims_review` —
  ① **it has standing and was explicitly ruled on and explicitly rejected with reasons** (especially if it also went into dissent) → this is not an assembly failure, it is **a rejection ruled in the wrong direction**:
  an ordinary judgment error, held responsible lightly (one band higher than silent dropping, but a wrong ruling is wrong, not especially high); the verdict writes
  "claim X was rejected by the ruling sheet (reason Y) and was realized in the postcheck" — do not write it as "omitted";
  ② **the ruling sheet has no such entry at all** (a half-formed judgment vanished silently) → this is the real final-report assembly failure, held responsible heavily;
  the verdict states explicitly at which arbitration / weighting / evidence-assembly step the final report dropped it.
  In both cases **the debater / agent and the specific judgment it got right must be named** (event outcome, pricing, post-event reaction or operation);
  a vague "the opposing side once flagged the risk" is not acceptable. This attribution is used later to adjust debater and arbiter weights, and is stored separately from the final-report score.
  Also: when the two sides' numbers were skewed but the final ruling realized only a small half of the difference and the skewed side's direction was realized in the postcheck →
  write the attribution as "conversion-layer discount", cite the reasons in `residual_adjustment.basis` to assess its quality (a grounded discount = judgment error, lightly; hollow reasons = inherits
  the case history of "the first ruling just takes the middle"). The numbers: v3.4 reports (they carry `scenario` and `s_ref_z` / `s_ref_r`) compare the adopted `s_ref` with each side's endorsed scenario; the adopted distribution is the mixture at the arbiter's weight `scenario.mixture.w`; older reports use `ledger_score_z/r` (before the 2026-10-01 migration `organized_z/r`).
  For v3.4 reports the d+20 digest also carries `forecast` (R25): CRPS / Brier / interval hit of the adopted distribution and each side's endorsed scenario on the day-15 close, with the skill against the ATR baseline — cite it when the question is whose distribution was closer, not as the operation score.
  It also adds `delta_crps` = CRPS(A) − CRPS(A at w = ½): negative means the arbiter's weight moved the distribution closer to the outcome than an even mix, positive means further; it scores the weight, not the operation. Reports without `scenario` (v3.3 and earlier) have no `forecast`.
  **`path_fit` (PF1, from 2026-11-06; every tier; diagnostic only, never a score)**: how the realised daily closes sat inside the adopted tree day by day, up to min(dt, T_days) — the days past the tree's horizon are not its samples.
  Per day a directional PIT `u` (small = the market came in below the tree, large = above), `tail`, a normalised CRPS and the same for a zero-drift Gaussian on the same ATR scale, the central-80 width, and whether that day's path point was authored, terminal or interpolated.
  `summary`: `coverage_90` (share of days not in a 5% tail), `coverage_50` (the duty loop's `inside`), `tail_days_low / high` and `bias`, `first_breach_day` / `longest_breach_run` / `recovery_day`, `path_crps_norm` and `skill_vs_zero_drift` (1 − Σc/Σb over t < T), `w80_norm_mean`
  (a wide band covers cheaply — read coverage with width). `terminal` is the day-T PIT on its own; `segments` are pre / post the first event date inside the window; `event_checks` tell whether authored open / gap_open / touch events were actually reached.
  Read it as one diagnostic layer: a tree that never lost the path (no breach, skill > 0) earned something even when the operation went nowhere; a breach that never recovered says the tree was wrong before the horizon. At d+3 the three values are case evidence, not a calibration.
  It places nothing in a band and moves nothing inside one; the same stop or drawdown is never deducted again as "path error". Cite it in `diagnosis` only when it carries information (one sentence: where the path left the tree, coverage, the CRPS skill, "diagnostic, no band change"); when the block is missing or `status` is not `scored`, write "path reference not assessable". Calibration (PIT uniformity, interval width) is a batch question, never read off one report.

**D+20 long-window mixed hits (the primary deliverable sets the band; the cardinal sin is a mid-score mush; soft ruling settled 2026-10-17)**:

- First determine the **primary deliverable** from the final report at T (read from the primary operation + primary mentality + mentality_basis; the verdict states in one sentence "this debate's primary deliverable is ___"), then separately check the realization of the pre-event judgment, event pricing, post-meeting branches
  and the continuing operation constraints; place into a band by the success or failure of the primary deliverable, with secondary gains and losses deciding the position within the band, not composed by segment day counts or fixed weights. You may not, after seeing the postcheck, re-select whichever of "preserve capital", "catch the rally", "did not buy" is most favorable.
- The single-score table (calibration bands, candidate; landing point = band midpoint ±3; discipline mitigation only within the band, totaling no more than 10 points):

  | Primary deliverable | Secondary net loss | Secondary merits and faults offset / unverified | Secondary with independent realized value |
  |---|---:|---:|---:|
  | **Failed**: the due risk adjustment was not delivered, or the main action ran opposite to the supported state | 20-34 | 35-39 | 40-49 |
  | **Partially completed**: the wait / branch had value, but not enough to prove the primary task fully succeeded | 50-54 | 55-59 | 60-64 |
  | **Succeeded**: the main action and the key state-transition arrangement are supported by path evidence | 65-74 | 75-84 | 85-94 |

  Severe opposite execution with no valid contribution 0-19; only when the primary deliverable, the numbers and the operation are all highly accurate, or a higher-rung event judgment is fully realized, does it enter 95-100. No major loss does not automatically equal success; correctly maintaining exposure does not require trading either.
- **Merely registering the event and HOLDing** does not enter 70-85 on that basis: first prove what valuable task was completed (correct holding or correct waiting can count, merely not trading cannot); it usually lands in "partially completed" (50-64).
- **HOLD also has a cost**: HOLD is one choice in that state, compared against the adjacent operations reachable in the same state — candidates are constructed only from structural levels registered at T (lines the report itself wrote, snapshot structure points — the same causal ATR zigzag the debaters saw), and a one-sided candidate arises only in a state where the judgment line
  has been substantively accepted (`line_state=accepted`); otherwise the reference is doing nothing (the alternative result of acting at the current price is in digest `alternatives`, usage in the side-by-side settlement section of Step 2, reachability the same); the gap between the report and the reference is recorded in four classes: same / parameter difference (direction and state transition right, size, level or confirmation delay differ) /
  missing state transition (a necessary state transition that had grounds at the time was not delivered) / opposite; if it cannot be identified, record unidentifiable. A report that itself saw the one-sided case yet gave HOLD is recorded as "judgment not carried into operation", with no exemption. Do not substitute the reader's position: the report
  only answers the best operation in the state at that time; the reckoner may not compute one set for holders and another for those flat.
- Event segmentation (`segments.pre / event_bar / post`: each segment's return, direction-agnostic up/down excursion, drawdown), the significance threshold (`eps_window_pct` and each segment's `eps_pct`, volatility-scaled, σ frozen at T),
  and the judgment-line acceptance state (`line_state`: untouched / touched_only / accepted / accepted_then_reclaimed / pending / unknown, assessed only within the short-term validity window including grace) are given by the digest;
  the reckoner cites them and does not recompute; `pending` (outside the line at window end but not enough bars or magnitude) is neither rewarded nor punished, `unknown` (missing data) is not guessed.
- A short-term claim is assessed only within its own validity window and the set grace; the path afterwards earns no late reward and no late penalty; the same short-line error, if already counted through a real trading loss, is not deducted again. An expired short-term direction is not inherited as the medium-term direction.
- The 20d tier grades only the operation and the overall tone: a short_term past its validity window (including grace) is not held responsible in this tier (see the soft settlement in Step 2).
- Case references (design anchors, not formal re-grades; origin `docs/design/reorg/d20-calibration-20261017.md`): MUFG partially completed × offset ≈ 57; SFTBY partially completed × net loss ≈ 52
  (re-graded after the starting-price correction); GENERAL_JP succeeded × net loss ≈ 65-68; HYMTF failed × independent realization ≈ 44; MP failed × independent realization ≈ 42.

**Two scoring traps**:
- `touched_target` for a HOLD_NEUTRAL report is often a **false signal** — `operation_target` is mostly below the current price,
  so any dip "touches" it; the whole line may also be an **upside confirmation level** rather than a profit level. Tell them apart before scoring.
- **Right end-point ≠ right path** — for a high-ATR symbol ret can look like a "range hit" while mae/mfe are each ±15~20%.
  mae/mfe must be read together with ret.

Then judge `review`, using a **single inspection-waiver exception**:
- Only when the final report's **numerical forecast is especially accurate, and the corresponding final operation was also correctly realized**, may
  `review=false` be set: verify the result with the digest alone and give a high score directly (usually 95–100), without reading debate_history, going online,
  or investigating how it managed it. A precise numerical forecast is in itself a scarce, high-quality deliverable.
- **In every other case `review=true` and proceed to Step 3 to read the full debate for problems**. An obvious wrong-footed call, an ordinary hit, a HOLD that missed,
  half right half wrong, a black swan or noise — none may be skipped merely because "the result is easy to explain"; the core value of the postcheck is to trace the debate and final-ruling chain.
- Whether the waiver applies is read from the final report's delivery and the digest's realization, not from an audit of the thought process. If both the numbers and the operation are accurate, do not nitpick extra to appear strict.
- **A black swan is a postcheck conclusion, not a waiver premise**: only after first confirming the report was indeed wrong, then reading the full debate / evidence at the time, and checking
  the sudden events that later occurred within the window, can you judge that the event was unknowable at T and the error should not be attributed to the AI. Before this chain of investigation is complete,
  pre-labeling "black swan / pure noise" is forbidden; otherwise you are using a not-yet-established conclusion to skip the establishing.

> GENERAL class: each index in `digest.members[]` (SPY/QQQ/DIA etc.) has its own path; judge from the three lenses together whether the market call was accurate.

> Lens-wise reports (`digest.lenswise` true; the report carries `by_lens`): every lens settles its own operation from its own `by_lens` entry (its own level and operation family; every member has `role: operation`), exactly as a GENERAL member is settled, and each member carries its own `forecast` at dt >= 15. A lens whose bars are missing shows an `error` and is never borrowed from another lens. You still write ONE composite score, weighing the lenses by each member's `weight` (the frozen config weights, `digest.session_score_weights`). `lens_weights` (λ, the session's own weights) is not a scoring input.

## Step 3: Postcheck attribution (only when review=true)

`Read` `report.debate_history` (the full debate at the time + reasoning + evidence), to see clearly **what this verdict was based on** at the time and where it was wrong / right. When needed:
- **The wilder the miss, the more you should check** what happened in the period: `WebSearch` neutrally for the facts about the symbol / sector / market / macro within the window `win_start~win_end` (a sudden event? a weak signal confirmed afterwards?).
- **🔴 Hard cutoff `win_end`**: never refer to "future" information later than the end of the postcheck window (that would be unfair to the time).
- **WebSearch is a cheap substitute for grounding; use it sparingly**: if the raw data already explains it (pure noise / an unmistakable market-wide black swan / black-and-white in the data), do not search; attribute directly.

Write `diagnosis` (a single paragraph of text, worthy of the amount of information you read; do not phone it in). First, per Step 2's "selective layered summary", name only
the notable strengths, notable weaknesses and inter-layer breaks; ordinary layers need not be filled in. Then **explicitly answer three questions**:
1. Was there a **sudden event** unknown at the time (T) that directly explains the move?
2. Was there **weak evidence / an early sign** at the time that was confirmed within the window afterwards (weak turned strong; could have been weighted more at the time)?
3. **How much of this miss was accident** and how much was information that was already there but misread (a reusable judgment lesson, concrete to "next time X happens, do Y")?

Here "black swan / unattributable sudden event" can stand only after the above investigation is complete: you must state what the event was, when it happened, why it was not reasonably foreseeable at T,
and whether the original report did its duty within its pre-event responsible range. A sudden move or major news alone cannot directly make it a black swan.

**Responsibility allocation after the detailed check (affects the single composite score; no separate sub-score)**:
- **Unknowable accident / black swan**: the sudden event had no reasonable way of being foreseen at T, and the report did its duty within the pre-event responsible range →
  although the directional result was wrong, this part is not attributed to the AI; the composite score should be accommodating and may not be punished heavily as an ordinary backward call.
- **Weak evidence that later strengthened**: there was an early sign at T, but its strength, reliability or transmission was not yet sufficient, and it was confirmed within the window afterwards →
  the AI and the accident each bear part of the responsibility; usually understood as a reference anchor of "about half the responsibility", adjusted by how identifiable the weak evidence was at the time,
  with an explanation of why more or less than half.
- **Analysis / assembly error**: important information already existed at T and was sufficiently prominent, yet was misread, ignored, mispriced, or the correct debater view
  was not absorbed by the final report → mainly attributed to the AI; deduct by the real judgment and execution loss, with no exemption for later market swings.
- The order of apportioning blame must come after the fact-finding; the responsibility anchors calibrate the composite score and are not a mechanical three-term weighted formula.

Land on concrete numbers / events; do not be vague. If it truly cannot be found, write from the information at hand and honestly mark "to be confirmed".

**Take a separate look at Evidence**: the outcalls at the time came in three kinds — data calls (`kind:data`, computed by evidence_quant), evidence searches (`kind:search`) and model calls (synthesis analyses the Executor did by the manual; the ledger is in `report.model_calls`; before the 2026-10-01 migration called `tasks`). Scan the `status` of each item in `report.evidence`: **wherever an IBKR data pull failed** (`not_found`, or `blocked` in `evidence_added`), say one sentence in the diagnosis — what data was missing and whether it materially affected the judgment at the time (or was dispensable). A cluster of data-pull failures in one report → explain separately; that is a data-layer illness and is not charged to the analysis.
(Optional) If you raised something but did not / could not check it, put the question you wanted to check into `search_request` (grounding results themselves are not stored; only the question is recorded).

## Step 4: Write to the database (once per tier)

Write this tier's judgment as a judgment JSON (`Write` to `data/postcheck_staging/`):
```json
{
  "staging_path": "<the path Step 1 gave>",
  "review": true,
  "diagnosis": "[Detailed conclusion] … [Overview] … (including the answers to the three questions)",
  "lesion": "L5->L6",
  "verdict": "Direction right but fell back before reaching the target",
  "score": 70,
  "search_request": "",
  "reckoner": "<your own exact model id>"
}
```
**`lesion` (the R21 judgment stack)**: one classification label derived from the diagnosis; it affects only attribution statistics, not
the score. The stack: L1 facts (data reading) / L2 interpretation (mechanism and pricing judgment) / L3 adversarial settlement (debate and ledger) /
L4 conviction (composite score) / L5 playbook (mentality) / L6 execution (placement / position / lines). Three kinds of values:
**layer value** `L2` = that layer's own judgment was wrong (e.g. mechanism read backward); **joint value** `L3->L4` = transmission broke (upstream right, downstream lost,
e.g. a debater was right but the final report did not adopt it = L3->L4; judgment right but placement twisted = L5->L6); `none` = no clear lesion (a hit or
pure luck / external cause). Fill in only **the one place that best explains the composite score** (same philosophy as the single composite score); write the other lesions in the diagnosis prose;
when unsure, pick the more upstream one. Conversion from the four-segment chain above: ① = L1, ② = L2, ③ = L4/L5/L6 (conviction / playbook / execution,
please subdivide in lesion), ④ = L3->L4 (assembly and conversion).
When `review=false`, leave `diagnosis`/`search_request`/`lesion` empty. `quant`/`symbol`/`as_of`/`dt`/`ruler` are taken by save from staging automatically; you do not fill them in (PG key = symbol+as_of+dt+ruler: a rerun with the same ruler overwrites, a different ruler is stored separately). To store:
```bash
./venv/bin/python datalayer/pg/postcheck_store.py save <judgment.json path>
```
`status:saved` = success (upsert; the same symbol+as_of+dt is overwritten and rewritten).

**Then the last step of the tier — translate the verdict** (owner, 2026-10-25; one Translator per language in `config/i18n.yaml` `translate_to`, cheapest model):
```bash
./venv/bin/python scripts/translate_reports.py --extract --table postcheck --symbol <SYMBOL> --as-of <TS> --dt <dt> --ruler <ruler> --to <lang>
```
→ spawn the Translator (`.claude/agents/translator.md`; CC: Agent `subagent_type: translator`; Codex: `spawn_agent` on the worker model) with
`Translate the staging file <path> in place; reply with the path and the count.` → `./venv/bin/python scripts/translate_reports.py --load <path>` → `{"ok": true, …}`.
Only `verdict` is translated; `diagnosis` is not. A failed translation does not undo the save: report it and move on.

## Step 5: Output the receipt

Once all tiers are done, **output only one JSON block** (for the upper layer to parse):
```json
{
  "symbol": "NVDA",
  "postchecks": [
    {"as_of":"20260508T235900Z","dt":3,"score":70,"review":true,"verdict":"…"},
    {"as_of":"20260508T235900Z","dt":20,"score":55,"review":false,"verdict":"…"}
  ],
  "skipped": [{"dt":20,"why":"immature"}]
}
```

## Error tolerance / iron rules
- A single tier immature/no_report → goes into skipped; it does not block other tiers.
- PG unreachable / debates unreadable → prep returns `no_report`; do not succeed silently, say so clearly in the receipt.
- **Price facts come only from the digest**; WebSearch is used only for qualitative verification of "what happened within the window", never to quote prices.
- **Hindsight iron rule**: you stand where the outcome is known, but attribution accepts only facts, no empty hindsight talk; if it was a black swan, say black swan honestly, do not fabricate reasons to seem profound. The purpose is to make later debates more accurate, not to shame old verdicts.
- File names / as_of are always UTC, aligned with the report's `market_data_timestamp_utc` (the postcheck is a derivative of a single-issue report; as_of corresponds exactly to that issue).
