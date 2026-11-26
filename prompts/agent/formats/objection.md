# Objection-round output format (shared by Zealot and Reaper; TASK: objection)

Free objection after the draft ruling is published. The first round addresses the draft report; the second round may also rebut the other side's objections. Work mainly from existing material, checking against citations as allowed, without reopening outcalls by default (what truly needs new evidence to resolve goes in `needs_new_evidence`; the arbiter decides on exceptional rework or leaves it unresolved).

When a LENSES block is present, first read the rules file it names; its field overrides apply to this format.

```json
{
  "objections": [
    {
      "target": "which field or ruling of the draft report (e.g. claims_review[2] / operation_target / short_term_path.direction)",
      "kind": "factual_error | misreading | omission | double_counting | broken_inference | inconsistency",
      "statement": "specifically what is wrong, missing, double-counted, or broken",
      "impact": "what this would change (operation, level, score, or mentality)",
      "refs": ["cited claim numbers, evidence numbers, or ledger entry numbers"]
    }
  ],
  "replies": [
    {"to": "the index or target of the other side's objection", "statement": "rebuttal or supplement"}
  ],
  "needs_new_evidence": [],
  "scenario_revision": {"predecessor": "<the record id of the scenario you revise: your last-round final id (e.g. R3.Z), or your latest objection id that carried a revision (e.g. OBJ1.Z); the ids are on the records in your view>", "ops": [{"op": "p_move", "from": "A", "to": "B", "amount": "<amount>"}], "why": "one sentence naming the claims or evidence"}
}
```

- `kind` is one of six, corresponding to the six kinds of objection that can enter the redraft; "the score is too high, too low" is not an objection — do not submit it.
- With no objections, output `{"objections": [], "replies": [], "needs_new_evidence": []}`.
- `scenario_revision` (optional): ops in the grammar of `formats/debate.md`, applied atomically to the scenario named by `predecessor`, complete and probability-conserving, not limited to the atoms your objections name; revisions accumulate and your endorsed scenario is the last-round scenario plus the chain. It enters the arbiter's replay only if the redraft changes `claims_review` or the weight; otherwise it is your endorsement only.
- The product is only this JSON object, with no explanatory text, markdown markup, or fences (how to deliver follows the `OUTPUT` line at the end of the prompt).
