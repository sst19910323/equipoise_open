# Symbol context · GENERAL (US broad market)

## Object of analysis and lenses

The US broad market as a whole, with the three benchmark ETFs SPY, QQQ, DIA compared side by side, overlaid with macro news, economic data, and sentiment.
Treating any single ETF as the object of analysis is strictly forbidden: the three represent different sector exposures, and the divergence among them is itself a key signal.
- SPY (S&P 500): broad-based; the primary lens for judging whether risk appetite is on or off.
- QQQ (Nasdaq 100): the main battlefield of the technology and artificial-intelligence narrative.
- DIA (Dow 30): industrials, financials, and consumer staples; the contrast lens when technology runs on its own.

Each of the three lenses gets an independent trend judgment; the speech and the final report each cite their own indicators once; when the three lenses diverge in direction, the meaning of the divergence
must be named (sector rotation, risk-off in cyclicals, and the like) — no fudging.

## Second-order variables that must be answered

- The Fed's policy path, long-end rates and the valuation bridge: compare a dated, scope-matched earnings yield of each lens with the same-date 10-year yield, stating basis and arithmetic. The spread is a valuation input, not an equity risk premium estimate or a timing rule: a wide spread supports the upside case, a thin or negative one requires the growth, payout or risk change that offsets it, over this window. If the metric is unavailable or unsuitable, say why and use a justified alternative.
- The dollar.
- The breadth of earnings-expectation revisions.
- Credit and liquidity.
- What moved recently, and why: over the last five completed sessions (plus any move in the current session up to the cutoff), name the move or divergence among the three lenses most relevant to this decision — lens, dates, size in that lens's own ATR — and its dated explanation, separating facts available by the cutoff from causal hypotheses; justify any longer lookback. Rises and falls alike. An unexplained move that could change your scenario is an open question for an outcall, not background; the move itself proves neither continuation nor reversal.

Inflation, employment, and growth data are inputs for explaining the policy path and the earnings path; they do not form a separate must-answer list. Divergence among the three lenses is a phenomenon to be explained; it does
not itself serve as a cause. How these variables transmit into price: method in the "Transmission of a macro variable" section of P10; direction is judged by the evidence of the period, not presumed.

## Level format

`short_term_path.target` uses a three-level string, separated by semicolons, joined by equals signs, two decimals:
`SPY=612.50; QQQ=667.50; DIA=445.20`. In a range (震荡), each lens writes a range: `SPY=600-620; ...`.
