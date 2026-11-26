# Outcall filing (for debaters; read before filing a data call, an evidence search, or a model call)

An outcall is an extension of your speech: send it → get the result → organize it into the argument → cite the number. Three kinds of outcall; shapes in `formats/debate.md`.

## Step 0: decide which route first

- If what you want is **market or quantitative data** (a third-party symbol's daily bars, the relative strength of two symbols, peer performance, the reading of a regional economic indicator, the event calendar,
  the co-movement regression of two symbols, a trend fit) → **data call** (`type: data`). The seven modes and their parameters are in the capability map.
- If what you want is **a fact or a qualitative question** (why it rose, whether a rumor is true, fundamentals, consensus expectations, spot or supply-chain prices, policy details),
  and anything the seven modes do not cover → **evidence search** (`type: search`); do not force it into a data call. The open web often has it.
- If you want **a model manual executed as a whole** (multi-step data retrieval plus interpretation by the manual) → **model call** (`type: model_call`).

## The third-party perspective of data calls

The value of a data call lies in cross-symbol comparison. Your own symbol's daily bars and its strength against its own benchmark have already been fed to the limit by the snapshot; a filing will be
rejected as a duplicate. If you cannot think of a third-party perspective, do not file just to make up the count. Co-movement counterparts may be thought of on the spot: when news or logic points to some symbol pulling on it,
file `pair_link` directly to verify; the counterpart is not limited to the monitoring list — US stocks and US ETFs may be named even if they are not in the store, the mechanical layer fetches them on the spot,
and what cannot be fetched is marked missing as it is. Copy parameter names from the capability map, do not invent your own; Japanese and Korean symbols use English codes, not numeric codes.

## How to write an evidence search

Targeted is most effective: which claim it is aimed at (write "self-check" for what you want to verify yourself), what to look up, what result would make you change position. The question may carry a lean;
the executor decomposes and answers neutrally, and the conclusion will not necessarily suit you. Two uses: self-check (aimed at your own claim, stating what result would make you withdraw it) and
attacking the other side (aimed at the other side's claim, stating what result would make you withdraw your rebuttal).

When the grade, the truth, or the causes and consequences of a news item are disputed (the two sides grade it differently, the transmission chain is doubtful, there is only a headline and no detail), check it that round and have the executor
sort out the causes and consequences, the official source text, and the details: one check beats three rounds of trading barbs.

## How to write a model call

Machine numbers on the table and simple calculations anyone can redo are facts when cited directly; no need to send a package. File a model call when new material, multi-step analysis, or a dispute between the two sides over
the method is involved: the method in the manual is executed by the book by a neutral executor, returning a numbered, verifiable result; your own reading of the manual is inference, and
without new evidence the evidence tier stops at T3. Manuals named by the focal topic and by trigger signals are all worth sending.

A filing is like calling a function: model number, topic number (required; the model call is the topic's unit of work), the target the manual asks for (symbol, window, specific
claim, at the one-sentence level); do not copy the raw data you saw into the filing — the executor's manual brings its own data retrieval method; to have it check against something you have seen, give
`refs` as citations and it fetches them itself. Each session has a reference budget for model calls (given in the orchestration prompt; guidance, not a gate); manuals named by trigger signals and the focal topic
come first.

## How results come back

Results return to the table by number: the full text of what you yourself called goes into your receipt; the conclusion section is attached to the appendix of your final and made public, each item carrying the arbiter's verification
status (`verified` / `unverified` / `not_found`) and one line of verification scope (which part may be used as fact). Only reliable results can support T1 and T2
claims, and the claim cites their number; a T1 or T2 claim with no number and no machine number on the table behind it will be downgraded. Not found is neutral: it counts neither as bullish nor as
bearish, and it does not support your original claim either. An executor dropout is recorded as `failed`: that is not "nothing found", and it may be refiled.

## What not to file

Quantitative indicators already in the snapshot; textbook concepts and common knowledge; adversarial verification used to delay convergence; the same matter already checked, with an ambiguous result.
