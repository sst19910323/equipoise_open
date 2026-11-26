# P01 · Expectations and pricing (the event-pricing topic; what the current price depends on when there is no event)

**When to use**: there is a binary event inside the window (earnings, macro print, rate decision, corporate decision); someone uses "the good or bad news landed" as directional evidence;
you want to claim the event as a catalyst; or an in-flight process is being treated as a pending event. There is no discrete event in the window, but someone says expensive, cheap, valuation overstretched
or valuation reasonable, or a stretch of rise or fall must be split into what shows up in earnings expectations versus in the multiple: use the later section "When there is no discrete event", and state in the filing which one you are asking about.

The tradable question about an event is "how will the price react", not "is it good or bad". This is not a lookup table, it is a topic: the machine gives facts,
the debaters argue "how much is priced in, what remains after landing" for a few rounds, and the arbiter prices it by the evidence. The executor (model call) only collects
facts; it does not rule "fully priced".

## Topic frame: two questions

1. **How much is priced in**: what the market pre-paid before the event, where the consensus stands, where you differ from it.
2. **What remains after landing**: when the result lands, which reaction is more likely — overreaction (reversal on landing) or underreaction (post-announcement drift).
   The two are competing explanations, not an either-or rule; switch explanation with the evidence and finally converge into one main operation.

## Facts the machine gives (cite them if present; if missing, file an outcall or mark as missing as it is)

- **Pre-event move**: how much it has risen or fallen cumulatively since the last catalyst (or over the last twenty trading days), large or small relative to this symbol's own volatility
  (the twenty-day cumulative in the snapshot's `trend_signal_inputs` and the extension trigger signal).
- **Expected range**: the option-implied event move (available for symbols with US-listed options; otherwise search; if it cannot be found, convert from the symbol's own historical volatility,
  and say that it is a conversion).
- **Consensus and revisions**: the level of consensus expectations, and whether it was revised up or down over the past month — a consensus being revised up is raising the bar.
- **This symbol's reaction distribution to past events of the same kind**: the reaction on landing day and over the following few days for the last several events of the same kind (use machine numbers if there are machine numbers,
  otherwise search, and failing that mark as missing as it is; do not pass off another symbol's experience as this one's).
- **On-track or binary**: a catalyst already public and advancing on its set schedule (production ramp, regulatory progression, volume deliveries) is digested by the market day by day;
  there is no "landing day" to wait for, and "waiting for it to land" cannot be the reason for not pricing it; its only question is "how much is not yet reflected". While the process is still on track both sides bear the burden:
  the side claiming neutral explains why the remaining part is already reflected (or produces evidence of derailment); the side claiming there is still room explains how much is not yet reflected —
  "still advancing" by itself does not prove there are unreflected gains.

## How to argue

- The side claiming "the event is a catalyst" must answer: whether the pre-event move, compared with the expected range, has already pre-paid it; whether the direction of consensus revision has already raised the bar;
  how large a beat is needed for new fuel.
- The side claiming "already fully reflected" must answer: who has not yet entered; why there would be no new buyers or sellers after landing; the proportion of underreaction
  among past events of the same kind.
- Compare again after realization: separate the part already expected from the new information. The part already expected is no longer new fuel (when the only reason left is that one known news item,
  that card has been played); new information (the size of the beat, changes in guidance and in basis) is priced as usual. Do not downweight automatically because of the words "it was realized".
- Judging in advance whether the event itself is good or bad, and judging in advance how much the market has already reflected, are two different things; only doing both and giving the post-landing reaction path and
  the timing of the operation makes a complete event judgment.

## Reference anchors (not switches; scale by this symbol's volatility, and give reasons for departing)

- The pre-event cumulative move reaches or exceeds the implied range, and consensus has been revised up consecutively: lean "largely reflected" — landing is close to a sell-point candidate, and a beat
  of some size is needed for new fuel. Without an implied range, use about twice the normal monthly range as the reference, because that is roughly one complete monthly move. This is only a weak
  reference: the window and the information contained in the pre-event rise and in the implied range need not coincide, and this ratio alone cannot settle "priced in"; connect the specific variables expected beforehand,
  the actual result, the change in guidance, and the move relative to the sector.
- Pre-event sideways or underperforming the sector, and consensus widely dispersed or nobody talking about it: lean "not reflected" — the event is real directional fuel, and pricing only starts on landing.
- In between: write which end it leans toward and which observation is missing.

## Reversal on landing and post-announcement drift

- When a largely reflected direction runs the opposite way on landing day, it is often the mechanical unwinding of hedges rather than the market rejecting the news; the test is whether there is absorption or continuation
  over the following one to three days.
- When a direction was not reflected, the post-landing drift often lasts several days; the test is whether volume and breadth keep up.
- Both explanations must give "what would prove me wrong": the reversal camp must say where the absorption is, the drift camp must say when it stalls.

## When there is no discrete event: what earnings and multiple the current price depends on

With no event in the window, "how much is priced in" still has to be answered. This section is a conditional sensitivity analysis: it does not compute a "fair price", it is not a table due every session,
and it does only the one step relevant to the dispute.

**Align the basis first.** Which security, per share or per depositary receipt, which currency; which period the earnings cover (trailing twelve months, next twelve months, which fiscal year,
write out the period end); the accounting basis (reported under accounting standards or adjusted) and the share-count basis (basic or diluted), which are two separate dimensions; whose judgment it is (company guidance,
a single forecaster, aggregated consensus, your own assumption), and when it was formed. Compute only when period, definition, per-share unit, and observation date are comparable; where a conversion is possible note it,
where it is not, place them side by side or mark as missing; do not splice across bases.

**Decompose the past** (only when earnings expectations at the two dates are comparable):

```
price ratio = ratio of earnings expectations × ratio of multiples        (the two changes multiply, they do not add)
```

The expectations at both dates must be those available at the time and aimed at the same target period; rolling the expectation forward a year is not an upward revision; without the expectation of the time, write "the historical decomposition cannot be
quantified reliably", and do not backfill with today's number. The decomposition only says where the price change shows up; the cause and whether it can persist need separate proof: a change in the multiple may come from the persistence of growth, the quality of earnings,
or a change in risk, and does not automatically equal sentiment; an upward revision of expectations is a change in forecasts, not the same as already realized.

**Look forward** (choose a small number of conditional anchors relevant to the dispute; one that does the job is enough, do not pad to three):

```
conditional price ≈ a given earnings figure × a grounded multiple
earnings needed to support the current price ≈ current price ÷ multiple anchor          multiple implied by the current price ≈ current price ÷ earnings of some period
```

For each anchor make clear: the basis for the earnings; the basis for the multiple (its link to growth, quality, risk, or comparables, not a number picked out of the air); what change brings this combination about;
roughly when it gets reflected. Earnings and multiple often move together; do not treat them as independent of each other. The current price alone cannot solve for market expectations: when neither earnings nor multiple has a basis, you may only say
"these combinations correspond to the current price", not "the market expects some number".

Reading:
- A conditional price and the confidence that it happens are two different things. What is computed is "where the price is if the assumption holds"; how much confidence and when it is realized are judged by the debate.
- Align the decision horizon before connecting to the composite score's reference scale: several years of business runway are not counted whole into this session's window as upside or downside, unless you explain why the re-rating
  would happen within this window.
- A high or low multiple does not by itself yield a buy or sell direction. The gap between what the current price requires and your judgment must be told together with the path and timing of realization or re-rating; a gradual path (earnings continuing to be realized,
  risk changing step by step) is also a path, and no catalyst day needs to be invented. The effect on operation and size follows section 4 of the principles; this manual sets no separate threshold.
- The side claiming "the old multiple range no longer applies" bears the burden of showing what changed in earning power, growth persistence, or risk; the side claiming "a return to the normal range" bears the burden of showing why nothing changed.
  The same burden of proof on both sides.
- Choose variables that match the value driver and that the material on the table can support; there is no need to run every category: when earnings are negative or near zero, do not force an earnings multiple; when earnings swing widely with the cycle,
  read the multiple against the position in the cycle; for indices and broad funds look at aggregate earnings expectations and the aggregate multiple; when using enterprise-value multiples what you get is enterprise value, and you must say how it converts to per share;
  assets without cash flow do not use this section, go back to their own driving variables.
- Missing data limits only the corresponding step: without the old expectation, skip the historical decomposition, but still look forward. Missing data is not a reason to wait, nor does it push the score back to 50.

Data: the snapshot's fundamentals file has only the last few periods of actuals and the current-period consensus; forward earnings expectations, their revisions, and the historical range of the multiple come from evidence search,
recording the value, basis, source, and observation date.

## Exposure before the event

How to reduce exposure inside the event window and which exits exist is in section four of the execution-lines manual (P06); this manual only supplies the inputs it needs: expected range, pre-event move,
consensus and direction of revision.

## Executor output (on a model call)

The summary paragraph writes: the event and its date; the pre-event cumulative move (relative to the implied range or the symbol's own volatility); the consensus value and direction of revision; the reaction distribution to past events of the same kind (or marked missing).
The verdict addresses the filing side's claim: the facts support it = `supports`, the facts overturn it = `refutes`, half and half = `mixed`.
What counts as fact: the pre-event move, the implied range, the consensus value and direction of revision, the historical reaction distribution (each with source and date). What counts as interpretation: "how much is reflected"
and which path it takes after landing — the executor only collects facts; these two questions are left to the debate.

When the question is the section "When there is no discrete event", the summary paragraph writes: the basis (security and per-share unit, earnings period, accounting and share-count basis, whose judgment, observation date);
which step was done and what the result is; which assumptions the conditional result depends on; the parts that could not be computed and why (basis mismatch, missing old expectation). Compute correctly what can be computed, leave what cannot
as it is, do not pad to completeness. A conditional price computed correctly does not mean the assumption holds: the assumption is inference, written separately from the facts found.
