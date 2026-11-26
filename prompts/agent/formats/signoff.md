# Signoff output format (shared by Zealot and Reaper; TASK: signoff)

After the redraft, you take a position on this version of the final report. The inputs are only: your last-round final, your objections and their handling record, the full redraft. The signoff is not a second debate; no outcall filings.

When a LENSES block is present, first read the rules file it names; its field overrides apply to this format.

```json
{
  "reservation": "reservation, one or two sentences; with no objection, leave empty or confirm briefly",
  "dissent_refs": ["numbers of your own unsettled claims, as pointers to your strongest argument (may be empty)"],
  "scenario_revision": {"predecessor": "<the record id of the scenario you revise: your last-round final id (e.g. R3.Z), or your latest objection id that carried a revision (e.g. OBJ1.Z); the ids are on the records in your view>", "ops": [{"op": "P_T_set", "node": "C", "value": "<price>"}], "why": "one sentence naming the claims or evidence"}
}
```

- Write `reservation` first; `scenario_revision` is optional (ops in the grammar of `formats/debate.md`, complete and probability-conserving, applied to the scenario named by `predecessor`).
- Your endorsed scenario may differ from your last-round scenario; the score follows it: `endorsed_composite_score` is derived by the script from the endorsed scenario (you may omit it; an authored value is kept as `authored_score`). A signoff revision changes your endorsement only, never the ruling's distribution.
- The product is only this JSON object, with no explanatory text, markdown markup, or fences (how to deliver follows the `OUTPUT` line at the end of the prompt).
