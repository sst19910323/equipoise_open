"""Canonical market universes derived from config/symbols.yaml.

Keep membership and ordering in YAML; runners should consume this module instead
of maintaining their own symbol lists.
"""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS_CONFIG = ROOT / "config" / "symbols.yaml"
MARKET_REGIONS = {"us": "US", "eu": "EU", "jp": "JP", "kr": "KR"}


def load_symbol_config() -> dict:
    return yaml.safe_load(SYMBOLS_CONFIG.read_text(encoding="utf-8")) or {}


def research_group(key: str, config: dict | None = None) -> dict:
    cfg = config if config is not None else load_symbol_config()
    groups = cfg.get("research_groups", {})
    if key not in groups:
        raise ValueError(f"Unknown research group {key}. Available: {', '.join(groups)}")
    group = groups[key]
    members = group["members"]
    if (not isinstance(members, list) or len(members) < 2 or len(set(members)) != len(members)
            or not set(members) <= set(symbols_for("stocks", group["region"], cfg) + symbols_for("etfs", group["region"], cfg))
            or group["benchmark"] not in cfg["symbol_contracts"]):
        raise ValueError(f"Invalid active roster or benchmark for {key}")
    return group


def _region(contracts: dict, symbol: str) -> str:
    return str((contracts.get(symbol) or {}).get("region") or "US").upper()


def dispatch_jobs(config: dict) -> dict:
    """Each member has exactly one launch key. A reference alone never owns a job."""
    jobs = {}
    owners = set()
    for key in config.get("research_groups", {}):
        group = research_group(key, config)
        members = group["members"]
        if owners.intersection(members):
            raise ValueError(f"Duplicate dispatch owner for {key}")
        owners.update(members)
        jobs[key] = dict(market=group["region"].lower(), members=members, group_definition=group,
                         cost_units=1 + 0.25 * (len(members) - 1), flag="--group")
    for kind in ("generals", "stocks", "etfs"):
        for symbol in config["analysis_targets"][kind]:
            if symbol not in owners:
                jobs[symbol] = dict(market=_region(config["symbol_contracts"], symbol).lower(),
                                    members=[symbol], cost_units=1, flag="--symbols")
    return jobs


def symbols_for(kind: str, region: str, config: dict | None = None) -> list[str]:
    cfg = config or load_symbol_config()
    targets = cfg.get("analysis_targets") or {}
    contracts = cfg.get("symbol_contracts") or {}
    wanted = region.upper()
    return [symbol for symbol in (targets.get(kind) or []) if _region(contracts, symbol) == wanted]


def postcheck_universes(config: dict | None = None) -> dict[str, list[str]]:
    """复盘侧可选的标的集合, 与辩论侧同源 (config/symbols.yaml)。

    每个大区给出 <market>_generals / _stocks / _etfs (空的不登记) 和 <market>_all;
    "all" = 四大区全集。generals 一律计入 <market>_all —— 宏观报告也要复盘。
    """
    cfg = config or load_symbol_config()
    universes: dict[str, list[str]] = {}
    everything: list[str] = []
    for market, region in MARKET_REGIONS.items():
        groups = [(kind, symbols_for(kind, region, cfg)) for kind in ("generals", "stocks", "etfs")]
        combined: list[str] = []
        for kind, symbols in groups:
            if symbols:
                universes[f"{market}_{kind}"] = symbols
                combined.extend(symbols)
        if combined:
            universes[f"{market}_all"] = combined
            everything.extend(combined)
    universes["all"] = list(dict.fromkeys(everything))
    return universes


def market_waves(config: dict | None = None) -> dict[str, list[tuple[str, list[str]]]]:
    cfg = config or load_symbol_config()
    contracts = cfg.get("symbol_contracts") or {}
    waves: dict[str, list[tuple[str, list[str]]]] = {}
    for market, region in MARKET_REGIONS.items():
        generals = symbols_for("generals", region, cfg)
        stocks = symbols_for("stocks", region, cfg)
        groups: list[tuple[str, list[str]]] = [("0-GENERAL", generals)]
        if region == "US":
            etfs = symbols_for("etfs", region, cfg)
            no_sector = [s for s in stocks if not (contracts.get(s) or {}).get("sector")]
            sector = [s for s in stocks if (contracts.get(s) or {}).get("sector")]
            groups.extend([
                ("1-ETFs", etfs),
                ("2a-NoSector", no_sector),
                ("2b-Sector", sector),
            ])
        else:
            groups.append((f"1-{region}stocks", stocks))
        waves[market] = [(label, symbols) for label, symbols in groups if symbols]
    return waves
