# Mentality table (read before writing the decision reason; kept in sync with MENTALITY_V34 in `scripts/validate_debate.py` — change one, change the other)

Mentality is "the play chosen under the current price, expectations, and risk". The reader is an ideal, unburdened decision-maker at the neutral reference `q_ref` = half the standard unit (no cost basis, no unrealized gain or loss); every comparison below is made from there. The mentality is **one representative decision reason out of seven**; the strategy comparison on the scenario chooses the operation (principles sections 3-5), and `mentality_basis` — the situational fact, citing the atom or event that carries it — is what the reviewer checks against the operation.

## The seven decision reasons (enum value → what the scenario must show)

| Reason | Situational premise (one sentence in `mentality_basis`) | What the scenario must show |
|---|---|---|
| `with_trend` | The basis for continuation is still there, and participating under current conditions still has an edge | The continuation node carries the weight on a stated base rate; the strategy comparison favors acting (now or on a condition) |
| `against_trend` | There is an articulable gap between current pricing and a grounded judgment; both the loss from participating against the trend and the cost of waiting can be stated | The reversion or repair node carries the weight on stated evidence (the three damage questions, pricing gap); the comparison favors acting |
| `realizing` | An accepted price judgment supports reducing participation — not the reader's unrealized gain | The upside remaining in the nodes is small relative to the downside, so an underweight strategy (now or on a condition) wins the comparison at `q_ref` |
| `thesis_exit` | The basis for holding has suffered substantive damage; the reason lands on a specific change | The repair node's probability has been moved out by a verified damage finding; the comparison favors liquidate (or a staged exit) |
| `exposure_adjustment` | Risk, correlated exposure, or a scheduled event changed how much participation is worth, without the thesis having failed | The event or risk is inside the nodes (both sides, executable stop) and the comparison at `q_ref` favors an underweight (or an overweight) strategy — the same rule as every other reason, no separate discipline |
| `continuing` | The remaining reasons support continued participation, and a buy or sell adjustment now has no better basis | Neutral wins the comparison at `q_ref` — it wins only when the best action's certainty equivalent is ≤ 0; a positive best action within the tie threshold still wins over neutral — and no dated catalyst is pending |
| `waiting` | Some unresolved change may alter the judgment: an event with a nameable name and date, or a change taking shape (signal and time budget) | An unresolved dated event or a budgeted signal makes neutral or a conditional strategy win at `q_ref`; `mentality_basis` answers what is awaited, how many trading days, and what happens if it does not come (an order level or a concession); for a calendar event, what to do in each of the two states after it lands |

Distinctions to keep clear:
- `waiting` is legitimate only for an unresolved change; a process advancing on its known schedule, or a symbol running inside its own normal range, carries no confirmation obligation — it is priced, not waited for.
- `exposure_adjustment` before a scheduled event is not a free exit: the event is folded into the nodes, the stop is priced at the executable post-gap price, and reducing exposure must win the comparison like any other strategy. "Uncertainty is high" by itself is not a premise.
- `realizing` and `thesis_exit` both sell; they differ in the node they point at — the upside being used up versus the repair node having been emptied by damage.
- A reason that contradicts the chosen operation (a `with_trend` basis with a trim, a `thesis_exit` basis with an entry) is named by the arbiter; the fix is the scenario or the operation, not the label.
