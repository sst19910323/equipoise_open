#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ruler_era.py — the one test for "is this a ruler (v3.4) report / buffer?".

A report or record that carries `scenario` is a ruler (v3.4) one and must carry the session's odds snapshot; one without
is pre-ruler (v3.3 and earlier) and takes the stake-ledger paths. Used by validate_debate, closure_check, ledger_tally,
rebalance_brief and workflows/postcheck_quant.
"""
from __future__ import annotations

import json
from typing import Any

RULER_SCHEMA = "3.4"


def _content(e: Any) -> dict | None:
    c = e.get("content") if isinstance(e, dict) else None
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except json.JSONDecodeError:
            return None
    return c if isinstance(c, dict) else None


def is_ruler_entries(entries: Any) -> bool:
    """A buffer / history (entries with `content`, a dict or a JSON string) where some final carries a `scenario`."""
    return any(isinstance((_content(e) or {}).get("scenario"), dict) for e in entries or [])


def is_ruler_report(doc: Any) -> bool:
    """A ruler report: `schema_version == "3.4"`, a `scenario` object, or any debate_history final with a `scenario`.
    Works on the file form and on the PG row form (which carries `schema_version`)."""
    if not isinstance(doc, dict):
        return False
    if str(doc.get("schema_version") or "") == RULER_SCHEMA or isinstance(doc.get("scenario"), dict):
        return True
    return is_ruler_entries(doc.get("debate_history"))
