# Arithmetic hints in the task material (read by the script; roles do not read this file)

One tier is set at the opening (`run_step.py open --calc-check off|remind|always`; unset means `remind`). The script splices only the one sentence of that tier into
the task material of executors and debaters: the roles see no other tier, nor the notion of a "tier" at all. `off` splices nothing. `always` requires a tool that can run commands;
roles without that tool (the Claude Code version's debaters) get the `remind` sentence spliced in instead. One line per tier, format `tier name: the sentence`.

remind: For numbers you derive yourself, write out the arithmetic; unit changes (percentages vs decimals, basis points, multiples, quote direction) are where errors are most common — glance at the unit and the order of magnitude before writing it down.
always: Every number you derive yourself and will write into the product is computed with a local command from the existing inputs through to the final result with its unit (several related ones computed in one go), with the arithmetic written out; source numbers cited directly and existing tool results need not be recomputed; compute only from existing inputs, and do not use it to fetch new data.
