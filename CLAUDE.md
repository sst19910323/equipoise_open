# Equipoise — shared maintenance guide

This public snapshot includes the current research implementation and its rules. Read [release scope](docs/source-snapshot.md) before operating it. Local configuration, PostgreSQL contents, IBKR login, agent credentials and host services are not supplied.

## Where to work

| Task | Entry |
| --- | --- |
| Shared rules, roles and models | `prompts/agent/principles.md`, then its manual index |
| Debate execution | `skills/equipoise-run-debates/SKILL.md`, then `references/codex.md` or `references/cc.md` |
| Group hearings | `skills/equipoise-run-debates/references/group-production.md` |
| Dispatch and data readiness | `skills/equipoise-dispatch/SKILL.md` |
| Postcheck | `skills/equipoise-postcheck/SKILL.md`, `orchestration/postcheck-protocol.md` |
| Add symbols | `skills/equipoise-add-symbol/SKILL.md` |
| IBKR operations | `skills/equipoise-manage-ibkr/SKILL.md` |
| Worker usage calibration | `skills/equipoise-usage-calibration/SKILL.md` |
| Monitoring and scheduling | `HERMES.md`, `orchestration/duty.md` |
| Historical drills | `scripts/drill/`, `prompts/drill/` |
| Backend API and storage | `equipoise_entry.py`, `datalayer/pg/` |

Use the adapter for the harness that is actually running the task. Codex and Claude Code have separate runners over one shared corpus. Model names and worker capacities in the source are configurable examples; verify their availability on your own accounts.

## Maintenance checks

The source uses Python 3.12. `requirements.txt` is the upstream dependency list; `requirements-dev.txt` adds test and optional drill/browser tools. Neither file provisions PostgreSQL, IBKR or model accounts.

```bash
python scripts/gen_agent_files.py --check
python scripts/gen_skill_stubs.py --check
python -m pytest scripts -q
```

After changing canonical agent content, run `python scripts/gen_agent_files.py`; after changing skill frontmatter, run `python scripts/gen_skill_stubs.py`. Keep generated files in the same commit. Runtime and acceptance scripts under `scripts/drill/check_*` and `group_http_acceptance.py` have separate infrastructure requirements; they are not ordinary unit tests.

## Operational boundaries

- PostgreSQL is the historical archive. JSON/CSV files are transport and working material; evidence records preserve actual payload text, not just paths.
- Original research and judgments remain English. Translations are overlays; do not overwrite the original record.
- A configured IBKR service must be available for live market preparation and postcheck prices. The reference deployment uses port 4001, selectable with `EQUIPOISE_IBKR_PORT`.
- Gateway restarts require the operator to be ready for phone 2FA. Use one explicit attempt via `ibkr_restart.sh`; do not create an automatic retry loop. These scripts assume an already configured Linux service.
- Do not load maintenance notes or reviewer references into blind drill roles. Frozen historical material and review-side material have different purposes.
- `run_debate.py`, `run_one_debate.py`, `init_all_debates.py` and `run_postcheck.py` are retired API-era entry points retained upstream. Current agent workflows use the skills and `scripts/run_step.py`. `horizon_sentinel.py` remains a library dependency; do not delete it merely because its scheduler is retired.

The private development chronicle, sealed drill outcomes and personal portfolio layer are deliberately absent. Their absence is not a request to retrieve them.
