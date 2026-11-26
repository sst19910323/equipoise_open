# P09 · Peer comparison

**When to use**: a claim that a single name is strong or weak relative to its sector needs proof; someone treats a parent-layer or sector conclusion in the same direction as independent evidence; you need to tell a sector event
from a single-name event.

## Execution steps

1. First read the peer-comparison file computed in the data-preparation stage (path given by the orchestration); peer performance over the window is usually already there.
2. Fetch on demand only if that is not enough: `peer_compare` (peer line-up); for excess return against a benchmark use `relative_strength`, with the benchmark chosen from this symbol's configured
   benchmark or sector, not defaulting to the US vehicles; circular references are rejected, switch to a third-party view.
3. The snapshot of the symbol's own strength against its own benchmark is already there; this model's value lies in the peer line-up and in comparing across benchmarks and windows.

## Reading (reference values; high-volatility sectors scale them up themselves)

| Judgment | Reference (origin) | Reading |
|---|---|---|
| Significant excess return | Twenty-day excess return of about 3% or more (about two to three times the sector's daily volatility, outside the noise band) | The minimum proof for a single-name claim to hold |
| No excess return, the same risk | Excess return hugging zero (within about plus or minus 1.5%) | Moving with the sector — citing the sector conclusion does not constitute additional independent evidence, and no resonance leg is added |
| Sector event | Peers rising and falling together (peaks and troughs on the same day, same direction) | The single-name good- or bad-news reading is downweighted; the fuel is at the sector layer |
| Single-name event | Moving alone (peers quiet) | Look for its own cause; the sector narrative cannot shield it |

## Executor output

The summary paragraph writes: twenty-day excess return (relative to which benchmark), peers moving together or diverging (numbers), rule excess return holds, the same risk, sector event, or single-name
event. The verdict addresses the single-name relative claim. What counts as fact: the machine numbers of peer comparison and relative strength. What counts as interpretation: the characterization "sector event or
single-name event". "Stronger than peers" without comparison data is only an assertion; once challenged, a request must be sent out or the claim withdrawn.
