# Data capability map (executors read it before acting; debaters consult it when judging whether to go through a data call)

Answers three questions: what can be queried, how codes are written, what explicitly cannot be queried. The authoritative source for subscription facts is
`skills/equipoise-manage-ibkr/references/operations.md`; subscription changes are made there first and then synced here.

## 1. Queryable scope and precision by region

| Region | What can be queried | Precision and timeliness |
|---|---|---|
| US stocks and US ETFs | Historical bars (1m/5m/1h/1d) and real-time quotes for all US symbols | No consolidated market-wide real-time: only single-venue real-time streams and snapshots; historical minute bars lag about fifteen minutes. Go by the returned market-data type |
| Europe (Germany, Netherlands, France, Denmark) | Names listed on XETRA and Euronext, German ETFs, the DAX and IAEX benchmarks | Level 1 real-time is available; the configuration treats it as a delayed source |
| Korea | Korean symbols, the KODEX200 benchmark ETF and the KOSDAQ 150 index (`KOSDQ150`, broad-market second lens) | The Korean stock bundle's Level 1 is subscribed; real-time once contract resolution goes through |
| Japan | This repository's codes use English short names (ADR codes serve only as names); quotes go through TSE contracts; levels are in yen; N225 benchmark and the TOPIX index (`TOPX`, broad-market second lens) | No real-time TSE subscription; quotes are a delayed source; market closures and delays are not data faults |
| FX | IDEALPRO currency pairs | Real-time, subscribed |

## 2. Codes

- Always send requests with this repository's codes (`config/symbols.yaml` is authoritative); the mechanical layer does the conversion itself.
- The codes of Japanese and Korean symbols are English; TSE and KRX numeric codes are used only in the configuration as a quote fallback; numeric codes are forbidden in requests —
  a number would be misparsed as a price. Codes with dots are written as they are.
- The bare KOSPI composite index has no contract definition; it may not be used as a substitute and its points may not be invented; the Korean broad-market lenses are the KODEX200 ETF, whose level is denominated in
  won price, not index points, and the KOSDAQ 150 index (`KOSDQ150`, index points).

## 3. What explicitly cannot be queried (mark missing on sight; do not try)

- China A-shares, Hong Kong stocks, Asian real-time quotes other than Korea.
- Japanese TSE real-time quotes (delayed source only).
- Commodity spot and supply-chain prices (memory spot, oil price, and the like): no such data source; go through evidence search for qualitative verification.
- Fundamentals and consensus expectations of symbols not in the store (market data is not a fundamentals store): go through evidence search.
- Co-movement regression between macro indicators and price: economic indicators are monthly, only a few dozen points in five years, statistically underpowered and prone to spurious relations; not provided; macro
  attribution goes through `econ_series` readings plus your own argument.
- Error codes: 354 and 10089 are genuinely unsubscribed; 10167 can fall back to delayed data.

## 4. The seven data-call modes (the only legitimate way to retrieve data; ad hoc scripts are forbidden)

```
python evidence_quant.py '<request_json>'
```

Pass two gates before choosing a mode: is it market or quantitative data (if not, go through evidence search); is it a third-party perspective (your own symbol's daily bars and its strength against its own benchmark
are already in the snapshot; a filing will be rejected as a duplicate). Copy parameter names from the table below; common misspellings are normalized and echoed back; unknown keys are rejected outright with the
list of legal keys echoed back.

| Mode | When to use | Parameters (required → optional) |
|---|---|---|
| `peer_compare` | Cross-sectional comparison for a single stock, or a sector ETF that has a peer group (the peer group comes from the evidence map) | `symbol` → `window_days` (default 20) |
| `bars` | Raw daily bars of some third-party symbol; or the intraday series of a symbol prepared for this session (itself, benchmark, sector, broad-market lens) | `symbol` → `lookback_days` (daily default 30), `interval` (`1d` default / `1h` / `5m`), `date` (only for `5m`: the trading day in the exchange's local time, default the most recent) |
| `relative_strength` | Strength curve of two symbols, choosing a specific third party as benchmark | `symbol`, `benchmark` → `window_days` (default 20) |
| `econ_series` | Current reading of a regional economic indicator | `region`, `indicator` |
| `event_calendar` | Whether there is a macro binary event in the window | none → `symbol`, `horizon_days` (default 14) |
| `pair_link` | Co-movement modeling of any two symbols: daily-return regression (beta, annualized alpha, goodness of fit, residuals over the last twenty days), direction of the current correlation, percentile of the co-movement strength against five years of history, drift of the strength (tightening / decoupling / sign flip) | `symbol`, `counterpart` → `window_days` (default 60) |
| `trend_fit` | Trend quality of any third-party symbol: linear fit on log price, annualized slope and goodness of fit for a short window and a long window each; short-long divergence is a potential turning point | `symbol` → `window_days` (default 60) |

Misuses the code cannot catch and only judgment can:
- Broad-market vehicles (SPY, QQQ, DIA and each region's broad-market lens) have no peer group; do not use `peer_compare`; for a comparison use `relative_strength`
  with a specific third party, or go through evidence search.
- `event_calendar` covers only macro policy and inflation dates, not company earnings dates; company events go through evidence search. An empty result is a valid answer of "no known
  event in the window", not a failure.
- The intraday tiers of `bars` only read files the data preparation has already written down and do not fetch on the spot: symbols that were not prepared have daily bars only. `1h` gives the most recent few trading days (default 5, at most 10);
  `5m` gives only one trading day per call. Each bar carries its start time, session, and open-high-low-close-volume; the receipt states which sessions this data covers (pre- and post-market exist only for US stocks, and only in `5m`: the hourly file is regular-session only).
  A bar's time is its start; ordering within one bar cannot be seen; when the receipt reports missing or says the source is unverified, it is absent — do not guess intraday from daily bars.
- Regression, correlation, and fitting are the ceiling of model-level processing; cointegration, volatility models, and the like are not on the menu; do not invent paradigms.

## 5. Other conventions

- Peer comparison is computed in the data-preparation stage and the trend-structure numbers are inside the technical snapshot: the orchestration gives the paths in the request; read them first, and call again only if that is not enough.
- US stocks and US ETFs not held locally are fetched once from IBKR on the spot as daily bars; `pair_link` and `trend_fit` ask directly for five years of depth; when the history is shorter than
  five years the output carries a `depth_warning`; carry it along when citing.
- The output may carry a `stale_warning` (data expired and the live fetch failed): note the time point when citing; do not treat it as the latest price.
- A return of `ok:false` or `gaps` is a data gap: enter it into the result as it is; not found is neither bullish nor bearish; a question that can be answered qualitatively is moved to
  evidence search and retried.
