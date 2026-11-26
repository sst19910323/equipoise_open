# Formal sector hearing

Only the roster frozen in the opening receives decisions. When an ETF is on that roster, price the fund using its own quote, exposures and concentration. A stock-specific catalyst transfers only to the extent its weight and other holdings support it. A benchmark or peer cited by another hearing receives no decision from that hearing.

The caller hosts one group job in a detached service. Its one new Codex child is the arbiter. The trusted parent prepares the full roster, opens the frozen member ledgers and names the exact `GROUP_DIRECTORY`. Read its `opening.json`, the ordinary `.codex/run.md` adapter and the shared run protocol. The group uses the ordinary ruler, evidence capabilities, closure, objection, redraft and signoff rules. Each member has its own pairs of scenarios, claims settlement, mixture, reasoning, risk and timing. There is no group score. The number of rounds follows the normal closure rules.

1. **One group discussion per round.** Read the frozen roster and materials together. For openings with `round_scope: agenda`, let the live question determine which members enter this exchange. For example, a seven-member hearing can discuss only three companies when they offer the relevant comparison. Either side may nominate a member or challenge the scope; before exchanging private filings, give both sides the same agenda and access to the group's previously sealed discussion. Select the union of material disputes, including relevant contrary evidence. A group-wide development can bring back a member left out earlier. Keep a ledger in debate while a material question can still change its pricing; every frozen member ultimately needs its own supported decision and signatures.

   Spawn one Zealot and one Reaper for the group at each debate stage. Each submits one JSON: `shared` explains the common issue once and names the affected instruments where relevant; `members` contains the paired filings for this round's agenda. Use member fields for price paths, local evidence, disagreements and consequences. Members outside the agenda contribute no filing, tally or closure note in that round. Their last recorded positions and unanswered claims stand. The opening roster is the publication roster, not an airtime quota. For a resumed opening without `round_scope: agenda`, use the full frozen roster on both sides of every round as before.
2. Intra-round evidence beats use ordinary member `route` and `close-beat` receipts. Route only actual need-more drafts until both sides' final filings are ready. If a side is ready early, hold its final until sealing. Return scoped evidence to the requesting role. Keep the opposing side's current final private. At the end of each round run:

   ```bash
   ./venv/bin/python scripts/group_run.py seal GROUP_DIRECTORY --round N --zealot Z_FILE --reaper R_FILE
   ```

   `--round N` here is the group exchange number. Each member's own round in `run_step.py status --symbol MEMBER --ts CUTOFF` counts only its appearances. Give both roles that member-local number for claim revisions, references, evidence and replay. For example, a company discussed in group R1 and R3 files local R2 in group R3. Route only the active members' sealed files from `GROUP_DIRECTORY/RN/` under their local numbers and ordinary beat numbers. On validation errors return the specific errors to the filing side. Repeating an identical seal is safe; on resume inspect each active member's status before routing. Complete routing, tally and close-round for this agenda before sealing the next exchange. Record the actual decision returned for each active member. A closure override keeps that member in debate; its peers can finish on their own accepted rounds. A skipped round contributes no evidence of agreement. Legacy openings keep their common round barrier and common closure decision.
3. At closure use the two ordinary role threads per stage for the whole group, submitting each member's own objections, dispositions, endorsed scenarios and signoff to its ledger. Continue through `finalize --stage signoff` on every member. A member terminal receipt here belongs to the group. Preserve all original English filings.
4. State the frozen roster and its size in each role handoff. Effort references describe a single symbol. Investigate shared facts once and continue while a material member dispute needs work. At the ruling, read each member's applicable shared reasoning together with its own analysis. Explain the transmission, falsifier and review consequence to the same depth as a single-symbol report. Retain member-specific price levels, horizon, direction and rationale.
5. For openings with `scoped_items_version: 1`, author a flat `scoped_items.json` in GROUP_DIRECTORY alongside the rulings and reconcile it against the final signed member reports before assembly. Each entry has a stable hearing-local ID, a concise English summary and an `applies_to` map. Map only affected frozen symbols to the places that explain the member-specific consequence. For example:
   ```json
   [{"id":"G1","summary":"Shared selling pressure tests closing support.","applies_to":{"HXSCL":["risk:0","price"],"SSNLF":["driver:1","price"]}}]
   ```
   Cover each signed risk, driver, price rationale and authored review event. Use zero-based indices in final list order for risk and driver, `price` for the member's short-term path and `review:E1` for that member's event ID. Link several items to one conclusion when appropriate. Include local factors with a single member. Keep IDs stable through redrafts. Reconcile expired or immaterial prior topics by judgment; counts are diagnostic only.
6. Run `./venv/bin/python scripts/group_run.py assemble GROUP_DIRECTORY`. The child returns the resulting group.json path. Only the parent calls the publication command. It reconstructs the report from the sealed turns, frozen quotes and rulers, signed member ledgers and evidence artifacts; the group archive, member decisions and group evidence are written together. The parent then translates short member fields using the cheap translator.

Operator invocation from `/opt/equipoise` follows the background-service example in `codex.md` with `codex_loop.py --group GROUP_KEY --harness ACCOUNT` and the suite's selected judgment model. `--dry-run` prints the single dispatch unit and members. Use `--resume-group YYYYMMDDTHHMMSSZ` with the same key to resume a specific opening or retry translation after publication. Use the existing group-demo reference for isolated display exercises.

For example, after dispatch selects codex1 for the US chip group:

```bash
systemd-run --collect --unit=equipoise-codex-group-us-chips \
  --property=WorkingDirectory=/opt/equipoise --setenv=EQUIPOISE_IBKR_PORT=4001 \
  --setenv=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /opt/equipoise/venv/bin/python /opt/equipoise/skills/equipoise-run-debates/scripts/codex_loop.py \
  --group GROUP_US_CHIPS --harness codex1
```
