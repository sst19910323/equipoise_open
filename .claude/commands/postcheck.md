---
description: Postcheck one mature debate report (rule wrong-footed / missed / hit and attribute, write PG postcheck). Usage: /postcheck NVDA [--dt 20] [--as_of <TS>]
---

Run a postcheck on `$ARGUMENTS`. Arguments = `<SYMBOL> [--dt N] [--as_of <TS>]`.

This file is only the Claude Code entry point. **The full specification is in `orchestration/postcheck-protocol.md`; Read it completely first, then follow it from Step 0
through Step 5**; the scoring basis, the three attribution questions, the judgment JSON shape and the receipt shape are all as it defines them.

Claude Code engine facts:
- You (the main session) are the reckoner, executing linearly in a single session, spawning no subagents; only you can WebSearch.
- The `reckoner` signature = the value of the "The exact model ID is …" sentence in your runtime environment, with the bracketed suffix (e.g. `[1m]`) removed.
- The headless invocation shape (this is how the batch driver `skills/equipoise-postcheck/scripts/cc_loop.py` launches it):
  ```
  cd /opt/equipoise && claude -p "/postcheck NVDA --dt 20" \
    --model opus --permission-mode dontAsk \
    --allowedTools "WebSearch,WebFetch,Read,Write,Bash(./venv/bin/python datalayer/pg/postcheck_store.py *)" \
    --output-format json
  ```
  When run via cc_loop, the parent layer also writes the signature back once more using the real model usage from the child process receipt.
