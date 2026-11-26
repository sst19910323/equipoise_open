# P10 · Linkage and transmission

**When to use**: news or logic points to "some symbol is pulling it" — supply chain upstream or downstream, a major customer or supplier, competitor substitution, a macro proxy
(rates, currencies, commodity vehicles). Linkages may be thought up on the spot; the counterparty is not limited to the watchlist (US stocks and US ETFs may both be named, the mechanical layer pulls them live;
a non-US counterparty that cannot be pulled is marked missing as it is, and the route switches to evidence search). When someone uses a macro variable (policy, rates, currencies, commodities, economic data) as an argument in this session,
use the later section "Transmission of a macro variable", state in the filing that you are asking about this section, and there is no need to measure the correlation of two symbols first.

## Execution steps

1. Measure linkage strength: `pair_link` (regression and correlation are the ceiling of model-level processing; cointegration and the like are not on the menu). Beyond static correlation, read
   in the output the direction of linkage (same or opposite), the percentile of linkage strength against history, the drift in strength (tightening, decoupling, sign flip), the recent residual —
   "the part it walked on its own" is more informative than static correlation. Strength is the absolute value of the correlation: a stable inverse relation is also linkage, and a deepening negative correlation is tightening, not decoupling.
2. Check the transmission chain: search revenue dependence, order share, or shareholding share, with specific numbers.
3. What happened to the counterparty itself, how large, and with what lag it transmits to this symbol; when the event itself is unclear, hand off to the event-pricing topic or evidence search.

## Reading (reference values; give reasons for departing)

| Judgment | Reference (origin) | Reading |
|---|---|---|
| Strong linkage | Absolute correlation of about 0.6 or more (empirical reference: about a third or more of daily variation can be linearly explained by the counterparty, and they did historically move together; a negative value is a stable inverse relation), and strength at a high historical percentile or tightening | The linkage narrative has a statistical footing; the transmission chain is still missing |
| Medium linkage | About 0.3 to 0.6 | Fit only for a supporting argument; cannot carry direction on its own |
| Weak linkage | About 0.3 or below, or strength currently decoupling | Legitimate evidence against a "dragged down or pulled up" narrative, equal weight in the reverse direction; but weak historical correlation does not directly overturn a newly emerged business transmission, which depends on the transmission chain |
| Sign flip | Correlation crosses zero with neither end hugging zero | The old reading of the linkage cannot be copied over: first check what changed the relationship between the two, then discuss direction |
| Substantive exposure | The transmission chain has numbers (revenue dependence of about 10% or more counts as substantive) | A "supply-chain panic" over a very small share (about 5% or below) is sentiment contagion, downweighted |

Correlation is not transmission: moving together in the past only says something about the past, and a directional claim still needs the current event chain — what happened to the counterparty, the share, the lag.

## Transmission of a macro variable

This section is an analytical method, and debaters may reason by it themselves. Only when this transmission may clearly change this session's direction, level, size, or invalidation condition, and the material on the table and simple
reasoning cannot resolve it, is it worth filing a model call; already verified macro facts cite the existing receipt and are not redone every round. The four questions below are the order of working through it, not four gates:
the parts that can be made clear may support the claim to a limited degree, the links that cannot be made clear limit its scope of effect, and a full answer is not required every time.

1. **What changed.** Actual minus consensus is the headline-basis surprise; before computing, check that indicator, period, unit, year-on-year or month-on-month, and release version match. When consensus
   is missing or the release time is only a date, write so as it is, and do not claim to have computed a surprise. Matching consensus only says there was no surprise on the headline basis: components, revisions to prior values, wording, path, and
   the removal of uncertainty may still carry incremental information; a long-standing background factor still has an effect when exposure or the market's understanding of it has changed. One cannot claim a new shock merely because "it was published" or "it stayed at
   this level".
2. **Through which channel to this object.** Three channels as navigation: valuation (rates and risk appetite change the multiple the market is willing to pay), earnings (demand, costs, and currency translation
   change earnings expectations), money (liquidity and cross-border flows change the buying and selling). Choose by relevance, they may cross, and there is no need to fill all three. When one variable runs several channels in opposite
   directions at once, tell them apart, then say which prevails in this window and on what basis; do not compress them into one number with a correlation coefficient.
3. **How much, and when.** Reference:

   ```
   effect on this channel (rough reference) ≈ the variable's surprise change × the sensitivity on this channel
   ```

   Sensitivity is not exposure: revenue share, basis points of rates, percentage moves in a currency cannot be multiplied directly into a price effect. Most of the time only direction and order of magnitude are judged; when sensitivity
   has no credible basis, do not fill in a number, use words to make the transmission chain clear. Time comes in two: when operations and cash flow realize it, and when the market may adjust its expectation of it. Late realization
   does not automatically rule out a price effect in this window; what may be updated in this window and why it would be updated is told by current-period evidence; when there is only a long-horizon story and no basis for transmission in this window,
   reduce its effect on this action, do not delete it outright. Nor are the valuation and money channels unconditionally deemed already transmitted.
4. **What would make this transmission fail to stand.** Say what should be seen if the transmission holds. Not seeing it is counter-evidence that needs explaining: first check whether the observation time is long enough, whether there is data,
   whether a reverse channel offsets it; if what should appear within a reasonable window keeps failing to appear, revise this inference down. The absence of an immediate price reaction does not mean the business channel does not exist.

Division of labor: this section provides the basis for direction and magnitude, and the timing of realization and re-rating; "how much is priced in" is settled once only in expectations and pricing (P01), and the discount is not estimated
again here. The same macro fact may have opposite effects on different objects; that is not a contradiction; only a mismatch in the factual basis (indicator, period, value, version) is.

`pair_link` serves in this section only as a supporting check: what it gives is the statistical relation of the symbol's return to the price return of the proxy vehicle, the net result of several channels combined, not the sensitivity to
the rate or currency change itself; a bond vehicle's price and its yield move in opposite directions, and the units differ.

## Executor output

The summary paragraph writes: the two symbols' correlation (with direction) and beta, strength percentile and drift, the transmission share number or its gap, one sentence on the event, rule strong, medium, or weak linkage and whether the exposure
is substantive. The verdict addresses the linkage claim (weak linkage against a "dragged down" claim = `refutes`). What counts as fact: the machine numbers of regression and correlation,
the share numbers found. What counts as interpretation: whether the transmission holds. A linkage claim is by nature interpretation and usually cannot carry the highest tier; when the transmission has actually happened and become a direct-impact-grade
event, follow that news item; with only a story and no numbers, it is only inference.

When the question is the section "Transmission of a macro variable", the summary paragraph writes: the variable and its basis (indicator, period, actual value, consensus or its absence, release time); which channel it runs,
the direction of each, which prevails in this window; the basis for the magnitude or its gap; when realization and re-rating each occur; whether the phenomena that should be seen have appeared. What counts as fact:
published values, consensus, the exposure numbers found. What counts as interpretation: whether the channel holds, which prevails, the magnitude.
