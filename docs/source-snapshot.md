# Public source snapshot — 2026-11-26

The public repository now contains the research source exported from main-project commit `1d217213b2c31dab28b0413be91d3389d43982bd`. It is a selected source export, without the private repository's Git history. The [file manifest](source-manifest.json) records source and published hashes and public adaptations.

## Included

- Current shared rules, model manuals, output contracts, Codex and Claude Code adapters and generated role files.
- Market preparation, technical analysis, PostgreSQL storage/readers, grouped hearings, evidence records, scenario calculations, final reports and postcheck.
- Duty scheduling and the project-side Hermes task dispatcher.
- Historical drill tools, prompts, offline tests and selected acceptance fixtures. The dated drill plans are historical design references, not a fresh implementation checklist.
- The dashboard backend API. The frontend is maintained in a separate project and is not included in this export.

The root now follows the current source layout. The earlier file-based implementation remains on the [v1 branch](https://github.com/sst19910323/equipoise_open/tree/v1); use that branch with [README-v1](../README-v1.md), rather than mixing its setup steps with the current source. The small files under `data/examples/` are retained v1 samples, not fixtures for the current report contract.

## Not included

Personal holdings, portfolio/rebalancing code, credentials, live database contents, runtime archives, private consultation records and maintenance handoffs are outside this release. The host's Hermes installation, cron jobs and numbered account wrappers also live outside the project source and are not bundled. Historical experiment overlays and one-off analysis scripts are not part of this first export.

Tests solely for the unpublished portfolio layer are omitted; public-core assertions in mixed test files are retained. Tests for an optional historical overlay explicitly skip when that overlay is absent. Personal usage-calibration history is replaced by an empty configuration. Example configuration files contain no account keys. Drill code copies also exclude local review material and sealed outcomes. See the manifest for per-file changes.

## Running it: initialization still to come

This is a source release, not a complete installation package. The code and existing operator protocols are available to inspect now; the guided database, Hermes and multi-account initialization will be organized separately.

The upstream deployment uses Python 3.12 and Linux, PostgreSQL, IB Gateway/IBC, and separately authenticated Codex or Claude Code installations. Several scripts still assume `/opt/equipoise`, a `venv` directory, Linux process/service commands and numbered worker wrappers. Those conventions need adaptation to a new machine. Bundled schedules and worker capacities are reference configuration, not a statement that those accounts or services exist.

Credential-bearing local files are ignored by Git. Their shapes are provided in `config/models.example.yaml` and `config/data_sources.example.yaml`; configure local copies only when preparing your deployment. PostgreSQL connections also accept `EQUIPOISE_PG_*` environment variables. Default local database values are development examples, not production credentials. The dependency list is inherited from the main project and is not a pinned environment lock.

For code navigation, start with [architecture](ARCHITECTURE.md), then [the shared maintenance guide](../CLAUDE.md). See [verification results and known limits](source-verification.md) for the passing checks and inherited translation-test failures. Full fresh-machine initialization and live broker/model/database acceptance remain separate work; no live service is provisioned by this source sync.

## 中文说明

本次先把主工程的研究源码同步到公开仓库：规则与公式模型、数据层、群组辩论、task／evidence、定价与操作输出、复盘、演习工具和调度脚本都已放入。初始化辅助后续再做。

这不是已经装好的运行环境。数据库内容、盈透登录、agent 账号、Hermes 宿主配置与编号账号包装器需要另行准备；现有脚本中的 Linux 路径和服务约定也需要适配。个人调仓层仍暂不公开。截图所用的看板前端属于另一个工程，本次同步的是后端与研究引擎。

v1 已从当前代码布局中退出；查看或运行旧版时，请切换到上面的 v1 分支。当前规则直接阅读 `prompts/agent/`，无需从历史设计笔记猜测。
