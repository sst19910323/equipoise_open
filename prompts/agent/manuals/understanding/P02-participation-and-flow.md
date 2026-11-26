# P02 / P03 · Participation structure and flow drivers

Two questions sharing one data pull, bound in one manual. The filing names one of the numbers, and the executor answers only that one; the data pull fetches only the items that question uses.

| Number | Section | What it answers |
|---|---|---|
| P02 | Crowding | Who may be crowded on the same side, how large the reflexivity risk is |
| P03 | Flow attribution | Which drivers are behind this stretch of price action, where the fragile links are |

Crowding and mechanical flows are not the same thing: one-sided positioning can come from fundamental money, and mechanical flows can occur in an uncrowded market. The two questions are answered separately.

## Shared: data pull and basis

| Item | How to fetch | What it is |
|---|---|---|
| Short interest as a share of float | Search, record the value and the data date. For US stocks it is the exchange's semi-monthly disclosure, so being two weeks stale is normal; for non-US use the local basis and search in the local language (Japan 空卖残高, Korea 공매도 잔고, Europe regulatory net-short disclosures) | Direct positioning evidence (short side). Note: daily short-sale volume is not the short-interest balance; do not mix them up |
| Institutional holdings changes | Search the latest quarterly disclosure; when there are rumors naming an institution or a person, search those too | Direct positioning evidence, but only a quarterly snapshot, long side only, not real-time net exposure |
| Sector ETF net flows | Search the direction and size of weekly net flows (relative to assets under management) | Direct flow evidence |
| Margin debt balance | Search on this market's own basis (US broker margin balances, Japan 信用取引残高, Korea 신용융자 잔고), record the year-on-year direction | Direct evidence of leverage, disclosed monthly or weekly |
| Market breadth | `relative_strength` of this region's cap-weighted broad index against its equal-weight mirror (for US stocks e.g. SPY vs RSP, for tech add QQQ vs QQEW); where there is no equal-weight mirror, search advance/decline counts | Machine number; says whether gains and losses are concentrated in a few heavyweights or broad-based |
| Options and derivatives activity | Search on this market's own basis (US: share of zero-day options and the put/call ratio; Japan and Korea: index futures and options open interest) | Direct evidence of speculative structure; say so plainly if it cannot be found |
| Liquidity at the margin | Search the direction of this region's central-bank balance sheet or net liquidity | Background variable |
| Sentiment index, fourteen-day RSI, volume ratio, news density | Read directly from the data on the table | **Proxies**: they reflect price momentum and attention, not positioning evidence |

Three rules on basis:
- Every number carries its data date and disclosure frequency. Disclosures from different dates cannot be stitched into "the state of money right now": write which date each one is from.
- Proxies do not pass for direct evidence: a high RSI only says price momentum is strong and cannot be written directly as "heavy positioning".
- If it cannot be found, write missing. Lack of positioning data does not equal "not crowded"; you may only write "cannot be determined".

## I. Crowding (P02)

**When to use**: judging reflexivity risk in a main up-leg or down-leg; someone shouts "everyone is buying" or "shorts are too crowded". Crowded longs flash-crash easily, crowded shorts
squeeze easily.

| Indicator | Reference band (origin; scale by symbol and market) | Reading |
|---|---|---|
| Short interest as a share of float | Low (roughly below 5%) / elevated (5 to 10) / high (10 to 20) / extreme (above 20, where most historical big squeezes sat) | High tier = shorts are crowded, which is squeeze fuel, not evidence of a fall |
| ETF weekly net flows | One-directional, reaching about 1% of assets under management for two consecutive weeks | Direct evidence of one-sided money |
| Sentiment index | Above 75 or below 25 (the system's existing extreme-value convention) | Proxy for one-sided weight at the market level |
| Fourteen-day RSI | Above 70 or below 30 (classic thresholds) | Extreme price momentum — a proxy for heavy positioning, not "due to reverse" |

A single item is only a clue. Confidence rises with the number of independent items pointing the same way: with direct positioning or flow evidence pointing the same way, you may call it crowded; with only proxy items pointing the same way, write
"proxies point to heavy positioning, direct evidence missing", and lower confidence accordingly. Usage: crowded plus main up-leg = beware a reflexive break, which changes the size tier and does not by itself set direction;
uncrowded plus main up-leg = the rise has a solid base; crowded shorts plus stabilization = short-squeeze candidate.

## II. Flow attribution (P03)

**When to use**: a stretch of price action needs characterizing — is it driven by price-setting money (fundamental or information driven) or mechanical flows (trend following, passive creations and redemptions, options
hedging, short covering); when someone calls a fall merely on "no fundamental support", this is the standard rebuttal.

| Observation | Reference (origin; give reasons for departing) | Reading |
|---|---|---|
| Narrow breadth | Cap-weighted outperforms equal-weight by about 3% or more over twenty days (noise band about plus or minus 1%) | A few heavyweights lifting the index. By itself it cannot tell mechanical or concentrated flows from earnings expectations improving in just these few names: judge again together with those names' earnings revisions and flows |
| Deteriorating leverage | Year-on-year increase of about 20% or more and at a new high for the phase | Persistently high levels do not count as deterioration; look at the rate of change |
| Fragile structure | Items among breadth, leverage, speculation, and liquidity deteriorating at the same time | The more items deteriorating together, the more solid; with only one or two, write which one or two and what is still missing, and lower confidence accordingly |

Attribution need not be either-or: a stretch of price action is often several drivers stacked; write which explanation fits the existing observations better and which part remains unclear. Several risk proxies changing at the same time
does not prove which kind of money is pushing.
Consequences of a mechanical-flow finding: mechanical flows are real buying and selling and followable; a fall may not be called merely because "there are no fundamentals". But they do not price on fundamental improvement, and when the trigger
reverses they reverse just as mechanically — this goes in the risk section and is settled symmetrically.

## Executor output

Write only the question that was named. The summary paragraph is written in four parts: **direct observations** (value, date, basis), **proxies**, **which explanation fits better**, **what is still missing**.
- P02: rule crowded, not crowded, one-sided weight, or cannot be determined; state which items the basis is and how many of them are direct evidence.
- P03: write the composition of the drivers and the fragile links; to what degree the fragile-structure claim holds (which items are deteriorating).

The verdict addresses the filing side's claim. What counts as fact: dated disclosure numbers and machine-computed breadth. What counts as interpretation: attribution and the composite
judgment of "crowded". What cannot be found publicly (the order book, trend-following funds' positions, single-name options hedging) can only be inference, and is written as inference.
