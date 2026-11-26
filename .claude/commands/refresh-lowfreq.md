---
description: Refresh low-frequency economic indicators (look up → write PG → write back the schedule). hermes scheduling entry point. Usage: /refresh-lowfreq US cpi_yoy,core_cpi_yoy
---

Refresh the low-frequency economic indicators named by `$ARGUMENTS`: WebSearch the latest released values → write PG `equipoise.economic` → write back last/next in `config/update_schedule.yaml`.

**This is the integration entry point for hermes (the scheduling AI)**, usually called headless:
```
cd /opt/equipoise && claude -p "/refresh-lowfreq US cpi_yoy,core_cpi_yoy" \
  --model sonnet --permission-mode dontAsk \
  --allowedTools "WebSearch,WebFetch,Read,Write,Edit,Bash(./venv/bin/python datalayer/pg/db.py *),Bash(date *)" \
  --output-format json
```
**Runs on the server at `/opt/equipoise`** (PG on localhost). python is always `./venv/bin/python` (this project's venv, with psycopg2).

## Step 0: Parse arguments

`$ARGUMENTS` = `<REGION> <ind1,ind2,...>`:
- REGION = the first token, uppercase, must be ∈ {`US`, `EZ`, `UK`, `JP`, `KR`} (note: the eurozone is written to the database as EZ, not EU; Korea is `KR`)
- INDICATORS = the second token, comma-separated
- Parse failure / empty → output `{"error":"usage: /refresh-lowfreq <REGION> <ind1,ind2,...>"}` and stop

Read `config/update_schedule.yaml` and take each indicator's existing entry (the `reason` field says what it is + `last`) as query context. An indicator not in the yaml → record skipped (`unknown indicator`); do not invent.

## Step 1: Verify each indicator (WebSearch, neutral)

For each indicator:

1. **Look up the latest released value**: a neutral query (e.g. "US CPI year over year latest release actual consensus"), getting:
   - `value` = **the number** as released officially / in the news (a YoY indicator is the YoY %, MoM is the MoM %, a rate is the rate %, a PMI is the index value — store the released raw value, do not convert it yourself)
   - `consensus` = market expectation (the consensus number before release); **no clean expectation found → leave null, do not invent**
   - `unit` = unit (`% YoY` / `%` / `Index` / `K` / `Billion JPY` / `JPY per USD` etc.)
   - `release_time` = the data point's **effective date / release date** (ISO `YYYY-MM-DD`)
2. **Look up the next release time**: the next release on the official release calendar (BLS/BEA/FOMC/Eurostat/ECB/ONS/BoE/MoF/BoJ/Japan MIC etc.), pinned to a specific UTC time; weekly-frequency items (treasury yields / exchange rates) are set to ~+7 days.

### 🔴 Iron rules of honesty (a violation = data pollution, worse than no data)
- **Unreleased data never enters the database**: if `release_time` > today (UTC) = not released yet, **skip this indicator** (record `not yet released`) and only write back next.
- **Not found / uncertain → skip**; do not invent values, do not pass an old value off as new.
- **Do not store derived quantities**: prior value / last year / YoY-MoM change / beat vs. expectation are all computed by the read side from history; only the current `value` + `consensus` are stored here.
- Searches strictly neutral, with no preset conclusion.
- Unsure of region (EU vs EZ): always write `EZ` to the database.

## Step 2: Write staging + load into PG

Assemble what was found (skipped items are not written) into a staging JSON:
```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
```
Write `data/lowfreq_staging/_staging_refresh_<REGION>_<TS>.json`:
```json
{"points":[
  {"region":"US","indicator":"cpi_yoy","value":4.2,"consensus":4.2,"unit":"% YoY","release_time":"2026-06-10"}
]}
```
Load (upsert, idempotent):
```bash
./venv/bin/python datalayer/pg/db.py load data/lowfreq_staging/_staging_refresh_<REGION>_<TS>.json
```

## Step 3: Write back the schedule yaml

For each **successfully refreshed** indicator, Edit the corresponding line in `config/update_schedule.yaml`:
- `last` ← this run's `release_time`
- `next.time.at` ← the next release UTC time found in Step 1
- `next.time.reason` ← one sentence (e.g. "US July CPI release")

### Two-stage early wake-up (decided 2026-07-02)
When writing back next, choose one of two by "how confirmed the next date is":
- Official calendar **confirmed** → `next.time.at` set directly at the release time.
- **Not confirmed / estimated** (estimate / pending official announcement) → `next.time.at` set **2-3 days before** the estimated date, with reason noting "early wake-up: confirm the release date first".
  When woken that time: first search for the confirmed date → if not yet released, **only write back** the precise next (the confirmed time), without refreshing data (the receipt records `date-confirm only`);
  if already released, refresh as usual.
Split "confirm the date" (a few days early, one cheap search) and "refresh data" (at the release time, punctual) into two wake-ups — in exchange for not running empty at the wrong time.

Skipped indicators: `last` untouched, but if next is found to be already past, you may move `next.time.at` on to the next release (and note pending release in reason).

Times are always **UTC**. Write only the **next** next (the one after is computed at the next refresh).

## Step 4: Verify + output the receipt

Check what entered the database:
```bash
./venv/bin/python datalayer/pg/db.py snapshot <REGION>
```
Finally **output only one JSON block** (for hermes to parse; `--output-format json` wraps it in `.result`):
```json
{
  "region": "US",
  "refreshed": [
    {"indicator":"cpi_yoy","value":4.2,"consensus":4.2,"release_time":"2026-06-10","next":"2026-07-14T12:30:00Z"}
  ],
  "skipped": [
    {"indicator":"retail_sales_mom","why":"not yet released"}
  ]
}
```

## Error tolerance
- A single indicator not found → goes into skipped, does not block the other indicators.
- PG connection failure → output `{"error":"pg_connect_failed"}`; no silent success.
- yaml write-back failure → the data already in the database is authoritative; mark `schedule_write_failed` in the receipt so hermes knows the schedule was not updated.
