#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_skill_stubs.py — 从 skills/<name>/SKILL.md 生成各 harness 的 skill 路标。

skill 的权威正文只有一份, 在工程中立层 `skills/<name>/SKILL.md`。各 harness 的 skill 目录(仓库里的
`.claude/skills/`, 服务器上的 `/root/.claude/skills/`、`/root/.codex/skills/`、`/root/.codex-2/skills/`、`/root/.codex-3/skills/`)
只放路标: frontmatter(name 与 description, 供 harness 命中)原样照抄, 正文一句话指回权威文件。
清单就是 `skills/` 下有 SKILL.md 的目录, 不另外维护名单。

用法:
    python scripts/gen_skill_stubs.py                 # 生成仓库内 .claude/skills/(相对路径指路), 清掉多余的旧路标
    python scripts/gen_skill_stubs.py --check         # 只比对, 过期或有多余路标则 exit 1(测试用)
    python scripts/gen_skill_stubs.py --out /root/.codex/skills --root /opt/equipoise [--prune]
                                                      # 生成到别的 skill 目录, 用绝对路径指路;
                                                      # --prune 同时删掉该目录下已不在清单里的旧路标
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
REPO_OUT = ROOT / ".claude" / "skills"
MARK = "由 `scripts/gen_skill_stubs.py` 生成"


def _frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("缺 frontmatter")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return "\n".join(lines[: end + 1])


def sources() -> dict[str, str]:
    """{skill 名: frontmatter 原文}。"""
    out = {}
    for p in sorted(SKILLS.glob("*/SKILL.md")):
        out[p.parent.name] = _frontmatter(p.read_text(encoding="utf-8"))
    return out


def stub_text(name: str, frontmatter: str, root: str) -> str:
    base = f"{root.rstrip('/')}/skills/{name}" if root else f"skills/{name}"
    return (
        f"{frontmatter}\n\n"
        "# 路标(权威正文在工程中立层, 勿在此写内容)\n\n"
        "本文件只指路, 不含操作内容。被本 skill 命中时: **立即完整 Read 下面的权威正文并照它执行**, "
        "不凭本路标或记忆行动。\n\n"
        f"- 正文: `{base}/SKILL.md`\n"
        f"- 同目录的 `references/` 与 `scripts/` 按正文的指引取用。\n\n"
        f"本文件{MARK}, 不要手改; 改内容只改正文。\n"
    )


def _is_stub(path: Path) -> bool:
    """认得出是路标的才允许删: 新版带生成标记, 旧版带"路标 stub"字样。"""
    f = path / "SKILL.md"
    if not f.exists():
        return False
    text = f.read_text(encoding="utf-8", errors="replace")
    return MARK in text or "路标 stub" in text


def stale(out: Path, root: str) -> list[str]:
    problems = []
    want = sources()
    for name, fm in want.items():
        f = out / name / "SKILL.md"
        if not f.exists() or f.read_text(encoding="utf-8") != stub_text(name, fm, root):
            problems.append(f"{name}: 缺失或过期")
    if out.exists():
        for d in sorted(p for p in out.iterdir() if p.is_dir()):
            if d.name not in want and _is_stub(d):
                problems.append(f"{d.name}: 多余的旧路标")
    return problems


def generate(out: Path, root: str, prune: bool) -> list[str]:
    done = []
    want = sources()
    for name, fm in want.items():
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(stub_text(name, fm, root), encoding="utf-8", newline="\n")
        done.append(f"生成 {d / 'SKILL.md'}")
    if prune and out.exists():
        for d in sorted(p for p in out.iterdir() if p.is_dir()):
            if d.name not in want and _is_stub(d):
                shutil.rmtree(d)
                done.append(f"删除旧路标 {d}")
    return done


def main(argv: list[str]) -> int:
    out, root = REPO_OUT, ""
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        root = str(ROOT)
    if "--root" in argv:
        root = argv[argv.index("--root") + 1]
    if "--check" in argv:
        problems = stale(out, root)
        if problems:
            print("skill 路标与正文不一致: " + "; ".join(problems))
            return 1
        print("skill 路标与正文一致")
        return 0
    prune = "--prune" in argv or out == REPO_OUT   # 仓库内的路标目录完全由本脚本管
    for line in generate(out, root, prune):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
