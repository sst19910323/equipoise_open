# P04 / P05 / P07 · Price and trend structure

Three questions on the same set of price-volume data, bound in one manual. The filing names one of the numbers, and the executor does only that section; the other sections need not be run.

| Number | Section | What it answers |
|---|---|---|
| P04 | Magnitude and threshold | How far an indicator crossing (cross up, cross down, turning positive or negative) is from the threshold, whether it is widening or converging, whether it counts |
| P05 | Price action | Whether the market accepted a specific action (breakout, breakdown, spike or dump, long wick) |
| P07 | Trend structure | Whether the move over a longer window is a structural fact or naive extrapolation; how to recognize the end of an extension and fading momentum |

## Shared: data pull and basis

1. **Read what is on the table first**: the technical snapshot — current values, ATR, moving averages, the three structure layers (confirmed highs and lows with their confirmation time, the unconfirmed right end), the
   two-year price digest, and the trend-structure numbers (`trend_signal_inputs`: twenty-day cumulative, fit and goodness of fit, counter-move retracement during the period, days a key level has held, distance to the
   sixty-day high and low; `positioning`: pullback ladder and rebound ladder). Paths are given by the orchestration.
2. **Call for more only if that is not enough**: to change window, get a series, or look at another symbol, use `bars` and `trend_fit`.
3. **The limits of the daily bar**: `bars` gives daily bars by default (open, close, high, low, with volume when available). What happened in pre-market, after-hours, and data-release windows, and
   at what moment the extreme printed, the daily bar cannot answer — if the session information in the snapshot on the table is enough, cite it; if not and the symbol was data-prepared for this session, file `bars` at the intraday tier
   (`interval` set to `5m` for pre-market / after-hours questions — the hourly file is regular-session only; order of events within a single bar still cannot be seen); if it cannot be obtained, write "session unknown" as it is, and do not guess from the daily bar.
4. **One noise ruler**: whether a crossing hugging the threshold counts is scaled by this symbol's own volatility (ATR); high volatility widens it, low volatility narrows it. The
   percentages in the three sections below are common magnitudes for a medium-volatility symbol, a reference, not a switch: when it falls a little short, write where it falls short and how much confidence is discounted, not "does not hold".
5. The machine numbers cited are facts, whoever cites them; "so it is noise", "so it is valid", "so the structure is intact" are interpretations.

## I. Magnitude and threshold (P04)

**When to use**: anyone uses an indicator's sign event directly as a conclusion. The scale is not a switch. The data is all in the snapshot, and citing it directly is usually enough; when asked
to re-check, send out a request for the executor to rerun it.

Steps: copy out the indicator's current value, the threshold, and the distance (RSI points from 70 or 30; the fifty-day average's twenty-day slope; close's deviation from the twenty-day or fifty-day moving
average) → see whether it widened or converged over the last five days → check volume on the crossing day relative to the twenty-day average volume (the snapshot gives the last full day's volume ratio and the up-day / down-day volume ratio).

| Indicator | Hugging (default noise band) | Substantive |
|---|---|---|
| RSI vs 70 or 30 | Within about five points of the threshold | About ten points or more from the threshold |
| Trend fit (annualized slope of the log-price fit, R²) | R² below about 0.3, or the twenty-day and one-year slopes disagreeing | R² about 0.55 or more with both windows agreeing |
| Moving-average deviation | Crossing within about plus or minus 2% of the average (daily noise band) | Deviation of about 5% or more |
| Key level (prior high or low, round-number level) | Within about plus or minus 1% | Clearly away from it and holding |
| Volume | Below the twenty-day average volume | About one and a half times the average volume or more |

A small crossing hugging the threshold with no volume is treated as noise by default; the side citing it explains why this time is an exception (an event, volume, structure). It works in both directions:
it blocks "the fifty-day average turned down = bears confirmed" (a slope hugging zero) just as it blocks "RSI not yet overbought = room to rise" (far from 70).
The product is a magnitude rebuttal: the other side should make a numerical correction, not a reversal of position; the same applies when you are hit.

## II. Price action (P05)

**When to use**: citing or rebutting a specific piece of price action. Support and resistance, breakouts and breakdowns, use the same acceptance test.

Steps:
1. Three numbers for the day of the action: volume as a multiple of the twenty-day average, the session in which it occurred (regular session, pre-market or after-hours, data-release window; see the limits of the daily bar above),
   the close relative to the action level (holding outside the range or pulled back inside).
2. One to three days after: did it hold (retest absorbed, volume continued, or snapped back).
3. Location: how far the action price is from the prior low, prior high, or round-number level — whether it was an action inside a stop-cluster zone (defined in the execution-lines manual).
4. Attribution needing "who was buying and selling" hands off to flow attribution (P03); an action at the end of an extended move hands off to the event-pricing topic (P01).

| Observation | Judgment |
|---|---|
| Market acceptance | With volume (about one and a half times the average or more), close holding outside the range, no snap-back afterwards (within about three days). All three present = complete evidence; if one is missing, write which, lower confidence accordingly or change the play (go light first, wait for the missing one); it does not mean it did not happen |
| Noise or suspected stop sweep | No volume and the crossing magnitude falls inside the noise band, or it occurred in a thin-liquidity session |
| Sweep and reclaim (bad for the bear thesis) | Breaks below a key level and reclaims it quickly (within about a day) with selling pressure exhausted — someone was absorbing there |
| Sweep of the upper edge and distribution (bad for the bull thesis) | Pushes through the prior high and immediately falls back with the attack exhausted — the mirror pattern, do not get the direction backwards |
| The reverse counts equally | An expected reaction that did not occur is hard evidence: bad news lands and the day closes flat or up with no follow-through fall the next day, which is price falsification of the bear thesis; good news landing without a rise, likewise |

The paired sentence: complete confirmation is a way to guard against noise, not a universal default posture — signals on popular large caps are arbitraged instantly by the whole market, and demanding complete confirmation
is structurally late (for a quality name, signs of fading momentum or absorption are enough, see size and quality tiering); to wait for confirmation, you must be able to say which unresolved change you are waiting for.

## III. Trend structure (P07)

**When to use**: someone argues direction from a move already made — classify first: naive extrapolation ("it rose so it will keep rising", forbidden) or structural description (legitimate consideration);
or you want to claim a grinding rise or grinding fall structure, a key level held or lost, or fading momentum.

Steps: read the snapshot's `trend_signal_inputs` and the trigger signals derived from them (both use the same reference lines; the sole source of the values is the code constants, and this table only explains how to read them) →
classify against the table → to upgrade the structure into a directional forecast, add an evidence search: which catalyst or money would continue or reverse it (hindsight upgraded to forward-looking).

| Judgment | Reference (origin) | Meaning |
|---|---|---|
| Grinding rise or grinding fall structure | Twenty-day cumulative of about 5% or more, goodness of fit of about 0.55 or more, counter-move retracement during the period not exceeding half the move (fit over one half = more than half the move is explained by a straight line; about 5% is a valid monthly-scale move) | A machine-provable structural fact about the past: it supports a cycle-stage determination and a "structure intact or deteriorating" description; it enters the scenario as conditioning information for the nodes' probabilities, not as a verdict for either side |
| Key level held or lost | Consecutive closes above the prior twenty-day high or below the low for about two days or more (a single-day crossing is noise; the fact starts at two days) | Structural fact; a label applied before this fact must be reconciled before it is repeated |
| Extension of the current leg | The snapshot's `leg_z`: how far the current leg has run from the last confirmed tactical extreme in units of the symbol's own range (ATR·√bars); read it with the leg's bars and the twenty-day cumulative | Conditioning information for both the continuation node and the reversion node: a large extension alone implies neither weaker nor stronger continuation; weigh it with a horizon-matched reference class when one exists, otherwise state the uncertainty; a confirmation signal at a large extension passes through the event-pricing topic before anyone chases |
| Fading momentum (symmetric, both directions) | The first ten days forceful (about 4% or more), the last ten days stalled (within about 1%), and close to the sixty-day high or low; highs no longer making new highs, or lows no longer making new lows, as a reinforcing item | Upside fading = legitimate bearish structural evidence; stalling at the highs weighs the same as a grinding rise, the bull's "trend intact" label must settle this account, and a light-tier trim is the symmetric exit; downside fading = it cannot fall further, and when a quality name is treated as a false alarm, this counts as entry evidence at the "sufficient" grade |
| No clear trend | Low goodness of fit (about 0.3 or below): a straight line does not explain this stretch — it may be a range, or a turn, or irregular; the fit says nothing about magnitude | Do not force a trend label; directional claims go back to events and money for proof; to call it wide or narrow, look separately at the distance between the range's high and low and the daily range |

## Executor output

Write only the result of the section that was named. The summary paragraph:
- P04: the indicator's current value, distance from the threshold, direction over the last five days, volume ratio on the day; rule hugging or substantive.
- P05: date and action, volume multiple, session (or "session unknown"), close position, behavior afterwards, distance from the key level; rule acceptance, noise, sweep, or
  reverse evidence, writing out what is missing. When it occurred at the end of an extension, note that the event-pricing topic must be passed before chasing is discussed.
- P07: twenty-day cumulative, goodness of fit, counter-move retracement, days held; rule the structure type; whether the other side's citation is naive extrapolation or legitimate structural description.

The verdict addresses the claim under examination. What counts as fact: the snapshot (including its trend-structure numbers) and freshly fetched machine numbers. What counts as interpretation: the characterization of those numbers.
Naive extrapolation does not count as evidence; purely hindsight consideration cannot carry a high tier.
