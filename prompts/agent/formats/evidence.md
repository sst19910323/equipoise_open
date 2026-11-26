# Evidence output format (produced by executors; the archive contract is shared by both engines)

## Scout item (evidence search)

```json
{
  "question": "the full research question of the original request (including the claim it targets)",
  "detailed": "[Conclusion] ...\n[Source text] ...\n[Source] ... (several triplets, covering both the supporting and the opposing side; source text keeps its original language and wording)",
  "simple": "one-sentence conclusion, derived from detailed and inheriting its uncertainty (only this sentence is visible on the table, so the key numbers must be in it)",
  "source": ["url", "..."],
  "verdict": "supports | refutes | mixed | not_found | unrelated"
}
```
Field order is generation order: the detailed record first, then the summary. `verdict` addresses the factual claim the original request doubted; it is the one-word title of the summary.
A dropout (API down, tool error, not executed) takes the failure shape: `{"status": "failed", "reason": "one sentence: where it failed"}`.

## Data-call item

Reuse the four fields above (`question` states the request's meaning) and add:
```json
{
  "kind": "data",
  "mode": "peer_compare",
  "request": {"mode": "peer_compare", "symbol": "NVDA", "window_days": 20},
  "payload_text": "the complete data text the model actually read, stored verbatim as one string (written by the script from the file, not transcribed by the model)",
  "source_file": "optional, for provenance only"
}
```
`source` uses a stable data-source code such as `data:evidence_quant:peer_compare:NVDA`. An evidence search may write `kind: "search"` explicitly.

## Runner item (model call)

```json
{
  "model_call_id": "call number, copied back verbatim",
  "model": "P02",
  "issue": "topic number, copied back verbatim",
  "target": "the target of this execution: symbol, claim, parameter summary",
  "steps": [
    {"kind": "data | search", "what": "what was run: mode and parameters, or what was looked up", "result": "summary of this step's result, numbers with specific values"}
  ],
  "finding": "the summary returned to the table, three to five sentences: conclusion first; cite the manual's criteria and reference values to say which side the measured value falls on; the core number of each step",
  "verdict": "supports | refutes | mixed | not_found | unrelated",
  "suggested_evidence_tier": "T1 | T2 | T3",
  "payload_text": "the actual returned text of each step"
}
```
Failure shape (manual cannot be located, cannot be executed per the manual, all routes down): `{"model_call_id": "...", "model": "P02", "status": "failed", "reason": "one sentence: what is missing, where it failed"}`; the orchestration records `failed` (dropout, may be refiled) and broadcasts it as it is.

## Fields archived by the mechanical layer (attached by scripts during routing and archiving; the model does not write them)

- Each evidence item: `verified_scope` (the arbiter's one-line verification: which part may be used as fact, which part is only inference or not found), `status` (the one-word title of that line: `verified | unverified | not_found`; a dropout is recorded by the script as `failed`), `round`, `requested_by`.
- Items produced by model calls additionally carry `model_call_id`, `model`, `issue`, `suggested_evidence_tier`; at assembly they are mapped onto the Scout four fields (`simple` ← `finding`, `detailed` ← the `steps` record, `payload_text` ← each step's source text, `source` ← each step's actual sources).
- In the report, `evidence` = the list of all evidence items of the session (embedded archive); `model_calls` = one ledger row per model call; `evidence_added` = a short index `[{"item", "as_of", "status": "answered | blocked", "requested_by", "source"}]`; `evidence_generated_at` = generation time (archive key); `search_rounds_used` = the number of rounds that actually triggered outcalls.

## Visibility

`simple` and `finding` are the on-table visible layer; none of the numbers and conclusions that belong there may be missing; `detailed` and `payload_text` are the archive layer, not returned to the table by default, fetched item by item only through the "detail" exception channel. Local paths handle only this round's transport; the database is the sole truth of history; on reproduction the body text is injected, and paths are not handed to the model in place of the text.
