# Quota reset window (Codex engine, a selection-volume signal)

Read only when the Codex engine is about to report the worklist. It only affects how many to select this round; it does not replace the region, event and freshness judgment.

Every time before reporting an Equipoise worklist to the user, check two things first; this is a selection-volume signal and does not replace the region, event and freshness judgment:

1. Pull the public tracker's live feed (read-only, short timeout):
   ```bash
   curl -fsS --max-time 10 https://codex-reset.com/api/feed
   ```
   Focus on `fetched_at/stale`, the latest relevant tweet's `at/text/kind/tibo_lane/explicit_reset_claim`
   and the banked status. The tracker is an unofficial transcription; the original post text is the evidence, the classification fields are only aids.
2. Check the most recent Codex `token_count.info.rate_limits` of **the harness actually used this time**: codex1 corresponds to
   `/root/.codex/sessions`, codex2 to `/root/.codex-2/sessions`, codex3 to `/root/.codex-3/sessions`. Read at least
   `primary.used_percent/window_minutes/resets_at` and `plan_type`; use only fresh records, and report the record time along with it.
   Never pass off another identity's percentage as this identity's, and do not guess the runner identity from `CODEX_HOME`.

From this, identify a "reset burn window":

- **Open**: Tibo's original post explicitly announces that current usage will be automatically / hard reset and the stated landing time has not yet arrived, or it explicitly says a
  reset is in progress while the feed has no completion evidence yet. The window starts at the announcement time and ends when the feed shows an explicit original post that it is done / has been reset, or this identity's
  measured usage actually refills accordingly.
- **Not open**: only a `banked reset` is announced (it does not discard the current balance), a vague tease / joke, an ordinary natural weekly reset,
  an old announcement, or the feed is stale / unavailable with no original-post evidence. Do not treat a `kind` guess as a hard-reset fact.
- When the window is open, the more balance remains and the closer the landing, the more this period should be treated as a **high-consumption deadline**: still keep the region boundary, but
  this round's selection may be expanded from "most in need of update" to more symbols that are "already not fresh or have a secondary valid signal", feeding them in rolling fashion until the window ends,
  the worklist is done, the quota tops out, or a normal technical gate appears. Do not rerun the same anchor (exit 12), use stale data, or cross regions just to burn quota,
  and do not bypass the IBKR / PG-store discipline. The concurrency pool still follows the existing quota and stability rules; do not enlarge it without limit because of the window.
- In the launch announcement, state in one sentence: the feed evidence time and status, this harness's usage percentage and sampling time, burn window
  open / closed; if open, also state which secondary symbols were included because of it. During a long batch, if the promised landing time arrives, re-check the feed + this identity's
  usage; once completion is confirmed, return to ordinary selection volume and do not keep treating the post-announcement "possibly completed" as a burn window to grab.
