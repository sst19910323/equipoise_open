---
name: equipoise-usage-calibration
description: >
  Re-measure how much weekly Codex quota one debate burns under the current corpus version, and record it.
  Use when the owner says "calibrate usage / how many debates can this version run per week / update the usage
  estimate" — typically right after a major corpus or engine deploy. Deterministic script + one yaml entry;
  the duty loop (scripts/duty_status.py) reads the latest entry to pace launches.
---

# equipoise-usage-calibration

Working directory `/opt/equipoise`, python `./venv/bin/python`. Codex identities: codex1 and codex3 (main) / codex2 (postcheck).

## What it measures

For the current numbered accounts, planning weights, effective stop and trading-day pace, read the [weekly dispatch unit policy](../../orchestration/duty.md#weekly-dispatch-units).

`cost_pct` = (weekly `used_percent` at the end of a window − at its start) ÷ debates stored in that window,
read from the identity's own session logs (`rate_limits.primary`) and PG `debates.harness_truth`. The window
must contain **only debates of this identity** — no postcheck, no duty sessions, no chat — or those get
averaged in. Pick the span from the first `prepare` of a wave to the last store receipt.

## Steps

1. Find a clean window: one finished wave (the US open batch, or a JP/KR morning batch). Take `since` from the
   first `data/status/codex_run_<SYM>_*.json` of the wave minus ~15 min, `until` from the last store + ~5 min.
   `journalctl -u 'equipoise-codex-*' --since ...` or the dispatcher log shows the span.
2. Dry measure:
   ```bash
   ./venv/bin/python scripts/usage_calibrate.py --harness codex1 --since <ISO> --until <ISO> --label "<wave>"
   ```
   Check `window.segments` (1 unless the weekly reset fell inside), `used_from/to`, `debates`. A US wave should
   land near 0.5–0.7 %/debate; JP/KR waves run higher (more outcalls). If `debates` is 0 or the delta is
   absurd, the window is wrong — do not write.
3. Measure a second wave if available (US + non-US), then write the blended number, labelled with the version:
   ```bash
   ./venv/bin/python scripts/usage_calibrate.py --harness codex1 --since <ISO> --until <ISO> \
     --label "<wave>" --version "<e.g. v3.4 (C12)>" --note "<what was in the window>" --write
   ```
   `--version` defaults to `config/odds.yaml` schema + `DEPLOY_COMMIT`. The entry is appended to
   `config/usage_calibration.yaml`; old entries stay (they show how versions differ).
4. Confirm the duty loop picked it up: `./venv/bin/python scripts/duty_status.py --no-probe --json-only` →
   `usage.codex1.cost_source` names the new entry; report `debates_per_week_at_stop` to the owner in one line.

## Rules

- Never edit `config/usage_calibration.yaml` by hand except to fix a label; numbers come from the script.
- One entry per version per identity is enough; re-run only after a deploy that changes how much a debate talks.
- If the blended cost moves more than ~30% from the previous entry, say so explicitly — that is a cost regression
  worth the owner's attention, not just a new number.
