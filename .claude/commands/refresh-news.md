---
description: The news leg for EU/JP/KR — a region's market news or one non-US single name's own news, searched with your own web search. Usage: /refresh-news EU|JP|KR|<SYMBOL>
---

Search news for `$ARGUMENTS` → write staging → load into PG `news`. The argument is either a region (`EU` / `JP` / `KR` → that region's market news) or one non-US single name's symbol (e.g. `SIE`, `TM`, `SSNLF` → that company's own news; US names do not come here, they have Finnhub).

**Positioning**: since v3.3 (2026-10-26) this is **the** news leg for EU / JP / KR — you (the engine: CC or Codex) search the web yourself; the Linkup leg is retired.
Data preparation prints `NEWS: …` naming the regions / symbols whose PG news is older than 12 hours; run this for each, then prepare again. The business basis is unified with the
Finn leg: **≤20 items / last 3 days** for a region, about 10 for a single name. This document is the business specification **shared by CC and GPT Codex** (codex enters via the thin
shell `.codex/refresh-news.md`; each uses its own search interface).

Runs on the server at `/opt/equipoise`; argument parse failure → output `{"error":"usage: /refresh-news EU|JP|KR|<SYMBOL>"}` and stop.

## Step 1: Search (neutral, event-oriented)

Topics (same source as the Linkup leg's REGION_QUERIES):
- **EU**: ECB rate policy / eurozone inflation / DAX·CAC·AEX index moves / defense spending / semiconductor sector headlines
- **JP**: BOJ policy / yen exchange rate and intervention / Nikkei 225 moves / semiconductors / defense / US-Japan trade headlines
- **KR**: Bank of Korea (BoK) rates / won exchange rate / KOSPI moves / Samsung·SK hynix·HBM memory / semiconductor exports / defense export headlines
- **A single name** (symbol argument): that company's own news of the last 3 days — earnings and guidance, orders and contracts, products, regulation and litigation, management, analyst moves, and the one or two sector headlines that move it. Search in the company's home language **and** in English (the company name comes from `config/symbols.yaml` / the PG `company` row; ADR / OTC symbols are the parent company). Do not fill the slots with the region's macro news — that is the region leg's job.
  **About ten items is enough** (the loader caps a single name at 10): the purpose is a sketch — what roughly happened to this name and what is worth checking — so the debaters know where to look. Tracing cause and effect is the debate's job (Scout searches and data calls); do not dig to the bottom here.

Rules:
- **Event-oriented**; do not search quote pages; exclude quote aggregators (finance.yahoo.com / stockanalysis / seekingalpha /
  stocktwits / tipranks / marketbeat / investing.com / tradingview / barchart) — the loader filters
  again, but do not waste item slots.
- Prefer **local-language + authoritative English** sources (Reuters / Bloomberg / FT / Nikkei / Handelsblatt / NHK etc.);
  Simplified Chinese sources are usable, but mind stance and second-hand retelling bias.
- News within the last **3 days**; every item must have a **real url**; `headline` copied verbatim, not rewritten (original language kept);
  `summary` ≤100 characters (a Chinese summary is fine); include `published` (YYYY-MM-DD) when a publication date is available.
- 🔴 Honesty: however many are found is how many there are; fewer than 20 (region) / 10 (single name) → **do not pad**; never fabricate urls/headlines/dates.

## Step 2: Write staging + load into PG

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
```
Write `data/lowfreq_staging/_staging_news_<REGION|SYMBOL>_<TS>.json`:
```json
{"items":[
  {"headline":"日銀、追加利上げを示唆...","summary":"BOJ 暗示...","source":"nikkei.com",
   "url":"https://www.nikkei.com/...","published":"2026-07-08"}
]}
```
Load (**normalization / dedup / excluded sites / the 20-item cap are all in the loader code**; you only keep the staging clean):
```bash
./venv/bin/python fetch_eu_jp_news.py --load data/lowfreq_staging/_staging_news_<REGION>_<TS>.json --region <EU|JP|KR>
./venv/bin/python fetch_eu_jp_news.py --load data/lowfreq_staging/_staging_news_<SYMBOL>_<TS>.json --symbol <SYMBOL>     # single name
```

## Step 3: Receipt

Output only one JSON block:
```json
{"region":"JP","loaded":17,"sources_top":["nikkei.com","reuters.com"],"notes":""}
```
or, for a single name:
```json
{"symbol":"TM","loaded":12,"sources_top":["nikkei.com","toyota-global.com"],"notes":""}
```

## Error tolerance

- Search down / nothing found → `loaded: 0` + notes stating it honestly; does not block the debate (non-US news is warning level, and inside /run
  the role agents can still file request_searches as a fallback).
- PG connection failure → `{"error":"pg_connect_failed"}`; no silent success.
