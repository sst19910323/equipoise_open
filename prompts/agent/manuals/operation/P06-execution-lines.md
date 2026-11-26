# P06 · Execution lines and entry levels

**When to use**: before giving any stop, target, cancel line, add-on confirmation line, entry level, or trim execution price. Execution lines are designed,
not round numbers read off a chart. ATR and structural levels are both in the snapshot; citing them directly is usually enough. Exposure inside an event window and the special rules for executing quality names also live only in this manual
(sections four and five): the event-pricing topic and size-and-quality tiering supply the inputs; line placement, staging, and re-evaluation land here.

## I. The four questions for execution lines (answer in order; a line whose questions cannot be answered may not be reported)

1. **What it protects or what it realizes**: which hypothesis's invalidation (stop) or degree of realization (target) this line corresponds to. "A convenient round number" is not a hypothesis —
   find the hypothesis first, then the level.
2. **How far**: calibrate the distance with the snapshot's fourteen-day ATR. Reference values: a stop about one and a half ATR or more from entry (one ATR is roughly a single day's normal
   range; the extra half is a buffer so intraday noise does not sweep it out); inside an event window about two ATR or more (gaps are implied; set the margin on the basis that "a gap also counts"; when expected-move
   data is available, use it in place of the fixed multiple). Risk equals position times distance — widening the distance should shrink the position; moving both at once is adding risk, not being conservative.
3. **Where**: liquidity check — a line placed just below a prior low, just above a prior high (the snapshot's confirmed structure points, any layer), or within about half an ATR of a round-number level sits in a stop cluster: either
   move it out, or explain why you place it there knowing it is a pool.
4. **How it triggers**: declare the trigger semantics (touch, close confirmation, N days without reclaim, time stop — state whether the constraint binds the unfilled order or the position after a fill;
   N is set by the basis of the corresponding operation; if the path's validity window is cited, say so explicitly; if the thesis does not play out, admit the error); staged execution is allowed (move half first), and targets are likewise taken in stages.

Every line lives where it is enforced — as an event of the scenario tree, in the chosen contract (`operation_execution.conditional`) or in `risk_management` (trigger → action); the future facts that would make you re-judge go to `next_review.events` (2026-11-06: no bare price lines in `next_review` any more); no discussing without placing the order.

## II. Hard rules

- The entry anchor and the stop or cancel line may not share a price; leave at least one ATR between them (the same price means one break turns a dip buy into catching a knife).
- An order that waits for a level writes fill conditions judgeable on the daily bar (level, number of closes, volume-ratio threshold) — a fill is information, not a discount: at the moment it fills, price has already moved to the
  position you specified (a dip below is the adverse direction, a breakout above is the favorable direction); the fill condition answers "once it got there, is it still worth doing". When a quality name is treated as a false alarm, the thesis-intact check may substitute (section five); when substituting, state the result of the check.
- Free in the design phase (all four questions are discretionary), mechanical in the execution phase — once a line triggers, no improvised argument may move it farther away.
- Target prices are set by market structure (resistance, measured move, volatility range), not by "how much I want to make".

## III. Entry and trim levels: compare complete strategies first, then settle on one price (buy and sell side by side)

Market or limit is not an option; it is the outcome of the comparison. Every trade-type operation gives one reference level: acting now at the current price fills in the current price; waiting for a level fills in that level; the at-market / at-a-level
record in the final report is derived by the assembler from the level, and neither debaters nor the arbiter choose it. Before settling on a level, compare three complete strategies, all three under the same set of prior scenarios and the same end of window:
- Act now at the current price: the outcome in each scenario, plus how the exit conditions after the fill are realized.
- Act on a set condition (wait for a level or a confirmation): the return after triggering is estimated conditional on "price has reached that point" — reaching the level is an information update, not a discount; what must be estimated is whether, in the world where it has fallen
  (or risen) to that level, it is still worth doing next. The untriggered branch records two entries at the same time: what was missed and what was avoided.
- Do nothing: the net account of what was missed and what was avoided at the same end of window (choosing HOLD requires explaining why not moving now beats a light move; see the mentality table).

The comparison is computed by the assembler from the scenario and its execution events (certainty equivalent of each complete strategy on the same nodes, from the same neutral reference, at the same cost, on the same valuation day); you write the inputs — nodes, paths, the stop and conditional orders of each direction — not the arithmetic, and the cost is computed from observable measures. The final report carries the level of the strategy the comparison chooses on the session's distribution; to change the choice, change an input. Waiting for a level wins on one condition only: the price improvement and
avoided loss brought by waiting are enough to compensate for what is missed. When the judgment is time-sensitive, explain why you are not waiting; do not use a distant order to dodge a judgment already accepted. The reference price is no guarantee of a fill; if the order is placed and not filled, the judgment is still entered in the ledger,
and the postcheck settles it side by side with "acting at the current price at the time".

Depth is measured with this symbol's own volatility as the ruler (the snapshot's ATR, daily range, the count of pullback-ladder / rebound-ladder steps in `positioning`, the confirmed structure points and the unconfirmed right end), used to say how often that level tends to arrive, not the probability of a fill; the report carries only the
level as a number, and the method of measurement does not enter the format. The sell side follows the same law: the trim execution price is compared side by side with "trim now at the current price"; not getting the sale means continuing to carry this stretch of risk, and the return from continuing to hold on a rise enters the
comparison naturally, with no separate disclaimer.

## IV. A scheduled event inside the window

A scheduled event inside the valuation window is a branch of the scenario, never a separate discipline: its outcomes and price reactions sit in the nodes on both sides (the expected move, pre-event price action and consensus come from the event-pricing topic, P01), the stop is priced at the executable price after a gap, and nothing is deducted again for "event risk" once it is inside the distribution. Reducing exposure before the event, waiting for it and acting on a condition afterwards, or acting now are strategies like any other in the comparison — same nodes, same valuation day; a strategy that waits cannot collect the move that has already happened by the time its condition triggers. Long-dated contracts and guidance decide only whether to re-evaluate after the event; they do not change the nodes before it.

## V. Special rules for bargain-hunting in quality names (the execution frame for a quality name where no core damage was found; all other symbols follow the hard rules above)

Whether to use this section is a choice of execution frame, not a finding that "the false alarm is clean": size-and-quality tiering P08 gives which questions have been checked through and which
merely turned up no problem; the unchecked tail (credit, liquidity, valuation) is a node or a path in the scenario, with a stated probability — the word "unknown" has no veto power of its own and no exemption either.
When the repair node carries the weight, the semantics of the execution lines reverse:
- The cancel line is a thesis-falsification event, not a level: the order's invalidation condition is written as "which fact would prove it is real damage" (any of the three real-damage questions turning to yes, or
  newly added core damage at the direct-impact grade). Price continuing to fall is not a reason to cancel — with the thesis intact, lower equals a deeper discount, the next rung of the net.
- A disaster line is still required: keep one deep price line (reference: about three ATR or more from the anchor, or below a major structural level); its trigger semantics is stop buying and re-evaluate
  ("the market may know something I do not"), not mechanical cutting; if after re-evaluation the thesis is still intact, resume the net; if it is real damage, liquidate.
- Holding-above confirmation may be waived, replaced by the thesis-intact check (none of the three real-damage questions found core damage, and no new direct impact); state the basis and the parts still
  unchecked — with the thesis intact, the deeper the fall the cheaper the buy, and the adverse-selection concern eases accordingly; but a fall may also carry information you do not yet know, which is why the disaster
  line above cannot be dropped.
- The entry shape may be a staged net below, or a first tranche now at the current price: each rung goes through the section-three comparison just the same;
  when staging, the rung spacing references one ATR or more, total exposure is the sum of the rungs, and size follows principles section 4. The report field `operation_target`
  carries only the rung at hand (one level); rung spacing, subsequent rungs, and each rung's fill conditions go in `risk_management` and the body; rungs further down are placed at re-evaluation. Placing "a stop below
  plus a confirmation buy above" at the same time is selling low and buying high, paying a premium for confirmation — to use this shape, explain why a momentum frame applies to this quality name.
- Targets are taken in stages at mean-reversion levels (the twenty-day or fifty-day moving average, prior platform, gap fill); once price returns to the mean, this "bargain" thesis is realized,
  and the remaining position switches to regular trend discipline.

## VI. Output

This model's output is an execution design, not directional evidence. The summary section writes the answers to the four questions plus a line table (line, hypothesis, distance in ATR multiples, trigger semantics)
and the entry level with its comparison. Execution lines that have not passed the four questions will be named by the arbiter.
