# Multi-lens protocol (sessions whose material carries a LENSES block; read before the base format)

At the proposed and final rulings, use the base final format's `protection_by_symbol` for every investable lens. Use its frozen ticker as key. Omit observation-only indices; a mixed index/ETF session can carry protection on the ETF lens alone.

A multi-lens session (today GENERAL, GENERAL_EU, GENERAL_JP and GENERAL_KR) prices every lens on its own ruler inside one debate. This file extends the base formats — `formats/debate.md`, `formats/final.md`, `formats/objection.md`, `formats/signoff.md` — and overrides only the fields it names; everything else (the scenario shape, the op grammar, claims and their freezing, `applies`, the revision chain, the ruling sheet, `mixture.w`, voids) follows the base format unchanged. Where a base example and this file differ, this file wins.

## 1. One debate, one ruler per lens

- The LENSES block names the session lenses in their frozen order, each lens's reference price and `T_days`; the context manual gives each lens's meaning and unit.
- Each lens is priced on its own: its own scenario on its own price, its own execution contracts, its own `S_ref`, and the script derives its own operation from them. The lenses react to different drivers, so they need not agree — adding one lens while trimming another is a legitimate conclusion, not an inconsistency to smooth away; say which driver splits them.
- Shared across lenses: the claims, the evidence, the arbiter's ruling sheet and the one weight `w`. Only the ops are lens-tagged.
- The first lens is only the anchor for the compatibility fields; it carries no extra weight in the argument.

## 2. Debate rounds (`TASK: init | debate`; extends `formats/debate.md`)

Replace the entire `scenario` field with `scenario_by_lens`: every session lens present, each value exactly the base `scenario` shape on that lens's own price (`P0` copied from that lens's reference price in the LENSES block; `P_T`, `events` and `execution` levels in that lens's unit). The opening round still writes `scenario_updates: []`.

```json
"scenario_by_lens": {
  "<lens 1>": {"schema": "3.4", "T_days": 15, "P0": "<lens 1 reference price>", "nodes": [...], "event": {...}, "execution": {...}},
  "<lens 2>": {"schema": "3.4", "T_days": 15, "P0": "<lens 2 reference price>", "nodes": [...], "event": {...}, "execution": {...}}
}
```

- Node ids belong to their lens (`A` in one lens and `A` in another are unrelated).
- Every op — in both effect branches (`if_true` and `if_false`) and in every `scenario_updates` entry — carries `lens`, the lens whose scenario it acts on; its node ids resolve in that lens's scenario: `{"op": "p_move", "lens": "<lens>", "from": "A", "to": "C", "amount": "<0..p(A) in that lens>"}`. A missing or unknown `lens` is rejected.
- An effect may carry ops on several lenses and none on another. An update that `applies` an effect carries that effect's ops on every lens the registered effect has ops on, each equal to the registered ops on that lens (a lens left out is rejected); a lens the effect has no ops on needs none. The base `applies: null` rule holds with lens-tagged ops, and in a multi-lens session the comparison is per lens: a null update that reproduces a registered effect's ops on any lens is refused.
- Each update is replayed onto its lens's previous scenario and must reproduce that lens's entry in `scenario_by_lens`.
- `short_term_path.target` and `invalid`: a multi-level string joined by `=` and separated by `;` (`<lens 1>=<level>; <lens 2>=<level>`); for 震荡 each lens writes a range (`<lens 1>=<lower>-<upper>; …`).
- `operation_target`: one level on the first lens (the compatibility anchor); each lens's own levels live in that lens's `execution`.
- `composite_score`: the script writes one score per lens in `composite_by_lens` and their default-weighted sum in `composite_score`.
- `scenario` in a multi-lens session is rejected.

## 3. Objection and signoff (`TASK: objection | signoff`; extends `formats/objection.md`, `formats/signoff.md`)

- Write the task's base format; no full scenario of any kind.
- Every op in `scenario_revision.ops` carries `lens` (an untagged op is refused): `{"op": "P_T_set", "lens": "<lens>", "node": "C", "value": "<price>"}`.
- `predecessor` is shared across lenses: your latest objection id that carried a revision, even when that revision changed a different lens (none yet: your last-round final id).

## 4. The ruling (arbiter; extends `formats/final.md`)

- The same ruling sheet and the same `w` apply to every lens; the script replays each lens's Z′ and R′ and derives each lens's operation from them. Lenses landing on different operations is a result, not something to reconcile.
- `short_term_path.target` and `invalid` use the multi-level string of section 2.
- Lens importance λ is a separate, optional judgment, written beside `mixture`:

```json
"lens_weights": {"<lens 1>": "<λ>", "<lens 2>": "<λ>"},
"lens_weights_reason": "<required when any lens moves more than 0.1 from its default: name the claim or evidence (by id) that makes that lens lead this period, and why>"
```

  Omit both to keep the defaults (the config weights shown on the tally). When written: every session lens and no other key, each a finite number ≥ 0, summing to one (within 0.01; normalized and recorded); a lens whose normalized λ moves more than 0.1 from its default (exactly 0.1 is inside) needs `lens_weights_reason` naming at least one claim id or returned evidence / model-call id of this session. The rules apply at every stage (first ruling, draft, redraft). λ weights only the summary composite score; it does not reorder the report (the config lens order is the stable display order), never changes any lens's operation, level or size, and is not a postcheck input.
- `voided`: an effect void may name the lens on which the effect fails (`"lens": "<lens>"`). An effect void without `lens` applies on every lens the effect touches (the lenses on which that side's registered effect has ops; every lens when it has ops on none) and must fail on each of them — on a lens where the effect executes it is refused ("void rejected: effect is executable"). A candidate void without `lens` applies on every lens.
- The script runs the five-strategy comparison on every lens separately (that lens's two replayed scenarios, its candidates, its reference price and cost). You do not write `by_lens` or `lens_weights_default`.
- Injected by the assembler: `by_lens` (per lens: `scenario`, `operation_type`, `operation_target` as a number, `operation_size`, `operation_execution`, `composite_score`, `s_ref`, `s_ref_z`, `s_ref_r`; the candidates and strategies sit under `by_lens[L].scenario.derived` — the structured truth), `lens_weights` (the λ used), `lens_weights_default` and `lens_weights_reason`. The top-level `scenario` and `operation_type` / `operation_size` / `operation_execution` are the first lens's (compatibility only), `operation_target` is the display string `"<lens 1>=<level>; <lens 2>=<level>"`, `composite_score` = round(Σ λ · each lens's composite), the top-level `s_ref`, `s_ref_z`, `s_ref_r` are default-weighted summaries, and `reference_prices` carries one price per lens.
