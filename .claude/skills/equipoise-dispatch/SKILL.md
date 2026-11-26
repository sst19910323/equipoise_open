---
name: equipoise-dispatch
description: >
  The dispatch entry point for Equipoise debates and postchecks. Use when the user expresses the intent to run, update, take a look at, or start a stock market region
  (US / Europe / Japan / Korea / Japan-Korea), or names symbols, asks for a region's full set, or asks for a postcheck.
  Does not depend on "update" or any other fixed command word; infer the mode from the action intent, the region and the quantity modifiers.
  This document covers three things: which symbols to pick, whether IBKR is up, and how to report once done. How to launch is in equipoise-run-debates; postcheck is in equipoise-postcheck.
---

# 路标(权威正文在工程中立层, 勿在此写内容)

本文件只指路, 不含操作内容。被本 skill 命中时: **立即完整 Read 下面的权威正文并照它执行**, 不凭本路标或记忆行动。

- 正文: `skills/equipoise-dispatch/SKILL.md`
- 同目录的 `references/` 与 `scripts/` 按正文的指引取用。

本文件由 `scripts/gen_skill_stubs.py` 生成, 不要手改; 改内容只改正文。
