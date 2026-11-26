# Historical replay (the retired API version)

The API version (`run_one_debate.py` / `init_all_debates.py`, the deepseek code engine) **is retired and no longer maintained**, and is not the default for any "run it"
request. It retains one capability found nowhere else: **replay at a historical moment**. Use it only when the user names "the API version / code version / ds version" or wants
a historical replay. The old full instructions are archived in `docs/archive/skill-apex-run-debates-api-2026-10.md`.

## Command

```bash
cd /opt/equipoise
EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python run_one_debate.py <SYM> --as-of <UTC time, e.g. 2026-06-10T13:30:00Z>
```
`--as-of`: does not connect to IBKR, reads PG's historical snapshot as of that moment, forces evidence search off, and does not write back to the schedule table.
Exit codes: `0` ran · `10` no market data · `12` a report already exists at the same anchor (add `--force` to rerun) · `20` error.

## What to know before using it

- **It does not run the current rules.** The prompts are the old three-party council set in `prompts/api/`, unrelated to the current two-debaters-plus-arbiter corpus, pricing chain and release gate.
  Replay results cannot be compared directly with the current engine's reports.
- **It writes PG directly** (the same `debates` table, the same `(symbol, time)` primary key; it overwrites the row at the same anchor). Key names are converted to the current
  names on storing, but the content is still the old shape (no ledger, no evidence verification scope, no model-call ledger).
- It cannot replay searches: network evidence at a historical moment cannot be reproduced.
- The current engine currently has **no** path for replay at a historical moment (data preparation only takes the present). To re-examine an old session under the current rules, use the frozen-input check
  method: take a stored session's object-store material and resend it without re-fetching data.
