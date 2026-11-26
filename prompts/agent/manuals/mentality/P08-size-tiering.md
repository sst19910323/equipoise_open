# P08 · Size and quality tiering

**When to use**: mandatory at the opening (declare the tier in a sentence or two); raised to a topic when the tier conclusion is challenged by the other side; when a quality name falls.

**What the tier is**: an input to the scenario, never an action permission. Tier labels confer no action permissions. Observed liquidity, verified business damage and validated reference classes may inform costs and the return distribution; state uncertainty where evidence is missing; neither size nor an unfalsified thesis fixes repair probabilities or action — the action follows from the strategy comparison on the scenario.

## Steps

1. Initial size judgment (self-executed): market cap and trading activity. Reference bands (divided by "whether market-wide money is watching it", in US dollars; non-US converted into the same bands):
   mega about 200 billion dollars and above, large-to-mid about 10 billion to 200 billion, small about 10 billion and below.
2. One sentence on each of the three quality dimensions (self-executed, internal knowledge): business-model resilience (moat, cyclical resistance), financial quality (stability of earnings and cash flow),
   narrative verifiability (clarity of strategy, trend of industry position). Each sentence must carry a "because of what, therefore" basis; empty labels are void.
   When financial quality is challenged, or "expensive or not" becomes a dispute, check in the directions below according to the dispute (not a financial audit required every session):
   - Which operating factors or one-off factors the changes in revenue and profit come from, and which have a durable basis.
   - Why profit and operating cash flow differ; how necessary investment and debt service affect available cash; how changes in share count affect per-share equity. Investment is not automatically
     bad — look at use and return; dilution already counted in earnings per share is not discounted again; state which method of cash-flow calculation is used.
   - Which judgments about growth, margins, or cash realization the current price depends on, and changing which assumption would significantly change the conclusion (how far exactly is left to P01's
     section "when there is no discrete event" to measure).
   Dynamic financial figures are cited from the material on the table; internal knowledge is used to explain, not to pose as the latest actual values.
3. One sentence on heat: whether money has been watching it recently (trading activity, news density, institutional attention). An illiquid name's decline phase can be extremely long; say so as a wider, slower downside path, not as a rule.
4. When the tier is challenged, file for verification: institutional ownership share (reference about seventy percent or above with broad analyst coverage = the money-side basis for large-name treatment);
   the record of consecutive profitability and the history of cash flow turning negative, landing "financially stable" on checkable facts.

## What the tier feeds into the scenario

| Finding | Where it goes |
|---|---|
| Small, speculative or illiquid name | Execution inputs: a higher cost (computed from its volatility and traded value), wider stop distance at the executable price, slower fills; the downside tail written as its own node or sub-branch with a stated probability — not as a rule that forbids an entry |
| Large name, high quality, no core damage found (see the three questions) | The repair node may carry a base rate from a validated reference class when one exists; where none exists, say the probability is a judgment and name what would move it; the unchecked tail (credit, liquidity, valuation) is a node with a stated probability, neither a veto nor an exemption |
| High market cap but low quality (narrative-overdrawn) | Treated as a small name for execution inputs; size alone proves nothing about the repair node |
| Core damage found (any of the three questions yes) | Probability moves out of the repair node into the damage node; what remains is the scenario's business, not a "do not catch it" rule |

## The three real-damage questions (large names; self-executed, internal knowledge plus news grading)

1. Is the strategy or core business broken — a strategy-level error, the core product losing competitiveness?
2. Has the technology or ecosystem been replaced — is what the replacer takes away its demand itself?
3. Has the moat or demand structure been substantively damaged — structural, not a single quarter's earnings fluctuation?

Not finding does not equal proving absence: state which questions were checked through and which merely turned up no problem, what a misjudgment would lose, and what fact appearing would move the repair node. Large size, heavy institutional
ownership, and a thesis not yet falsified — none of them shows that this fall carries no new information; unknown is neither automatically damage nor automatically a false alarm, it is a probability with a stated basis. The same applies at a high: a quality name chased to an extension with upside exhaustion on the table has its upside node weighed accordingly; whether to realize follows the comparison at the neutral reference.

How to set the lines (the cancel condition as a damage event rather than a level, the disaster line, staged nets and staged targets) is only in the execution-lines manual (P06) section five; this manual gives only the inputs it needs: the tier, how far each of the three questions was checked, and the cost and tail inputs above.

## Executor output

The summary section writes: market cap, institutional ownership or the gap in it, one sentence on each of the three quality dimensions, the tier judgment, the verified facts on the disputed dimension, and which scenario nodes or execution inputs the findings touch. The tier conclusion changes costs, tails and base rates; it does not directly change the composite score or license an action. The conclusion addresses the challenged tier claim. What counts as fact: verified numbers such as market cap, institutional ownership, profitability and
cash-flow records. What counts as interpretation: the assessment of the three quality dimensions and the answers to the three questions; an assessment based only on internal knowledge may continue to be challenged.
The final report's `asset_tier` is filled accordingly: `quality_large`, `mid`, `small_spec` (high market cap with low quality also fills this tier), `na` (market-wide and ETF sessions).
