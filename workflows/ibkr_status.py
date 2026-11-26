# -*- coding: utf-8 -*-
"""
ibkr_status.py — 盈透(IBKR)挂了状态旗。Claude 编排 agent 写, 笨 Hermes 读。

约定: data/status/ibkr_down.flag 存在 = 盈透判定为挂; 内容是 JSON {detected_at, symbols}。
Claude 连续 >=2 个不同标的 skipped-no-market-data 时写旗; 某标的重新 ran 时清旗。
Hermes 读到旗 → 这波先别再喊辩论。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLAG_PATH = PROJECT_ROOT / "data" / "status" / "ibkr_down.flag"


def set_down(symbols: List[str], detected_at: Optional[datetime] = None) -> bool:
    """写盈透挂旗 (含判定时刻 + 触发标的)。"""
    detected_at = detected_at or datetime.now(timezone.utc)
    try:
        FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        FLAG_PATH.write_text(json.dumps({
            "detected_at": detected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbols": [str(s).upper() for s in symbols],
        }, ensure_ascii=False), encoding="utf-8")
        logging.warning(f"[ibkr_status] ⚠️ 盈透挂旗已写: {symbols}")
        return True
    except Exception as e:
        logging.warning(f"[ibkr_status] 写旗失败: {e}")
        return False


def clear() -> bool:
    """清旗 (盈透恢复)。不存在也算成功。"""
    try:
        FLAG_PATH.unlink(missing_ok=True)
        return True
    except Exception as e:
        logging.warning(f"[ibkr_status] 清旗失败: {e}")
        return False


def is_down() -> bool:
    return FLAG_PATH.exists()


if __name__ == "__main__":
    assert clear() and not is_down()
    assert set_down(["AAPL", "MSFT"]) and is_down()
    data = json.loads(FLAG_PATH.read_text(encoding="utf-8"))
    assert data["symbols"] == ["AAPL", "MSFT"], data
    assert clear() and not is_down()
    print("ibkr_status 自检通过")
