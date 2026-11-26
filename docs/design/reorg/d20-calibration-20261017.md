# D+20 复盘口径校准 · 过程与改动记录(2026-10-17)

这是浓缩记录。原始材料(服务器 Codex 的交接包 `d20-calibration-20261017/` 与本地 council 七轮的问答全文)已于 10-17 晚按库主要求删除;
中间辩论的细节不再保留, 只留结论、拍板与改动清单。来源标签: [库主] / [我]=CC / [GPT]=gpt-6-astra / [Gemini]=gemini-4-argon-high / [服务器 Codex]。

## 1. 起因

服务器 Claude(opus-5-5)10-17 当天跑了 40 份 D+20 复盘(R23); [服务器 Codex](astra)抽 8 份盲评(最低 4 份 + 随机 4 份), 写了交接包到 `/root/.codex`。
两边 digest 关键字段逐项相等, 分差全在口径:

| 标的 | 报告日 | Claude | Codex | 差 |
|---|---|---:|---:|---:|
| KB | 09-14 | 22 | 38 | +16 |
| HOCPY | 09-14 | 25 | 36 | +11 |
| MP | 09-18 | 28 | 52 | +24 |
| MUFG | 09-14 | 28 | 68 | +40 |
| GENERAL_JP | 09-18 | 48 | 68 | +20 |
| HYMTF | 09-14 | 35 | 58 | +23 |
| TM | 09-14 | 60 | 58 | −2 |
| SFTBY | 09-14 | 42 | 72 | +30 |

平均绝对分差 20.75, 7/8 Codex 更高。[库主] 的目标: 客观明确的口径, 换模型分数与结论不应大变。

## 2. 过程(七轮, 只记走向)

1. 第一轮(GPT + Gemini 读交接包): 两家都说根子不在模型宽严, 在规范没锁定"评价对象与责任窗口"; 还查出 R23 四个摘要缺陷(老报告起点价退到前收、`_first_float` 千分位、路径抽稀丢 OHLC、假日重复 bar)。
2. 第二轮(第一件: 事件边界): 两家互相读对方理由后收敛——事件是状态分水岭不是断头台, 不硬截断不固定权重; 事件日 K 线单列、默认不拉分时; 执行线从严、判断线从柔。[库主] 拍板: **柔性**, 公式指标三个 AI 商量。
3. 第三轮(三家各提一份细则 + HOLD 价值方案): 细则高度一致(ε(H)=max(ε_min, 0.5σ√H)、连续两收盘越线算接受、主要交付 3×3 定档、只登记事件+HOLD 落 50-64); HOLD 收敛到"T 时登记结构位 + 被接受的状态"双锁下的可达相邻操作, 差距四类。
4. 第四轮(HOLD 的定义): 三家合稿"50% 中性 / 七档比例 / 读者映射"。[库主] **撤回**: 市场状态动态, 小额/正常/大额该怎样就怎样; 报告只答"此时状态下的最佳操作", 不替读者算仓位; "让 HOLD 也有代价"落在复盘。
5. 第五轮(限价单有效期挂哪; Gemini 从此轮起因 Google 地区限制 400 掉线): [我] 原提议没写期限回退到 horizon_days; [GPT] 指出双重激励(辩手为单子改路径天数); [库主] 定: 明示优先, 否则默认 3 个交易日, 与 horizon 解绑, 因动了辩论规则升 v3.2。
6. 第六轮(GPT 复核施工): 11 条必须改 + 5 条建议改; 修了 9 条必须改与全部建议改。
7. 第七轮(GPT 回访): 剩余 6 条里又修 4 条(收盘确认/N 日止损模拟、带单位数字剔除、明写到期日期、同价线角色合并); 两处按设计保留: 接受后的候选算到窗末; 报告自己登记的挂单/减仓位作候选。

## 3. [库主] 三件拍板(全文原在 decisions.md, 已并入此处)

前提: D+20 只是定一个中期战术长度, 不是预测到期日; 分歧可能有更深层原因(辩论时对市场状态讨论充不充分、终报有没有空间描述); 他管宏观与设计目的, 细则三个 AI 商量;
相信某个状态下只有一个综合最佳选择; 散户可全仓, 读者仓位不是报告的事; 曾经好多个版本 HOLD 太占便宜, 修了好多次, 现在又类似。

1. **事件边界柔性分段**: 事件前评等待理由与已做动作, 事件后评交付的分支与仍有效约束; 唯一综合分按主要交付定档、次要得失定档内位置; 事件日 K 线单列, 触及与破位必入判词; 执行线(挂单价、减仓价、止损、订单期限)从严, 判断线(目标/失效/区间/观察锚/重评线)从柔; "只登记事件 + HOLD" 不再默认 70-85。
2. **报告只回答"此时状态下的最佳操作"**(HOLD 或方向 × 小额/正常/大额), 不定中性百分比、不定七档、不替读者算空仓满仓; 定义层只加一句"选 HOLD 要说明为什么此时不动优于轻动"; HOLD 的代价在复盘: 与同一状态下可达的相邻操作比, 差距记 相同/参数差/缺转态/反向/不可识别, 看出单边仍 HOLD 按"判断没落实为操作"记。
3. **条件单按撞线数学判**: 收盘等于线算成交, 不猜守稳; 限价单有效期报告明写按报告(日期/事件/最多等 N 日), 否则从基准日下一交易日起 3 个交易日含末日; **不看 horizon_days**; 重评时点不自动撤单; 上游禁软条件(先告警)。
4. 版本: 议会语料 **v3.2**(切分线 2026-10-17T13:38Z), 后验尺子 **R24**。

## 4. 改了什么(提交按时间)

| 提交 | 内容 |
|---|---|
| `9295e69` | 语料 v3.2: principles 订单期限与路径有效期解绑; final.md / debate.md 硬条件、订单期限可省略默认 3 日、`next_review.notes` 定义; mentality 等待三问日历事件版 + HOLD 要说明为何不动优于轻动; P06 时间止损解绑; 终裁手册核对三种期限 |
| `df871ce` | 后验规范: 条件单撞线数学与期限; 事件边界柔性分段两段重写; D+20 主要交付 3×3(失败 20-49 / 部分 50-64 / 成功 65-94 × 净损失/相抵/独立兑现, ±3); HOLD 也有代价; digest 新字段引用; 五案例锚(MUFG≈57, SFTBY≈52, GENERAL_JP≈65-68, HYMTF≈44, MP≈42) |
| `3736890` | 尺子 R24 `workflows/postcheck_quant.py` + `scripts/test_postcheck_quant.py`(R24Test) |
| `a10e79f` | 出厂闸软条件告警(`validate_debate.py`, 先告警不拦) |
| `0bf95ec` | 按 GPT 复核修 9+5 条(含 `postcheck_store._prior_timeline` 只继承同尺 3d 排班、ATR 真实波幅、日线去重、候选去掉事后视角、快照价遍历 lenses) |
| `7bb0edf` | `frozen-checks/r24_rerun_20261017.py`: 对旧 D+20 staging 用 R24 重算只打印分布 |
| `67c7f76` `e7e52ec` | GPT 回访修法; 大盘场 digest 也带 `order_valid_source` / `stop_rule` |
| `126559b` | 编年史 v3.2 节 + HANDOFF 现场 |

R24 digest 新字段: `anchor_source`(reference_prices > 5m 收盘 > 技术快照 > 前收)、`order_valid_days/_source/_until`、`stop_rule`、`entry.simulated_exit_ret_pct/exit_reason`、`segments{pre,event_bar,post}`、`eps_window_pct`、`line_state`(untouched / touched_only / accepted / accepted_then_reclaimed / pending / unknown)、`up_exc_pct / down_exc_pct / time_above_T_pct / efficiency_ratio`、`daily_bars`(含 `vol_ratio_20d`)、`reference.candidates[]`。
候选阈值集中在文件头: `EPS_MIN_PCT=1.0, EPS_SIGMA_MULT=0.5, SIGMA_LOOKBACK=20, ATR_LOOKBACK=14, ACCEPT_CONSEC_CLOSES=2, ACCEPT_DEPTH_ATR=0.5, ORDER_VALID_DAYS_DEFAULT=3`——全是候选, 留出样本校准后冻结。

同日顺手的另三件(与本题无关, 详见 HANDOFF 第 7 节): MU 10-13 停机案(`eadbc83`)、KO 首场目录案(`207336b`)、`operation_target` 全链收敛(`7d9d015` → `9bad190`, 单标的 数值 或 数值-数值, 大盘场每镜头一个, HOLD 也要价)。

## 5. 部署与验证

- 全部提交已部署服务器(`/opt/apex_quant`, 13:38Z 起, 修法随后热部署); 服务器单测全绿; `r24_rerun_20261017.py` 对 97 份 R23 d+20 staging 重算: 起点价 93 份改走技术快照、4 份前收(SFTBY 6540→5823, HYMTF 382500→370500)。
- 新复盘入库尺子 R24, 与 R23 旧行并存(主键含 ruler), 不覆盖。
- GPT 复核结论: 可部署; 两处设计保留见第 2 节第 7 条。

## 6. 待办

1. [库主] 安排: 这 8 份作开发案例, 服务器 Claude 按新规范重评, 与原分、Codex 盲评放一起看; 再从剩余 D+20 分层抽留出样本双模型盲评, 先验事实一致率(成交状态、责任窗口、主要交付)再验分差。
2. 过了再校准候选阈值(SFTBY 失效线在 0.5·ATR 深度规则下只到 `pending`, 是校准点之一), 决定 R23 旧行重评范围。
3. 编年史 v3.2 节"后验结论"回填。
4. Gemini 掉线(Google 地区限制), council 暂时只有 GPT。

## 7. 原始材料处置

- 已删(本地): `council/20261017-1836-d20-calibration/`(七轮问答、stderr 日志、synthesis、decisions、plan)与 `docs/design/reorg/d20-calibration-20261017/`(交接包: 8 份 staging、Claude 原复盘、Codex 盲评稿、规则快照、40 份快照)。
- 仍在: PG `postcheck` 表的 R23 原行(未动); 服务器 `/root/.codex` 下 Codex 自己的交接说明(未动); 8 份原 staging 在服务器 `data/postcheck_staging/`。
