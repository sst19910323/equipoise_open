> Historical design reference from the main project. Current code and protocols take precedence; this is not a new task or a prerequisite to use the public source.

# 演习版代码 实施计划(Drill v0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产配置(sol 辩手 + sol 仲裁者)在一个冻结到 as_of 的沙箱里盲打一场历史节点, 外呼全部被拦截成"当时能知道的答案", 并在终裁前分叉一份给 Claude 上帝视角仲裁者做校准裁决。

**Architecture:** 每个节点一个沙箱根目录 `/opt/equipoise_drill/<SYM>_<TS>_<run>/` = 代码**拷贝**(不是软链: `run_step.py` 用 `Path(__file__).resolve()` 定 ROOT, 软链会把对象仓写回生产) + venv 软链 + 按 as_of 截断的数据。数据外呼由 `evidence_quant.py` 读环境变量 `EQUIPOISE_AS_OF` 机械截断; Scout / Runner 任务由 `run_step` 在演习模式下改成"排队"命令, 沙箱外的 oracle 守护进程用更强的模型(默认 Claude Opus 5.5)模拟"当时搜得到什么、会怎么答"。仲裁者与辩手各是独立的 sol 进程, 都不带 `--search`; 派人与推进由场外导演(Claude)做(Task 4)。

**Tech Stack:** Python 3.12(服务器 venv), 现有 `scripts/run_step.py` / `evidence_quant.py` / `codex_loop.py`, `claude -p`(服务器 2.1.319), Codex CLI(按 `resolve_codex()` 的身份), PG `equipoise`(只读, 仅打包时)。

**Spec:** `docs/design/drill/DRILL-PLAN-SEED-2026-11-07.md` + 库主 11-07 三条补充(见下 Global Constraints 第 1–3 条)。

## 定位: 演习 = PTR(测试服)(库主 11-07)

演习是额外的一块功能, 像游戏的 PTR 版本; 最终目的是校准、优化 skill, 改好了要进正式版。分法(库主 11-07 同意):
- **引擎开关进主线, 默认关**: Task 1(`evidence_quant` 的 `EQUIPOISE_AS_OF`)、Task 3 的 `run_step` 排队命令(沙箱里有 `drill/pack.json` 才生效)、
  (原计划的 `codex_loop._cmd` 开关已不需要)。几十行, 不开时生产行为不变, 由现有测试守住(补丁会随生产每周的改动烂掉, 所以不做补丁)。
- **其余都在 PTR**: 沙箱 / 冻结包 / oracle / 泄漏扫描 / sol 启动器 / 校准分叉 / 度量, 全在 `scripts/drill/**`、`prompts/drill/**`。
- **语料候选改法以补丁进 PTR**(`drill/overlay/*.patch`, 构建时 `git apply` 到沙箱拷贝; 打不上 = 生产语料已变, 构建拒绝): sol 在同一节点验收, 过了再提给生产。

## Global Constraints

- **角色分工(库主 11-07)**: sol = 演习方, 辩手与仲裁者全用 `gpt-6.1-sol`, 完全不知道结果(平时生产就用 sol)。Claude 系列 = 校准方, 第二个仲裁者, **上帝视角**(给它结果), 用来校正 skill。
- **外呼拦截(库主 11-07)**: 数据外呼截到 as_of; 搜索 / 模型调用不用 luna, 用更先进的 AI, 尽量借助当时搜得到的东西模拟当时会怎么答; 按当时时间线, 不该知道的就不知道。
- 一回合一回合来: 导演在每个 `close-round` 之后停, 由库主看完再续跑。
- 演习**不写 PG、不翻译、不动** `/opt/equipoise/data`; 所有产物只在沙箱根下。
- 会话内一律英文(铁规矩); oracle 提示词英文。
- 主线只加三个默认关的开关(见上); 语料(`prompts/agent/**`、`orchestration/run-protocol.md`)本计划不改。
- 开关不开时, 现有 `test_evidence_quant.py` / `test_run_step.py` 全过、`evidence_quant.py --selftest` OK。
- 一次只做一块, 每块做完给库主看再进下一块(不着急)。
- 部署走现有 LF archive 管道, 覆盖前先 diff 服务器现有版(记忆 feedback_deploy_diff_server_first)。
- 首个节点: `GENERAL`, as_of `20261009T133700Z`(原场 `data/store/GENERAL/20261009T133700Z/_meta.json` 的 `data_files` 即当场材料)。

## Review Focus

- as_of 当天的日线(10-09 美股 13:37Z 还没收盘)不得出现在任何 `bars` / `relative_strength` / `trend_fit` 结果里 → Task 1 `test_as_of_drops_same_day_bar`。
- 沙箱里任何脚本解析出的 ROOT 必须是沙箱根, 不是 `/opt/equipoise` → Task 2 `test_pack_root_is_isolated`。
- oracle 产出里出现晚于 as_of 的日期或事件 → 必须被扫出并记入审计侧档, 不能静默流进辩论 → Task 3 `test_leak_scan_flags_later_dates`。
- oracle 守护进程没起 / 卡死 → 排队命令在超时后写 `failed` 形状, 辩论按协议 §2.3 走, 不无限挂 → Task 3 `test_request_times_out_to_failed`。
- 暂停后续跑: 停在 close-round 后再推进, 必须接着原仲裁者会话、不重铸对象 → Task 4 `test_pause_at_close_round`。
- sol 进程读到沙箱外(生产数据 / 封存结果) → 必须被扫出 → Task 4 `test_out_of_root_read_flagged`。

---

### Task 1: `evidence_quant.py` 的 as_of 时钟

**Files:**
- Modify: `evidence_quant.py`(`_latest_csv` / `_bars` / `_is_stale` / `_live_fetch` / `_intraday_file` 调用处 / `econ_series` / `event_calendar`)
- Test: `scripts/test_evidence_quant.py`(追加)

**Interfaces:**
- Produces: `_drill_as_of() -> datetime | None`(读 `EQUIPOISE_AS_OF`, 格式 `YYYYMMDDTHHMMSSZ`, 解析失败抛错而不是静默忽略); `_today() -> date`(有 as_of 用 as_of 的 UTC 日期, 否则 `date.today()`)。

- [ ] **Step 1: 写失败测试**(合成 CSV 到临时 ROOT, monkeypatch 环境变量)
  - `test_as_of_drops_same_day_bar`: bars 含 10-08、10-09、10-12; `EQUIPOISE_AS_OF=20261009T133700Z` → `handle({"mode":"bars","symbol":"A","lookback_days":10})["data"][-1]["date"] == "2026-10-08"`。
  - `test_as_of_disables_live_fetch`: 设 as_of 且设 `EQUIPOISE_IBKR_PORT=4001` → `_live_fetch("A", 10) is None`, 且不 import ib_insync(monkeypatch 让 import 抛错也不触发)。
  - `test_as_of_calendar_window`: `event_calendar` 的窗口起点 = 2026-10-09, 10-15 的事件在 14 天窗口内、11-05 的不在。
  - `test_as_of_econ_picks_file_not_after`: 两份 `economic_US_20261009T134050Z.txt` / `economic_US_20261020T090000Z.txt` → 选前者(规则: 文件名时刻 ≤ as_of + 15 分钟备餐余量; 按文件名时刻取最大, 不按 mtime)。
  - `test_as_of_intraday_defaults`: 盘中 `bars` 请求不带 as_of 时用环境 as_of。
  - `test_no_env_unchanged`: 不设环境变量时上述五个场景输出与改前一致。
- [ ] **Step 2: 跑测试确认失败** — `./venv/bin/python -m pytest scripts/test_evidence_quant.py -k as_of -v` → FAIL。
- [ ] **Step 3: 实现** — 日线截断规则: 保留 `date < as_of.date()`(保守: 不论区域收盘时刻, 当天日线一律不给; 盘中细节由备餐快照与盘中档负责)。`PREP_SLACK_MIN = 15` 作为模块常量。
- [ ] **Step 4: 跑全部 evidence_quant 测试** — `./venv/bin/python -m pytest scripts/test_evidence_quant.py -v` → 全 PASS。
- [ ] **Step 5: Commit** — `feat(drill): evidence_quant honours EQUIPOISE_AS_OF (bars, calendar, econ, no live fetch)`

### Task 2: 冻结包 / 沙箱构建器

**Files:**
- Create: `scripts/drill/build_pack.py`
- Test: `scripts/drill/test_build_pack.py`

**Interfaces:**
- Consumes: Task 1 的 `EQUIPOISE_AS_OF` 约定。
- Produces: `build_pack(symbol: str, as_of: str, run: str, src: Path = Path("/opt/equipoise"), dest_base: Path = Path("/opt/equipoise_drill")) -> Path`(返回沙箱根); 沙箱内 `drill/pack.json`: `{"symbol","as_of","run","origin_store","files":{relpath: sha256},"built_at"}`; `drill/archive.jsonl`(oracle 用的当时材料); `drill/outcome.json`(**只给校准方**, 放在 `drill/_sealed/` 下, 演习方的提示词不提这个目录)。

- [ ] **Step 1: 写失败测试**(临时目录造一个迷你 src: 代码文件 + `data/store/X/<TS>/_meta.json` + 若干数据文件)
  - `test_pack_root_is_isolated`: 在沙箱根下 `python -c` 导入 `scripts/run_step.py` 的 ROOT 与 `evidence_quant.ROOT`, 都等于沙箱根。
  - `test_pack_copies_session_data_files`: 原场 `_meta.json.data_files` 列的每个文件都在沙箱里且 sha 相同; 同目录晚于 as_of 的文件不在。
  - `test_pack_truncates_daily_csv`: 每个标的只留一份 `*_drill_1d_<as_of>.csv`, 末行日期 < as_of 日期。
  - `test_pack_intraday_not_after`: 盘中 5m / 1h 文件只留文件名时刻 ≤ as_of 的。
  - `test_pack_store_empty`: 沙箱 `data/store/` 为空, `data/debate/` 为空。
  - `test_pack_sealed_outcome`: `drill/_sealed/outcome.json` 含 as_of 之后的日线(镜头标的 + 本标的), 沙箱 `data/market_data/` 里没有任何 as_of 之后的 bar。
- [ ] **Step 2: 跑测试确认失败** — `./venv/bin/python -m pytest scripts/drill/test_build_pack.py -v`。
- [ ] **Step 3: 实现**
  - 代码: rsync 拷贝, 排除 `data/ venv/ logs/ pids/ results/ .git/ __pycache__/ council/ docs/ reports/`; `venv` 软链回 src。
  - 数据: ① 原场 `_meta.json.data_files` 全部路径; ② `data/evidence_prep/<SYM>/*_<近 as_of 的时刻>*`(时刻 ∈ [as_of, as_of+15min]); ③ `config/symbols.yaml` 全部标的的日线截断; ④ 盘中文件 ≤ as_of; ⑤ 各区域 `data/economic/economic_<R>_*.txt` 取 ≤ as_of+15min 的最新一份; ⑥ `config/event_calendar.yaml` 原样(日程是事前公开的)。
  - `archive.jsonl`: PG 只读(`sudo -u postgres psql` 或现有 PG 读助手), 取 ① 原场 `evidence` 行全文; ② 同区域 as_of 前 14 天的 `news`; ③ 同区域 as_of 前 30 天其它场的 Scout evidence(`evidence` 表, as_of ≤ 本节点)。每行 `{"source","published_at","title","text"}`。
  - `outcome.json`: as_of 后 25 个交易日的日线 + PG `postcheck` 里该原场的各 dt 行。
- [ ] **Step 4: 测试 PASS**; 再在服务器上真建一次 `GENERAL 20261009T133700Z run=a`, 手看 `pack.json` 文件数与 `SPY_drill_1d_*.csv` 末行 = 2026-10-08。
- [ ] **Step 5: Commit** — `feat(drill): frozen pack builder (code copy, as_of-truncated data, sealed outcome)`

### Task 3: 外呼拦截 —— 排队命令 + oracle 守护进程 + 泄漏扫描

**Files:**
- Modify: `scripts/run_step.py`(route 产出 jobs 处, 约 L921: 演习模式下给每个 job 加 `command`)
- Create: `scripts/drill/oracle_request.py`, `scripts/drill/oracle_daemon.py`, `scripts/drill/leak_scan.py`, `prompts/drill/oracle.md`
- Test: `scripts/drill/test_oracle.py`, `scripts/test_run_step.py`(追加一条)

**Interfaces:**
- Consumes: 沙箱 `drill/pack.json`(存在 = 演习模式; run_step 用 `self.root / "drill" / "pack.json"` 判断, 不加命令行参数)、`drill/archive.jsonl`。
- Produces:
  - job 字段 `command: "./venv/bin/python scripts/drill/oracle_request.py --job <material> --out <output>"`(只在演习模式)。
  - `oracle_request.py`: 写 `drill/queue/<job_id>.req.json`, 阻塞等 `<output>` 出现, 默认超时 1500 秒; 超时写 evidence.md 的 failed 形状 `{"job_id", "failed": "oracle timeout"}` 并退出 0。
  - `oracle_daemon.py --pack <root> [--engine claude|codex] [--model claude-opus-5-5] [--parallel 4]`: 沙箱外运行; 每个请求起一个 `claude -p --model <model>`, cwd = 沙箱根, 允许工具 `Read, Write, WebSearch, WebFetch, Bash(./venv/bin/python evidence_quant.py:*)`, 环境带 `EQUIPOISE_AS_OF`, 不带 `EQUIPOISE_IBKR_PORT`; 提示词 = `prompts/drill/oracle.md` + job 材料全文。写完跑 `leak_scan`, 命中则把命中行回给同一 oracle 重写一次; 两次都命中 → 照写产出, 审计记 `leak: unresolved`(校准方会看到, 演习方看不到)。
  - `leak_scan(text: str, as_of: datetime) -> list[dict]`: 找出晚于 as_of 的显式日期(ISO、`Oct 12`、`10/12`、`12 October 2026` 等)及"as of <later date>"之类措辞; 返回 `[{"match","date","line"}]`。
  - 审计侧档 `drill/_sealed/audit/<job_id>.json`: 用了哪些来源(archive / web)、每条来源的发布日期、被排除的晚于 as_of 的条目、扫描结果。
- `prompts/drill/oracle.md` 要点(英文写): 现在是 `<as_of>`; 你在重建一个认真的研究员在那一刻能查到什么、会怎么回答; 先用 `drill/archive.jsonl`, 再用网络; 只引用发布时间早于 as_of 的来源并写明日期; 日期不明或晚于 as_of 的不用; 那一刻还不知道的事, 回答"当时未知 / not found"; 不得暗示后来的走势; 产出形状严格按任务材料里的 FORMAT; 被排除条目只写进审计侧档路径, 不进 OUTPUT。

- [ ] **Step 1: 写失败测试**
  - `test_leak_scan_flags_later_dates`: as_of 2026-10-09T13:37Z; 文本含 "on October 12, 2026 QQQ fell" 和 "2026-10-08 close" → 只命中前者。
  - `test_leak_scan_ignores_scheduled_events`: "FOMC meeting scheduled for Oct 28" 这种事前已排期的事 → 带 `scheduled: true` 标记、不算泄漏(关键词: scheduled / due / expected on / calendar)。
  - `test_request_times_out_to_failed`: 无守护进程, 超时 1 秒 → 输出文件为 failed 形状, 退出码 0。
  - `test_drill_jobs_carry_command`(`test_run_step.py`): MockTools + 沙箱里放 `drill/pack.json` → route 回执每个 job 有 `command`; 没有 pack.json → 没有 `command`(生产不变)。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现**(守护进程的并发用 `concurrent.futures`; 请求文件用 rename 原子落盘防半读)。
- [ ] **Step 4: 测试 PASS**; 服务器真跑一次: 在 Task 2 建的沙箱里手放一个原场的 `_job_R1B1-1.md`(P10 宏观传导那条), 守护进程产出后人工检查日期全 ≤ 10-09。
- [ ] **Step 5: Commit** — `feat(drill): time-locked oracle for scout/runner jobs, leak scan, sealed audit`

### Task 4: 导演 + 各角色独立 sol 进程(断网, 逐回合停)+ 事后知识检测

**分工(库主 11-07 改定)**: 不是一个 sol 主会话从头跑到尾, 而是**场外导演**(Claude: 本地对话经 ssh 或服务器 CC 都行, 靠同一份 runbook)
把仲裁者的杂活接过来, sol 只做判断:
- **仲裁者(sol)**: 一个独立 codex 会话, 整场 `codex exec resume` 续同一上下文; 照常读材料、调 `run_step`、写裁定, 但**不 spawn**: 需要派人时
  回合内停下, 回一张派单 `{"spawn": [{"role", "material", "output", "thread"}]}`, 由导演执行后把 OUTPUT 路径续给它。
- **辩手(sol)**: Zealot / Reaper 各自独立 `codex exec`, 只拿自己那份材料; 同一回合内的追问续它自己的会话(`thread` 字段), 回合之间新进程(同生产 `fork_turns: none`)。
- **外呼**: Scout / Runner 一律走 Task 3 的 oracle 队列(job 自带 `command`), 场外 AI 按 as_of 作答并审计。
- **逐回合**: 导演在每个 `close-round` 回执后停, 把双方发言、裁定、外呼答复、审计摘要交库主看, 点头再续; 收到 TERMINATE 的 close-round 也停(Task 5 分叉点)。
- **读隔离**: 不建专用用户(库主: 个人服务器, root 即可)。改为 ① `_sealed/` 打包后拉回本地、服务器上删, 校准时再给; ② 每回合导演扫各 sol 进程的
  codex 日志(`--json` 事件里的命令), 读过沙箱根以外路径 = 该回合作废并报库主。

**Files:**
- Create: `scripts/drill/sol_proc.py`(起 / 续 sol 进程), `scripts/drill/director.py`(推进与停点), `prompts/drill/arbiter-addendum.md`,
  `prompts/drill/director.md`(导演 runbook, 本地与服务器 CC 共用), `prompts/drill/knowledge-probe.md`
- Test: `scripts/drill/test_director.py`

**Interfaces:**
- Consumes: Task 2 沙箱; Task 3 守护进程心跳 `drill/queue/.daemon_alive`(没有就拒绝起步)。
- Produces:
  - `sol_proc.py start|resume --pack <root> --role arbiter|zealot|reaper --prompt-file <f> [--thread <id>] [--model gpt-6.1-sol] [--harness codex3]`:
    codex 命令 `exec -C <沙箱根> --sandbox workspace-write --json -o <reply>`, **不带 `--search`**, env `EQUIPOISE_AS_OF`、无 `EQUIPOISE_IBKR_PORT`;
    会话 id 记 `drill/threads.json`; 每次调用写 `drill/runs/<n>.json`(argv、模型真名、起止、最后回复、越界读路径列表)。
  - `director.py next --pack <root>`: 读仲裁者最近一条回复, 是派单就执行(辩手 → `sol_proc`; 带 `command` 的 job → 跑命令, 可并行), 再把结果续给仲裁者;
    碰到 close-round 回执即退出, 打印 `{"paused": "after_round", "round": N, "terminate": bool, "out_of_root_reads": [...]}`。
  - `director.py probe --pack <root>`: 同身份同模型断网问 `knowledge-probe.md` 三问(10-09 之后 QQQ / 日经 / KOSPI 怎么走; 不知道就说不知道), 存 `drill/probe.json`。
  - `arbiter-addendum.md`(英文, 只写执行机制不带规则): 不 spawn, 改回派单; 不写 PG、不翻译; 其余照 `.codex/run.md`。
- `codex_loop.py` 不改(导演不经它)。

- [ ] **Step 1: 写失败测试**(假 codex 可执行文件, 记录 argv / env, 按脚本回固定回复)
  - `test_sol_cmd_has_no_search`: argv 无 `--search`, `-C` = 沙箱根, env 有 as_of 无盈透端口。
  - `test_refuses_without_daemon`: 无心跳 → 非 0 退出, 提示先起 oracle_daemon。
  - `test_spawn_sheet_dispatch`: 仲裁者回派单(两个辩手 + 一个带 command 的 job) → 两个辩手各起独立进程、job 跑了命令、续给仲裁者的消息含三个 OUTPUT 路径。
  - `test_debater_followup_resumes_thread`: 同回合第二次派给同一 `thread` → 用 resume 而不是新进程。
  - `test_pause_at_close_round`: 仲裁者回复里出现 close-round 回执 → `next` 退出并带 round 号; 再 `next` 从 R2 继续, manifest 不重铸。
  - `test_out_of_root_read_flagged`: 假 codex 的 json 事件里有 `cat /opt/equipoise/data/x` → `out_of_root_reads` 非空。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 测试 PASS**; 服务器真跑 `probe`, 再 `next` 到 R1 停, 交库主看 R1。
- [ ] **Step 5: Commit** — `feat(drill): director + independent sol processes per role (no search, round pause, read audit, knowledge probe)`

### Task 5: 校准方 —— 终裁前分叉 + Claude 上帝视角裁决 + 评判稿

**Files:**
- Create: `scripts/drill/fork_for_calibration.py`, `prompts/drill/calibrator.md`, `docs/design/drill/templates/judgement.md`
- Test: `scripts/drill/test_fork.py`

**Interfaces:**
- Consumes: Task 4 导演在 TERMINATE 的 close-round 处的停点; `drill/_sealed/outcome.json`; 原场(生产)对象仓路径 `pack.json.origin_store`。
- Produces: `fork_for_calibration(root: Path) -> Path`(把整个沙箱 `cp -a` 到 `<root>_calib`, 校验 `debate_store.py verify` 通过, 且 manifest 末对象是 TERMINATE 的 `NOTES.R<n>`)。校准方在分叉根里跑 `finalize --stage provisional / audit / proposed`(同一套脚本推导 A、操作、分数), 不跑异议与签字。
  - `calibrator.md`(英文; 给 Claude Opus, `claude -p` 或我在对话里当): 你是第二仲裁者, 知道结果(读 `drill/_sealed/`); 产出两样: ① 在同一份辩论记录上写上帝视角的 `prov.json` / `proposed.json`(看"同样的主张, 最好能裁到哪"); ② 评判稿, 按模板逐项填。
  - 评判稿模板 `judgement.md` 栏目: 当时客观应答(只用 as_of 前信息, 不用事后)/ sol 本场实际 / 上帝视角裁决 / 生产原场 / 差在哪层(L1 数据 … L6, 按 feedback_layered_attribution_reports 的四段链)/ 手册用量与引用 / 漏用的规则 / 只有 sol 乱用的规则 / 差距归类(a 语料缺 / b 语料在 sol 用不动 / c 材料缺)/ oracle 泄漏审计摘要 / 改法候选。
- [ ] **Step 1: 写失败测试**: `test_fork_requires_terminate`(末对象不是 TERMINATE 的 NOTES → 拒绝); `test_fork_is_independent`(在分叉里 mint PROV 后原沙箱 manifest 不变)。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 测试 PASS。**
- [ ] **Step 5: Commit** — `feat(drill): calibration fork and god-view arbiter brief + judgement template`

### Task 6: 度量汇总

**Files:**
- Create: `scripts/drill/metrics.py`
- Test: `scripts/drill/test_metrics.py`

**Interfaces:**
- Consumes: 沙箱对象仓、分叉对象仓、原场对象仓、`drill/_sealed/audit/`。
- Produces: `metrics(root: Path) -> dict` 写 `drill/metrics.json` + 一页 markdown: 每回合每方的手册调用(P01–P10)与外呼主题; claims 引用的原则; 三方(sol 演习 / 上帝视角 / 生产原场)的操作、力度、综合分、按 `outcome.json` 用现有 `postcheck_quant` 算的 d+3 / d+20 机械量(ret、MFE、MAE、触线); oracle 泄漏命中数; 四格表(上帝视角用了 / sol 用了 / 都没用 / 只有 sol 用)按手册编号。
- [ ] **Step 1: 写失败测试**: 用 `test_v34_fixtures.py` 的夹具造一个最小对象仓 → `metrics()["manual_calls"]["P07"] == <夹具里的次数>`, 四格表键齐全。
- [ ] **Step 2–4: 失败 → 实现 → PASS。**
- [ ] **Step 5: Commit** — `feat(drill): drill metrics (manual usage, three-way comparison, leak count)`

---

## 执行顺序与人工检查点

1. Task 1 → 2 → 3 → 4(依赖链), 5、6 可在 4 之后并行。
2. 检查点 A(Task 3 之后): 我手看一条 oracle 产出, 库主过目泄漏审计格式。
3. 检查点 B(Task 4 之后): `probe` 结果 + R1 暂停, 库主看 R1 再决定续跑。
4. 检查点 C: TERMINATE 的 close-round 停点 → 分叉 → 校准方裁决 → sol 续跑到签字 → 评判稿。
