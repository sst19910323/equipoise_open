"""
报告多语言 (2026-10-24 起)。

库主定的方向: 以后报告原文是英文, 生成后用最便宜的模型把"报告正文"(不长)翻成中文及以后的语言;
辩论过程不翻。老数据(2026-10-24 前)原文是中文, 只有中文版, 英文版之后再补。

存储 (debates 表):
  lang         text  NOT NULL DEFAULT 'zh'   原文语言(report jsonb 本身是什么语言)
  report_i18n  jsonb NOT NULL DEFAULT '{}'   译文覆盖层 {"en": {<字段>: <整棵译后子树>, "_meta": {...}}, "zh": {...}}
               覆盖层只放 TRANSLATABLE_FIELDS 里的顶层键, 每个键是整棵子树(数字/枚举照抄原文),
               读时按顶层键整体替换。原文 report 永远不改。

读 (API ?lang=xx):
  请求语言 == 原文语言 → 原文; 覆盖层里有 → 原文 + 覆盖; 都没有 → 回退原文。
  响应里带 lang_served(实际给的语言) / lang_source / lang_available, 前端据此显示"暂无译文"之类。

翻译范围 (库主 2026-10-24 口径):
  翻: 结论与给人看的正文 —— TRANSLATABLE_FIELDS。
  不翻: 辩论过程及其附属 —— debate_history / debate_summary / signatures / model_calls(原 tasks) /
        dissent(Zealot's/Reaper's Reservation 是人格签名, 只有原文) /
        arbiter_* / objections / evidence* / provisional_ruling / residual_adjustment 等。
        读时只认 TRANSLATABLE_FIELDS 里的键, 覆盖层里多出来的(如早先译过的 dissent)一律不用。
  子树里也不动: 本来就是英文的串、枚举/协议值(如 short_term_path.direction 的 上行/下行/震荡,
        怎么显示归前端管)、数字、日期、代码。见 KEEP_PATHS 与 translator 的叶子规则。
  复盘表(postcheck)同样两列 lang / report_i18n(2026-10-25 主工程加), 只翻 verdict —— POSTCHECK_TRANSLATABLE_FIELDS。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, Optional, Tuple

SUPPORTED_LANGS = ("zh", "en")

# 接口默认语言。切英文那天改环境变量 EQUIPOISE_DEFAULT_LANG=en 并重启 8001, 不用改代码。
DEFAULT_LANG = (os.environ.get("EQUIPOISE_DEFAULT_LANG") or "zh").strip().lower()

TRANSLATABLE_FIELDS = (
    "statement",
    "reasoning",
    "risk_management",
    "known_flaws",
    "next_review",
    "short_term_path",
    "operation_execution",
    "mentality_basis",
)

# 复盘行(postcheck 表)只翻一句判词 verdict; diagnosis / lesion / search_request 不翻(库主 2026-10-24 晚: 复盘不包括详述)。
# localize_report / localize_field 读复盘行时把它当 fields 传入(datalayer/pg/postcheck_pg_store.localize_items)。
POSTCHECK_TRANSLATABLE_FIELDS = ("verdict",)

# 子树内保持原样的路径(点分, 列表下标用 *): 协议枚举 / 机器读的字段
KEEP_PATHS = (
    "short_term_path.group_refs",
    "reasoning.group_context.*.id",
    "reasoning.group_context.*.applies_to",
    "reasoning.key_drivers.*.group_refs",
    "reasoning.risks.*.group_refs",
    "short_term_path.direction",
    "short_term_path.target",
    "short_term_path.invalid",
    "reasoning.risks.*.impact",
    "reasoning.risks.*.probability",
    "reasoning.key_drivers.*.weight",
    "reasoning.key_drivers.*.category",
    "reasoning.key_drivers.*.direction",
    "known_flaws.*.disposition",
    "next_review.recommend_after",
    "next_review.frequency_hint",
    "next_review.structured_triggers",
    "next_review.expires_at",
    "next_review.events",
    "next_review.known_events",
    "next_review.known_events_note",
    "operation_execution.derived",
    "operation_execution.declared",
    "operation_execution.reference",
)

_CJK = re.compile(r"[㐀-鿿豈-﫿]")


# 叶子路径白名单(2026-11-03, 库主 + GPT): 整棵子树不进翻译, 只翻这些字符串叶子; 覆盖层存在 report_i18n.<lang>._leaves
# = {"<点分路径>": "<译文>"}(路径段里的 * 匹配任意一段: 字典键或数组下标, 镜头名如 SPY 也行)。
#   execution_card.one_line  执行卡一句话(组装器 / 接口 ensure_display_fields 生成, 原文英文)
#   scenario.nodes.*.label   节点明细的标签(辩手写的英文短语; 子分支同)
#   scenario.mixture.reason  裁决解释(引用编号保留)
#   unresolved_claims.*.text 辩到最后仍未让步的主张原文(展示副本; 辩论记录本身不翻)
LEAF_PATHS = (
    "hedge_summary",
    "downside_protection.basis",
    "downside_protection.review",
    "by_lens.*.hedge_summary",
    "by_lens.*.downside_protection.basis",
    "by_lens.*.downside_protection.review",
    "execution_card.one_line",
    "by_lens.*.execution_card.one_line",
    "scenario.nodes.*.label",
    "scenario.nodes.*.sub.*.label",
    "by_lens.*.scenario.nodes.*.label",
    "by_lens.*.scenario.nodes.*.sub.*.label",
    "scenario.mixture.reason",
    "by_lens.*.scenario.mixture.reason",
    "unresolved_claims.*.text",
)
_LEAF_PATTERNS = tuple(p.split(".") for p in LEAF_PATHS)


def leaf_allowed(parts) -> bool:
    parts = [str(x) for x in parts]
    for pat in _LEAF_PATTERNS:
        if len(pat) == len(parts) and all(a == "*" or a == b for a, b in zip(pat, parts)):
            return True
    return False


def collect_leaves(report: Dict) -> list:
    """[(path_tuple, text)] for every string leaf of the report whose path matches LEAF_PATHS (document order)."""
    out = []

    def walk(o, path):
        if isinstance(o, str):
            if path and leaf_allowed(path) and o.strip():
                out.append((tuple(path), o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + [k])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + [i])

    for top in ("execution_card", "by_lens", "scenario", "unresolved_claims", "hedge_summary", "downside_protection"):
        if top in report:
            walk(report[top], [top])
    return out


def _get_path(node, parts):
    for p in parts:
        if isinstance(node, list):
            if not str(p).isdigit() or int(p) >= len(node):
                return None, False
            node = node[int(p)]
        elif isinstance(node, dict):
            if p not in node:
                return None, False
            node = node[p]
        else:
            return None, False
    return node, True


def leaf_src_hash(text: Any) -> str:
    """Fingerprint of a leaf's source text, stored next to its translation (`_leaf_src`) so a translation made for an
    earlier wording (the execution card's one_line changed shape on 2026-11-03) is never shown over the new source."""
    import hashlib
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def leaf_is_stale(dotted: str, original_text: Any, leaf_src: Optional[Dict]) -> bool:
    """A leaf translation is stale when its recorded source hash differs from the current source; a leaf with no recorded
    hash is trusted — except the execution card's one_line, whose wording is generated code and changes with the card
    schema (a legacy one_line translation without a hash is for the old long form and is dropped)."""
    rec = (leaf_src or {}).get(dotted) if isinstance(leaf_src, dict) else None
    if rec:
        return rec != leaf_src_hash(original_text)
    return str(dotted).endswith("execution_card.one_line")


def apply_leaves(doc: Dict, original: Dict, leaves: Dict, leaf_src: Optional[Dict] = None) -> Dict:
    """Overlay `_leaves` onto a shallow copy of the report: only allowed paths, only where the original leaf is a string
    and the translation is not stale for it (`_leaf_src`, see leaf_is_stale); the top-level subtree touched is
    deep-copied once so the original report is never mutated."""
    import copy as _copy
    copied = set()
    for dotted, val in (leaves or {}).items():
        if not isinstance(val, str) or not val.strip():
            continue
        parts = str(dotted).split(".")
        if not leaf_allowed(parts):
            continue
        orig, ok = _get_path(original, parts)
        if not ok or not isinstance(orig, str):
            continue
        if leaf_is_stale(str(dotted), orig, leaf_src):
            continue
        top = parts[0]
        if top not in copied:
            doc[top] = _copy.deepcopy(doc.get(top))
            copied.add(top)
        parent, ok = _get_path(doc, parts[:-1])
        if not ok:
            continue
        last = parts[-1]
        if isinstance(parent, list):
            parent[int(last)] = val
        elif isinstance(parent, dict):
            parent[last] = val
    return doc


def has_cjk(s: Any) -> bool:
    return isinstance(s, str) and bool(_CJK.search(s))


def norm_lang(v: Optional[str]) -> str:
    """'zh-CN' / 'ZH' / 'en_US' → 'zh' / 'en'; 空 → DEFAULT_LANG。不认识的原样小写返回(调用方回退)。"""
    s = (v or "").strip().lower().replace("_", "-")
    if not s:
        return DEFAULT_LANG
    return s.split("-", 1)[0]


def detect_lang(report: Dict) -> str:
    """原文语言: 报告自报 lang 优先; 否则看 statement(没有就看 debate_summary)里有没有汉字。"""
    if isinstance(report, dict):
        v = report.get("lang")
        if isinstance(v, str) and norm_lang(v) in SUPPORTED_LANGS:
            return norm_lang(v)
        probe = report.get("statement") or report.get("debate_summary") or ""
        if probe:
            return "zh" if has_cjk(probe) else "en"
    return "zh"


def available_langs(source_lang: str, i18n: Optional[Dict]) -> list:
    out = [source_lang or "zh"]
    for k, v in (i18n or {}).items():
        if isinstance(v, dict) and k not in out:
            out.append(k)
    return out


def localize_report(report: Dict, source_lang: Optional[str], i18n: Optional[Dict],
                    lang: Optional[str], fields: Optional[Iterable[str]] = None) -> Tuple[Dict, str]:
    """返回 (给前端的报告, lang_served)。原 report 不被修改。fields = 认哪些顶层键(默认 TRANSLATABLE_FIELDS;
    复盘行传 POSTCHECK_TRANSLATABLE_FIELDS); 覆盖层里多出来的键一律不用。"""
    allowed = tuple(fields) if fields else TRANSLATABLE_FIELDS
    src = source_lang or detect_lang(report)
    want = norm_lang(lang)
    if want == src:
        return report, src
    overlay = (i18n or {}).get(want)
    if not isinstance(overlay, dict) or not overlay:
        return report, src
    doc = dict(report)
    for k, v in overlay.items():
        if k in allowed:
            doc[k] = v
    if isinstance(overlay.get("_leaves"), dict):   # 2026-11-03: 叶子路径覆盖(执行卡一句话 / 节点标签 / 裁决解释 / 未让步主张)
        doc = apply_leaves(doc, report, overlay["_leaves"], overlay.get("_leaf_src"))
    if report.get("scope_origin") and "debate_ref" in report:
        from workflows.group_report_view import localize_group_scope
        doc = localize_group_scope(report, doc)
    return doc, want


def localize_field(report: Dict, source_lang: Optional[str], i18n: Optional[Dict],
                   lang: Optional[str], field: str, fields: Optional[Iterable[str]] = None) -> Tuple[Any, str]:
    """只取一个顶层字段的本地化版本(dashboard 用, 不复制整份报告)。"""
    allowed = tuple(fields) if fields else TRANSLATABLE_FIELDS
    src = source_lang or detect_lang(report)
    want = norm_lang(lang)
    if want != src:
        overlay = (i18n or {}).get(want)
        if isinstance(overlay, dict) and field in overlay and field in allowed:
            return overlay[field], want
        return (report or {}).get(field), src
    return (report or {}).get(field), src


def lang_meta(source_lang: Optional[str], i18n: Optional[Dict], served: str) -> Dict[str, Any]:
    src = source_lang or "zh"
    return {"lang_served": served, "lang_source": src, "lang_available": available_langs(src, i18n)}
