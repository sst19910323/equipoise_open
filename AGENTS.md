# Equipoise public edition — agent entry

Read `CLAUDE.md` for the shared maintenance guide, then `docs/source-snapshot.md` for the scope of this source release. The filename `CLAUDE.md` is historical; it serves both Codex and Claude Code.

- Follow the user's language in conversation. Role prompts, delegated tasks, debate artifacts and postcheck judgments use English, except quotations from original sources.
- Keep the shared rule corpus in `prompts/agent/`, protocols in `orchestration/`, and reusable workflows in `skills/`. Engine adapters belong in `.codex/` and `.claude/`.
- Change rules at their source; regenerate agent files with `python scripts/gen_agent_files.py`. Do not edit generated copies independently.
- Keep responsibilities in their natural layers, use compact mechanisms with broad analytical coverage, explain quantitative reference points, and give competing actions symmetric standards of evidence.
- This is a source snapshot, not a provisioned deployment. Existing `/opt/equipoise`, worker-home and systemd paths are deployment conventions, not evidence that services or accounts exist on this machine.
- Credentials, personal portfolio code, production data and host-specific Hermes/account setup are not part of this release. Never copy a private working tree wholesale into the public repository.
- Maintenance and offline tests do not require a live debate, broker login, database migration or deployment. Run those only when the user requests that operational work.

See `docs/ARCHITECTURE.md` for the code map. Initialization automation remains future work.
