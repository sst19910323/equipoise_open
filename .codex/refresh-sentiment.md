# GPT/Codex Refresh: Fear Index Fallback

You are the GPT/Codex worker for one Equipoise sentiment refresh task.

## Source Of Truth (the sole authority for the business specification)

Read `/opt/equipoise/.claude/commands/refresh-sentiment.md` and follow it
exactly (two stages: rerun the scraper first, then for whichever region is still down use web search as a fallback for the current value → staging →
load into PG → JSON receipt). Do not invent a parallel process. Adopt the iron rules of honesty in full:
fill in only what is found, leave what is not found empty and do not invent; fill in current values only.

## Codex shell differences (only these override the authority file)

1. **No-argument task**: no Runtime Input is needed; the 5 rows that should all be present are fixed as
   `US/fear_greed` `US/vix` `EZ/vstoxx` `JP/nikkei_vi` `KR/vkospi`.
2. **Search**: use your own web search capability (the caller launched you with `--search`).
3. **The scraper and loading may be blocked by the sandbox**:
   - Step 1's scraper rerun `./venv/bin/python datalayer/pg/sentiment_daily.py`
     needs internet + PG; if the sandbox blocks it, skip this step (the caller has usually already run it once outside the sandbox;
     if the Runtime context gives `FAILED_REGIONS`, trust it directly and fall back only for those regions).
   - Write the fallback values to staging `data/lowfreq_staging/_staging_sent_<TS>.json` as usual,
     then try `./venv/bin/python datalayer/pg/db.py load-sentiment <file>`.
     Connection failure → put the command verbatim into the receipt as `"pending_loads": [...]`;
     the caller (hermes) executes it for you outside the sandbox; the staging file must already be written.
4. **The final chat reply outputs only the JSON receipt** (the format of the authority file's Step 3 +
   the `pending_loads` field), no prose.

## Runtime Input

(the caller may append something like `FAILED_REGIONS: EU,JP`; if not appended, decide yourself per the authority file)
