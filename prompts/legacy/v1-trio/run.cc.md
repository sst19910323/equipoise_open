---
description: 跑单个标的的完整辩论流程 (parent 直接编排, 一次一个 symbol)。用法: /run NVDA
---

为标的 `$ARGUMENTS` 执行完整辩论流程。**一次只跑一个标的**(多标的并行 = 多 session)。

如果 `$ARGUMENTS` 为空, 提示用户:"告诉我跑哪个标的, 例如 `/run NVDA`"。

## 🖥️ 运行模式:单工程本地(整合后)

本 skill 跑在 **apex_quant 一个工程**里(由 `apex-run-debates-cc` 的 `cc_loop.py` fire,或直接 `claude -p "/run <SYM>"`)。**数据完整性由编排层(对话 CC / cc_loop / 代码)在 fire 之前判定——只有数据齐了才触发 /run**。所以本 skill 是**纯辩论编排器**,全局规则:

- **数据本地可读**:所有数据在本工程 `data/`(technical / news / fundamentals / fear_greed /
  economic —— 2026-07-09 起 economic/fundamentals 也由备餐水合成运输文件, 你不连 PG/api)。
  直接读相对 `data/...`,无 READ/WRITE_ROOT 之分(一个工程一个 data/)。真相永远在 PG, 文件全是临时件。
- **/run 不连 IBKR、不跨机 sync**:拉行情是 apex_quant 数据 pipeline 的事(IBKR fetch),在 /run **之前**由编排层完成;本 skill 不碰盈透、不 `sync_from_server`/`run_on_server`(那是已废的两机时代产物)。
- **数据自检当安全网**(Step 0e):万一在数据不全时被误 fire,自检缺关键文件就 **STOP**,告诉编排器"数据没齐,先拉行情再 /run",绝不用旧/缺数据硬跑(免 AI 幻觉价位)。
- **终报落地**:写本地 `data/` + 补写 PG `debates`(Step 7),供 postcheck 消费。无跨机推送。

---

## Step 0: 数据预检(本地,轻量)

**核心原则**:市场数据已由 pipeline 在 /run 之前拉好在 `data/`。Step 0 只做**轻量自检 + 取数据时间锚**,不 sync、不 fetch、不连 IBKR。

### 0a. 确定标的与上下文
1. SYMBOL ← `$ARGUMENTS`(去空格大写;中文俗名你自己识别,拿不准反问)
2. CONTEXT_TYPE:
   - SYMBOL ∈ {`GENERAL`, `GENERAL_EU`, `GENERAL_JP`} → `general`
   - SYMBOL 在 `config/symbols.yaml` 的 `etfs:` 列表 → `etf`
   - 否则 → `stock`
3. TASK FRAMING 文件路径:
   - `GENERAL` → `prompts/cc/tasks/raw_data_general.md`
   - `GENERAL_EU` → `prompts/cc/tasks/raw_data_general_eu.md`
   - `GENERAL_JP` → `prompts/cc/tasks/raw_data_general_jp.md`
   - ETF → `prompts/cc/tasks/raw_data_etf.md`
   - Stock → `prompts/cc/tasks/raw_data_stock.md`

### 0b / 0c. (已废除 — 整合后单工程本地)

旧的 `scheduler/sync_from_server.py`(跨机拉数) / `scheduler/run_on_server.py`(远端触发 IBKR 重拉)**已删除**——一个工程、数据本地、行情由 pipeline 在 fire /run 前拉好。本 skill 不在这里 sync 或 fetch。**新鲜度/完整性是编排层(对话 CC / cc_loop)fire 之前的责任**;/run 只在 0e 做安全网自检。直接进 0d/0e。

### 0d. DATA_TIMESTAMP 取值(铁律)
- 取本地 `data/technical/<SYMBOL>/...technical_<TS>.json` 文件名后缀
- GENERAL 场景:取 SPY/QQQ/DIA 三镜头中最新的 TS
- **不要**用墙钟 / `datetime.now()`

### 0e. Pre-init 数据自检(**铁律,init 前必须过**)

这些文件是编排层在 fire 本命令前跑**备餐** `prepare_debate_data.py` 刚从 PG 水合出来的**临时运输
文件**(PG 才是真相, 文件只是给 subagent Read 的载体)——自检 = 确认备餐没漏。
按 CONTEXT_TYPE + region 列出**必需文件清单**,逐一 Glob 验证存在。**全部齐全才能进 Step 2 init**;有缺失按规则处理(STOP / 主动 WebSearch / 警告但继续)。

#### 共用必需(所有 CONTEXT_TYPE)
| 类别 | 文件 pattern | 缺失处理 |
|---|---|---|
| 经济 (US, 全球背景) | 备餐水合的 `data/economic/economic_US_*.txt`(真相在 PG, 文件是运输载体) | 缺 → **警告但继续**, 标记 agent 可申报搜索兜底宏观 |
| 情绪 (US) | `data/fear_greed/fear_greed_latest_*.json` | **STOP** 同上 |
| 宏观新闻 (US) | `data/news/GENERAL/GENERAL_news_*.json` | **STOP** 同上 |

#### region != US 额外检查(EU / JP 等)
| 类别 | 文件 pattern | 缺失处理 |
|---|---|---|
| 区域经济 (EU/JP) | 备餐水合的 `data/economic/economic_{EU,JP}_*.txt` | 缺 → 警告 + agent 申报搜索兜底 |
| 区域情绪 | `data/fear_greed/fear_greed_{eu,jp}_latest_*.json` | 同上 |
| 区域宏观新闻 | `data/news/GENERAL_{EU,JP}/...` | 同上 |

#### CONTEXT_TYPE = stock 额外
| 类别 | 文件 pattern | 缺失处理 |
|---|---|---|
| 个股量化 | `data/technical/<SYMBOL>/<SYMBOL>_technical_*.json` | **STOP** 必须有 IBKR 数据 |
| 个股新闻 | `data/news/<SYMBOL>/<SYMBOL>_news_*.json` | US: **STOP**(应有 Finnhub);非 US: 警告 + 标记 WebSearch 补 |
| 个股基本面 | 备餐水合的 `data/fundamentals/<SYMBOL>/<SYMBOL>_fundamentals_*.txt` | 缺 → 警告但继续, 标记 agent 可申报搜索补 |
| benchmark 量化 | `data/technical/<benchmark>/<benchmark>_technical_*.json` | **STOP** 没基准没法对比 |
| sector 量化(若有 sector) | `data/technical/<sector>/<sector>_technical_*.json` | **STOP**(海外 sector=null 时跳过此项) |
| sector 新闻(若有 sector) | `data/news/<sector>/<sector>_news_*.json` | US: 警告但继续(辩论时能跨层去重就行) |

#### CONTEXT_TYPE = etf 额外
| 类别 | 文件 pattern | 缺失处理 |
|---|---|---|
| ETF 量化 | `data/technical/<SYMBOL>/<SYMBOL>_technical_*.json` | **STOP** |
| ETF 新闻 | `data/news/<SYMBOL>/<SYMBOL>_news_*.json` | 警告但继续 |
| benchmark 量化 | `data/technical/<benchmark>/<benchmark>_technical_*.json` | **STOP** |

#### CONTEXT_TYPE = general 额外
| 类别 | 文件 pattern | 缺失处理 |
|---|---|---|
| 镜头量化 | US: `SPY/QQQ/DIA` 三镜头;EU: `DAX/IAEX` 双镜头(德+荷, 对齐 symbols.yaml);JP: `N225` | **STOP** 缺一不可 |

### STOP 时给用户的话(模板)

```
Pre-init 数据自检失败, 缺以下必需文件:
  - data/technical/NVDA/...  (个股量化, 无可恢复路径需要服务器 IBKR)
  - data/news/NVDA/...       (个股新闻, Finnhub key 服务器侧)

数据没齐 = 编排层不该 fire /run。先跑备餐:
  APEX_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py NVDA
输出 READY 再 /run NVDA。(本 skill 不连 IBKR、不 fetch; 备餐是 /run 之前的独立步骤。)
```

非 US 标的的"警告但继续"则直接进入 init,但在给三方 spawn prompt 时**显式列出缺失项**,提示 role-agent 第一轮就申报 `request_searches`。

## Step 1: 收集 raw_data 文件清单(不读、不概括)+ 新闻去重预处理

按 CONTEXT_TYPE 用 Glob 找出所有相关数据文件的**最新版本**。**不要把数据内容塞到 prompt 里**, agent 自己用 Read 读。

文件来源:
- 经济: **读备餐水合的运输文件**(🔀2026-07-09 文件为界: codex sandbox 连不上 PG/api, CC 对称) ——
  parent Read `data/economic/economic_<R>_<TS>.txt`(最新), 整段贴进每个 agent prompt 的
  `ECONOMIC SNAPSHOT` 段(role-agent 只有 Read/Glob, 但这文件是 prompt-ready 文本, parent 贴省事)。
  R 按标的 region(US/EU/JP), US 全场景必带。真相在 PG, 复现仍拿报告时间戳查 /api/economic。
- 情绪: `data/fear_greed/fear_greed_latest_*.json`
- 新闻原始三层: `data/news/<SYM>/<SYM>_news_*.json` + `data/news/<SECTOR>/...`(若有 sector) + `data/news/GENERAL/...`(宏观)
- 技术: `data/technical/<SYM>/...technical_*.json`
- 基本面 (stock): **读备餐水合的运输文件**(同 economic; 旧"parent 用 Bash 连 PG 注入"路线
  2026-07-09 废止, sandbox 里跑不了) —— parent Read
  `data/fundamentals/<SYM>/<SYM>_fundamentals_<TS>.txt`(最新)整段贴进 `FUNDAMENTALS SNAPSHOT`
  段; 文件不存在 = PG 无该公司基本面, 段内写 "(无基本面 —— agent 可在 request_searches 申报搜索补)"。
- benchmark / sector ETF 技术: 从 `config/symbols.yaml` 查 `symbol_contracts.<SYM>.benchmark` / `.sector` 然后读对应 technical
- `config/symbols.yaml` 本身也给 agent 看(它需要知道 sector / benchmark 路由)
- **父层报告(可选, 不强制; 2026-07-09 定)**: **判定权威在备餐/PG**——备餐查 PG debates, 父层
  报告 1h 内鲜才水合出 `data/debate/<PARENT>/<PARENT>_Analysis_<TS>.json`(文件只是运输载体,
  不是"碰巧有文件就用")。备餐水合了 → 附进 RAW DATA FILES 清单, 标注 `(父层结论, 可选参考 ——
  读不读由你判断, 不算必需数据)`; 备餐没水合(父层无报告/超龄) → 直接省, **不是缺失**。
  API 版默认带继承; agent 版辩手模型强、usage 金贵, 所以只"可有"。用了就把路径填进
  `parent_analysis_file`, 没用填 null。

### 新闻多层文件(V11.1 沿用,不写合并文件)

新闻按层独立(个股 / 板块 / 宏观),每层一个 `<SYM>_news_<TS>.json` 文件。**parent 不需要写 `_combined_` 合并文件** —— 让 agent 自己读 3 个,在 prompt 里提醒按 Finnhub `id` 去重(优先级 个股 > 板块 > 宏观,同 id 高层留低层删)。

### data_files 字典记录(**字段名严格固定, 不许擅自取名**)

`data_files` 是前端定位文件的索引, **任何擅自发明 key / 改 key 拼写都会让前端找不到文件**(空白板块)。**必须**用这套固定 schema:

```jsonc
{
  "macro": {
    // 🔴 经济(economic)不再进 data_files —— 走 PG, 报告靠 market_data_timestamp_utc 当 as_of 查 /api/economic 复现
    "fear_greed_us":   "data/fear_greed/fear_greed_latest_<TS>.json",       // US 永远在
    "news_general_us": "data/news/GENERAL/GENERAL_news_<TS>.json",
    // 区域(region != US 时, 文件存在才填; 不存在就别写这个 key, 不许填空串/null/伪路径):
    "fear_greed_eu":   "data/fear_greed/fear_greed_eu_latest_<TS>.json",
    "news_general_eu": "data/news/GENERAL_EU/GENERAL_EU_news_<TS>.json",
    "fear_greed_jp":   "data/fear_greed/fear_greed_jp_latest_<TS>.json",
    "news_general_jp": "data/news/GENERAL_JP/GENERAL_JP_news_<TS>.json",
    "fear_greed_kr":   "data/fear_greed/fear_greed_kr_latest_<TS>.json",
    "news_general_kr": "data/news/GENERAL_KR/GENERAL_KR_news_<TS>.json"
  },
  "benchmarks": {
    "<TICKER>": "data/technical/<TICKER>/<TICKER>_technical_<TS>.json"      // key = benchmark ticker 大写, 如 "SPY" / "QQQ" / "FTSE"
  },
  "sector": {                                                                // sector=null 时此对象为 {} (不要省略整个 key)
    "<TICKER>":      "data/technical/<TICKER>/<TICKER>_technical_<TS>.json", // key = sector ETF ticker 大写
    "<TICKER>_news": "data/news/<TICKER>/<TICKER>_news_<TS>.json"            // 同 ticker + "_news" 后缀
  },
  "symbol": {                                                                // stock / etf 时
    // 🔴 基本面(fundamentals)不再进 data_files —— 走 PG(parent 注入), 同 economic 待遇
    "technical":    "data/technical/<SYM>/<SYM>_technical_<TS>.json",
    "news":         "data/news/<SYM>/<SYM>_news_<TS>.json"                   // 没有就别写这个 key
  },
  "config": "config/symbols.yaml"                                            // 字符串, 不是对象
}
```

GENERAL 场景:`symbol` 子键改成 `lenses`(对象, key = 镜头 ticker), `benchmarks` / `sector` 留空对象。

**铁律 (违 = 前端坏)**:
- **key 必须用上面列出的**, **不许**自创 `news_de` / `fundamentals_alpha` / `RHM_sector` / `fear_greed_eu_proxy` 这类
- **value 必须是真实存在的本地路径** (Step 8a 会 Bash 验证;不存在就**别填这个 key**,不要填 `null` / 空串 / 编一个不存在的路径凑)
- 文件名 TS 后缀必须用**真实文件名**(Glob 拿出来,**不要**手敲 `<TS>` 或编 `_LATEST`)
- 海外标的常见错:文件不存在时**编一个 EU/JP 路径**填进去 —— 宁可省 key 也不要编

非美股例子(RHM, EU, sector 借 US ITA, 没 EU 区域文件):
```json
{
  "macro": { "fear_greed_us": "...", "news_general_us": "..." },
  "benchmarks": { "SPY": "..." },
  "sector": { "ITA": "...", "ITA_news": "..." },
  "symbol": { "technical": "...", "news": "..." },
  "config": "config/symbols.yaml"
}
```
注:经济(economic)与基本面(fundamentals)**整类不进 data_files**(都走 PG);这里也没写 `fear_greed_eu` 因为本地确实没该文件 → 直接省 key,前端不会因为缺 key 报错,但会因为路径乱编报错。

## 🔴 File-Backed Buffer 铁律(所有 Step 必须遵守)

**问题**:Claude(parent)在 context 增大时会**潜意识压缩历史**,即使 spec 说不许。导致最终 debate_history 缺失/篡改。

**对策**:**每轮 spawn 收到响应后,立即 Bash append 到本地 _inflight 文件**,最终落盘**从文件读不从 context memory 读**。

### Buffer 文件清单

两个 JSONL 文件,每个 `/run` 各自维护:

```
data/debate/<SYMBOL>/_inflight_<DATA_TIMESTAMP>.jsonl       辩论轮次原文 (Z/R/F 完整 JSON)
data/evidence/<SYMBOL>/_inflight_evidence_<DATA_TIMESTAMP>.jsonl  parent 查证 log (FRESH_SEARCHES 原始结果)
```

### 写入规则(每轮强制)

- 收完 Round 1 (init) 3 份 JSON → 立即 Bash 追加 3 行(每行一个 JSON 对象):
  ```jsonl
  {"round": "Init", "role": "Zealot", "content": {...完整 JSON 原文不剪辑...}}
  {"round": "Init", "role": "Reaper", "content": {...}}
  {"round": "Init", "role": "Fulcrum", "content": {...}}
  ```
- 跑完 Step 3 search → 立即 append 一行到 evidence buffer:
  ```jsonl
  {"round": "Init", "queries": ["...","..."], "results": ["原文1...","NOT_FOUND",...]}
  ```
- 后续轮同理,**绝不批量延迟,每轮 N 完成立即 append**

### 落盘时(Step 5/7)的读取规则

- 写 draft 终报、构建 debate_history 字段、构建 Evidence 文件:**100% 从 _inflight.jsonl 读**,**禁止**从 context memory 复述
- 读法:`Bash cat _inflight.jsonl` → parse 每行 JSON → 拼成 debate_history list / evidence list
- 若 _inflight 文件丢失或损坏(不应该发生),**立即 STOP 报告用户**, 不许用 context 凑

### 完成清理

写完 Analysis + Evidence + 写 PG debates 后,_inflight 文件**重命名**为 `.complete.jsonl`(保留作复盘日志),不要删:
```bash
mv data/debate/<SYM>/_inflight_<TS>.jsonl data/debate/<SYM>/_inflight_<TS>.complete.jsonl
mv data/evidence/<SYM>/_inflight_evidence_<TS>.jsonl data/evidence/<SYM>/_inflight_evidence_<TS>.complete.jsonl
```

---

## Step 2: Round 1 (init)

**模型分工(2026-07-09 定)**: 三个辩手 sub-agent 用 **sonnet**(`.claude/agents/*.md` frontmatter
已设, spawn 时自动生效); **你(parent)保持 opus** —— 你兼证据官/史官/轮次裁判/信息传递, 是全场
判断力所在。辩手有立场、任务单纯(读数据+表态+申报搜索), sonnet 够用且省 usage。

**一条消息**内并发 spawn 3 个 sub-agent(必须真 spawn,**绝不**自己代笔):

每个 prompt 长这样(按 CONTEXT_TYPE 注入 `prompts/cc/tasks/raw_data_<stock|etf|general>.md` 的内容):

```
TASK: init
SYMBOL: <X>
CONTEXT_TYPE: <X>
TURN: 1

--- TASK FRAMING ---
<这里把 prompts/cc/tasks/raw_data_<CONTEXT_TYPE>.md 的内容原样塞进来>
(这块包含: 任务铁律 / 分层逻辑 / 横向对比义务 / GENERAL 三镜头 等关键引导)

--- RAW DATA FILES (read these with Read tool, 按上面 framing 里列的清单) ---
- data/technical/NVDA/NVDA_technical_20260508T235900Z.json   (自身技术)
- data/technical/QQQ/QQQ_technical_20260508T235900Z.json     (benchmark)
- data/technical/SMH/SMH_technical_20260508T235900Z.json     (sector)
- data/news/NVDA/NVDA_news_*.json                            (自身新闻)
- data/news/SMH/SMH_news_*.json                              (sector 新闻)
- data/news/GENERAL/GENERAL_news_*.json                      (宏观新闻)
- data/fear_greed/fear_greed_latest_*.json                   (情绪)
- config/symbols.yaml                                         (路由信息)

注:三层新闻可能 Finnhub `id` 重复, 你按"个股 > 板块 > 宏观"优先级去重即可
注:经济与基本面都不在文件清单 —— 见下方两个 SNAPSHOT 段(parent 已从 PG 注入, agent 直接用)

--- ECONOMIC SNAPSHOT (parent 从备餐水合文件 data/economic/economic_<R>_<TS>.txt 贴入) ---
<parent Read 该文件整段贴这里(prompt-ready 文本, 每条 region/indicator/value/consensus/unit/
 release_time, 前值/超预期 agent 自行从多行历史判断)。
 文件缺 → 写 "(宏观缺失 —— agent 可在 request_searches 申报搜索兜底)">

--- FUNDAMENTALS SNAPSHOT (stock 场景才有; parent 从备餐水合文件贴入, 见 Step 1) ---
<parent Read data/fundamentals/<SYM>/<SYM>_fundamentals_<TS>.txt 整段贴这里;
 文件缺 → 写 "(无基本面 —— agent 可在 request_searches 申报搜索补)">

--- DEBATE HISTORY ---
(空, 这是第 1 轮)
```

(parent 一开始就 Read 三个 framing 文件之一缓存进自己 context, 后续每轮 spawn 复用同一份, 不用每轮重读。)

收 3 份 JSON 响应,解析校验:
- 必有: `debate_intensity / action / operation_type / operation_target / operation_volume / mentality / summary_statement / analysis_text / request_searches`
- 校验失败 → 重 spawn 一次,仍失败用占位继续

把每份**完整**响应存进内存 `debate_history[1] = {zealot: <full JSON>, reaper: <full JSON>, fulcrum: <full JSON>}`。**不要截断、不要摘要、不要二次加工**。

## Step 3: 处理 request_searches → FRESH_SEARCHES (含 parent 主动加查)

### 3a. 处理三方申报的查询

聚合三方的 `request_searches`,**去重**(按字面 + 语义近似)。

对每个去重后的查询:
- 你**中性**调用 WebSearch / WebFetch(不带任何立场,例如 "AMD 2026 Q1 数据中心营收" 而不是 "找证明 AMD 落后")
- 评估 ROI:如果是琐碎查询 / 与 raw_data 重复,标 `SKIPPED: out of scope`
- 如果搜不到:标 `NOT_FOUND`(不要编造)
- 搜到 → **保留原始结果文本**(不要摘要 / 二次加工), 并尽量保留/标注信息的**发布时间**(三方判定价程度的原料)

### 3b. parent 主动加查(可选, 推动辩论用)

你不只是被动响应 request_searches —— 看到下面情况可以**主动 spawn 一两条查询**塞进 FRESH_SEARCHES:
- raw_data 或新闻里某事件的关键细节缺失, 三方都没要求查但显然会影响判断
- 三方都模糊提到某主题(例如"AMD 竞争"), 但没人具体引用最近季报数据 —— 你主动查给他们当事实基础
- 某个潜在催化(财报日 / 监管节点 / 大佬动作)三方未提及, 但你判断与本标的高度相关
- 三方陷入立场重复, 缺乏新数据 → 你主动找一条新闻 / 新角度的事实, 看能不能撬动死结

**主动加查的边界**:
- 仍然**严格中性**(不预设结论)
- 标记为 `[Q-PARENT]` 与 agent 申报的 `[Q1]/[Q2]` 区分(可追溯)
- 别一轮加超过 2 条, 否则会变成 parent 主导辩论(违背"三方独立"设计)
- 找到的内容也走 FRESH_SEARCHES 三方共享, 不偏袒任何一方

整理成 `FRESH_SEARCHES` 段(每条查询保留原始结果):
```
FRESH_SEARCHES (parent 中性查证):
[Q1] AMD 2026 Q1 数据中心营收
RESULT:
<WebSearch 返回的原文, 完整保留>

[Q2] ServiceNow Project Arc NVIDIA 联合产品
RESULT:
NOT_FOUND

[Q3] 近期半导体板块资金流出数据
RESULT:
<WebSearch 返回的原文, 完整保留>
```

**同时**,把 parent 这一轮跑的有价值查证记录追加到内存的 `parent_evidence_log`(简短索引项, 后面
进报告 `evidence_added` 用, 形状见 `prompts/cc/formats/evidence_output.md`)。**证据档案本体**
(Scout 形状)不在这里维护 —— Step 7 从 `_inflight_evidence` buffer 整理(原始查询+结果都在 buffer
里, 别靠 context 记忆)。

## Step 4: Round 2..N

每轮你自己判断 CONTINUE/TERMINATE。

**通常的参考(不是硬规)**:
- 前几回合通常需要充分辩论 —— 三方还在引入新论据 / 申报 request_searches / 立场仍在演变, 就继续
- 单轮 analysis_text **参考指数 1500 字** —— 比这短得多通常说明欠展开, 仍有空间继续
- 三方真达成共识(立场贴近 + 没新论据可挖)再 TERMINATE
- 通常 6-12 轮可收敛, 超过 15 轮基本是死结
2. 若 CONTINUE,**一条消息**并发 spawn 3 个新 Agent。每个 prompt 把 history **完整原样** 塞进去, 不要摘要、不要剪辑:

```
TASK: debate
SYMBOL: <X>
CONTEXT_TYPE: <X>
TURN: <N>

--- TASK FRAMING ---
<同 Round 1 的 framing>

--- RAW DATA FILES (read these with Read tool) ---
<同 Round 1 的文件清单>

--- DEBATE HISTORY ---
[Round 1]
Zealot:
<Round 1 zealot 的完整 JSON 响应原文, 不要简化>

Reaper:
<Round 1 reaper 的完整 JSON 响应原文>

Fulcrum:
<Round 1 fulcrum 的完整 JSON 响应原文>

FRESH_SEARCHES (parent 在 Round 1 后查证):
[Q1] <查询>
RESULT:
<WebSearch 返回原文, 完整保留>

[Q2] ...

[Round 2]
Zealot:
<Round 2 zealot 完整 JSON 原文>
...
```

3. 收响应,聚合新 request_searches,跑搜索,准备下一轮 FRESH_SEARCHES。每轮的三方完整 JSON 都附进 history,**永不摘要**。

**关于 spawn 下一轮时 history 长度的处理**:
- **默认全文塞**(对应 Step 7 的 mechanical verbatim 落盘版本)
- 只有当 prompt 真的撑爆(例如已经 8+ 轮且 context 接近上限)才考虑压缩,**且只删以下三类**(继承 V11.1 chronicler_moderation 的 round_memory 规则):
  * 寒暄客气("感谢对方的分析"等)
  * 纯修辞性的形容词堆砌(不影响论据成立的)
  * 对前几轮已复述过内容的重复铺垫
- **其他一律保留**:核心论据、数据引用、action / operation 数值、对对方的回应、未来判断 / 风险量化 / 具体价格的论述
- **绝对禁止**:改变原意 / 补充新内容 / 二次解读 / 用自己的话重组(即使你觉得更清晰)
- 拿不准是否冗余的 → 一律保留

**注**:这条压缩规则只针对 spawn 下轮 prompt 的"传输精简",**不影响 Step 7 落盘**。落盘永远 verbatim。

## Step 5: 写 draft 终报(你自己,无 chronicler agent)

辩论 TERMINATE 后,基于完整 `debate_history` + 最后一轮三方立场,你按 `prompts/cc/formats/final_report_output.md` 的 schema **自己**写出终报 JSON 草稿。

**关键约束**(你执行的史官原则):
- evidence 必须来自辩论中三方提及内容,严禁编造
- action 反映真实综合,避免 50/65/70 整齐数(老 V11.1 注意事项)
- 包络线原则:最终 action 不超出三方最终立场区间
- 不预设立场:你不是 Zealot/Reaper/Fulcrum,只综合三方所言
- `dissent` 块的 `final_action` / `reservation` **暂时填三方最后一轮的 action 和 summary,等签章后会被覆盖**

## Step 6: signoff (再 spawn 一次 Z/R/F)

**一条消息**内并发 spawn 3 个新 Agent,`TASK: signoff`。每个 prompt 必须含**完整 debate_history + draft 终报全文**(Z/R/F 是 fresh Opus, 没有上一轮记忆, 不给完整 history 它们没法 informed signoff):

```
TASK: signoff
SYMBOL: <X>
CONTEXT_TYPE: <X>

--- TASK FRAMING ---
<同辩论阶段, 来自 prompts/cc/tasks/raw_data_<CONTEXT_TYPE>.md>

--- RAW DATA FILES ---
<同辩论阶段的文件清单>

--- DEBATE HISTORY (全部轮次原文) ---
[Round 1]
Zealot: <完整 JSON 响应>
Reaper: <完整 JSON 响应>
Fulcrum: <完整 JSON 响应>

FRESH_SEARCHES (parent 在 Round 1 后查证):
[Q1] ...
RESULT: ...

[Round 2]
...
[最后一轮]
...

--- DRAFT FINAL REPORT (parent 综合写的) ---
<完整终报 JSON pretty-printed>
```

收 3 份 signoff JSON `{final_action, reservation}`,合并到 draft 终报的 `dissent` 块:
- `dissent.zealot_final_action` ← signoff.final_action
- `dissent.zealot_reservation` ← signoff.reservation
- (同理 reaper / fulcrum)

## Step 7: 补 metadata wrapper + 写盘

最终终报对象 = chronicler-style fields + metadata 字段:
- `market_data_timestamp_utc` = DATA_TIMESTAMP
- `report_generated_at` = 现在的 UTC 时间(`2026-05-09T13:42:15Z` 这种格式)
- `parent_analysis_file` = `null`(默认无 inheritance;若 role-agent 在某轮 Read 了父层报告,从其响应推断)
- `data_files` = Step 1 记录的字典
- **证据四件套(2026-07-09 与 API/codex 版统一, 规范见 `prompts/cc/formats/evidence_output.md`)**:
  - `evidence` = Scout 形状证据 list(从 `_inflight_evidence` buffer 整理, 同时也是 Evidence 文件内容)
  - `evidence_added` = 简短索引 list(来自 parent_evidence_log)
  - `evidence_generated_at` = 证据生成时刻 UTC ISO(= PG evidence 存档键, 前端复现查询用)
  - `search_rounds_used` = 真实触发搜索的轮数(如实计数)
- `debate_history` = 完整轮次 list,每条 `{role, round, content}`
  - `role`: `"Zealot"` / `"Reaper"` / `"Fulcrum"` (首字母大写)
  - `round` 命名约定 (沿用 V11.1, **前端兼容必需**):
    - 第 1 轮 (init) → `"Init"`
    - 第 2 轮 → `"回合1"` (中文字符串, 不是数字)
    - 第 3 轮 → `"回合2"`, ..., 第 N 轮 → `"回合<N-1>"`
  - `content`: 该 agent 当轮返回的**完整 JSON 原文**(含 `preliminary_mentality` / `request_searches` 等所有字段)

**🔴 落盘铁律(违反 = 报告作废)**:`debate_history` 必须 **mechanical verbatim** —— agent 返回啥就存啥,**任何压缩 / 摘要 / 截断 / 二次加工都禁止**。哪怕 analysis_text 1 万字也原样存。这是事后复盘的唯一依据,失去就再也回不来。

写两个文件:

**Analysis(主报告,前端兼容)**:
```
data/debate/<SYMBOL>/<SYMBOL>_Analysis_<DATA_TIMESTAMP>.json
```

**Evidence(持久证据档案, 2026-07-09 起 Scout 形状)**:
```
data/evidence/<SYMBOL>/<SYMBOL>_Evidence_<DATA_TIMESTAMP>.json
```
内容 = 报告 `evidence` 字段的同一份 Scout list(task/simple/detailed/source), 从
`_inflight_evidence` buffer 整理, **格式细则全在 `prompts/cc/formats/evidence_output.md`**
(【结论】【原文】【来源】三元组、原文保原语言、来源纪律、诚实规则)。老 compact
`[{"item","as_of"}]` 形状**不再是文件内容**(它只作报告里的 `evidence_added` 索引)。

## Step 8: data_files 自检(🔀 2026-07-09 起本 skill **不写 PG**)

**文件为界架构**: 你(agent 子进程)的职责到"本地 JSON 落地 + 自检"为止 —— **不连 PG、不跑写库
脚本**(codex 版 sandbox 网络隔离连不了 PG, CC 版为两引擎对称也不写)。PG 写库(debates+evidence)
由**沙盒外的编排层**(cc_loop / codex_loop / 对话 CC)在收到你的 ok 回执后跑统一脚本完成:
`./venv/bin/python skills/apex-run-debates-cc/scripts/pg_store_debate.py <SYM>`(一条命令双写)。
所以这步只剩一件事: 自检 data_files 路径真实存在。

### 8a. data_files 路径反查(本地自检)

**铁律**:前端经常报"基本面 / 技术 / 新闻没数据",根因常是 Analysis JSON 的 `data_files` 字段写了**不存在**或**拼错**的路径(前端按这个字段去服务器找文件,找不到就空白)。

写完 Analysis 后,**逐个 Bash 验证 `data_files` 字典里所有路径在本地真实存在**:

```bash
python -c "
import json, os, sys
d = json.load(open('data/debate/NVDA/NVDA_Analysis_20260508T235900Z.json', encoding='utf-8'))
missing = []
for section, files in d.get('data_files', {}).items():
    if isinstance(files, dict):
        files = list(files.values())
    if isinstance(files, str):
        files = [files]
    for f in files or []:
        if f and not os.path.exists(f):
            missing.append((section, f))
print('MISSING:' if missing else 'OK')
for s, f in missing:
    print(f'  [{s}] {f}')
sys.exit(1 if missing else 0)
"
```

有缺失 → **修 `data_files` 路径**(常见错:把 `*_LATEST.json` 写进去了,实际是带 TS 后缀的文件;或新闻写了 sector 但 sector=null)。修完重写 Analysis,再继续。

> 注:CC 版终报仍**双落**——本地 `data/`(你写) + PG `debates`+`evidence`(编排层写库脚本写,
> 键=(symbol, market_data_timestamp_utc); 脚本签名 `pg_store_debate.py <SYMBOL> [path]`,
> **第一个参数是 SYMBOL 别漏**)。你在 summary 里提醒编排层跑写库即可, 自己不碰 PG。

## Step 9: 给用户简短 summary

≤ 250 字,关键数据点直接说:

```
=== <SYMBOL> 辩论完成 ===
ROUNDS: 5
ACTION: 63 (MARKET_ENTRY @ $172.50, PILOT_SIZE)
KEY: <statement, 一句话>
NEXT_REVIEW: 2026-05-15 (weekly)
DISSENT: Z=78 / R=42 / F=60 (spread=36)
SEARCHES: 8 条 parent 中性查证 (其中 2 条 NOT_FOUND)
FILES:
  - data/debate/NVDA/NVDA_Analysis_20260508T235900Z.json
  - data/evidence/NVDA/NVDA_Evidence_20260508T235900Z.json
```

## 出错容忍

- Round 中某 agent JSON parse 失败 → 重 spawn 一次, 仍失败用占位 (action=50, summary="parse_error") 并 log warning
- WebSearch 失败 → 标 NOT_FOUND, 不阻塞辩论
- 写盘失败 → 报告给用户, Analysis 优先于 Evidence
- 数据缺/旧 → 本 skill 不碰盈透不 fetch; 指回备餐: 先 `APEX_IBKR_PORT=4001 ./venv/bin/python prepare_debate_data.py <SYM>` 出 READY 再 /run
