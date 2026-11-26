---
name: equipoise-postcheck
description: >
  Postcheck (shared by both engines). Use when the user says "run postcheck / run the postcheck / postcheck NVDA / postcheck US / continue postcheck".
  The main session itself acts as the reckoner: judges whether each mature debate report was wrong-footed, missed or hit, gives one composite score, writes the attribution, and stores it in PG postcheck.
  Does not spawn subagents; resumable across sessions via the worklist. The scoring spec is in orchestration/postcheck-protocol.md.
---

# 路标(权威正文在工程中立层, 勿在此写内容)

本文件只指路, 不含操作内容。被本 skill 命中时: **立即完整 Read 下面的权威正文并照它执行**, 不凭本路标或记忆行动。

- 正文: `skills/equipoise-postcheck/SKILL.md`
- 同目录的 `references/` 与 `scripts/` 按正文的指引取用。

本文件由 `scripts/gen_skill_stubs.py` 生成, 不要手改; 改内容只改正文。
