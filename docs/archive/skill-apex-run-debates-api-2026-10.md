---
name: apex-run-debates
description: >
  deepseek/API 版辩论驱动器(run_one_debate.py / init_all_debates.py) —— **已退役, 基本不再维护**。
  只在用户点名 "用 api 版/代码版/ds 版跑", 或需要历史回放(`--as-of`)时用。
  裸喊 "跑美股/跑日本/跑一下 NVDA" 都**不是**本 skill —— 那走当前 harness 的 agent 版
  (Codex → apex-run-debates-codex, Claude Code → apex-run-debates-cc), 调度见 apex-dispatch。
---

# apex-run-debates — API 版(退役)

> **本引擎已退役**, 更新滞后于 agent 版。默认辩论走 agent 版; 本文只为点名调用和历史回放保留。

跑在 `/opt/apex_quant`。单标的 `run_one_debate.py <SYM>`, 全量批 `init_all_debates.py`。

## 命令

```bash
cd /opt/apex_quant
APEX_IBKR_PORT=4001 ./venv/bin/python run_one_debate.py <SYM> [--search] [--force]
APEX_IBKR_PORT=4001 ./venv/bin/python run_one_debate.py <SYM> --as-of <UTC时刻>   # 历史回放
```
`--as-of` 语义: 跳盈透、按时刻读 PG 历史快照、**强制关证据搜索**(无 point-in-time 回放)、
**不回写** `debate_schedule.yaml`。

标的之间无执行顺序依赖, 想并发就并发(clientId 每进程随机, 单 gateway 上并发安全)。

## 退出码 / STATUS

`0 ran` · `10 skipped-no-market-data`(盈透挂 / 无新 bar / 无价) · `12 skipped-fresh-exists`(加 `--force` 重跑) ·
`20 error` · `2 usage`。

**盈透挂的判定要保守**: 单个 exit 10 不算挂(可能只是无新 bar/周末), 照常汇报; 同一波 ≥2 个不同标的
连续 exit 10, 才判挂 → 停下找用户。

## 要点

- **输出只进 PG**。成功日志 `[debate_pg] ✅ <SYM> @ <ts> 写入 debates` + `STATUS: ran`。
  `data/debate/<SYM>/` 下**没有**新 JSON 是正确的, 不是 bug。
- **`runnable: false` 只挡自动调度器**, 不挡点名跑。EU/JP 标的(无 IBKR 订阅)点名照跑, 盈透给延迟盘中数据, 够用。
- **JP/EU 必须带 `--search`**(打开 `APEX_EVIDENCE_SEARCH`)——Finnhub 免费新闻不覆盖它们, 新闻只能靠 grounding 搜索。
  US ETF/GENERAL 默认自搜; 到处都带 `--search` 无害。
- EU/JP 标的**不在 `config/debate_schedule.yaml` 里**, 所以会看到
  `[schedule_writeback] <SYM> 不在排班表, 不回写` —— 预期行为, 无 next_review。

## 前置检查

```bash
ss -ltn 2>/dev/null | grep -E '4001|4002' || echo "IBKR GATEWAY DOWN"
```
端口 **4001** = live gateway。没有 gateway 则每次都 exit 10。盈透故障处理见 `apex-manage-ibkr`。

## 跑完验收

```bash
cd /opt/apex_quant
for s in <SYMS>; do f=$(ls -t logs/run_*_${s}_*.log 2>/dev/null|head -1);
  echo -n "$s: "; grep -oE 'STATUS: [a-z-]+' "$f"|tail -1; done
```
都应是 `STATUS: ran`。`skipped-fresh-exists` → 加 `--force`; `skipped-no-market-data` → 查 gateway / 该市场有没有新 bar。

## ds 版复盘

```bash
APEX_IBKR_PORT=4001 ./venv/bin/python run_postcheck.py --dt 3|20|50 --latest [--symbols A,B] [--start YYYY-MM-DD]
```
`--dt` = 持有期档位(天, 按**美股交易日**计), 必填, 会带进 `postcheck` 表的 `dt` 列。
`--symbols` 缺省全量, `--latest` = 每标的只取最新一份。
复盘默认走 agent 版(`apex-postcheck-codex` / `apex-postcheck-cc`), 本命令仅在点名"用 ds 版复盘"时用。
