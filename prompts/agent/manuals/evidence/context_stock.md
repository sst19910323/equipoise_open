# Symbol context · Single stock

## Object of analysis and lenses

The single stock itself, read within its benchmark and its sector (if the configuration has a sector ETF). Relative position comes before absolute indicators: explicitly compare whether, relative to the sector and
relative to the benchmark, it is rising with them, falling with them, or moving against them; agreement in direction across layers does not equal several independent pieces of evidence (a heavily weighted symbol is itself pushing the benchmark and the sector); when they conflict, name
which layer dominates.

## Second-order variables that must be answered

- Size and quality tier (declared at the opening, open to challenge).
- Broad-market regime: risk appetite on or off, sector rotation; risk control layers market-level triggers on top. Whether a macro news item concerns this symbol at all: method in the
  "Transmission of a macro variable" section of P10.
- The valuation bridge: a dated earnings yield (or a justified alternative measure) against the same-date 10-year yield in the trading currency, with basis and arithmetic. The spread is a valuation input, not a timing rule: a wide spread supports the upside case, a thin or negative one requires the growth or risk change that offsets it over this window.
- Timeliness of fundamentals: financial metrics are anchored to the most recent fiscal quarter on a TTM basis, self-assessed by the time since the last report; do not pretend fields the snapshot lacks
  exist; missing ones go to outcall.
- Non-US single stocks: levels and currency follow the market the contract trades in; Japanese symbol codes are English short names, quotes are TSE yen prices, and levels are always written in yen.

## Level format

`operation_target` and `short_term_path.target` take a single numeric level (denominated in this contract's trading currency; a bare number only, no currency suffix).

## Regional first-hand sources (evidence search priority)

US: company investor relations, exchange announcements, regulatory filings, then the mainstream financial wire services.
Europe: company investor relations, local exchange and regulatory disclosures, authoritative local-language financial media, then the English-language wire services.
Japan: company investor relations, the TSE and Bank of Japan websites, authoritative Japanese-language financial media, then the English-language wire services.
Korea: company investor relations, the Korea Exchange, the Bank of Korea and Statistics Korea, then the English-language wire services.
