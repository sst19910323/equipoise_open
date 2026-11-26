# Source-sync verification — 2026-11-26

Verified locally on Windows with Python 3.12. This records source-export checks, not a fresh Linux deployment or live trading-service acceptance.

## Passing checks

- All 217 exported Python files parse; exported YAML and JSON parse.
- All 123 local links checked in the README, source map, maintenance guides, release notes and screenshot gallery resolve.
- Canonical agent content matches generated role files; skill pointers match their source bodies.
- Seven shell scripts pass `bash -n`; they were not executed.
- Source and public file hashes are recorded in [the manifest](source-manifest.json). Publication checks found no private credential matches or retained production host addresses in the export.
- The focused regression set below passed **282 tests**, with **2 explicit skips** for an optional historical overlay absent from the public snapshot; 14 subtests also passed.

```bash
python -m pytest scripts/drill scripts/test_corpus_sync.py scripts/test_odds_model.py scripts/test_scenario_replay.py scripts/test_postcheck_execution.py scripts/test_postcheck_quant.py scripts/test_validate_role_record.py scripts/test_validate_debate.py scripts/test_group_report_view.py -q
```

Both tests in `scripts/test_formal_group.py` also passed, covering a two-member hearing and a seven-member hearing with changing agendas. These use mock roles and temporary files, not live models, IBKR or PostgreSQL.

## Known limits

A broader boundary set returned 52 passes and two failures in `scripts/test_execution_card.py`:

- `test_scope_is_per_lens_and_primary_copy_follows_it`
- `test_notice_translation_requires_matching_source_and_preserves_severity`

Both assertions were reproduced directly against the unmodified source snapshot. They concern execution-card notice text missing from translation-leaf collection. This sync preserves the upstream behavior; it does not claim those tests pass or that notice translation is complete.

The full suite was not completed on this machine. Collection of `scripts/test_prepare_freshness.py` requires Linux's `fcntl`; another quota transport test uses POSIX executable-script semantics. The repository's existing Linux queue tests skip on Windows. A broader Windows run was stopped after additional failures; only the explicitly reported focused results above constitute this release's passing test scope.

No live model turn, broker restart, database initialization, Hermes cron installation or production deployment was performed. The dashboard frontend is outside this export. Initialization and Linux deployment acceptance remain follow-up work.
