# Source map

The shared content has one authoritative version. Codex and Claude Code differ in their execution adapters, not in the research rules they receive.

| Layer | Location | Purpose |
| --- | --- | --- |
| Rules and models | `prompts/agent/` | Principles, roles, evidence and pricing manuals, operations, synthesis and output contracts |
| Shared protocols | `orchestration/` | Debate, postcheck and duty procedures |
| Reusable workflows | `skills/` | Dispatch, debate, postcheck, symbol onboarding, broker operations and usage calibration |
| Engine adapters | `.codex/`, `.claude/commands/` | Harness-specific execution and data-refresh entry points |
| Generated role files | `.claude/agents/` | Generated from the canonical roles and debater rules; used by both harnesses |
| Data and archive | `datalayer/fetchers/`, `datalayer/pg/` | Market ingestion, PostgreSQL schemas/readers/writers and group records |
| Analysis | `analysis/`, `workflows/` | Technical snapshots, forecasts, execution cards and postcheck calculations |
| Debate machinery | `scripts/run_step.py`, `scripts/debate_store.py`, `scripts/group_run.py` | Session progression, evidence objects, role filings and grouped hearings |
| Quantitative models | `scripts/odds_model.py`, `scripts/scenario_replay.py`, `scripts/ruling.py` | Scenario arithmetic, executable effects, mixtures and derived decisions |
| Historical drills | `scripts/drill/`, `prompts/drill/` | Frozen packs, isolated role sessions, evidence queues and review tools |
| Scheduling | `scripts/duty*`, `scripts/hermes_task.py`, `HERMES.md` | Monitoring, capacity selection and agent wake-ups |
| Dashboard API | `equipoise_entry.py` | Read the archive and expose reports, data and monitoring state |

`scripts/gen_agent_files.py` generates role adapters. `scripts/gen_skill_stubs.py` generates `.claude/skills/` pointers to the shared skill bodies. Check both after changing content; do not maintain a second rule corpus in adapters.

`prompts/api/` and `prompts/legacy/` retain retired implementations. See [legacy inventory](LEGACY-API-ERA.md) and the [release scope](source-snapshot.md). The dashboard frontend is a separate project; this snapshot contains its backend API, not the web application shown in the screenshots.
