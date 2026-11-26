---
description: Refresh the fear indices (scraper first, WebSearch fallback for regions where the scraper failed) → PG sentiment. hermes scheduling entry point. Usage: /refresh-sentiment
---

Refresh the fear indices into the PG `sentiment` table. **Two stages: first run the deterministic scraper (cheap), then for whichever region the scraper failed you (Claude Code) fill in with a WebSearch fallback** — this is why it goes through Claude Code rather than a pure script: the scraper gets blocked by Cloudflare / pages get redesigned, and you need to self-heal.

Coverage: US = `fear_greed` (0-100) + `vix`; EZ = `vstoxx`; JP = `nikkei_vi`; KR = `vkospi`. Daily frequency.

**hermes headless invocation** (server /opt/equipoise):
```
cd /opt/equipoise && claude -p "/refresh-sentiment" \
  --model opus --permission-mode dontAsk \
  --allowedTools "WebSearch,WebFetch,Read,Write,Edit,Bash(./venv/bin/python datalayer/pg/*.py *),Bash(date *)" \
  --output-format json
```

## Step 1: Run the scraper first (deterministic, cheap)
```bash
cd /opt/equipoise && ./venv/bin/python datalayer/pg/sentiment_daily.py
```
Watch for it printing `upserted N sentiment rows: ...` — note **which rows successfully entered the database today** (US/fear_greed, US/vix, EZ/vstoxx, JP/nikkei_vi, KR/vkospi).

## Step 2: Find what the scraper missed, WebSearch fallback
The 5 rows that should all be present: `US/fear_greed`, `US/vix`, `EZ/vstoxx`, `JP/nikkei_vi`, `KR/vkospi`. Whichever is missing from Step 1 (that region's scraper blocked / timed out / returned empty → commonly ends up here) → you fill it with a **neutral WebSearch for the current value**:
- US Fear & Greed current value (0-100, CNN/feargreedmeter) + current US VIX
- EU VSTOXX (V2TX) current
- JP Nikkei VI (NK225VI) current
- KR VKOSPI (KOSPI volatility index) current

🔴 Honesty: fill in only what is found; leave what is not found empty (that row is simply missing today, do not invent); fill in **current values** only; weekly/monthly changes are computed by the read side from history. Date = today UTC (`date -u +%F`) or the trading day marked by the source.

Write the fallback findings to staging → load:
```bash
# data/lowfreq_staging/_staging_sent_<TS>.json : {"points":[{region,indicator,value,unit,date}]}
./venv/bin/python datalayer/pg/db.py load-sentiment <file>
```
unit: `0-100` for fear_greed, `index` for the rest.

## Step 3: Receipt
```json
{"scraped":["US/fear_greed","US/vix","EZ/vstoxx"],
 "websearch_filled":["JP/nikkei_vi","KR/vkospi"],
 "still_missing":[]}
```

## Notes
- If everything scraped successfully, Step 2 has nothing to do (zero WebSearch) — the cheapest case.
- Once a day is enough (after all three regions close, e.g. UTC ~18:00).
- A region completely unobtainable (scraper failed + nothing found) → mark it in `still_missing`; does not block the other regions, and no silent success.
