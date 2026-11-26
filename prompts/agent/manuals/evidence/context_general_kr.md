# Symbol context · GENERAL_KR (Korean broad market)

## Object of analysis and lenses

The Korean broad market as a whole, with the two price anchors KODEX 200 ETF (which tracks the KOSPI 200 index) and the KOSDAQ 150 index compared side by side, overlaid with Korean economic data,
the Korean volatility index, and the global backdrop. Treating either single lens as the object of analysis is forbidden: the two represent different exposures, and their divergence is itself a key signal.
- KODEX200: the large caps of the KOSPI 200 — memory and foundry leaders, autos, banks, the foreign-flow bellwether. Its level is the ETF's won price (hundreds of thousands), not index points.
- KOSDQ150 (KOSDAQ 150): the growth and mid/small-cap board — biotech, secondary batteries, entertainment, domestic retail flow. Its level is index points (thousands).

Each of the two lenses gets an independent judgment; the speech and the final report each cite their own indicators once; when they diverge, name the meaning of the divergence. The bare KOSPI composite
index has no contract definition, and its points may not be invented.

## Second-order variables that must be answered

- Bank of Korea policy, dollar-won, and the transmission of foreign flows.
- Where total exports, semiconductor exports, and the trade balance sit in the cycle.
- The concentrated influence of the heavily weighted stocks (the memory and foundry leaders) on the broad market.
- Whether the Korean volatility index agrees with global risk appetite.
- The US backdrop uses raw data, not the conclusions of the US broad-market report; the anchor of the conclusion is Korea.

How these variables transmit into price: method in the "Transmission of a macro variable" section of P10; direction is judged by the evidence of the period, not presumed.

## Level format

`short_term_path.target`: `KODEX200=109500; KOSDQ150=1205.00` (ETF won price as an integer; index points with two decimals; each lens in its own unit); in a range (震荡), each lens writes a range.
KOSDQ150 is an index: its levels are observation lines, traded through a proxy. The exchange rate and the volatility index do not enter the operation level.
