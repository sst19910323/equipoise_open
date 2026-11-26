---
name: equipoise-add-symbol
description: >
  The master checklist for initializing a new symbol. When the user says "add a new stock / onboard <SYM> / new symbol", execute per this document:
  two places in symbols.yaml → fundamentals refresh schedule → first fill and smoke. A one-off action done in one go, not relying on fallbacks at run time.
  US and Europe/Japan/Korea are both in this document; non-US has one extra section on how to write contracts and benchmarks.
---

# equipoise-add-symbol — initializing a new symbol

**Why this skill exists**: when MCD was onboarded on 2026-09-24 only `symbols.yaml` was changed; it was missing from the fundamentals refresh schedule, so its earnings data never updated.
A new symbol's belongings are scattered across several places; miss one and it limps on one leg.

## Checklist (execute in order; every step is required)

### 1) config/symbols.yaml — two places, both required

- Add one line to the `analysis_targets.stocks` (or `.etfs`) list, with a one-line comment (company name, sector, why it was added). Non-US symbols also go into `stocks`,
  placed in the section of the same region.
- The `symbol_contracts.<SYM>` block: `symbol / sec_type / exchange / currency / primary_exchange / description /
  benchmark / sector`. **For US names, sector must be an ETF already in the pool** (SMH / XLF / XLP / XLV / XLE / XLU etc.); when the industry classification
  does not match any pool ETF, pick the nearest and explain in a comment (precedent: MCD is actually a consumer-discretionary constituent, the pool has no matching ETF, so it hangs on XLP as defensive consumer).
  Codes with a dot (BRK.B type) replace the dot with a space when quoting IBKR; the fetch layer already handles it, write as usual.

### 2) Fundamentals refresh schedule (a server-side job)

On Contabo the Hermes due-job reads **`/opt/equipoise/config/update_schedule.yaml`**. All regions share its `fundamentals:` section. Preserve the live table's existing dates when adding entries. Add one entry in the form of the neighboring lines:
```yaml
<SYM>: { last: null, next: { time: { at: <approximate date of the next earnings report, UTC>, reason: "<SYM> Q<N>-<year> earnings (around month X, to be verified; first fill <date>)" } } }
```
If the earnings date is uncertain, write the approximate date plus "to be verified"; the fundamentals refresh task verifies it and writes back when it runs.

### 3) PG — nothing to do

All tables (technical, news, fundamentals, debates, postcheck, evidence) are keyed by symbol; a new symbol needs **zero DDL**. Whoever wants to create a table for a new symbol is making a mistake.

### 4) First fill + smoke (a server-side job)

```bash
cd /opt/equipoise && EQUIPOISE_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py <SYM>
```
Data preparation hydrates the full set of technical, news, fundamentals and evidence preparation pieces; watch the receipt to confirm the IBKR contract quote succeeded (no quote = `symbol_contracts` written wrong,
go back to step 1). Onboarding counts as complete only when the smoke is green; the first debate goes through `equipoise-dispatch`.

## The extra items for non-US symbols (Europe / Japan / Korea)

Write in the form of the existing entries of the same region (EU RHM, JP NTDOY, KR SSNLF can serve as samples):

- **Key names use the English code** (the US ADR ticker or the romanization). The local exchange's numeric code goes into `ibkr_ticker`, used only to quote IBKR;
  filings, reports and PG always use the English code; the numeric code is never exposed.
- **`region: EU | JP | KR`** is required (missing it is treated as US); it decides which region's general, macro and sentiment data to pair with, and also decides the frontend grouping.
- **`benchmark` is the region's first lens**: Japan `N225`, Korea `KODEX200`, Europe by listing venue `DAX` / `IAEX` / `EXSA` (the regional generals' second lenses — `TOPX`, `KOSDQ150`, `IAEX` — are not single-name benchmarks).
  Do not hang it on a US broad-market index.
- **`runnable: false`**: follow the same region's entries (the retired scheduler's switch; named runs are unaffected by it).
- **`currency` and `primary_exchange`** are those of the local listing (JPY with TSEJ, KRW with KSE, EUR with IBIS, etc.); the quote-currency discipline is carried to the debaters by
  the orchestration material.
- **First confirm the account has market data for this market**: the subscription list is in `skills/equipoise-manage-ibkr/references/operations.md`. A market without a subscription
  only gets delayed data or no quote at all, which shows up at first fill.
- **Indices or symbols that borrow another analysis frame** (e.g. an index run under the broad-market ETF frame): use the contract field `context_type: etf | stock`,
  **do not put them into `analysis_targets.etfs`** — that list doubles as the US grouping basis, and entering it hangs the symbol in the US pile on the frontend (the 2026-09-30 DAX case).
- News goes through the regional news leg and is warning-level: empty news at first fill is not a failure; see "Non-US news leg" in `equipoise-dispatch`.
- Adding a new **regional general** (`GENERAL_<region>`) is outside this document's scope: it also needs macro, sentiment, event calendar and context manual; discuss with the owner first.

## Boundaries

- Do not add symbols while a debate is in progress (data preparation and the running session would compete for IBKR).
- `config/debate_schedule.yaml` is the retired API version's schedule; the current engine neither reads nor writes it; no need to touch it when onboarding.
- Removing a symbol = steps 1 and 2 in reverse (delete the list line, delete the schedule entry); historical data (PG and `data/`) is left alone.
