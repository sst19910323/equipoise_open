---
description: Refresh one company's fundamentals (look up the latest earnings report → write PG fundamentals + company → write back the schedule). hermes scheduling entry point. Usage: /refresh-fundamentals AAPL
---

Refresh the fundamentals of the company named by `$ARGUMENTS`: WebSearch/WebFetch the latest earnings report → write PG `equipoise`'s `fundamentals` (financials) + `company` (identity) → write back last/next of `fundamentals.<SYMBOL>` in `config/update_schedule.yaml`.

**hermes headless invocation** (runs on the server at `/opt/equipoise`, PG on localhost, python is `./venv/bin/python`):
```
cd /opt/equipoise && claude -p "/refresh-fundamentals AAPL" \
  --model sonnet --permission-mode dontAsk \
  --allowedTools "WebSearch,WebFetch,Read,Write,Edit,Bash(./venv/bin/python datalayer/pg/db.py *),Bash(date *)" \
  --output-format json
```

## Step 0: Parse

`$ARGUMENTS` = `<SYMBOL>` (an optional second argument restricts fields; omitted = refresh all of the company's common fields). Read `fundamentals.<SYMBOL>` in `config/update_schedule.yaml` for `last` (the release date refreshed to last time). Unknown SYMBOL → output `{"error":"unknown symbol"}`.

🔴 **Use SYMBOL verbatim, never normalize or rewrite it** (quant matches exactly; a misspelling = silent empty lookups on its side). Berkshire is `BRK.B` (uppercase + ASCII period, not BRK-B/BRK.A); Japanese names use the canonical English `MHVYF`/`SHECY`/`NTDOY` etc. (**not** the numeric 7011/4063 — the numbers are only the ibkr_ticker in config and do not enter the database). The symbol written to company/fundamentals must be identical to the one passed in.

## Step 1: Look up the latest earnings report (WebSearch/WebFetch, neutral)

Look up the company's **new earnings reports since `last`** (usually just the most recent period). For each field get:
- `field`: **named precisely in the company's own terms** (US: revenue/net_income/eps_diluted/gross_margin/operating_income; EU: sales/operating_result/operating_margin/order_backlog; JP: net_sales/operating_profit/ordinary_profit/net_income …). **No forced cross-company uniformity**; but **the same field of the same company must keep the same name across periods** (look up and reuse the field names already in PG).
- `span`: **period length** `3M`/`6M`/`9M`/`12M` (how many months the report covers); point-in-time stocks (balances such as order_backlog/net_debt) use `snapshot`. ⚠️ European and Japanese companies often report cumulatively (9M/H1=6M); do not treat everything as 3M.
- `value` / `unit` (with currency: `USD M` / `EUR M` / `JPY B` / `%` / `USD`)
- `consensus`: market expectation (store it if available, otherwise leave null)
- `release_date`: **the earnings release date** (ISO; the axis as_of uses to prevent lookahead)

### 🔴 Iron rules of honesty + accuracy
- **Do not trust the prose of search summaries for numbers** (it mixes up periods and figures) — verify key numbers against an authoritative table via `WebFetch` (stockanalysis / Macrotrends / company IR / SEC 8-K).
- **Unreleased data does not enter the database** (release_date > today → skip). No clean value found → skip that field; do not make one up.
- Derived quantities (prior value / YoY / beat vs. expectation) are not stored; compute them at read time.

## Step 2: Refresh the company identity while at it

Having found the company's current name/sector/industry/currency (which rarely change), assemble one `company` record (meaningful only when changed, harmless to upsert when unchanged). `region` uses the data-brain codes: US/EZ/UK/JP/KR.

## Step 3: Write staging + load into PG

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
```
- Company identity → `data/lowfreq_staging/_staging_company_<SYM>_<TS>.json` (`{"companies":[...]}`) → `./venv/bin/python datalayer/pg/db.py load-company <file>`
- Financial points → `data/lowfreq_staging/_staging_fund_<SYM>_<TS>.json` (`{"points":[{symbol,field,span,value,unit,consensus?,release_date}]}`) → `./venv/bin/python datalayer/pg/db.py load-fund <file>`

## Step 4: Write back the schedule + verify + receipt

Edit `fundamentals.<SYMBOL>` in `config/update_schedule.yaml`:
- `last` ← the latest `release_date` of this run
- `next.time.at` ← the **next earnings date** found (UTC); if it cannot be pinned down → fall back to a rough alarm ~3 months out (quarterly)
- `next.time.reason` ← one sentence (e.g. "Apple Q3-FY2026 earnings")

**Two-stage early wake-up (decided 2026-07-02)**: next earnings date **officially confirmed by IR** → set next directly at the release time;
only listed by aggregators / estimated from history ("pending IR confirmation") → set next **2-3 days before** the estimated date, with reason noting "early wake-up: confirm the earnings date first".
When woken that time: first search for the confirmed date → if not yet released, **only write back** the precise next, without refreshing data (the receipt records `date-confirm only`); if released, refresh as usual.

Verify: `./venv/bin/python datalayer/pg/db.py snapshot-fund <SYMBOL>`.

Finally output only the JSON receipt (for hermes):
```json
{"symbol":"AAPL","loaded":[{"field":"revenue","span":"3M","value":111184,"release_date":"2026-04-30"}],
 "skipped":[{"field":"...","why":"not yet released"}], "next":"2026-07-31T20:00:00Z"}
```

## Error tolerance
- A single field not found → skipped, does not block the others.
- PG connection failure → `{"error":"pg_connect_failed"}`; no silent success.
- Reporting basis uncertain (cumulative vs. single quarter) → better to mark `span` as the cumulative basis actually found than to pretend it is a single quarter.
