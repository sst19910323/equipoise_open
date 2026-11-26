# 新旧命名对照表(naming-migration-map)

> **面向使用方的汇总(PG 迁移与前端改动清单)在 `docs/handoff/FIELD-RENAMES-2026-10.md`, 以那份为准**; 本表是施工流水。
> 批 4(2026-10-01)新增: 证据 `verified_scope`、核定状态 `failed`、终报 `unresolved` / `caps`; 详见那份汇总。

> 施工会话逐批追加。用途: 前端(`D:\front_end\apex_dash`, 另议)与 PG 历史数据迁移(库主另开 session)
> 按此表改; 本表只记**已经落进代码**的改名, 规划中的看 `naming-inventory-cc.md`。
> 旧数据不回写: PG 里 `technical` / `postcheck.quant` 的历史 JSON 仍是旧键, 读旧行要兼容两套名。

## 批 1 · 数据层与后验(2026-09-30)

### technical 快照(`analysis/technical_snapshot_builder.py`, PG `technical` 表 JSON; 扁平化后带 `{SYMBOL}_` 前缀)

| 旧 | 新 | 位置 | 为什么 |
|---|---|---|---|
| `session_context.latest_bar_et` | `session_context.latest_bar_local` | session_context | 非美标的按本交易所时区, 不再是纽约时间 |
| `session_context.prior_regular_close_et` | `session_context.prior_regular_close_local` | session_context | 同上 |
| (无) | `session_context.exchange`, `session_context.exchange_tz` | session_context | 读者要知道时段按哪个交易所判的 |
| `positioning.relative_strength_vs_spy` | `positioning.relative_strength_vs_benchmark` | positioning | 基准按 symbols.yaml `benchmark`, 不再写死 SPY |
| `positioning.correlation_vs_spy` | `positioning.correlation_vs_benchmark` | positioning | 同上 |
| (无) | `positioning.benchmark_symbol` | positioning | 相对谁算的; 缓存缺/不鲜时值为 `"QQQ (stale cache, skipped)"` 这类带原因的字符串, 三个相对指标为 null |
| `nearest_support_resistance.asymmetry_ratio_upside_over_downside` | `nearest_support_resistance.range_30d_upside_over_downside` | positioning | 它就是"到 30 日高距离 ÷ 到 30 日低距离", 不是非对称赔率 |
| `cross_timeframe_momentum_score` 取值 `Strongly Bullish / Bullish / Bearish / Strongly Bearish`(三票) | `Bullish / Leaning Bullish / Mixed / Leaning Bearish / Bearish (k/n timeframes RSI>50)`; 一票没有 → null | cross_timeframe_summary | 缺票不投票(5 分钟 RSI 非盘中为 null, 旧版永远按看空算) |
| `cross_timeframe_trend_alignment` 取值 `Bullish / Bearish / Mixed / Unknown-DailyLegUnavailable…` | 同名; 新增 `Mixed (1d Mixed / 1h down)` 明细形、`Unknown-HourlyLegUnavailable(1d …)` | cross_timeframe_summary | 日线 Mixed 不再算 Bearish; 缺腿不定向 |
| `instrument_metadata.has_extended_hours` 恒 true | 按交易所: 美股 true, 其余 false | instrument_metadata | 事实 |

### 后验 digest(`workflows/postcheck_quant.py`, PG `postcheck.quant`)

| 旧 | 新 | 为什么 |
|---|---|---|
| `entry.entry_filled` / `entry.fill_date` / `entry.post_fill_ret_pct` | `entry.entry_price_reached` / `entry.reached_date` / `entry.post_reach_ret_pct` | 触价 ≠ 成交 ≠ 满足报告的全部条件; 名字照实说 |
| `trim_exec.exec_filled` (同族) | `trim_exec.exec_price_reached` | 同上(卖侧镜像) |
| (无) | `ruler` (= `RULER_VERSION`, 现 `"R22"`) | 尺子版本, 评估记录版本化 |
| (无) | `short_term` 对象: `horizon_days / grace_days / horizon_covered / grace_covered / expiry_date / expiry_close / outcome_at_expiry / outcome_with_grace / first_touch / progress_at_expiry_pct / expiry_inside_band / expired_before_window_end` | short_term 按有效期柔性清算(决策契约 §4.6) |
| `direction_call` 由 action ≥55/≤45 决定 | 由 操作族 → short_term.direction → action 粗方向 决定 | 后验主要由操作决定; action 是综合基调 |
| GENERAL `short_term.target = "SPY=95-105"` 被读成 95 | 解析为区间 → 成员 `st_band {low, high}` | 震荡区间两沿都要 |

### 后验表(PG `postcheck`; `datalayer/pg/postcheck_store.py` 与 `postcheck_pg_store.py` 同 schema)

| 旧 | 新 | 为什么 |
|---|---|---|
| 主键 `(symbol, as_of, dt)` | `(symbol, as_of, dt, ruler)`; 列 `ruler text NOT NULL DEFAULT 'R21'`(旧行回填) | 旧批用新尺重评另存, 不覆盖旧分 |
| 档位 `3d / 20d / 50d` | `3d / 20d` | 50d 退役(决策契约 §4.7); 旧 dt=50 行仍在, 只是不再登记 |
| staging 文件名 `{SYM}_prep_{TS}_dt{dt}.json` | `{SYM}_prep_{TS}_dt{dt}_{ruler}.json` | 同上 |
| `GET /api/postcheck` items 每档位一行 | 每档位可能多行(每把尺一行, 新尺在前), 行内带 `ruler` | 前端另议时按 ruler 分组或只显示最新尺 |

### 路由(`hydrate_pg_to_files.py`)

| 旧 | 新 | 为什么 |
|---|---|---|
| KR 个股父路由落 GENERAL / 板块 | `GENERAL_KR`; 新增 `symbol_region()` / `sentiment_regions()` | 韩国个股此前读到美国大盘语境, 恐慌指数也漏 KR |

## 批 2 · 新规则树与命名(2026-09-30)

### 辩论轮 JSON(`formats/debate.md`; 由 `scripts/validate_role_record.py` 机检)

| 旧 | 新 | 为什么 |
|---|---|---|
| `action` | `composite_score` | 综合评分, 不是操作也不是信心刻度 |
| `operation_volume` | `operation_size` | 规模档位, 不是数量 |
| `short_term` | `short_term_path` | 短期路径判断, 不是短线操作 |
| `preliminary_mentality` | `mentality` | 一词一义; 辩论轮与终报同名 |
| `mentality[].probability` | `mentality[].weight` | 倾向权重, 不是胜率 |
| `pillars` | `claims` | 押分主张; "支柱"是比喻 |
| `pillars[].kind`: `pillar` / `boost` | `claims[].kind`: `own` / `reinforce`(`challenge` 不变) | 自家主张 / 加强 |
| `pillars[].tier`, `moves[].tier`, `held[].tier` | `evidence_tier` | tier 三义之一, 说清是证据档位 |
| `request_evidence[].type`: `task` | `model_call` | 模型调用 |
| `request_evidence[].playbook` | `request_evidence[].model` | 招式→模型手册 |
| analysis_text 首行手写"立场牌" | 删除(展示层由代码从字段渲染) | 生成顺序按判断链, 综合评分最后 |
| (无) | `beat: {status: final 或 need_more, next_is_final}` | 一轮多拍, 拍数辩手自报 |
| 字段顺序 | analysis_text → stance_ledger → claims → short_term_path → mentality → operation_* → composite_score → summary_statement → request_evidence → beat | 生成顺序即推理顺序 |

### 终报 JSON(`formats/final.md`; 由 `scripts/validate_debate.py` 机检)

| 旧 | 新 | 为什么 |
|---|---|---|
| `action` / `operation_volume` / `short_term` | `composite_score` / `operation_size` / `short_term_path` | 同上 |
| `mentality[].probability` | `mentality[].weight` | 同上 |
| `organized_z` / `organized_r` | `ledger_score_z` / `ledger_score_r` | 账本整理分; 由组装器从 ledger_tally 注入, 仲裁者不写 |
| `residual_adjustment.delta_vs_organized` | `residual_adjustment.delta_vs_ledger` | 同上 |
| `conviction_budget{legs[{leg, refs, claim}], total_claim}` | `deviation_support{legs[{basis, refs, points}], total_points}` | 综合评分偏离 50 的证据支撑(审计), 不是"信念额度" |
| `arbiter_shadow_log` | `arbiter_private_scores` | 仲裁者私记评分; "影子"是比喻 |
| `arbiter_shadow_log[].action` / `provisional_ruling.action` | `composite_score` | 同上 |
| `tasks[]` | `model_calls[]` | 模型调用台账 |
| `tasks[].task_id` / `playbook` / `tier_eligible` | `model_calls[].model_call_id` / `model` / `suggested_evidence_tier` | 建议档位与仲裁核定区分 |
| `dissent.zealot_final_action` / `reaper_final_action` | `dissent.zealot_composite_score` / `reaper_composite_score` | 签章认可的综合评分 |
| (无) | `objections`(异议回合原文与处理记录) | 拟定终裁 → 异议 → 再定稿 → 签章 |
| `statement`"高确信度宣言" | `statement`(决策核心理由一句) | 刻度不表示置信 |
| `known_flaws` 的"评分偏离 20 以上建议填" | 删除; 只按操作极端度(AGGRESSIVE / LIQUIDATE)必填 | 决策契约 §3 |
| 字段顺序 | reasoning → asset_tier → short_term_path → mentality → mentality_basis → operation_* → risk_management → known_flaws → next_review → composite_score → residual_adjustment → deviation_support → statement → debate_summary; 机械字段由组装器注入 | 生成顺序按判断链 |

### 签章与异议(`formats/signoff.md`、`formats/objection.md`)

| 旧 | 新 |
|---|---|
| signoff `final_action` + `reservation` | `reservation` → `dissent_refs[]` → `endorsed_composite_score` |
| (无) | objection: `objections[{target, kind, statement, impact, refs}]`, `replies[]`, `needs_new_evidence[]` |

### 证据与执行者输出(`formats/evidence.md`)

| 旧 | 新 |
|---|---|
| Scout 项 `task`(研究问题) | `question` |
| Scout `verdict: supported` / runner `verdict: verified` | 统一 `supports` / `refutes` / `mixed` / `not_found` / `unrelated`(新增: 无关或不能判断) |
| runner 项 `task` / `playbook` / `legs[]` / `tier_eligible` | `model_call_id` / `model` / `steps[]` / `suggested_evidence_tier` |
| 核验状态 `status` | 不变: `verified` / `unverified` / `not_found`(仲裁者核定, 只用于事实主张) |

### 对象仓与编排(批 3 再改, 这里先登记)

`TK` 编号前缀 → `MC`; 对象仓 kind `task` / `task_evidence` → `model_call` / `model_call_evidence`;
buffer 行键 `task_id` / `playbook` / `tier_eligible` / `arbiter_shadow` → `model_call_id` / `model` /
`suggested_evidence_tier` / `arbiter_private_score`(组装器已能读两套); `[ARB 评分]` 公示行退役
(交锋期仲裁者沉默); `beacon` → `trigger_signal`(备餐与趋势语境输出)。

### PG(库主另开 session 迁移)

`debates.action` → `composite_score`; `debates.operation_volume` → `operation_size`; `tasks` 表 →
`model_calls`(列 `task_id` / `playbook` / `tier_eligible` 同上改名)。写库层现按"新名优先、旧名兜底"读报告,
写入旧列名, 迁移后再改写入侧。

### 术语(prompt 正文, 零代码)

招式 / 心法 → 模型手册 / 原则; 挂牌 / 牌价 → 押分; 圆桌 → 对手; 致死错误 → 人格边界; 影子分 → 仲裁者
私记评分; 按语 → 仲裁者评注; 盖章 → 证据核定; 腿 → 数据调用 / 搜索取证 / 模型调用; 备餐 → 数据准备;
两拍 / 私有小会 → 拍 / 回执; 引信 → 触发信号; 挂点 → 限价位; 姿态表 → 市况限价规则; 新闻 L1/L2/L3 →
直接冲击 / 情绪扰动 / 标题噪音; "已定价"四义拆开(市场已反映 / 押分 / 风险已计入 / 已充分讨论);
多头 / 空头 → 乐观者 / 现实者。

### 文件树(旧文件已在批 3 删除, tag `v-old-parliament-2026-09-30` 有备份)

| 旧 | 新 |
|---|---|
| `prompts/agent/shared_rules.md` | `prompts/agent/principles.md`(三方常驻) + `debater.md`(辩手共有) + 各手册 |
| `.claude/agents/{zealot,reaper}.md`(手写) | `prompts/agent/roles/{zealot,reaper}.md`(灵魂) + `debater.md` → 由 `scripts/gen_agent_files.py` 生成 |
| `.claude/agents/{scout,runner}.md`(手写) | `prompts/agent/roles/executor.md` + `roles/{scout,runner}.md` → 生成 |
| (无) | `prompts/agent/roles/arbiter.md` |
| `prompts/agent/playbooks/P0x-*.md` | `prompts/agent/manuals/{understanding,mentality,operation}/P0x-*.md` |
| `prompts/agent/playbooks/ARB-final-ruling.md` | `manuals/synthesis/arbiter-final-ruling.md`(+ `arbiter-evidence-review.md`、`arbiter-round-notes.md`) |
| `prompts/agent/formats/{debate_output,final_report_output,evidence_output,signoff_output}.md` | `formats/{debate,final,evidence,signoff,objection}.md` |
| `prompts/agent/tasks/raw_data_<type>.md` | `manuals/evidence/context_<type>.md`(Read 清单归备餐脚本) |
| `prompts/agent/ibkr_capability_map.md` | `manuals/evidence/ibkr_capability_map.md` |
| (散在 shared_rules §2 / §5) | `manuals/evidence/news.md`、`manuals/evidence/request.md`、`manuals/mentality/mentality.md` |
| (无) | `docs/design/principles.md`(设计者手册, 不进 prompt) |

## 批 3 · 新编排与对象仓(2026-09-30)

### 对象仓(`scripts/debate_store.py`; `data/store/<SYM>/<TS>/`)

| 旧 | 新 | 为什么 |
|---|---|---|
| kind `draft`(`DRAFT.R2.Z`) | kind `beat`(`R2.Z.B1`, 拍号在名字里) | 一轮多拍, 中间拍归档不进视图 |
| kind `task` / `task_evidence`(`R.TK1` / `R.TK1.EV1`) | `model_call` / `model_call_evidence`(`R.MC1` / `R.MC1.EV1`) | 模型调用 |
| `--by Q-PARENT`, 前缀 `P` | `--by Arbiter`, 前缀 `A`(`A.EV1`; 旧值兼容读) | 仲裁者中立补查 |
| kind `arbiter`(`ARB.R2`) | kind `notes`(`NOTES.R2`) | 仲裁者评注(程序公告) |
| kind `shadow`(`SHADOW.R2`) | kind `private_score`(`PRIVATE.R2`), 内容须含 `composite_score` | 私记评分 |
| 行键 `task_id` / `playbook` / `tier_eligible` / `task_request` / `arbiter_shadow` | `model_call_id` / `model` / `suggested_evidence_tier` / `model_call_request` / `arbiter_private_score` | 同批 2 |
| buffer `_inflight_shadow_<TS>.jsonl` | `_inflight_private_<TS>.jsonl`(组装器 `--private-buffer`; `--shadow-buffer` 兼容) | 同上 |
| `council`(`_council_R2_zealot.jsonl`) | `receipt --round n --beat k`(`_receipt_R2_B1_zealot.jsonl`) | 拍回执 |
| (无) | kind `proposed`(`PROPOSED`) / `objection`(`OBJ1.Z`) / `ruling`(`RULING.v1`, 可再版) / `signoff`(`SIGN1.Z`, 数字=所签版本) | 拟定终裁 → 异议 → 再定稿 → 签章 |
| `view --signoff --draft <file>` | `view --signoff`(再定稿从仓里取) / `view --objection k` | 视图只从对象仓拼 |
| 视图里的 `[ARB 评分]` 行 | 删除; 评注行保留机械行 + 程序公告 | 交锋期沉默 |

### 编排(新文件; 旧 `.claude/commands/run.md` 777 行流程正文全部退役)

| 旧 | 新 |
|---|---|
| `.claude/commands/run.md` 的 Step 0–9 流程 / `.codex/run.md` 的必读清单与拼装协议 | `orchestration/run-protocol.md`(两引擎共用); 两份 run.md 只剩工具映射 + 固定命令表 |
| parent 手写 spawn prompt / FRESH_EVIDENCE / 小会回执路径 | `scripts/run_step.py` 生成材料文件 `_spawn_R<n>_<role>.md` / `_resume_R<n>_B<k>_<role>.md` / `_job_<id>.md` / `_spawn_OBJ<k>_<role>.md` / `_spawn_SIGN<v>_<role>.md` |
| 两拍(初稿 → 小会 → 终稿) | 拍循环: `route` → `close-beat` → 再唤, 直到 `beat.status=final` 或拍上限 |
| parent 手判 CONTINUE / TERMINATE | `close-round` 硬条件归代码(open 主张 / 轮上限 → `unresolved`), 是否谈透仍由仲裁者 |
| 准入盖章(3b, 数据腿也由仲裁者补 simple) | 数据调用由 `route` 机械执行并核定(`ok:true`→verified, `ok:false`→not_found 并降级一条搜索取证); Scout / Runner 结果由仲裁者在 `close-beat` 前核定 |
| draft 终报 → 签章 → 手写 metadata → assemble | `finalize` 六段: provisional → audit → proposed → objection(≤2 回合) → ruling(组装 + 签章前闸) → signoff(注入 dissent, 同一条闸, buffer 收尾) |
| `evidence_added` 由仲裁者手写; `search_rounds_used` 自报 | 组装器从证据行机械派生 |
| `parent_analysis_file` 字段 / 父层报告进 RAW DATA FILES | 退役: 辩手不读上一场报告; 字段不再写入(水合通道 `hydrate_parent_report` 仍在数据准备里, 库主定去留) |
| `beacons` / `beacon`(trend_context / dealer_gamma) | `trigger_signals` / `trigger_signal`(旧键兼容读) |
| RAW DATA FILES 由 parent 逐条 Glob 拼 | `workflows/data_manifest.py` 一份代码: 数据准备 READY 回执打印 + `run_step open` 拼进材料 |
| codex_loop 自带 `REQUIRED_REPORT_FIELDS` 父层验收 | 同一条闸 `run_step.py gate`(validate_debate + 结构契约 + Evidence 侧文件一致); `pg_store_debate.py` 写库前也跑它(`--skip-gate` 只给迁移) |
| `scripts/build_debate_views.py` / `scripts/append_jsonl.py` | 删除(视图与写入全在 debate_store) |

### 终报新增字段(由组装器 `--meta` 注入)

`objections[]`(异议原文, 带 id / by / round)、`dissent.ruling_version` 与 `dissent.<side>_dissent_refs`、`ruling_version`、
`unresolved[]`(轮上限到时仍 open 的主张)、`caps`(本场参考值)、`harness_truth.driver = scripts/run_step.py`。
