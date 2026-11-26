# 上古 API 时期的残余(库主 2026-10-25: 逐步删, 目前不管, 先归到这一处)

这些文件属于退役的 API 版辩论 / 代码版复盘 / Linkup 证据官时代。现在的主干是 agent 版(Codex / CC 主会话 + run_step + PG), 不依赖它们, 除了下面标注"仍在用"的那几个入口。**不要再往这些文件上加新路径**; 真要删的时候按这张表, 先 grep 确认没有活引用。

| 文件 | 状态 |
|---|---|
| `run_one_debate.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `init_all_debates.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `run_postcheck.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `run_debate.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `backfill_debates.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/nodes.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/llm_client.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/prompt_manager.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/xml_response_parser.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/xml_final_report_parser.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/state.py` | 退役, 只在库主点名回放时用(见 skills/equipoise-run-debates/references/replay.md) |
| `workflows/linkup_search.py` | Linkup / API 证据官时代; 10-25 晚起区域新闻腿也不再调它(备餐不再跑 fetch_eu_jp_news --region), 已无活引用, 可删 |
| `workflows/evidence_officer.py` | Linkup / API 证据官时代; 10-25 晚起区域新闻腿也不再调它(备餐不再跑 fetch_eu_jp_news --region), 已无活引用, 可删 |
| `workflows/evidence_officer_linkup.py` | Linkup / API 证据官时代; 10-25 晚起区域新闻腿也不再调它(备餐不再跑 fetch_eu_jp_news --region), 已无活引用, 可删 |
| `workflows/review_scout.py` | Linkup / API 证据官时代; 10-25 晚起区域新闻腿也不再调它(备餐不再跑 fetch_eu_jp_news --region), 已无活引用, 可删 |
| `workflows/review_scout_linkup.py` | Linkup / API 证据官时代; 10-25 晚起区域新闻腿也不再调它(备餐不再跑 fetch_eu_jp_news --region), 已无活引用, 可删 |
| `prompts/api/` | API 版 prompt 与 v1 三方人格; 回放用 |
| `prompts/legacy/` | API 版 prompt 与 v1 三方人格; 回放用 |
| `fetch_eu_jp_news.py(仅 Linkup 区域腿与封存的 Gemini 腿; --load 灌库那半仍在用)` | 仍在用的部分见括号 |
| `horizon_sentinel.py(API 版调度器; 只当库用: IbkrFetcher / _tech_text_for)` | 仍在用的部分见括号 |

欧日韩新闻(区域大盘与个股)从 v3.3 起只走 agent 腿 `/refresh-news <EU|JP|KR|SYM>`(主会话自带搜索: CC=opus, GPT=sol), Linkup 彻底不用; `fetch_eu_jp_news.py` 只剩 `--load` 灌库那半在用。
